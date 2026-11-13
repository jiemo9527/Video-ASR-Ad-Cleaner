"""隐式字幕队列：字幕单文件下载不进 Aria2 常规队列，由 Scanner 直接下载（最多同时 20 个）。"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_subtitle_queue_test.db')
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from core_logic import is_subtitle_download_name  # noqa: E402
from database import db  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode()
        self.headers = {'Content-Type': 'application/json'}

    def json(self):
        return self._payload


class SubtitleHandler(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        SubtitleHandler.seen.append((self.path, dict(self.headers)))
        if 'expired' in self.path:
            body = b'{"status":403,"message":"request expired"}'
            self.send_response(403)
        else:
            body = '[Script Info]\nTitle: 测试\n'.encode('utf-8')
            self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SubtitleQueueTests(unittest.TestCase):
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
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), SubtitleHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='scanner_subtitle_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        SubtitleHandler.seen = []

    def post(self, payload, upstream=None):
        # 浏览器测试会替换已注册的代理路由，这里直接调用真实的代理函数
        with mock.patch.dict(scanner.app.config, {'LOGIN_DISABLED': True}), \
             mock.patch.object(scanner, 'get_aria2_rpc_config', return_value=(6800, 'sec')), \
             mock.patch.object(scanner.requests, 'post', return_value=FakeResponse(upstream)) as posted, \
             mock.patch.object(scanner, 'enqueue_subtitle_download', return_value=True) as enqueued, \
             scanner.app.test_request_context('/api/aria2/jsonrpc', method='POST', json=payload):
            resp = scanner.app.make_response(scanner.aria2_jsonrpc_proxy())
        return resp, posted, enqueued

    def test_subtitle_names(self):
        for name in ('a.ass', 'B.SRT', 'https://x/y/c.ssa?t=1', 'd.vtt', 'x/e.sup'):
            self.assertTrue(is_subtitle_download_name(name), name)
        for name in ('a.mkv', 'a.ass.mkv', 'https://x/a.mp4?f=.srt', '', None):
            self.assertFalse(is_subtitle_download_name(name), name)

    def test_proxy_takes_subtitle_adduri_without_forwarding(self):
        options = {'dir': '/data/g01/Season 1', 'out': 'S01E05.default.chi.zh-cn.ass',
                   'header': ['Cookie: x=1'], 'referer': 'https://115.com/', 'gid': 'abc', 'pause': 'true'}
        payload = {'jsonrpc': '2.0', 'id': 's1', 'method': 'aria2.addUri',
                   'params': ['token:sec', ['https://cdn/x?t=1'], options]}
        resp, posted, enqueued = self.post(payload)
        posted.assert_not_called()
        self.assertEqual(resp.get_json()['id'], 's1')
        self.assertTrue(resp.get_json()['result'])
        uris, opts, name = enqueued.call_args.args
        self.assertEqual(uris, ['https://cdn/x?t=1'])
        self.assertEqual(opts['out'], 'S01E05.default.chi.zh-cn.ass')
        self.assertEqual(opts['header'], ['Cookie: x=1'])
        self.assertNotIn('gid', opts)
        self.assertNotIn('pause', opts)
        self.assertEqual(name, 'S01E05.default.chi.zh-cn.ass')

    def test_video_and_torrent_still_go_to_aria2(self):
        payload = {'jsonrpc': '2.0', 'id': 'v', 'method': 'aria2.addUri',
                   'params': [['https://cdn/a.ass'], {'out': 'a.mkv'}]}
        _, posted, enqueued = self.post(payload, {'jsonrpc': '2.0', 'id': 'v', 'result': 'real'})
        posted.assert_called_once()
        enqueued.assert_not_called()
        payload = {'jsonrpc': '2.0', 'id': 't', 'method': 'aria2.addTorrent',
                   'params': ['data', [], {'out': 'a.srt'}]}
        _, posted, enqueued = self.post(payload, {'jsonrpc': '2.0', 'id': 't', 'result': 'bt'})
        posted.assert_called_once()
        enqueued.assert_not_called()

    def test_discard_rule_wins_over_subtitle_queue(self):
        config = dict(scanner.get_final_config(None), discard_extra_extensions='.srt')
        payload = {'jsonrpc': '2.0', 'id': 'd', 'method': 'aria2.addUri', 'params': [['https://x/a.srt']]}
        with mock.patch.object(scanner, 'get_final_config', return_value=config):
            _, posted, enqueued = self.post(payload)
        posted.assert_not_called()
        enqueued.assert_not_called()

    def test_multicall_keeps_order(self):
        payload = {'jsonrpc': '2.0', 'id': 'm', 'method': 'system.multicall', 'params': [[
            {'methodName': 'aria2.addUri', 'params': [['https://x/a.mkv']]},
            {'methodName': 'aria2.addUri', 'params': [['https://x/a.ass']]},
            {'methodName': 'aria2.addUri', 'params': [['https://x/b.mkv']]},
        ]]}
        resp, posted, enqueued = self.post(payload, {'jsonrpc': '2.0', 'id': 'm', 'result': [['g1'], ['g3']]})
        self.assertEqual(len(posted.call_args.kwargs['json']['params'][-1]), 2)
        enqueued.assert_called_once()
        result = resp.get_json()['result']
        self.assertEqual(result[0], ['g1'])
        self.assertEqual(result[2], ['g3'])

    def test_download_sends_task_headers_and_queues_detection(self):
        job = {'uris': [self.base + '/expired.ass', self.base + '/ok.ass?t=1'], 'name': 'x',
               'options': {'dir': self.tmp, 'out': 'Season 1/E05.ass', 'header': ['Cookie: UID=1', ''],
                           'referer': 'https://115.com/', 'user-agent': 'UA-Test'}}
        with mock.patch.object(scanner, 'create_trigger_task', return_value=(None, False)) as trigger:
            target = scanner.run_subtitle_download(job)
        self.assertEqual(target, os.path.join(self.tmp, 'Season 1', 'E05.ass'))
        with open(target, encoding='utf-8') as handle:
            self.assertIn('测试', handle.read())
        trigger.assert_called_once()
        self.assertEqual(trigger.call_args.args[0], target)
        headers = SubtitleHandler.seen[-1][1]
        self.assertEqual(headers.get('Cookie'), 'UID=1')
        self.assertEqual(headers.get('Referer'), 'https://115.com/')
        self.assertEqual(headers.get('User-Agent'), 'UA-Test')
        self.assertEqual([p for p in os.listdir(os.path.dirname(target))], ['E05.ass'])

    def test_expired_link_fails_fast_without_file(self):
        job = {'uris': [self.base + '/expired.ass'], 'name': 'x', 'options': {'dir': self.tmp}}
        with mock.patch.object(scanner, 'create_trigger_task') as trigger:
            self.assertEqual(scanner.run_subtitle_download(job), '')
        trigger.assert_not_called()
        self.assertEqual(len(SubtitleHandler.seen), 1)  # 4xx 不重试
        self.assertEqual(os.listdir(self.tmp), [])

    def test_existing_file_gets_aria2_style_rename(self):
        with open(os.path.join(self.tmp, 'ok.ass'), 'w') as handle:
            handle.write('old')
        job = {'uris': [self.base + '/ok.ass'], 'options': {'dir': self.tmp}}
        with mock.patch.object(scanner, 'create_trigger_task', return_value=(None, False)):
            self.assertEqual(scanner.run_subtitle_download(job), os.path.join(self.tmp, 'ok.1.ass'))

    def test_unsafe_out_is_rejected(self):
        job = {'uris': [self.base + '/ok.ass'], 'options': {'dir': self.tmp, 'out': '../evil.ass'}}
        self.assertEqual(scanner.run_subtitle_download(job), '')

    def test_capture_moves_subtitle_tasks_out_of_aria2(self):
        sub_path = os.path.join(self.tmp, 'Season 1', 'E05.ass')
        tasks = {
            's1': {'gid': 's1', 'status': 'active', 'totalLength': '0', 'completedLength': '0', 'dir': self.tmp,
                   'files': [{'path': sub_path, 'uris': [{'uri': 'https://cdn/x?t=1'}, {'uri': 'https://cdn/x?t=1'}]}]},
            'v1': {'gid': 'v1', 'status': 'active', 'totalLength': '9', 'completedLength': '1', 'dir': self.tmp,
                   'files': [{'path': os.path.join(self.tmp, 'a.mkv'), 'uris': [{'uri': 'https://cdn/y'}]}]},
            's2': {'gid': 's2', 'status': 'paused', 'totalLength': '5', 'completedLength': '5', 'dir': self.tmp,
                   'files': [{'path': os.path.join(self.tmp, 'done.ass'), 'uris': [{'uri': 'https://cdn/z'}]}]},
            'b1': {'gid': 'b1', 'status': 'waiting', 'bittorrent': {'info': {'name': 'x'}}, 'dir': self.tmp,
                   'files': [{'path': os.path.join(self.tmp, 'x.srt'), 'uris': []}]},
        }
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            if method == 'aria2.tellActive':
                return [t for t in tasks.values() if t['status'] == 'active']
            if method == 'aria2.tellWaiting':
                return [t for t in tasks.values() if t['status'] in ('waiting', 'paused')]
            if method == 'aria2.getOption':
                return {'dir': self.tmp, 'out': 'stale', 'header': ['Cookie: a=1'], 'gid': params[0]}
            if method == 'aria2.forceRemove':
                tasks[params[0]]['status'] = 'removed'
                return params[0]
            if method == 'aria2.tellStatus':
                return {'status': tasks[params[0]]['status']}
            if method == 'aria2.removeDownloadResult':
                return 'OK'
            raise AssertionError(method)

        config = dict(scanner.get_final_config(None), scan_path=self.tmp)
        with mock.patch.object(scanner, 'call_aria2_rpc', side_effect=rpc), \
             mock.patch.object(scanner, 'get_final_config', return_value=config), \
             mock.patch.object(scanner, 'enqueue_subtitle_download', return_value=True) as enqueued:
            self.assertEqual(scanner.capture_subtitle_aria2_downloads(), 1)
        self.assertEqual([p[0] for m, p in calls if m == 'aria2.forceRemove'], ['s1'])
        self.assertIn(('aria2.removeDownloadResult', ['s1']), calls)
        uris, options, name = enqueued.call_args.args
        self.assertEqual(uris, ['https://cdn/x?t=1'])
        self.assertEqual(options['out'], 'Season 1/E05.ass')
        self.assertEqual(options['header'], ['Cookie: a=1'])
        self.assertNotIn('gid', options)
        self.assertEqual(name, 'E05.ass')

    def test_queue_runs_at_most_20_at_once_and_reports_busy(self):
        release = threading.Event()
        lock = threading.Lock()
        state = {'now': 0, 'peak': 0, 'done': 0}

        def fake_run(job):
            with lock:
                state['now'] += 1
                state['peak'] = max(state['peak'], state['now'])
            release.wait(10)
            with lock:
                state['now'] -= 1
                state['done'] += 1
            return ''

        with mock.patch.object(scanner, 'run_subtitle_download', side_effect=fake_run), \
             mock.patch.object(scanner, 'call_aria2_rpc', return_value={}):
            scanner.start_subtitle_download_workers()
            for index in range(30):
                self.assertTrue(scanner.enqueue_subtitle_download(
                    [f'https://x/{index}.ass'], {'dir': self.tmp}, f'{index}.ass'))
            self.assertFalse(scanner.enqueue_subtitle_download(['https://x/0.ass'], {'dir': self.tmp}))
            deadline = time.time() + 5
            while time.time() < deadline and state['now'] < 20:
                time.sleep(0.02)
            time.sleep(0.2)
            self.assertEqual(state['peak'], 20)
            self.assertEqual(scanner.subtitle_download_counts(), (10, 20))
            with mock.patch.object(scanner, 'get_scanner_api_token', return_value='tok'), \
                 mock.patch.object(scanner, 'call_aria2_rpc', return_value={'numActive': '0', 'numWaiting': '0'}), \
                 mock.patch.object(scanner, 'get_active_task_ids', return_value=set()):
                body = self.client.get('/api/status', headers={'X-API-Token': 'tok'}).get_json()
            self.assertTrue(body['aria2_ok'])
            self.assertTrue(body['busy'])
            self.assertEqual((body['subtitle_pending'], body['subtitle_running']), (10, 20))
            release.set()
            deadline = time.time() + 5
            while time.time() < deadline and state['done'] < 30:
                time.sleep(0.02)
            scanner.subtitle_download_queue.join()
        self.assertEqual(state['done'], 30)
        self.assertEqual(state['peak'], 20)
        self.assertEqual(scanner.subtitle_download_counts(), (0, 0))


if __name__ == '__main__':
    unittest.main()
