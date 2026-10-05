"""下载器「保存文件名」：未完成的单文件任务改名，保留已下载部分。"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_downloader_rename_test.db')
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from database import db, User  # noqa: E402

GID = '0123456789abcdef'


class FakeRpc:
    """Records RPC calls; forcePause flips status like Aria2 does once it stops."""

    def __init__(self, status, path, base_dir, bittorrent=False, files=1, fail_change=False, thief=None):
        self.task = {'gid': GID, 'status': status, 'dir': base_dir,
                     'files': [{'path': path}] * files}
        if bittorrent:
            self.task['bittorrent'] = {'info': {'name': 'x'}}
        self.calls = []
        self.fail_change = fail_change
        # thief: GID of a waiting task Aria2 starts in the slot freed by the pause
        self.thief = thief
        self.others = {}

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == 'aria2.tellActive':
            active = [GID] if self.task['status'] == 'active' else []
            return [{'gid': g} for g in active + [g for g, s in self.others.items() if s == 'active']]
        if method == 'aria2.tellStatus':
            if params[0] != GID:
                return {'status': self.others[params[0]]}
            return dict(self.task)
        if method == 'aria2.forcePause':
            if params[0] != GID:
                self.others[params[0]] = 'paused'
                self.task['status'] = 'active'
                return params[0]
            self.task['status'] = 'paused'
            if self.thief:
                self.others[self.thief] = 'active'
            return GID
        if method == 'aria2.unpause':
            if params[0] != GID:
                self.others[params[0]] = 'waiting'
                return params[0]
            self.task['status'] = 'waiting' if self.thief else 'active'
            return GID
        if method == 'aria2.changeOption':
            if self.fail_change:
                raise RuntimeError('Aria2 RPC 返回异常')
            return 'OK'
        if method == 'aria2.changePosition':
            return 0
        raise AssertionError(method)

    def methods(self):
        return [m for m, _ in self.calls]


class RenameDownloadTests(unittest.TestCase):
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
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.old = os.path.join(self.dir, 'old.bin')
        for path, data in ((self.old, b'partial'), (self.old + '.aria2', b'ctl')):
            with open(path, 'wb') as f:
                f.write(data)

    def rename(self, fake, name='new.mkv'):
        with mock.patch.object(scanner, 'call_aria2_rpc', fake):
            return self.client.post('/api/aria2/rename', json={'gid': GID, 'name': name})

    def test_active_task_paused_files_renamed_and_resumed_first(self):
        fake = FakeRpc('active', self.old, self.dir)
        scanner.downloader_file_cache[GID] = {'files': [{'path': self.old}]}
        r = self.rename(fake)
        self.assertEqual(r.status_code, 200, r.get_json())
        new = os.path.join(self.dir, 'new.mkv')
        with open(new, 'rb') as f:
            self.assertEqual(f.read(), b'partial')
        self.assertTrue(os.path.exists(new + '.aria2'))
        self.assertFalse(os.path.exists(self.old))
        self.assertEqual(fake.methods(), ['aria2.tellStatus', 'aria2.tellActive', 'aria2.forcePause', 'aria2.tellStatus',
                                          'aria2.changeOption', 'aria2.unpause', 'aria2.changePosition',
                                          'aria2.tellActive'])
        self.assertIn(('aria2.changeOption', [GID, {'out': 'new.mkv'}]), fake.calls)
        self.assertIn(('aria2.changePosition', [GID, 0, 'POS_SET']), fake.calls)
        self.assertNotIn(GID, scanner.downloader_file_cache)

    def test_slot_taken_during_pause_is_given_back(self):
        thief = 'fedcba9876543210'
        fake = FakeRpc('active', self.old, self.dir, thief=thief)
        self.assertEqual(self.rename(fake).status_code, 200)
        self.assertEqual(fake.task['status'], 'active')
        self.assertEqual(fake.others[thief], 'waiting')
        self.assertIn(('aria2.forcePause', [thief]), fake.calls)
        self.assertIn(('aria2.changePosition', [thief, 0, 'POS_SET']), fake.calls)

    def test_paused_task_stays_paused(self):
        fake = FakeRpc('paused', self.old, self.dir)
        self.assertEqual(self.rename(fake).status_code, 200)
        self.assertEqual(fake.methods(), ['aria2.tellStatus', 'aria2.changeOption'])
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'new.mkv')))

    def test_waiting_task_is_resumed_without_reordering(self):
        fake = FakeRpc('waiting', self.old, self.dir)
        self.assertEqual(self.rename(fake).status_code, 200)
        self.assertIn('aria2.unpause', fake.methods())
        self.assertNotIn('aria2.changePosition', fake.methods())

    def test_task_not_started_has_no_file_yet(self):
        os.remove(self.old)
        os.remove(self.old + '.aria2')
        fake = FakeRpc('paused', self.old, self.dir)
        self.assertEqual(self.rename(fake).status_code, 200)
        self.assertIn(('aria2.changeOption', [GID, {'out': 'new.mkv'}]), fake.calls)

    def test_sub_folder_in_out_is_kept(self):
        sub = os.path.join(self.dir, 'Season 1')
        os.makedirs(sub)
        old = os.path.join(sub, 'ep.bin')
        open(old, 'wb').close()
        fake = FakeRpc('paused', old, self.dir)
        self.assertEqual(self.rename(fake).status_code, 200)
        self.assertIn(('aria2.changeOption', [GID, {'out': 'Season 1/new.mkv'}]), fake.calls)
        self.assertTrue(os.path.exists(os.path.join(sub, 'new.mkv')))

    def test_existing_target_is_rejected_untouched(self):
        open(os.path.join(self.dir, 'new.mkv'), 'wb').close()
        fake = FakeRpc('active', self.old, self.dir)
        self.assertEqual(self.rename(fake).status_code, 409)
        self.assertEqual(fake.methods(), ['aria2.tellStatus'])
        self.assertTrue(os.path.exists(self.old))

    def test_change_option_failure_restores_files_and_resumes(self):
        fake = FakeRpc('active', self.old, self.dir, fail_change=True)
        self.assertEqual(self.rename(fake).status_code, 502)
        self.assertTrue(os.path.exists(self.old))
        self.assertTrue(os.path.exists(self.old + '.aria2'))
        self.assertIn('aria2.unpause', fake.methods())

    def test_bt_multi_file_and_finished_tasks_rejected(self):
        for fake in (FakeRpc('active', self.old, self.dir, bittorrent=True),
                     FakeRpc('active', self.old, self.dir, files=2),
                     FakeRpc('complete', self.old, self.dir)):
            self.assertEqual(self.rename(fake).status_code, 400)
            self.assertEqual(fake.methods(), ['aria2.tellStatus'])
        self.assertTrue(os.path.exists(self.old))

    def test_invalid_names_rejected_before_rpc(self):
        fake = FakeRpc('active', self.old, self.dir)
        for name in ('', '../x.mkv', 'a/b.mkv', 'a\\b.mkv', '..', 'x.aria2', 'x' * 256, 'cover.jpg'):
            self.assertEqual(self.rename(fake, name).status_code, 400, name)
        self.assertEqual(fake.calls, [])
        with mock.patch.object(scanner, 'call_aria2_rpc', fake):
            r = self.client.post('/api/aria2/rename', json={'gid': 'not-a-gid', 'name': 'a.mkv'})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
