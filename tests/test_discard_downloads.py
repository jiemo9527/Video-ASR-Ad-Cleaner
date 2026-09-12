"""RPC 下载请求过滤：图片/NFO 单文件请求丢弃，BT 种子与正常视频保持不变。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_discard_test.db')

# Bind the scratch DB before importing app; db.init_app() binds at import time.
# Set unconditionally so an inherited value cannot point tests at production.
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from core_logic import is_discarded_download_name  # noqa: E402
from database import db  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode()
        self.headers = {'Content-Type': 'application/json'}

    def json(self):
        return self._payload


class NameMatchTests(unittest.TestCase):
    def test_image_and_nfo_names_match(self):
        for name in ['poster.jpg', 'A.PNG', 'https://x/y/fanart.webp?token=1',
                     'movie.nfo', 'folder/thumb.tbn']:
            self.assertTrue(is_discarded_download_name(name), name)

    def test_media_names_do_not_match(self):
        for name in ['show.mkv', 'clip.mp4', 'subs.srt', 'x.nfo.mkv', '', None]:
            self.assertFalse(is_discarded_download_name(name), name)


class ProxyFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TMP_DB):
            os.remove(TMP_DB)
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

    def post(self, payload, upstream=None):
        with mock.patch.object(scanner, 'get_aria2_rpc_config', return_value=(6800, 'sec')), \
             mock.patch.object(scanner.requests, 'post',
                               return_value=FakeResponse(upstream)) as posted:
            resp = self.client.post('/api/aria2/jsonrpc', json=payload)
        return resp, posted

    def test_image_adduri_is_discarded_without_forwarding(self):
        payload = {'jsonrpc': '2.0', 'id': 'q1', 'method': 'aria2.addUri',
                   'params': [['https://x/poster.jpg']]}
        resp, posted = self.post(payload)
        self.assertEqual(resp.status_code, 200)
        posted.assert_not_called()
        body = resp.get_json()
        self.assertEqual(body['id'], 'q1')
        self.assertTrue(body['result'])

    def test_out_option_decides_over_uri(self):
        payload = {'jsonrpc': '2.0', 'id': 'q2', 'method': 'aria2.addUri',
                   'params': [['https://x/download'], {'out': 'movie.nfo'}]}
        _, posted = self.post(payload)
        posted.assert_not_called()

    def test_video_adduri_is_forwarded_untouched(self):
        payload = {'jsonrpc': '2.0', 'id': 'q3', 'method': 'aria2.addUri',
                   'params': [['https://x/show.mkv']]}
        upstream = {'jsonrpc': '2.0', 'id': 'q3', 'result': 'gid-real'}
        resp, posted = self.post(payload, upstream)
        posted.assert_called_once()
        self.assertEqual(resp.get_json()['result'], 'gid-real')

    def test_torrent_request_is_never_filtered(self):
        payload = {'jsonrpc': '2.0', 'id': 'q4', 'method': 'aria2.addTorrent',
                   'params': ['base64data', [], {'out': 'poster.jpg'}]}
        upstream = {'jsonrpc': '2.0', 'id': 'q4', 'result': 'gid-bt'}
        resp, posted = self.post(payload, upstream)
        posted.assert_called_once()
        self.assertEqual(resp.get_json()['result'], 'gid-bt')

    def test_multicall_keeps_order_and_drops_only_images(self):
        payload = {'jsonrpc': '2.0', 'id': 'q5', 'method': 'system.multicall',
                   'params': [[
                       {'methodName': 'aria2.addUri', 'params': [['https://x/a.mkv']]},
                       {'methodName': 'aria2.addUri', 'params': [['https://x/b.jpg']]},
                       {'methodName': 'aria2.addUri', 'params': [['https://x/c.mp4']]},
                   ]]}
        upstream = {'jsonrpc': '2.0', 'id': 'q5', 'result': [['gid-a'], ['gid-c']]}
        resp, posted = self.post(payload, upstream)
        forwarded = posted.call_args.kwargs['json']
        inner = forwarded['params'][-1]
        self.assertEqual(len(inner), 2)
        result = resp.get_json()['result']
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], ['gid-a'])
        self.assertEqual(result[2], ['gid-c'])
        self.assertNotIn(result[1][0], ('gid-a', 'gid-c'))

    def test_batch_with_only_discarded_calls_skips_upstream(self):
        payload = [
            {'jsonrpc': '2.0', 'id': 'b1', 'method': 'aria2.addUri',
             'params': [['https://x/1.jpg']]},
            {'jsonrpc': '2.0', 'id': 'b2', 'method': 'aria2.addUri',
             'params': [['https://x/2.nfo']]},
        ]
        resp, posted = self.post(payload)
        posted.assert_not_called()
        self.assertEqual([item['id'] for item in resp.get_json()], ['b1', 'b2'])


class SweeperTests(unittest.TestCase):
    def _entry(self, path, **extra):
        entry = {'gid': 'g1', 'status': 'active',
                 'files': [{'path': path, 'uris': [{'uri': 'https://x/' + os.path.basename(path)}]}]}
        entry.update(extra)
        return entry

    def test_single_image_download_is_detected(self):
        self.assertEqual(scanner.get_aria2_task_discard_name(self._entry('/root/downloads/a.jpg')), 'a.jpg')

    def test_video_and_torrent_are_kept(self):
        self.assertEqual(scanner.get_aria2_task_discard_name(self._entry('/root/downloads/a.mkv')), '')
        bt = self._entry('/root/downloads/a.jpg', bittorrent={'info': {'name': 'pack'}})
        self.assertEqual(scanner.get_aria2_task_discard_name(bt), '')

    def test_multi_file_task_is_kept(self):
        entry = {'gid': 'g2', 'files': [{'path': '/d/a.jpg'}, {'path': '/d/b.mkv'}]}
        self.assertEqual(scanner.get_aria2_task_discard_name(entry), '')

    def test_sweeper_removes_matching_task(self):
        calls = []

        def fake_rpc(method, params):
            calls.append((method, params))
            if method == 'aria2.tellActive':
                return [self._entry('/root/downloads/a.jpg')]
            if method == 'aria2.tellWaiting':
                return [self._entry('/root/downloads/b.mkv') | {'gid': 'g9'}]
            return 'OK'

        with mock.patch.object(scanner, 'call_aria2_rpc', side_effect=fake_rpc), \
             mock.patch.object(scanner, 'remove_discarded_aria2_files'):
            removed = scanner.discard_unwanted_aria2_downloads()

        self.assertEqual(removed, 1)
        self.assertIn(('aria2.forceRemove', ['g1']), calls)
        self.assertNotIn(('aria2.forceRemove', ['g9']), calls)


if __name__ == '__main__':
    unittest.main()
