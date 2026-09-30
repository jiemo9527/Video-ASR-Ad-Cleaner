"""账号超限自动切换 remote：错误识别、候选轮换与上传线程端到端。"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_limit_switch_test.db')

# The scratch DB MUST be selected before importing app; see test_prioritize_queue.py.
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from core_logic import ScannerCore  # noqa: E402
from database import db, Config, Task, User  # noqa: E402

HIJACK_KEYS = ['upload_remote_hijack_enabled', 'upload_remote_hijack_remote',
               'upload_remote_hijack_candidates', 'upload_remote_auto_switch']

# 模拟 rclone：目标 remote 为 s25 时报 Google Drive 每日上传超限，其余 remote 正常完成 moveto。
FAKE_RCLONE = r'''#!/usr/bin/env python3
import json, os, sys
src, dst = sys.argv[2], sys.argv[3]
if dst.startswith('s25:'):
    print(json.dumps({"level": "error", "msg": "Failed to copy: googleapi: Error 403: User rate limit exceeded., userRateLimitExceeded"}), file=sys.stderr)
    sys.exit(7)
os.remove(src)
print(json.dumps({"level": "info", "msg": "", "stats": {"speed": 1, "eta": 0, "transferring": [{"bytes": 1, "size": 1}]}}), file=sys.stderr)
'''


def is_scratch_database(path):
    if not path:
        return False
    resolved = os.path.normcase(os.path.realpath(path))
    temp_dir = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
    name = os.path.basename(resolved)
    return (os.path.dirname(resolved) == temp_dir
            and name.startswith('scanner_') and name.endswith('_test.db'))


class UploadLimitErrorTests(unittest.TestCase):
    def test_recognizes_drive_and_onedrive_limit_messages(self):
        for message in [
            'Failed to copy: googleapi: Error 403: User rate limit exceeded., userRateLimitExceeded',
            'Fatal error received - not attempting retries: upload limit exceeded',
            'googleapi: Error 403: The user\'s Drive storage quota has been exceeded., storageQuotaExceeded',
            'googleapi: Error 403: The file limit for this shared drive has been exceeded., teamDriveFileLimitExceeded',
            'quotaLimitReached: Insufficient Space Available',
        ]:
            self.assertTrue(ScannerCore.is_upload_limit_error(message), message)

    def test_ignores_ordinary_failures(self):
        for message in ['Failed to copy: couldn\'t connect: connection reset by peer',
                        'googleapi: Error 404: File not found', 'context deadline exceeded']:
            self.assertFalse(ScannerCore.is_upload_limit_error(message), message)


class PickNextRemoteTests(unittest.TestCase):
    def setUp(self):
        scanner.upload_limit_exhausted.clear()

    def test_rotates_after_current_and_skips_exhausted(self):
        candidates = ['a', 'b', 'c']
        self.assertEqual(scanner.pick_next_upload_remote('a', candidates, now=0), 'b')
        self.assertEqual(scanner.pick_next_upload_remote('b', candidates, now=1), 'c')
        self.assertIsNone(scanner.pick_next_upload_remote('c', candidates, now=2))

    def test_remote_outside_candidates_switches_to_first_available(self):
        self.assertEqual(scanner.pick_next_upload_remote('default', ['a', 'b'], now=0), 'a')

    def test_exhausted_remote_returns_after_cooldown(self):
        scanner.pick_next_upload_remote('a', ['a', 'b'], now=0)
        scanner.pick_next_upload_remote('b', ['a', 'b'], now=10)
        later = scanner.UPLOAD_LIMIT_COOLDOWN_SECONDS + 1
        self.assertEqual(scanner.pick_next_upload_remote(None, ['a', 'b'], now=later), 'a')


class UploadWorkerLimitSwitchTests(unittest.TestCase):
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

        cls.tmp = tempfile.TemporaryDirectory()
        bin_dir = os.path.join(cls.tmp.name, 'bin')
        os.makedirs(bin_dir)
        rclone = os.path.join(bin_dir, 'rclone')
        with open(rclone, 'w') as f:
            f.write(FAKE_RCLONE)
        os.chmod(rclone, os.stat(rclone).st_mode | stat.S_IEXEC)
        cls.old_path = os.environ['PATH']
        os.environ['PATH'] = bin_dir + os.pathsep + cls.old_path
        threading.Thread(target=scanner.upload_worker, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        os.environ['PATH'] = cls.old_path
        cls.tmp.cleanup()
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        scanner.upload_limit_exhausted.clear()
        Config.query.filter(Config.key.in_(HIJACK_KEYS)).delete(synchronize_session=False)
        Task.query.delete()
        db.session.commit()

    def set_config(self, **values):
        for key, value in values.items():
            db.session.add(Config(key=key, value=value))
        db.session.commit()

    def run_upload(self):
        media = os.path.join(self.tmp.name, 'downloads', 'movie.mkv')
        os.makedirs(os.path.dirname(media), exist_ok=True)
        with open(media, 'wb') as f:
            f.write(b'x' * 16)
        task = Task(filename='movie.mkv', filepath=media, status='pending_upload', log='',
                    overrides=json.dumps({'upload_remote': 's25', 'direct_upload': True}))
        db.session.add(task)
        db.session.commit()
        scanner.upload_queue.put(task.id)
        deadline = time.time() + 20
        while time.time() < deadline:
            db.session.expire_all()
            task = Task.query.get(task.id)
            if task.status in ('uploaded', 'error'):
                return task
            time.sleep(0.2)
        self.fail('upload did not finish, status=%s' % task.status)

    def test_limit_error_switches_to_next_candidate_and_retries(self):
        self.set_config(upload_remote_auto_switch='true', upload_remote_hijack_enabled='false',
                        upload_remote_hijack_remote='s25', upload_remote_hijack_candidates='s25\ng01')

        task = self.run_upload()

        self.assertEqual(task.status, 'uploaded', task.log)
        self.assertIn('🔀 s25: 账号超限，自动切换到 g01: 并重新上传', task.log)
        self.assertEqual(json.loads(task.overrides)['upload_remote'], 'g01')
        conf = scanner.get_final_config(None)
        self.assertTrue(conf['upload_remote_hijack_enabled'])
        self.assertEqual(conf['upload_remote_hijack_remote'], 'g01')

    def test_limit_error_without_auto_switch_fails_as_before(self):
        self.set_config(upload_remote_auto_switch='false', upload_remote_hijack_candidates='s25\ng01')

        task = self.run_upload()

        self.assertEqual(task.status, 'error')
        self.assertIn('账号超限，无可切换的候选 remote', task.log)
        self.assertNotIn('🔀', task.log)

    def test_settings_defaults_and_round_trip(self):
        conf = scanner.get_final_config(None)
        self.assertFalse(conf['upload_remote_auto_switch'])
        self.assertTrue(conf['upload_slow_restart'])

        if not User.query.get('tester'):
            db.session.add(User(id='tester', password_hash='x'))
            db.session.commit()
        client = scanner.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = 'tester'
        res = client.post('/api/settings', json={'upload_remote_auto_switch': True, 'upload_slow_restart': False})
        self.assertEqual(res.status_code, 200)
        data = client.get('/api/settings').get_json()
        self.assertIs(data['upload_remote_auto_switch'], True)
        self.assertIs(data['upload_slow_restart'], False)
        Config.query.filter_by(key='upload_slow_restart').delete()
        db.session.commit()

    def test_all_candidates_exhausted_ends_in_error(self):
        self.set_config(upload_remote_auto_switch='true', upload_remote_hijack_candidates='s25')

        task = self.run_upload()

        self.assertEqual(task.status, 'error')
        self.assertIn('账号超限，无可切换的候选 remote', task.log)


if __name__ == '__main__':
    unittest.main()
