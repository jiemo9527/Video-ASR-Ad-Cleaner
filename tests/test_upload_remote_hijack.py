"""远端劫持候选 remote：保存、切换、删除与校验。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_hijack_test.db')

# The scratch DB MUST be selected before importing app; see test_prioritize_queue.py.
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from database import db, Config  # noqa: E402

HIJACK_KEYS = ['upload_remote_hijack_enabled', 'upload_remote_hijack_remote', 'upload_remote_hijack_candidates']


def is_scratch_database(path):
    if not path:
        return False
    resolved = os.path.normcase(os.path.realpath(path))
    temp_dir = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
    name = os.path.basename(resolved)
    return (os.path.dirname(resolved) == temp_dir
            and name.startswith('scanner_') and name.endswith('_test.db'))


class UploadRemoteHijackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TMP_DB):
            os.remove(TMP_DB)
        scanner.app.config['TESTING'] = True
        scanner.app.config['LOGIN_DISABLED'] = True
        cls.ctx = scanner.app.app_context()
        cls.ctx.push()
        if not is_scratch_database(db.engine.url.database):
            cls.ctx.pop()
            raise RuntimeError('Refusing to run destructive tests against %s' % db.engine.url)
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        Config.query.filter(Config.key.in_(HIJACK_KEYS)).delete(synchronize_session=False)
        db.session.commit()
        self.client = scanner.app.test_client()

    def post(self, **payload):
        return self.client.post('/api/upload_remote_hijack', json=payload)

    def test_candidates_are_saved_deduplicated_and_include_current_remote(self):
        res = self.post(enabled=True, remote='g01:', candidates=['s25', 'g01', 's25', 'bad name', ''])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['candidates'], ['s25', 'g01'])

        data = self.client.get('/api/upload_remote_hijack').get_json()
        self.assertEqual(data, {'enabled': True, 'remote': 'g01', 'candidates': ['s25', 'g01']})

    def test_switching_remote_keeps_candidates(self):
        self.post(enabled=True, remote='s25', candidates=['s25', 'g01'])
        self.post(enabled=True, remote='g01', candidates=['s25', 'g01'])

        data = self.client.get('/api/upload_remote_hijack').get_json()
        self.assertEqual(data['remote'], 'g01')
        self.assertEqual(data['candidates'], ['s25', 'g01'])
        self.assertEqual(scanner.get_final_config(None)['upload_remote_hijack_remote'], 'g01')

    def test_removing_last_candidate_disables_cleanly(self):
        self.post(enabled=True, remote='s25', candidates=['s25'])
        res = self.post(enabled=False, remote='', candidates=[])
        self.assertEqual(res.status_code, 200)

        data = self.client.get('/api/upload_remote_hijack').get_json()
        self.assertEqual(data, {'enabled': False, 'remote': '', 'candidates': []})

    def test_enabling_without_remote_or_with_invalid_remote_is_rejected(self):
        self.assertEqual(self.post(enabled=True, remote='', candidates=['s25']).status_code, 400)
        self.assertEqual(self.post(enabled=False, remote='bad name', candidates=[]).status_code, 400)

    def test_legacy_config_without_candidates_lists_current_remote(self):
        db.session.add(Config(key='upload_remote_hijack_remote', value='old1'))
        db.session.commit()

        data = self.client.get('/api/upload_remote_hijack').get_json()
        self.assertEqual(data['candidates'], ['old1'])

    def test_batch_upload_remote_endpoint_is_removed(self):
        self.assertEqual(self.client.post('/api/tasks/batch_upload_remote', json={'remote': 's25'}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
