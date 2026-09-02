import unittest

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


if __name__ == '__main__':
    unittest.main()
