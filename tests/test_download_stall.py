"""下载无进度自动暂停：下载中任务的已下载大小连续 N 秒不变时 forcePause。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_download_stall_test.db')
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from database import db, Config, User  # noqa: E402


class FakeAria2:
    def __init__(self):
        self.tasks = {}  # gid -> dict(status, totalLength, completedLength, ...)
        self.calls = []
        self.down = False
        self.fail_pause = set()

    def add(self, gid, completed=0, total=1000, **extra):
        self.tasks[gid] = dict({'gid': gid, 'status': 'active', 'totalLength': str(total),
                                'completedLength': str(completed)}, **extra)

    def __call__(self, method, params):
        self.calls.append((method, params))
        if self.down:
            raise RuntimeError('Aria2 RPC 请求失败: down')
        if method == 'aria2.tellActive':
            return [dict(t) for t in self.tasks.values() if t['status'] == 'active']
        if method == 'aria2.forcePause':
            if params[0] in self.fail_pause:
                raise RuntimeError('Aria2 RPC 返回异常')
            self.tasks[params[0]]['status'] = 'paused'
            return params[0]
        if method == 'aria2.getFiles':
            return [{'path': '/root/downloads/' + params[0] + '.mkv'}]
        raise AssertionError(method)

    def paused(self):
        return [p[0] for m, p in self.calls if m == 'aria2.forcePause']


class DownloadStallTests(unittest.TestCase):
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
        Config.query.filter(Config.key.in_(['download_stall_pause', 'download_stall_seconds'])).delete()
        db.session.commit()
        scanner.download_stall_progress.clear()
        scanner.download_stall_paused.clear()
        self.fake = FakeAria2()
        patcher = mock.patch.object(scanner, 'call_aria2_rpc', self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_config(self, **values):
        for key, value in values.items():
            row = db.session.get(Config, key) or Config(key=key)
            row.value = str(value)
            db.session.add(row)
        db.session.commit()

    def tick(self, now):
        with mock.patch('builtins.print'):
            return scanner.pause_stalled_aria2_downloads(now=now)

    def test_defaults(self):
        conf = scanner.get_final_config(None)
        self.assertIs(conf['download_stall_pause'], True)
        self.assertEqual(conf['download_stall_seconds'], 70)

    def test_paused_after_70s_without_progress(self):
        self.fake.add('a', completed=100)
        self.assertEqual(self.tick(0), [])
        self.assertEqual(self.tick(65), [])
        self.assertEqual(self.tick(70), ['a'])
        self.assertEqual(self.fake.tasks['a']['status'], 'paused')
        self.assertIn('a', scanner.download_stall_paused)
        self.assertNotIn('a', scanner.download_stall_progress)

    def test_progress_resets_timer(self):
        self.fake.add('a', completed=100)
        self.tick(0)
        self.fake.tasks['a']['completedLength'] = '200'
        self.assertEqual(self.tick(60), [])
        self.assertEqual(self.tick(125), [])
        self.assertEqual(self.tick(130), ['a'])

    def test_only_stalled_task_is_paused(self):
        self.fake.add('stuck', completed=5)
        self.fake.add('moving', completed=5)
        self.tick(0)
        self.fake.tasks['moving']['completedLength'] = '50'
        self.assertEqual(self.tick(70), ['stuck'])
        self.assertEqual(self.fake.tasks['moving']['status'], 'active')

    def test_unknown_size_without_progress_is_paused(self):
        # 例如拿不到元数据的磁力链：totalLength 一直是 0
        self.fake.add('magnet', completed=0, total=0)
        self.tick(0)
        self.assertEqual(self.tick(70), ['magnet'])

    def test_finished_seeding_task_is_left_alone(self):
        self.fake.add('seed', completed=1000, total=1000, bittorrent={'info': {'name': 'x'}})
        self.tick(0)
        self.assertEqual(self.tick(500), [])
        self.assertEqual(self.fake.paused(), [])

    def test_manual_resume_starts_a_fresh_timer(self):
        self.fake.add('a', completed=100)
        self.tick(0)
        self.tick(70)
        self.tick(75)  # 暂停中，不在 tellActive 里
        self.fake.tasks['a']['status'] = 'active'
        self.assertEqual(self.tick(100), [])
        self.assertNotIn('a', scanner.download_stall_paused)
        self.assertEqual(self.tick(165), [])
        self.assertEqual(self.tick(170), ['a'])

    def test_task_leaving_active_list_forgets_timer(self):
        self.fake.add('a', completed=100)
        self.tick(0)
        self.fake.tasks['a']['status'] = 'waiting'
        self.tick(50)
        self.fake.tasks['a']['status'] = 'active'
        self.assertEqual(self.tick(80), [])
        self.assertEqual(self.tick(150), ['a'])

    def test_aria2_outage_resets_timers(self):
        self.fake.add('a', completed=100)
        self.tick(0)
        self.fake.down = True
        with self.assertRaises(RuntimeError):
            self.tick(30)
        self.fake.down = False
        self.assertEqual(self.tick(80), [])
        self.assertEqual(self.tick(150), ['a'])

    def test_pause_failure_retries_next_tick(self):
        self.fake.add('a', completed=100)
        self.tick(0)
        self.fake.fail_pause.add('a')
        self.assertEqual(self.tick(70), [])
        self.assertNotIn('a', scanner.download_stall_paused)
        self.fake.fail_pause.clear()
        self.assertEqual(self.tick(75), ['a'])

    def test_disabled_never_pauses(self):
        self.set_config(download_stall_pause='false')
        self.fake.add('a', completed=100)
        self.tick(0)
        self.assertEqual(self.tick(1000), [])
        self.assertEqual(self.fake.paused(), [])
        # 重新开启后从头计时
        self.set_config(download_stall_pause='true')
        self.assertEqual(self.tick(1005), [])
        self.assertEqual(self.tick(1075), ['a'])

    def test_custom_duration_and_clamp(self):
        self.set_config(download_stall_seconds='30')
        self.fake.add('a', completed=1)
        self.tick(0)
        self.assertEqual(self.tick(30), ['a'])
        self.assertEqual(scanner.get_download_stall_seconds({'download_stall_seconds': 1}), 10)
        self.assertEqual(scanner.get_download_stall_seconds({'download_stall_seconds': 'x'}), 70)
        self.assertEqual(scanner.get_download_stall_seconds({'download_stall_seconds': 99999}), 3600)

    def test_settings_round_trip_and_validation(self):
        r = self.client.post('/api/settings', json={'download_stall_pause': False, 'download_stall_seconds': 120})
        self.assertEqual(r.status_code, 200)
        data = self.client.get('/api/settings').get_json()
        self.assertIs(data['download_stall_pause'], False)
        self.assertEqual(data['download_stall_seconds'], 120)
        for bad in (5, 4000, 'abc', None):
            r = self.client.post('/api/settings', json={'download_stall_seconds': bad})
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.client.get('/api/settings').get_json()['download_stall_seconds'], 120)

    def test_waiting_list_marks_auto_paused_tasks(self):
        scanner.download_stall_paused['a'] = 1
        listed = [{'gid': 'a', 'status': 'paused'}, {'gid': 'b', 'status': 'paused'}, {'gid': 'c', 'status': 'waiting'}]
        with mock.patch.object(scanner, 'aria2_multicall', return_value=[{'numActive': '0'}, listed]), \
             mock.patch.object(scanner, 'attach_aria2_file_summaries', side_effect=lambda tasks: tasks):
            data = self.client.post('/api/aria2/task_list', json={'list': 'waiting'}).get_json()
        flags = {t['gid']: t.get('stallPaused', False) for t in data['tasks']}
        self.assertEqual(flags, {'a': True, 'b': False, 'c': False})


if __name__ == '__main__':
    unittest.main()
