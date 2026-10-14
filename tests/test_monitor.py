"""Cross-site overview: first-party login grants a scoped read-only capability."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + os.path.join(tempfile.gettempdir(), 'scanner_monitor_test.db').replace('\\', '/')
import app as scanner
from database import db, User, Task
from werkzeug.security import generate_password_hash

ORIGIN = 'https://iiiiililililllllocooocooooocccdoododo.jie02.top'


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.origins = mock.patch.dict(os.environ, {'SCANNER_MONITOR_ORIGINS': ORIGIN})
        self.origins.start()
        scanner.app.config.update(TESTING=True, LOGIN_DISABLED=False)
        self.ctx = scanner.app.app_context()
        self.ctx.push()
        db.create_all()
        db.session.query(Task).delete()
        db.session.query(User).delete()
        db.session.add(User(id='monitor-test', password_hash=generate_password_hash('test-only-password')))
        db.session.commit()
        self.client = scanner.app.test_client()

    def tearDown(self):
        from flask import g
        g.pop('_login_user', None)
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.origins.stop()

    def connect_url(self, origin=ORIGIN):
        from urllib.parse import urlencode
        return '/monitor/connect?' + urlencode({'origin': origin, 'state': 'a' * 40})

    def test_connect_requires_real_login_and_returns_to_connect(self):
        url = self.connect_url()
        r = self.client.get(url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login?', r.location)
        from urllib.parse import urlsplit
        r = self.client.post(urlsplit(r.location).path + '?' + urlsplit(r.location).query,
                             data={'username': 'monitor-test', 'password': 'test-only-password'})
        self.assertEqual(r.status_code, 302)
        from urllib.parse import unquote
        self.assertEqual(unquote(r.location), unquote(url))

    def grant(self):
        with self.client.session_transaction() as session:
            session['_user_id'] = 'monitor-test'
            session['_fresh'] = True
        response = self.client.get(self.connect_url())
        self.assertEqual(response.status_code, 200)
        import re, json
        match = re.search(r'token:([^,]+),expiresAt', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def test_grant_is_origin_bound_and_not_a_login_cookie(self):
        token = self.grant()
        fresh = scanner.app.test_client()
        headers = {'Origin': ORIGIN, 'Authorization': 'Bearer ' + token}
        with mock.patch.object(scanner, 'build_monitor_snapshot', return_value={'code': 200, 'data': {}}):
            r = fresh.get('/api/monitor/snapshot', headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Credentials', r.headers)
        from flask import g
        g.pop('_login_user', None)  # outer test app context must not cache the earlier logged-in client
        self.assertEqual(fresh.get('/api/tasks', headers=headers).status_code, 302)
        headers['Origin'] = 'https://evil.example'
        self.assertEqual(fresh.get('/api/monitor/snapshot', headers=headers).status_code, 403)
        self.assertEqual(fresh.get('/api/monitor/snapshot', headers={'Origin': ORIGIN}).status_code, 401)
        self.assertEqual(fresh.post('/api/monitor/snapshot', headers=headers).status_code, 405)

    def test_snapshot_does_not_expose_secrets_or_download_uris(self):
        token = self.grant()
        db.session.add(Task(filename='movie.mkv', filepath='/secret/movie.mkv', status='processing',
                            progress=61, log='secret-recognized-text', overrides='{"api_key":"secret-key", "_audio_segments":{"01片头":{"status":"passed", "text":"secret-transcript"}}}'))
        db.session.commit()
        def rpc(calls):
            if calls[0][0] == 'aria2.getFiles':
                return [[{'path': '/secret/dl.mkv', 'uris': [{'uri': 'https://secret-download-uri'}]}]]
            return [{'downloadSpeed': '123', 'uploadSpeed': '4', 'numActive': '1', 'numWaiting': '2', 'numStopped': '0'},
                    [{'gid': 'a'*16, 'status': 'active', 'totalLength': '100', 'completedLength': '25',
                      'downloadSpeed': '123', 'bittorrent': {}}]]
        with mock.patch.object(scanner, 'aria2_multicall', side_effect=rpc):
            r = self.client.get('/api/monitor/snapshot', headers={'Origin': ORIGIN, 'Authorization': 'Bearer ' + token})
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        raw = r.get_data(as_text=True)
        for text in ('secret-key', 'secret-recognized-text', 'secret-transcript', 'secret-download-uri', '/secret/'):
            self.assertNotIn(text, raw)
        self.assertEqual(payload['queues']['detect']['counts']['processing'], 1)
        self.assertEqual(payload['queues']['detect']['tasks'][0]['segments'], [{'name': '01片头', 'status': 'passed'}])
        self.assertEqual(payload['downloads']['tasks'][0]['progress'], 25)

    def test_running_tasks_rank_before_history_and_speeds_cover_hidden_rows(self):
        token = self.grant()
        for i in range(15):
            db.session.add(Task(filename='running-' + str(i), filepath='/missing', status='uploading',
                                upload_speed='1 MiB/s', overrides='{}'))
        db.session.add(Task(filename='latest-history', filepath='/missing', status='uploaded', overrides='{}'))
        db.session.commit()
        with mock.patch.object(scanner, 'aria2_multicall', side_effect=RuntimeError()):
            r = self.client.get('/api/monitor/snapshot', headers={'Origin': ORIGIN, 'Authorization': 'Bearer ' + token})
        q = r.get_json()['queues']['upload']
        self.assertEqual(len(q['tasks']), 12)
        self.assertTrue(all(t['status'] == 'uploading' for t in q['tasks']))
        self.assertEqual(q['speed'], 15 * 1024 * 1024)
        self.assertEqual(q['total'], 16)

    def test_monitoring_disabled_without_origin_configuration(self):
        with mock.patch.dict(os.environ, {'SCANNER_MONITOR_ORIGINS': ''}):
            response = self.client.options('/api/monitor/snapshot', headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    def test_preflight_restricted_to_allowlisted_origin(self):
        r = self.client.options('/api/monitor/snapshot', headers={'Origin': ORIGIN})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers['Access-Control-Allow-Origin'], ORIGIN)
        bad = self.client.options('/api/monitor/snapshot', headers={'Origin': 'https://evil.example'})
        self.assertEqual(bad.status_code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', bad.headers)

    def test_invalid_connect_state_rejected(self):
        self.grant()
        response = self.client.get('/monitor/connect?origin=' + ORIGIN + '&state=short')
        self.assertEqual(response.status_code, 400)

    def test_password_change_revokes_existing_grant(self):
        token = self.grant()
        user = db.session.get(User, 'monitor-test')
        user.password_hash = generate_password_hash('changed-test-only-password')
        db.session.commit()
        response = self.client.get('/api/monitor/snapshot', headers={
            'Origin': ORIGIN, 'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 401)

    def test_aria2_failure_keeps_scanner_queues_and_marks_rpc_unknown(self):
        token = self.grant()
        with mock.patch.object(scanner, 'aria2_multicall', side_effect=RuntimeError('secret-upstream-url')):
            r = self.client.get('/api/monitor/snapshot', headers={'Origin': ORIGIN, 'Authorization': 'Bearer ' + token})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['downloads']['ok'])
        self.assertIsNone(r.get_json()['downloads']['active'])
        self.assertNotIn('secret-upstream-url', r.get_data(as_text=True))

    def test_invalid_connect_origin_and_login_open_redirect_rejected(self):
        self.grant()
        self.assertEqual(self.client.get(self.connect_url('https://evil.example')).status_code, 400)
        r = self.client.get('/login?next=https://evil.example')
        self.assertEqual(r.location, '/')

    def test_deleted_user_or_expired_grant_is_rejected(self):
        token = self.grant()
        headers = {'Origin': ORIGIN, 'Authorization': 'Bearer ' + token}
        import monitor
        with mock.patch.object(monitor, 'MONITOR_TTL', -1):
            self.assertEqual(self.client.get('/api/monitor/snapshot', headers=headers).status_code, 401)
        db.session.query(User).delete()
        db.session.commit()
        self.assertEqual(self.client.get('/api/monitor/snapshot', headers=headers).status_code, 401)


if __name__ == '__main__':
    unittest.main()
