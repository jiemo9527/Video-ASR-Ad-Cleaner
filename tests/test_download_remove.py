"""下载器删除：停止任务并删除未完成的残留文件，已完成的文件保留。"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from flask import g

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_download_remove_test.db')
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from database import db, User  # noqa: E402


class FakeAria2:
    """forceRemove 后状态变成 removed；暂停任务的 forceRemove 不会删文件（与真实 Aria2 一致）。"""

    def __init__(self, tasks):
        self.tasks = {t['gid']: t for t in tasks}
        self.calls = []
        self.fail: set = set()

    def __call__(self, method, params):
        self.calls.append((method, params))
        first = params[0] if params and isinstance(params[0], str) else None
        if method in self.fail or (method, first) in self.fail:
            raise RuntimeError('boom')
        if method == 'aria2.tellStatus':
            if params[0] not in self.tasks:
                raise RuntimeError('GID not found')
            return dict(self.tasks[params[0]])
        if method == 'aria2.forceRemove':
            self.tasks[params[0]]['status'] = 'removed'
            return params[0]
        if method == 'aria2.removeDownloadResult':
            self.tasks.pop(params[0], None)
            return 'OK'
        if method == 'aria2.tellActive':
            return [dict(t) for t in self.tasks.values() if t['status'] == 'active']
        if method == 'aria2.tellWaiting':
            return [dict(t) for t in self.tasks.values() if t['status'] in ('waiting', 'paused')]
        raise AssertionError('unexpected RPC ' + method)


def task(gid, status, paths, root, total=100, done=10, **extra):
    entry = {'gid': gid, 'status': status, 'dir': root, 'totalLength': str(total),
             'completedLength': str(done), 'errorCode': '0',
             'files': [{'path': p} for p in paths]}
    entry.update(extra)
    return entry


class DownloadRemoveTests(unittest.TestCase):
    root = ''
    scan = ''

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

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.scan = os.path.join(self.root, 'downloads')
        os.makedirs(os.path.join(self.scan, 'g01'))
        patcher = mock.patch.object(scanner, 'get_final_config', return_value={'scan_path': self.scan})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, *parts, control=True):
        path = os.path.join(self.scan, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'x')
        if control:
            with open(path + '.aria2', 'wb') as f:
                f.write(b'c')
        return path

    def run_remove(self, fake, gids):
        with mock.patch.object(scanner, 'call_aria2_rpc', side_effect=fake):
            return scanner.remove_aria2_downloads(gids)

    def assertGone(self, path):
        self.assertFalse(os.path.exists(path), path)
        self.assertFalse(os.path.exists(path + '.aria2'), path + '.aria2')

    def test_active_paused_waiting_error_removed_delete_residual_files(self):
        g = os.path.join(self.scan, 'g01')
        paths = {s: self.make('g01', s + '.mkv') for s in ('active', 'paused', 'waiting', 'error', 'removed')}
        fake = FakeAria2([task('a%d' % i, s, [p], g) for i, (s, p) in enumerate(paths.items())])
        result = self.run_remove(fake, ['a%d' % i for i in range(5)])
        self.assertEqual(result, {'removed': 5, 'files': 10, 'failed': []})
        for path in paths.values():
            self.assertGone(path)
        self.assertEqual(fake.tasks, {})
        forced = sorted(p[0] for m, p in fake.calls if m == 'aria2.forceRemove')
        self.assertEqual(forced, ['a0', 'a1', 'a2'])

    def test_completed_files_are_kept(self):
        g = os.path.join(self.scan, 'g01')
        done = self.make('g01', 'done.mkv', control=False)
        seeding = self.make('g01', 'seed.mkv')  # 已下完在做种的 BT 仍有 .aria2
        fake = FakeAria2([
            task('c1', 'complete', [done], g, done=100),
            task('c2', 'active', [seeding], g, done=100, bittorrent={'info': {'name': 'seed.mkv'}}),
        ])
        result = self.run_remove(fake, ['c1', 'c2'])
        self.assertEqual(result['removed'], 2)
        self.assertEqual(result['files'], 0)
        self.assertTrue(os.path.exists(done))
        self.assertTrue(os.path.exists(seeding))

    def test_file_without_control_file_is_kept(self):
        g = os.path.join(self.scan, 'g01')
        path = self.make('g01', 'finished-elsewhere.mkv', control=False)
        fake = FakeAria2([task('p1', 'paused', [path], g)])
        self.assertEqual(self.run_remove(fake, ['p1'])['files'], 0)
        self.assertTrue(os.path.exists(path))

    def test_paths_outside_scan_path_are_kept(self):
        outside = os.path.join(self.root, 'other', 'x.mkv')
        os.makedirs(os.path.dirname(outside))
        for p in (outside, outside + '.aria2'):
            open(p, 'wb').close()
        fake = FakeAria2([task('o1', 'paused', [outside], os.path.dirname(outside))])
        result = self.run_remove(fake, ['o1'])
        self.assertEqual(result['files'], 0)
        self.assertTrue(os.path.exists(outside))
        self.assertTrue(os.path.exists(outside + '.aria2'))

    def test_foreign_file_error_codes_keep_files(self):
        g = os.path.join(self.scan, 'g01')
        path = self.make('g01', 'dup.mkv')
        fake = FakeAria2([task('d1', 'error', [path], g, errorCode='13')])
        self.assertEqual(self.run_remove(fake, ['d1'])['files'], 0)
        self.assertTrue(os.path.exists(path))

    def test_stopped_record_keeps_path_used_by_live_task(self):
        g = os.path.join(self.scan, 'g01')
        path = self.make('g01', 'retry.mkv')
        fake = FakeAria2([task('e1', 'error', [path], g), task('n1', 'active', [path], g)])
        result = self.run_remove(fake, ['e1'])
        self.assertEqual(result, {'removed': 1, 'files': 0, 'failed': []})
        self.assertTrue(os.path.exists(path + '.aria2'))
        self.assertIn('n1', fake.tasks)

    def test_stopped_record_is_kept_when_live_tasks_unknown(self):
        g = os.path.join(self.scan, 'g01')
        path = self.make('g01', 'unknown.mkv')
        fake = FakeAria2([task('e1', 'error', [path], g)])
        fake.fail.add('aria2.tellActive')
        result = self.run_remove(fake, ['e1'])
        self.assertEqual(result['removed'], 0)
        self.assertEqual(len(result['failed']), 1)
        self.assertTrue(os.path.exists(path))
        self.assertIn('e1', fake.tasks)

    def test_bt_task_removes_partial_files_and_empty_dirs_inside_scan_path(self):
        g = os.path.join(self.scan, 'g01')
        a = self.make('g01', 'Show.S01', 'Season 1', 'e1.mkv', control=False)
        b = self.make('g01', 'Show.S01', 'e2.mkv', control=False)
        keep = self.make('g01', 'Show.S01', 'notes.txt', control=False)  # 不属于任务的文件
        control = os.path.join(g, 'Show.S01.aria2')
        open(control, 'wb').close()
        fake = FakeAria2([task('b1', 'paused', [a, b], g, bittorrent={'info': {'name': 'Show.S01'}})])
        result = self.run_remove(fake, ['b1'])
        self.assertEqual(result['files'], 3)
        self.assertFalse(os.path.exists(a) or os.path.exists(b) or os.path.exists(control))
        self.assertFalse(os.path.exists(os.path.join(g, 'Show.S01', 'Season 1')))
        self.assertTrue(os.path.exists(keep))
        self.assertTrue(os.path.isdir(g))

    def test_single_file_subdirectory_removed_when_empty_but_scan_dirs_kept(self):
        g = os.path.join(self.scan, 'g01')
        path = self.make('g01', 'Season 2', 'e.mkv')
        fake = FakeAria2([task('s1', 'paused', [path], g)])
        self.run_remove(fake, ['s1'])
        self.assertFalse(os.path.exists(os.path.dirname(path)))
        self.assertTrue(os.path.isdir(g))
        self.assertTrue(os.path.isdir(self.scan))

    def test_force_remove_failure_keeps_files_and_reports(self):
        g = os.path.join(self.scan, 'g01')
        path = self.make('g01', 'f.mkv')
        fake = FakeAria2([task('f1', 'paused', [path], g)])
        fake.fail.add(('aria2.forceRemove', 'f1'))
        result = self.run_remove(fake, ['f1'])
        self.assertEqual(result['removed'], 0)
        self.assertEqual(result['failed'][0]['gid'], 'f1')
        self.assertTrue(os.path.exists(path))

    def test_files_deleted_only_after_task_stopped(self):
        g = os.path.join(self.scan, 'g01')
        path = self.make('g01', 'slow.mkv')
        fake = FakeAria2([task('w1', 'active', [path], g)])
        original = fake.__call__
        stops = {'n': 0}

        def slow(method, params):
            if method == 'aria2.forceRemove':
                fake.calls.append((method, params))
                return params[0]  # 还没停下
            if method == 'aria2.tellStatus' and params[1] == ['status']:
                stops['n'] += 1
                self.assertTrue(os.path.exists(path))
                if stops['n'] >= 3:
                    fake.tasks['w1']['status'] = 'removed'
            return original(method, params)

        with mock.patch.object(scanner, 'call_aria2_rpc', side_effect=slow), \
                mock.patch.object(scanner.time, 'sleep'):
            result = scanner.remove_aria2_downloads(['w1'])
        self.assertEqual(result['files'], 2)
        self.assertGone(path)

    def test_endpoint_validates_gids_and_requires_login(self):
        # 类级 app context 共享 g，其他测试/请求缓存的登录用户会串过来，每次请求前清掉
        g.pop('_login_user', None)
        anonymous = scanner.app.test_client()
        # TESTING / LOGIN_DISABLED 会让 Flask-Login 跳过检查，按生产配置测
        with mock.patch.dict(scanner.app.config, {'LOGIN_DISABLED': False, 'TESTING': False}):
            self.assertNotEqual(anonymous.post('/api/aria2/remove', json={'gids': ['a1']}).status_code, 200)
        g.pop('_login_user', None)
        client = scanner.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = 'tester'
        for bad in (None, [], ['../a'], ['a' * 17], [1]):
            self.assertEqual(client.post('/api/aria2/remove', json={'gids': bad}).status_code, 400)
        path = self.make('g01', 'api.mkv')
        fake = FakeAria2([task('abc', 'paused', [path], os.path.dirname(path))])
        with mock.patch.object(scanner, 'call_aria2_rpc', side_effect=fake):
            data = client.post('/api/aria2/remove', json={'gids': ['abc', 'abc']}).get_json()
        self.assertEqual(data, {'code': 200, 'removed': 1, 'files': 2, 'failed': []})
        self.assertGone(path)


if __name__ == '__main__':
    unittest.main()
