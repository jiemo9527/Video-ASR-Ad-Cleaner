"""空闲自动开始：没有下载中、等待中任务时开始全部暂停任务，优先级低于其他下载控制。"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_download_idle_resume_test.db')
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from database import db, Config, User  # noqa: E402

COOLDOWN = scanner.DOWNLOAD_MANUAL_PAUSE_COOLDOWN


class FakeAria2:
    def __init__(self):
        self.tasks = {}  # 插入顺序 = 队列顺序
        self.calls = []
        self.fail_unpause = set()
        self.on_unpause = None

    def add(self, gid, status='paused', total=1000, completed=0):
        self.tasks[gid] = {'gid': gid, 'status': status, 'totalLength': str(total), 'completedLength': str(completed)}

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == 'aria2.tellActive':
            return [dict(t) for t in self.tasks.values() if t['status'] == 'active']
        if method == 'aria2.tellWaiting':
            rows = [dict(t) for t in self.tasks.values() if t['status'] in ('waiting', 'paused')]
            return rows[params[0]:params[0] + params[1]]
        if method == 'aria2.forcePause':
            self.tasks[params[0]]['status'] = 'paused'
            return params[0]
        if method == 'aria2.unpause':
            if params[0] in self.fail_unpause:
                raise RuntimeError('Aria2 RPC 返回异常')
            self.tasks[params[0]]['status'] = 'waiting'
            if self.on_unpause:
                self.on_unpause(params[0])
            return params[0]
        raise AssertionError(method)

    def unpaused(self):
        return [p[0] for m, p in self.calls if m == 'aria2.unpause']


class FakeResponse:
    status_code = 200
    headers = {'Content-Type': 'application/json'}
    content = b'{"jsonrpc":"2.0","id":"1","result":"OK"}'

    def json(self):
        return json.loads(self.content)


class DownloadIdleResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TMP_DB):
            os.remove(TMP_DB)
        scanner.app.config['TESTING'] = True
        cls.ctx = scanner.app.app_context()
        cls.ctx.push()
        db.create_all()
        if not db.session.get(User, 'tester'):
            db.session.add(User(id='tester', password_hash='x'))
            db.session.commit()
        cls.client = scanner.app.test_client()
        with cls.client.session_transaction() as sess:
            sess['_user_id'] = 'tester'

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        Config.query.filter(Config.key.in_([
            'download_idle_resume', scanner.MANUAL_PAUSE_CONFIG_KEY, scanner.DISK_GUARD_CONFIG_KEY,
        ])).delete()
        db.session.commit()
        self.reset_memory()
        scanner.disk_guard_state.clear()
        scanner.disk_guard_state.update(scanner.new_disk_guard_state())
        scanner.aria2_control_busy['count'] = 0
        self.fake = FakeAria2()
        patcher = mock.patch.object(scanner, 'call_aria2_rpc', self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reset_memory(self):
        scanner.manual_pause_state.update({'loaded': False, 'gids': {}, 'all': 0.0})

    def set_config(self, **values):
        for key, value in values.items():
            row = db.session.get(Config, key) or Config(key=key)
            row.value = str(value)
            db.session.add(row)
        db.session.commit()

    def tick(self, now=10000.0):
        with mock.patch('builtins.print'):
            return scanner.resume_paused_when_idle(now=now)

    def proxy(self, payload, now=None):
        now = 10000.0 if now is None else now
        real = scanner.record_manual_aria2_controls
        with mock.patch.object(scanner.requests, 'post', return_value=FakeResponse()) as post, \
                mock.patch.object(scanner, 'record_manual_aria2_controls',
                                  side_effect=lambda commands: real(commands, now=now)):
            r = self.client.post('/api/aria2/jsonrpc', json=payload)
        return r, post

    def test_default_on(self):
        self.assertIs(scanner.get_final_config(None)['download_idle_resume'], True)

    def test_idle_resumes_all_paused_in_queue_order(self):
        for gid in ('c', 'a', 'b'):
            self.fake.add(gid)
        self.assertEqual(self.tick(), ['c', 'a', 'b'])
        self.assertEqual(self.fake.unpaused(), ['c', 'a', 'b'])

    def test_active_download_blocks(self):
        self.fake.add('run', status='active', completed=10)
        self.fake.add('p')
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.fake.unpaused(), [])

    def test_waiting_task_blocks(self):
        self.fake.add('w', status='waiting')
        self.fake.add('p')
        self.assertEqual(self.tick(), [])

    def test_seeding_task_does_not_count_as_active(self):
        self.fake.add('seed', status='active', total=1000, completed=1000)
        self.fake.add('p')
        self.assertEqual(self.tick(), ['p'])

    def test_unknown_size_active_counts_as_downloading(self):
        self.fake.add('magnet', status='active', total=0, completed=0)
        self.fake.add('p')
        self.assertEqual(self.tick(), [])

    def test_disabled_does_nothing(self):
        self.set_config(download_idle_resume='false')
        self.fake.add('p')
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.fake.calls, [])

    def test_disk_guard_engaged_has_priority(self):
        self.set_config(**{scanner.DISK_GUARD_CONFIG_KEY: json.dumps({'engaged': True, 'paused': ['p'], 'downloading': ['p']})})
        self.fake.add('p')
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.fake.calls, [])

    def test_disk_guard_engaging_midway_stops_resuming(self):
        for gid in ('a', 'b', 'c'):
            self.fake.add(gid)

        def engage(_gid):
            scanner.disk_guard_state['engaged'] = True
        self.fake.on_unpause = engage
        self.assertEqual(self.tick(), ['a'])

    def test_rename_in_progress_has_priority(self):
        self.fake.add('p')
        with scanner.aria2_control_hold():
            self.assertEqual(self.tick(), [])
        self.assertEqual(self.tick(), ['p'])

    def test_rename_holds_control(self):
        seen = []
        with mock.patch.object(scanner, '_rename_aria2_download',
                               side_effect=lambda gid, name: seen.append(scanner.aria2_control_in_progress()) or (200, 'ok')):
            self.assertEqual(scanner.rename_aria2_download('a', 'x.mkv'), (200, 'ok'))
        self.assertEqual(seen, [True])
        self.assertFalse(scanner.aria2_control_in_progress())

    def test_stall_paused_tasks_are_resumed(self):
        scanner.download_stall_paused['s'] = 1
        self.addCleanup(scanner.download_stall_paused.clear)
        self.fake.add('s')
        self.assertEqual(self.tick(), ['s'])

    def test_unpause_failure_continues_with_rest(self):
        self.fake.add('a')
        self.fake.add('b')
        self.fake.fail_unpause.add('a')
        self.assertEqual(self.tick(), ['b'])

    def test_manual_pause_cooldown(self):
        self.fake.add('m')
        self.fake.add('other')
        r, post = self.proxy({'jsonrpc': '2.0', 'id': '1', 'method': 'aria2.forcePause', 'params': ['m']}, now=1000.0)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.tick(now=1000.0 + COOLDOWN - 1), ['other'])
        self.fake.tasks['other']['status'] = 'paused'
        self.assertEqual(self.tick(now=1000.0 + COOLDOWN), ['m', 'other'])

    def test_manual_pause_survives_restart(self):
        self.fake.add('m')
        self.proxy({'jsonrpc': '2.0', 'id': '1', 'method': 'aria2.pause', 'params': ['m']}, now=1000.0)
        stored = json.loads(db.session.get(Config, scanner.MANUAL_PAUSE_CONFIG_KEY).value)
        self.assertEqual(stored['gids'], {'m': 1000.0})
        self.reset_memory()
        self.assertEqual(self.tick(now=1100.0), [])
        self.assertEqual(self.tick(now=1000.0 + COOLDOWN), ['m'])
        stored = json.loads(db.session.get(Config, scanner.MANUAL_PAUSE_CONFIG_KEY).value)
        self.assertEqual(stored, {'gids': {}, 'all': 0.0})

    def test_pause_all_covers_every_task(self):
        self.fake.add('a')
        self.fake.add('b')
        self.proxy({'jsonrpc': '2.0', 'id': '1', 'method': 'aria2.pauseAll', 'params': []}, now=1000.0)
        self.assertEqual(self.tick(now=1300.0), [])
        self.assertEqual(self.tick(now=1000.0 + COOLDOWN), ['a', 'b'])

    def test_manual_unpause_clears_record(self):
        self.proxy({'jsonrpc': '2.0', 'id': '1', 'method': 'aria2.forcePause', 'params': ['m']}, now=1000.0)
        self.proxy({'jsonrpc': '2.0', 'id': '2', 'method': 'aria2.unpause', 'params': ['m']}, now=1010.0)
        self.assertEqual(scanner.manual_pause_state['gids'], {})
        self.proxy({'jsonrpc': '2.0', 'id': '3', 'method': 'aria2.forcePauseAll', 'params': []}, now=1020.0)
        self.proxy({'jsonrpc': '2.0', 'id': '4', 'method': 'aria2.unpauseAll', 'params': []}, now=1030.0)
        self.assertEqual(scanner.manual_pause_state['all'], 0.0)
        self.fake.add('m')
        self.assertEqual(self.tick(now=1040.0), ['m'])

    def test_batch_and_multicall_pauses_are_recorded(self):
        self.proxy([
            {'jsonrpc': '2.0', 'id': '1', 'method': 'aria2.forcePause', 'params': ['token:x', 'a']},
            {'jsonrpc': '2.0', 'id': '2', 'method': 'system.multicall', 'params': [[
                {'methodName': 'aria2.pause', 'params': ['b']},
                {'methodName': 'aria2.tellStatus', 'params': ['c']},
            ]]},
        ], now=1000.0)
        self.assertEqual(set(scanner.manual_pause_state['gids']), {'a', 'b'})

    def test_internal_pauses_are_not_manual(self):
        # 无进度检查、磁盘保护等服务端暂停直接走 call_aria2_rpc，不经过代理
        self.fake.add('p', status='active', completed=10)
        self.assertEqual(scanner.force_pause_aria2([{'gid': 'p'}]), ['p'])
        self.assertEqual(self.fake.tasks['p']['status'], 'paused')
        self.assertEqual(scanner.manual_pause_state['gids'], {})
        self.assertEqual(self.tick(), ['p'])

    def test_blocked_unpause_does_not_clear_record(self):
        self.proxy({'jsonrpc': '2.0', 'id': '1', 'method': 'aria2.forcePause', 'params': ['m']}, now=1000.0)
        scanner.disk_guard_state.update({'loaded': True, 'engaged': True})
        r, post = self.proxy({'jsonrpc': '2.0', 'id': '2', 'method': 'aria2.unpause', 'params': ['m']}, now=1010.0)
        self.assertIn('error', r.get_json())
        self.assertEqual(post.call_count, 0)
        self.assertIn('m', scanner.manual_pause_state['gids'])

    def test_settings_round_trip_and_sys_key_protected(self):
        r = self.client.post('/api/settings', json={'download_idle_resume': False,
                                                    scanner.MANUAL_PAUSE_CONFIG_KEY: '{"all": 99999999999}'})
        self.assertEqual(r.status_code, 200)
        data = self.client.get('/api/settings').get_json()
        self.assertIs(data['download_idle_resume'], False)
        self.assertIsNone(db.session.get(Config, scanner.MANUAL_PAUSE_CONFIG_KEY))
        self.proxy({'jsonrpc': '2.0', 'id': '1', 'method': 'aria2.forcePause', 'params': ['m']})
        backup = self.client.get('/api/settings/backup').get_json()
        self.assertNotIn(scanner.MANUAL_PAUSE_CONFIG_KEY, backup['config'])
        self.assertIs(backup['config']['download_idle_resume'], False)

    def test_concurrent_records_are_consistent(self):
        def pause(i):
            with scanner.app.app_context():
                scanner.record_manual_aria2_controls(
                    [{'method': 'aria2.forcePause', 'params': [f'g{i}']}], now=1000.0)
        threads = [threading.Thread(target=pause, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(scanner.manual_pause_state['gids']), 20)


if __name__ == '__main__':
    unittest.main()
