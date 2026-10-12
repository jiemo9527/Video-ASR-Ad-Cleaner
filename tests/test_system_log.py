"""无 systemd/journald 环境下的文件系统日志：写入、读取、隐藏过滤、清空与后端选择。"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_system_log_test.db')

# The scratch DB MUST be selected before importing app; see test_prioritize_queue.py.
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
import core_logic  # noqa: E402
from database import db  # noqa: E402


class FileSystemLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scanner.app.config['TESTING'] = True
        scanner.app.config['LOGIN_DISABLED'] = True
        cls.ctx = scanner.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = scanner.app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        core_logic.clear_system_log_file()
        self.patches = [
            mock.patch.object(core_logic, 'SYSTEM_LOG_PATH', os.path.join(self.tmp, 'system.log')),
            mock.patch.dict(os.environ, {'SCANNER_LOG_BACKEND': 'file'}),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        core_logic.clear_system_log_file()
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_task_log_lines_are_shown_without_prefix(self):
        core = core_logic.ScannerCore(logger_callback=lambda _m: None, task_id=7)
        core.log('✅ 云端识别通过 [片头]')
        core.log('第二行\n续行')
        data = self.client.get('/api/system_logs').get_json()
        self.assertEqual(data['code'], 200)
        self.assertEqual(data['backend'], 'file')
        lines = data['data'].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \[Task-7\] ✅ 云端识别通过 \[片头\]$')
        self.assertTrue(lines[1].endswith('[Task-7] 第二行 ⏎ 续行'))

    def test_hidden_markers_are_filtered_but_kept_in_raw(self):
        core = core_logic.ScannerCore(logger_callback=lambda _m: None)
        core.log('✂️ 提取音频 [片尾]: 1.0s - 360.0s')
        core.log('✅ 全流程通过')
        shown = self.client.get('/api/system_logs').get_json()['data']
        self.assertNotIn('提取音频', shown)
        self.assertIn('全流程通过', shown)
        raw = self.client.get('/api/system_logs?raw=1').get_json()['data']
        self.assertIn('提取音频', raw)

    def test_lines_param_returns_tail(self):
        for i in range(30):
            core_logic.write_system_log(f'line {i}')
        lines = self.client.get('/api/system_logs?lines=5').get_json()['data'].splitlines()
        self.assertEqual([line.split(' ', 1)[1] for line in lines], [f'line {i}' for i in range(25, 30)])
        bad = self.client.get('/api/system_logs?lines=abc').get_json()
        self.assertEqual(bad['code'], 200)

    def test_tail_spans_rotated_backup(self):
        with mock.patch.object(core_logic, 'SYSTEM_LOG_MAX_BYTES', 200):
            core_logic.clear_system_log_file()
            for i in range(12):
                core_logic.write_system_log(f'rot {i:02d}')
        self.assertTrue(os.path.exists(core_logic.SYSTEM_LOG_PATH + '.1'))
        tail = core_logic.read_system_log_tail(6)
        self.assertEqual([line.rsplit(' ', 1)[1] for line in tail], [f'{i:02d}' for i in range(6, 12)])

    def test_clear_empties_log(self):
        core_logic.write_system_log('to be cleared')
        result = self.client.post('/api/system_logs/clear').get_json()
        self.assertEqual(result['code'], 200)
        self.assertEqual(self.client.get('/api/system_logs').get_json()['data'], '')
        core_logic.write_system_log('after clear')
        self.assertIn('after clear', self.client.get('/api/system_logs').get_json()['data'])


class BackendSelectionTests(unittest.TestCase):
    def setUp(self):
        core_logic._system_log_backend_cache = None

    def tearDown(self):
        core_logic._system_log_backend_cache = None

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {'SCANNER_LOG_BACKEND': 'journald'}):
            self.assertEqual(core_logic.system_log_backend(), 'journald')

    def test_without_systemd_falls_back_to_file(self):
        with mock.patch.dict(os.environ, {'SCANNER_LOG_BACKEND': ''}), \
                mock.patch.object(core_logic.shutil, 'which', return_value=None):
            self.assertEqual(core_logic.system_log_backend(), 'file')

    def test_with_systemd_uses_journald(self):
        with mock.patch.dict(os.environ, {'SCANNER_LOG_BACKEND': ''}), \
                mock.patch.object(core_logic.os, 'name', 'posix'), \
                mock.patch.object(core_logic.shutil, 'which', return_value='/usr/bin/journalctl'), \
                mock.patch.object(core_logic.os.path, 'isdir', return_value=True):
            self.assertEqual(core_logic.system_log_backend(), 'journald')

    def test_journald_backend_does_not_write_file(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'system.log')
        try:
            with mock.patch.dict(os.environ, {'SCANNER_LOG_BACKEND': 'journald'}), \
                    mock.patch.object(core_logic, 'SYSTEM_LOG_PATH', path), \
                    mock.patch.object(core_logic.syslog, 'syslog') as sys_log:
                core_logic.ScannerCore(logger_callback=lambda _m: None, task_id=3).log('hello')
            sys_log.assert_called_once()
            self.assertIn('[Task-3] hello', sys_log.call_args[0][1])
            self.assertFalse(os.path.exists(path))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
