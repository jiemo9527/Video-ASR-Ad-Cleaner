"""上传目标：scan_path 下第一级目录为 remote，更深的下载子目录在上传时丢弃。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_remote_path_test.db')

# The scratch DB MUST be selected before importing app; see test_prioritize_queue.py.
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402

SCAN = os.path.join(os.sep, 'root', 'downloads')


def p(*parts):
    return os.path.join(SCAN, *parts)


class ResolvePathRemoteTests(unittest.TestCase):
    def test_file_in_root_uses_default_remote(self):
        self.assertEqual(scanner.resolve_path_remote(p('a.mkv'), SCAN, 's25'), 's25')

    def test_first_level_folder_is_remote(self):
        self.assertEqual(scanner.resolve_path_remote(p('g01', 'a.mkv'), SCAN, 's25'), 'g01')

    def test_nested_download_folders_are_dropped(self):
        self.assertEqual(scanner.resolve_path_remote(p('g01', 'Season 3', 'a.mkv'), SCAN, 's25'), 'g01')
        self.assertEqual(scanner.resolve_path_remote(p('g01', 'Show', 'Season 3', 'a.mkv'), SCAN + os.sep, 's25'), 'g01')

    def test_outside_scan_path_keeps_legacy_folder_name(self):
        self.assertEqual(scanner.resolve_path_remote(os.path.join(os.sep, 'data', 'g02', 'a.mkv'), SCAN, 's25'), 'g02')


class UploadTargetTests(unittest.TestCase):
    def test_single_file_target_drops_subfolders(self):
        task = scanner.Task(filename='第6集.mkv', filepath=p('g01', 'Season 3', '第6集.mkv'), status='error')
        target = scanner.get_task_upload_target(task, {'scan_path': SCAN, 'rclone_remote': 's25'})
        self.assertEqual(target, 'g01:第6集.mkv')

    def test_directory_task_keeps_its_own_folder_but_drops_outer_ones(self):
        root = p('g01', 'Show', 'Pack')
        prefix, remote_path = scanner.build_directory_remote_path(root, os.path.join(root, 'S01', 'e1.mkv'), SCAN, 's25')
        self.assertEqual((prefix, remote_path), ('g01', 'g01:Pack/S01/e1.mkv'))
        _, remote_path = scanner.build_directory_remote_path(p('Pack'), p('Pack', 'e1.mkv'), SCAN, 's25')
        self.assertEqual(remote_path, 's25:Pack/e1.mkv')


if __name__ == '__main__':
    unittest.main()
