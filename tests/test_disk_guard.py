"""磁盘空间强制保护：下载中任务剩余体积之和 >= 剩余磁盘空间时暂停全部下载，空间够用后恢复。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_disk_guard_test.db')
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from database import db, Config, User  # noqa: E402

GB = 1024 ** 3


class FakeAria2:
    def __init__(self):
        self.tasks = {}  # 保持插入顺序 = 队列顺序
        self.calls = []

    def add(self, gid, status='active', total=5 * GB, completed=0):
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
            self.tasks[params[0]]['status'] = 'waiting'
            return params[0]
        raise AssertionError(method)

    def calls_of(self, method):
        return [p[0] for m, p in self.calls if m == method]


class DiskGuardTests(unittest.TestCase):
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
        Config.query.filter_by(key=scanner.DISK_GUARD_CONFIG_KEY).delete()
        db.session.commit()
        scanner.disk_guard_state.clear()
        scanner.disk_guard_state.update(scanner.new_disk_guard_state())
        self.fake = FakeAria2()
        self.free = 100 * GB
        for target, value in (('call_aria2_rpc', self.fake),
                              ('get_disk_usage_info', lambda path: {'path': path, 'total': 200 * GB, 'used': 0, 'free': self.free})):
            patcher = mock.patch.object(scanner, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tick(self):
        with mock.patch('builtins.print'):
            return scanner.enforce_disk_guard()

    def statuses(self):
        return {gid: t['status'] for gid, t in self.fake.tasks.items()}

    def test_example_three_downloads_15gb(self):
        for gid in ('a', 'b', 'c'):
            self.fake.add(gid, total=5 * GB)
        self.fake.add('w', status='waiting')
        self.fake.add('manual', status='paused')
        self.free = 16 * GB
        self.assertFalse(self.tick()['engaged'])
        self.free = 15 * GB  # 等于也触发
        status = self.tick()
        self.assertTrue(status['engaged'])
        self.assertEqual(status['need'], 15 * GB)
        self.assertEqual(status['paused_count'], 4)
        self.assertEqual(self.statuses(), {'a': 'paused', 'b': 'paused', 'c': 'paused', 'w': 'paused', 'manual': 'paused'})

    def test_counts_only_remaining_bytes(self):
        self.fake.add('a', total=10 * GB, completed=8 * GB)
        self.free = 3 * GB
        self.assertFalse(self.tick()['engaged'])
        self.free = 2 * GB
        self.assertTrue(self.tick()['engaged'])

    def test_unknown_size_and_idle_never_trigger(self):
        self.fake.add('magnet', total=0)
        self.free = 0
        self.assertFalse(self.tick()['engaged'])
        self.fake.tasks.clear()
        self.assertFalse(self.tick()['engaged'])

    def test_resumes_only_guard_paused_when_space_is_enough(self):
        self.fake.add('a', total=10 * GB)
        self.fake.add('w', status='waiting')
        self.fake.add('manual', status='paused')
        self.free = 5 * GB
        self.tick()
        self.free = 9 * GB
        self.assertTrue(self.tick()['engaged'])  # 仍按暂停时的下载任务计算 10GB
        self.assertEqual(self.fake.calls_of('aria2.unpause'), [])
        self.free = 11 * GB
        self.assertFalse(self.tick()['engaged'])
        self.assertEqual(self.fake.calls_of('aria2.unpause'), ['a', 'w'])
        self.assertEqual(self.statuses()['manual'], 'paused')

    def test_deleting_tasks_releases_guard(self):
        self.fake.add('a', total=10 * GB)
        self.fake.add('b', total=10 * GB)
        self.free = 15 * GB
        self.tick()
        del self.fake.tasks['b']
        self.assertFalse(self.tick()['engaged'])
        self.assertEqual(self.statuses(), {'a': 'waiting'})

    def test_tasks_started_during_guard_are_paused_again(self):
        self.fake.add('a', total=10 * GB)
        self.free = 5 * GB
        self.tick()
        self.fake.tasks['a']['status'] = 'active'  # 直连 RPC 手动开始
        self.fake.add('new', status='waiting')   # 保护期间新加入
        self.assertTrue(self.tick()['engaged'])
        self.assertEqual(self.statuses(), {'a': 'paused', 'new': 'paused'})
        self.free = 50 * GB
        self.tick()
        self.assertEqual(self.statuses(), {'a': 'waiting', 'new': 'waiting'})

    def test_state_survives_restart(self):
        self.fake.add('a', total=10 * GB)
        self.free = 5 * GB
        self.tick()
        saved = json.loads(db.session.get(Config, scanner.DISK_GUARD_CONFIG_KEY).value)
        self.assertEqual(saved['paused'], ['a'])
        scanner.disk_guard_state.clear()
        scanner.disk_guard_state.update(scanner.new_disk_guard_state())
        self.free = 50 * GB
        self.assertFalse(self.tick()['engaged'])
        self.assertEqual(self.statuses(), {'a': 'waiting'})
        self.assertFalse(json.loads(db.session.get(Config, scanner.DISK_GUARD_CONFIG_KEY).value)['engaged'])

    def test_cannot_be_disabled(self):
        # 没有对应设置项，写入任意开关都不影响
        self.client.post('/api/settings', json={'disk_guard': False, 'sys_disk_guard': '{"engaged": false}'})
        self.fake.add('a', total=10 * GB)
        self.free = 5 * GB
        self.assertTrue(self.tick()['engaged'])
        self.assertTrue(self.tick()['engaged'])
        saved = json.loads(db.session.get(Config, scanner.DISK_GUARD_CONFIG_KEY).value)
        self.assertTrue(saved['engaged'])
        backup = self.client.get('/api/settings/backup').get_json()['config']
        self.assertNotIn(scanner.DISK_GUARD_CONFIG_KEY, backup)

    def test_proxy_rejects_unpause_while_engaged(self):
        self.fake.add('a', total=10 * GB)
        self.free = 5 * GB
        self.tick()
        with mock.patch.object(scanner.requests, 'post') as post:
            for method in ('aria2.unpause', 'aria2.unpauseAll'):
                r = self.client.post('/api/aria2/jsonrpc', json={'jsonrpc': '2.0', 'id': 'x', 'method': method, 'params': ['a']})
                body = r.get_json()
                self.assertIn('磁盘空间不足', body['error']['message'])
            post.assert_not_called()
        self.assertEqual(self.statuses(), {'a': 'paused'})

    def test_waiting_list_and_server_info_report_guard(self):
        self.fake.add('a', total=10 * GB)
        self.fake.add('manual', status='paused')
        self.free = 5 * GB
        self.tick()
        listed = [{'gid': 'a', 'status': 'paused'}, {'gid': 'manual', 'status': 'paused'}]
        with mock.patch.object(scanner, 'aria2_multicall', return_value=[{'numActive': '0'}, listed]), \
             mock.patch.object(scanner, 'attach_aria2_file_summaries', side_effect=lambda tasks: tasks):
            data = self.client.post('/api/aria2/task_list', json={'list': 'waiting'}).get_json()
        self.assertTrue(data['disk_guard']['engaged'])
        flags = {t['gid']: t.get('diskGuardPaused', False) for t in data['tasks']}
        self.assertEqual(flags, {'a': True, 'manual': False})
        with mock.patch.object(scanner, 'get_masked_server_ip', return_value='1.2.x.x'):
            info = self.client.get('/api/server_info').get_json()
        self.assertTrue(info['disk_guard']['engaged'])
        self.assertEqual(info['disk_guard']['need'], 10 * GB)


if __name__ == '__main__':
    unittest.main()
