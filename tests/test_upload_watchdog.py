import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core_logic import ScannerCore


class UploadWatchdogTests(unittest.TestCase):
    def test_rclone_command_enables_transport_recovery(self):
        core = ScannerCore(rclone_remote='g01')

        command = core.get_upload_rclone_cmd('/tmp/movie.mkv', 'g01:movie.mkv')

        self.assertEqual(command[:4], ['rclone', 'moveto', '/tmp/movie.mkv', 'g01:movie.mkv'])
        self.assertIn('--timeout', command)
        self.assertEqual(command[command.index('--timeout') + 1], '60s')
        self.assertIn('--low-level-retries', command)
        self.assertEqual(command[command.index('--low-level-retries') + 1], '20')
        self.assertIn('--drive-stop-on-upload-limit', command)
        self.assertEqual(command[command.index('--drive-chunk-size') + 1], '128M')

    def test_slow_upload_watchdog_requires_sustained_low_speed_on_large_file(self):
        core = ScannerCore()

        self.assertFalse(core.should_restart_slow_upload(
            file_size=400 * 1024 * 1024,
            speed_bytes=100 * 1024,
            low_speed_started_at=0,
            now=100,
        ))
        self.assertFalse(core.should_restart_slow_upload(
            file_size=1024 * 1024 * 1024,
            speed_bytes=3 * 1024 * 1024,
            low_speed_started_at=0,
            now=100,
        ))
        self.assertFalse(core.should_restart_slow_upload(
            file_size=1024 * 1024 * 1024,
            speed_bytes=2 * 1024 * 1024,
            low_speed_started_at=66,
            now=100,
        ))
        self.assertTrue(core.should_restart_slow_upload(
            file_size=1024 * 1024 * 1024,
            speed_bytes=2 * 1024 * 1024,
            low_speed_started_at=0,
            now=35,
        ))

    def test_slow_upload_retry_is_bounded(self):
        core = ScannerCore()

        self.assertTrue(core.can_retry_slow_upload(0))
        self.assertTrue(core.can_retry_slow_upload(2))
        self.assertFalse(core.can_retry_slow_upload(3))


# 模拟 rclone：持续报告 1KB/s 的低速，最后正常完成。
SLOW_RCLONE = r'''#!/usr/bin/env python3
import json, sys
for _ in range(3):
    print(json.dumps({"level": "info", "msg": "", "stats": {"speed": 1024, "eta": 999, "transferring": [{"bytes": 1, "size": 1}]}}), file=sys.stderr, flush=True)
'''


class SlowRestartSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rclone = os.path.join(self.tmp.name, 'rclone')
        with open(rclone, 'w') as f:
            f.write(SLOW_RCLONE)
        os.chmod(rclone, os.stat(rclone).st_mode | stat.S_IEXEC)
        self.old_path = os.environ['PATH']
        os.environ['PATH'] = self.tmp.name + os.pathsep + self.old_path
        # 稀疏文件：大小超过看门狗阈值但不占磁盘
        self.media = os.path.join(self.tmp.name, 'movie.mkv')
        with open(self.media, 'wb') as f:
            f.truncate(ScannerCore.UPLOAD_WATCHDOG_MIN_FILE_SIZE + 1)

    def tearDown(self):
        os.environ['PATH'] = self.old_path
        self.tmp.cleanup()

    def upload(self, enabled):
        core = ScannerCore(logger_callback=lambda m: None)
        core.upload_slow_restart_enabled = enabled
        with mock.patch.object(ScannerCore, 'UPLOAD_WATCHDOG_GRACE_SECONDS', 0):
            return core, core.upload_with_progress(self.media, 'g01:movie.mkv')

    def test_enabled_watchdog_restarts_slow_upload(self):
        core, ok = self.upload(True)
        self.assertFalse(ok)
        self.assertTrue(core.upload_restart_requested)

    def test_disabled_watchdog_lets_slow_upload_finish(self):
        core, ok = self.upload(False)
        self.assertTrue(ok)
        self.assertFalse(core.upload_restart_requested)


if __name__ == '__main__':
    unittest.main()
