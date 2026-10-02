"""文件大小、公网 IP 打码与服务器磁盘空间展示。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_server_info_test.db')

# The scratch DB MUST be selected before importing app; see test_prioritize_queue.py.
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402


class MaskIpTests(unittest.TestCase):
    def test_ipv4_hides_last_two_octets(self):
        self.assertEqual(scanner.mask_ip('1.2.3.4'), '1.2.x.x')

    def test_ipv6_keeps_first_two_groups(self):
        self.assertEqual(scanner.mask_ip('2001:db8::1'), '2001:db8:x:x')

    def test_unknown_is_fully_masked(self):
        self.assertEqual(scanner.mask_ip(''), 'x.x.x.x')
        self.assertEqual(scanner.mask_ip('garbage'), 'x.x.x.x')

    def test_masked_ip_prefers_public_ip(self):
        with mock.patch.object(scanner, 'get_public_ip', return_value='8.8.4.4'), \
                mock.patch.object(scanner, 'get_server_ip', return_value='10.0.0.5'):
            self.assertEqual(scanner.get_masked_server_ip(), '8.8.x.x')
        with mock.patch.object(scanner, 'get_public_ip', return_value=''), \
                mock.patch.object(scanner, 'get_server_ip', return_value='10.0.0.5'):
            self.assertEqual(scanner.get_masked_server_ip(), '10.0.x.x')


class FileSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        scanner.dir_size_cache.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, size):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'\0' * size)
        return path

    def test_file_task_uses_live_size(self):
        path = self.write('a.mkv', 1234)
        task = scanner.Task(filepath=path, overrides=json.dumps({'_file_size': 99}))
        self.assertEqual(scanner.get_task_file_size(task), 1234)

    def test_moved_file_falls_back_to_stored_size(self):
        task = scanner.Task(filepath=os.path.join(self.tmp, 'gone.mkv'), overrides=json.dumps({'_file_size': 99}))
        self.assertEqual(scanner.get_task_file_size(task), 99)
        self.assertIsNone(scanner.get_task_file_size(scanner.Task(filepath=os.path.join(self.tmp, 'gone.mkv'))))

    def test_directory_size_skips_aria2_files(self):
        root = os.path.join(self.tmp, 'pack')
        self.write('pack/e1.mkv', 100)
        self.write('pack/S01/e2.mkv', 50)
        self.write('pack/e3.mkv.aria2', 7)
        task = scanner.Task(filepath=root, overrides=json.dumps({'_dir_task': True}))
        self.assertEqual(scanner.get_task_file_size(task), 150)

    def test_directory_task_prefers_stored_total(self):
        root = os.path.join(self.tmp, 'pack')
        self.write('pack/e1.mkv', 100)
        task = scanner.Task(filepath=root, overrides=json.dumps({'_dir_task': True, '_file_size': 500}))
        self.assertEqual(scanner.get_task_file_size(task), 500)


class DiskUsageTests(unittest.TestCase):
    def test_existing_path(self):
        tmp = tempfile.gettempdir()
        info = scanner.get_disk_usage_info(tmp)
        self.assertEqual(info['path'], tmp)
        self.assertGreater(info['total'], 0)
        self.assertGreaterEqual(info['free'], 0)

    def test_missing_path_falls_back_to_app_root(self):
        info = scanner.get_disk_usage_info(os.path.join(tempfile.gettempdir(), 'no-such-scan-path-xyz'))
        self.assertEqual(info['path'], scanner.APP_ROOT)


if __name__ == '__main__':
    unittest.main()
