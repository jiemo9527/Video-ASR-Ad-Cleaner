"""Optional browser test for Scanner's built-in downloader (no AriaNg, no live Aria2).

Requires Playwright in a separate test environment; SCANNER_TEST_CHROMIUM may
select a local Chromium. The Flask dashboard is rendered for real and
/api/aria2/jsonrpc is answered by an in-memory fake Aria2, so every action is
checked against the RPC calls it produced. CDN assets (Vue/Bootstrap/axios)
must be reachable. Without the prerequisites these tests skip; a skip is not
UI verification.
"""
import base64
import copy
import importlib
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sync_playwright = importlib.import_module('playwright.sync_api').sync_playwright
except ImportError:
    sync_playwright = None

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_downloader_browser_test.db')
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

GB = 1024 ** 3


def make_task(gid, name, status, total, done, speed=0, **extra):
    task = {
        'gid': gid, 'status': status, 'totalLength': str(total), 'completedLength': str(done),
        'downloadSpeed': str(speed), 'uploadSpeed': '0', 'connections': '8' if status == 'active' else '0',
        'dir': '/root/downloads', 'errorCode': '0',
        'files': [{'index': '1', 'path': '/root/downloads/' + name, 'length': str(total),
                   'completedLength': str(done), 'selected': 'true',
                   'uris': [{'uri': 'https://example.test/' + name, 'status': 'used'}]}],
    }
    task.update(extra)
    return task


class FakeAria2:
    """Minimal in-memory Aria2 implementing the methods the downloader uses."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.options = {'dir': '/root/downloads', 'max-overall-download-limit': '0',
                        'max-overall-upload-limit': '0'}
        self.tasks = [
            make_task('a1', 'Alpha.Show.S01E01.mkv', 'active', 4 * GB, GB, 20 * 1024 ** 2),
            make_task('a2', 'Beta.Movie.2024.mkv', 'active', 2 * GB, 1.5 * GB, 5 * 1024 ** 2),
            make_task('w1', '中文电影.mkv', 'paused', GB, 0),
            make_task('s1', 'Done.Episode.mkv', 'complete', GB, GB),
            make_task('e1', 'Broken.File.mkv', 'error', GB, 0, errorCode='3', errorMessage='Resource not found'),
        ]
        self.next_gid = 100

    def by_status(self, *statuses):
        return [copy.deepcopy(t) for t in self.tasks if t['status'] in statuses]

    def find(self, gid):
        return next(t for t in self.tasks if t['gid'] == gid)

    @staticmethod
    def strip_files(task):
        # The list endpoint must not request `files` (expensive on mirror-heavy tasks).
        task.pop('files', None)
        return task

    def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'aria2.getGlobalStat':
            active = self.by_status('active')
            return {'numActive': str(len(active)), 'numWaiting': str(len(self.by_status('waiting', 'paused'))),
                    'numStopped': str(len(self.by_status('complete', 'error', 'removed'))),
                    'downloadSpeed': str(sum(int(t['downloadSpeed']) for t in active)), 'uploadSpeed': '0'}
        if method == 'aria2.tellActive':
            return [self.strip_files(t) for t in self.by_status('active')]
        if method == 'aria2.tellWaiting':
            return [self.strip_files(t) for t in self.by_status('waiting', 'paused')]
        if method == 'aria2.tellStopped':
            return [self.strip_files(t) for t in self.by_status('complete', 'error', 'removed')]
        if method == 'aria2.tellStatus':
            return copy.deepcopy(self.find(params[0]))
        if method == 'aria2.getFiles':
            return copy.deepcopy(self.find(params[0])['files'])
        if method in ('aria2.forcePause', 'aria2.pause'):
            task = self.find(params[0]); task['status'] = 'paused'; task['downloadSpeed'] = '0'
            return params[0]
        if method == 'aria2.unpause':
            self.find(params[0])['status'] = 'active'
            return params[0]
        if method == 'aria2.forceRemove':
            self.find(params[0])['status'] = 'removed'
            return params[0]
        if method == 'aria2.removeDownloadResult':
            self.tasks = [t for t in self.tasks if t['gid'] != params[0]]
            return 'OK'
        if method == 'aria2.purgeDownloadResult':
            self.tasks = [t for t in self.tasks if t['status'] not in ('complete', 'error', 'removed')]
            return 'OK'
        if method in ('aria2.pauseAll', 'aria2.unpauseAll'):
            for t in self.tasks:
                if method == 'aria2.pauseAll' and t['status'] in ('active', 'waiting'):
                    t['status'] = 'paused'
                if method == 'aria2.unpauseAll' and t['status'] == 'paused':
                    t['status'] = 'active'
            return 'OK'
        if method == 'aria2.getOption':
            task = self.find(params[0])
            return dict({'dir': task['dir']}, **task.get('options', {}))
        if method == 'aria2.changeOption':
            self.find(params[0]).setdefault('options', {}).update(params[1])
            return 'OK'
        if method == 'aria2.getVersion':
            return {'version': '1.37.0', 'enabledFeatures': ['BitTorrent']}
        if method == 'aria2.changePosition':
            gid, pos, how = params
            queue = [t for t in self.tasks if t['status'] in ('waiting', 'paused')]
            task = self.find(gid)
            cur = queue.index(task)
            new = {'POS_SET': pos, 'POS_CUR': cur + pos, 'POS_END': len(queue) - 1 + pos}[how]
            new = max(0, min(len(queue) - 1, new))
            queue.remove(task)
            queue.insert(new, task)
            self.tasks = [t for t in self.tasks if t['status'] not in ('waiting', 'paused')] + queue
            return new
        if method == 'aria2.getGlobalOption':
            return dict(self.options)
        if method == 'aria2.changeGlobalOption':
            self.options.update(params[0])
            return 'OK'
        if method in ('aria2.addUri', 'aria2.addTorrent', 'aria2.addMetalink'):
            self.next_gid += 1
            gid = 'n%d' % self.next_gid
            name = params[0][0].rsplit('/', 1)[-1] if method == 'aria2.addUri' else 'from-torrent.mkv'
            self.tasks.append(make_task(gid, name, 'waiting', GB, 0))
            return gid
        raise AssertionError('unexpected RPC ' + method)

    def handle(self, payload):
        with self.lock:
            def one(cmd):
                if cmd['method'] == 'system.multicall':
                    return {'jsonrpc': '2.0', 'id': cmd.get('id'),
                            'result': [[self.call(c['methodName'], c.get('params', []))] for c in cmd['params'][0]]}
                return {'jsonrpc': '2.0', 'id': cmd.get('id'), 'result': self.call(cmd['method'], cmd.get('params', []))}
            return [one(c) for c in payload] if isinstance(payload, list) else one(payload)

    def methods(self):
        return [m for m, _ in self.calls]


@unittest.skipUnless(sync_playwright, 'requires playwright')
class DownloaderBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as scanner
        from database import db, User
        from werkzeug.serving import make_server

        if os.path.exists(TMP_DB):
            os.remove(TMP_DB)
        cls.scanner = scanner
        scanner.app.config['TESTING'] = True
        cls.ctx = scanner.app.app_context()
        cls.ctx.push()
        db.create_all()
        if not db.session.get(User, 'tester'):
            db.session.add(User(id='tester', password_hash='x'))
            db.session.commit()
        scanner.app.view_functions['aria2_jsonrpc_proxy'] = lambda: scanner.jsonify(cls.fake.handle(scanner.request.get_json()))
        # /api/aria2/task_list runs for real; only its HTTP call to Aria2 is answered by the fake.
        scanner.aria2_multicall = lambda calls: [cls.fake.handle({'method': m, 'params': list(p)})['result'] for m, p in calls]
        cls.server = make_server('127.0.0.1', 0, scanner.app, threaded=True)
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        cls.playwright = sync_playwright().start()
        options = {}
        if os.environ.get('SCANNER_TEST_CHROMIUM'):
            options['executable_path'] = os.environ['SCANNER_TEST_CHROMIUM']
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.ctx.pop()

    def setUp(self):
        type(self).fake = FakeAria2()
        self.scanner.downloader_file_cache.clear()
        fd, self.conf_path = tempfile.mkstemp(suffix='.conf')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('rpc-secret=keep\nmax-concurrent-downloads=3\n#user-agent=Old 1.0\nmax-overall-download-limit=0\n')
        self.addCleanup(os.remove, self.conf_path)
        self.scanner.ARIA2_CONFIG_PATH = self.conf_path

    def open(self, width=1280, prefs=None):
        mobile = width < 768
        context = self.browser.new_context(viewport={'width': width, 'height': 820}, is_mobile=mobile, has_touch=mobile)
        self.addCleanup(context.close)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], 'JavaScript errors on page'))
        # Log in by planting a Flask session cookie produced by the real app.
        client = self.scanner.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = 'tester'
            sess['_fresh'] = True
        cookie = client.get_cookie('session')
        context.add_cookies([{'name': 'session', 'value': cookie.value, 'url': self.base}])
        if prefs is not None:
            context.add_init_script('localStorage.setItem("scanner.downloaderPrefs", %s)' % json.dumps(json.dumps(prefs)))
        page.goto(self.base + '/')
        page.get_by_role('button', name='下载').first.click()
        page.locator('.sdl-item').first.wait_for()
        return page

    def items(self, page):
        return page.locator('.sdl-item')

    def test_lists_counts_search_and_sort(self):
        page = self.open()
        self.assertEqual(self.items(page).count(), 2)
        self.assertIn('2', page.locator('.sdl-list-btn').nth(0).inner_text())
        self.assertIn('1', page.locator('.sdl-list-btn').nth(1).inner_text())
        self.assertIn('2', page.locator('.sdl-list-btn').nth(2).inner_text())
        page.locator('.sdl-search input').fill('beta')
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 1")
        self.assertIn('Beta.Movie', self.items(page).first.inner_text())
        page.locator('.sdl-search input').fill('')
        page.locator('.sdl-sort').select_option('progress')
        page.wait_for_function("document.querySelector('.sdl-item .sdl-name').textContent.includes('Beta')")
        page.locator('.sdl-list-btn').nth(2).click()
        page.wait_for_function("[...document.querySelectorAll('.sdl-item')].some(e => e.textContent.includes('Resource not found'))")
        self.assertEqual(self.items(page).count(), 2)

    def test_pause_resume_and_delete_single_task(self):
        page = self.open()
        self.items(page).first.locator('[title="暂停"]').click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 1")
        self.assertIn(('aria2.forcePause', ['a1']), self.fake.calls)
        page.locator('.sdl-list-btn').nth(1).click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 2")
        page.locator('.sdl-item', has_text='Alpha.Show').locator('[title="开始"]').click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 1")
        self.assertIn(('aria2.unpause', ['a1']), self.fake.calls)
        page.locator('.sdl-item').first.locator('[title="删除"]').click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 0")
        self.assertFalse(page.locator('#confirmModal').is_visible())
        self.assertIn('aria2.forceRemove', self.fake.methods())
        self.assertIn('aria2.removeDownloadResult', self.fake.methods())

    def test_retry_failed_task_reuses_uri_and_options(self):
        page = self.open()
        page.locator('.sdl-list-btn').nth(2).click()
        page.locator('.sdl-item', has_text='Broken.File').locator('[title="重试"]').click()
        page.wait_for_function("![...document.querySelectorAll('.sdl-item')].some(e => e.textContent.includes('Broken.File'))")
        add = next(p for m, p in self.fake.calls if m == 'aria2.addUri')
        self.assertEqual(add, [['https://example.test/Broken.File.mkv'], {'dir': '/root/downloads'}])

    def test_list_payload_is_slim_but_retry_keeps_all_mirrors(self):
        mirrors = [{'uri': 'https://mirror%d.test/Big.mkv?sig=%s' % (i, 'x' * 250), 'status': 'used'} for i in range(64)]
        broken = self.fake.find('e1')
        broken['files'][0]['uris'] = mirrors
        page = self.open()
        payload = page.evaluate("axios.post('/api/aria2/task_list', {list: 'stopped'}).then(r => r.data)")
        row = next(t for t in payload['tasks'] if t['gid'] == 'e1')
        self.assertEqual(len(row['files'][0]['uris']), 1)
        self.assertEqual(row['fileCount'], 1)
        before = self.fake.methods().count('aria2.getFiles')
        page.evaluate("axios.post('/api/aria2/task_list', {list: 'stopped'})")
        page.wait_for_timeout(300)
        self.assertEqual(self.fake.methods().count('aria2.getFiles'), before, 'file summaries must be cached per GID')
        page.locator('.sdl-list-btn').nth(2).click()
        page.locator('.sdl-item', has_text='Broken.File').locator('[title="重试"]').click()
        page.wait_for_function("![...document.querySelectorAll('.sdl-item')].some(e => e.textContent.includes('Broken.File'))")
        add = next(p for m, p in self.fake.calls if m == 'aria2.addUri')
        self.assertEqual(len(add[0]), 64)

    def test_batch_select_and_pause(self):
        page = self.open()
        page.locator('.sdl-check input').check()
        self.assertIn('已选 2', page.locator('.sdl-check').inner_text())
        page.locator('.sdl-batch').get_by_role('button', name='暂停').click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 0")
        paused = sorted(p[0] for m, p in self.fake.calls if m == 'aria2.forcePause')
        self.assertEqual(paused, ['a1', 'a2'])

    def test_new_uri_task_with_advanced_options(self):
        page = self.open()
        page.locator('.sdl-new').click()
        modal = page.locator('.modal.show')
        modal.locator('textarea.sdl-uris').fill('https://example.test/one.mkv\nmagnet:?xt=urn:btih:abc')
        self.assertIn('共 2 个任务', modal.inner_text())
        modal.locator('summary').click()
        modal.locator('input[placeholder^="留空"]').first.fill('/root/downloads/g01')
        modal.locator('input[type=number]').nth(0).fill('8')
        modal.locator('input[type=number]').nth(1).fill('4')
        modal.locator('input[placeholder^="留空使用全局"]').fill('Custom-UA/2')
        modal.get_by_role('button', name='开始下载').click()
        page.wait_for_function("document.querySelectorAll('.modal.show').length === 0")
        adds = [p for m, p in self.fake.calls if m == 'aria2.addUri']
        self.assertEqual(len(adds), 2)
        self.assertEqual(adds[0][1], {'dir': '/root/downloads/g01', 'split': '8', 'max-connection-per-server': '4',
                                      'user-agent': 'Custom-UA/2'})
        self.assertEqual(adds[1][0], ['magnet:?xt=urn:btih:abc'])

    def test_new_task_dir_defaults_to_scanner_scan_path(self):
        from database import db, Config
        old = db.session.get(Config, 'scan_path')
        old_value = old.value if old else None
        db.session.merge(Config(key='scan_path', value='/root/downloads/scanner'))  # pyright: ignore[reportCallIssue]
        db.session.commit()

        def restore():
            row = db.session.get(Config, 'scan_path')
            if old_value is None:
                db.session.delete(row)
            else:
                row.value = old_value
            db.session.commit()
        self.addCleanup(restore)
        page = self.open()
        page.locator('.sdl-new').click()
        modal = page.locator('.modal.show')
        modal.locator('textarea.sdl-uris').fill('https://example.test/two.mkv')
        modal.locator('summary').click()
        self.assertEqual(modal.locator('input[placeholder^="留空"]').first.input_value(), '/root/downloads/scanner')
        modal.get_by_role('button', name='开始下载').click()
        page.wait_for_function("document.querySelectorAll('.modal.show').length === 0")
        add = next(p for m, p in self.fake.calls if m == 'aria2.addUri')
        self.assertEqual(add[1].get('dir'), '/root/downloads/scanner')

    def test_new_task_sends_referer_cookie_and_headers(self):
        page = self.open()
        page.locator('.sdl-new').click()
        modal = page.locator('.modal.show')
        modal.locator('textarea.sdl-uris').fill('https://example.test/protected.mkv')
        modal.locator('summary').click()
        modal.locator('input[placeholder^="例如 https"]').fill('https://example.test/page')
        modal.locator('input[placeholder^="例如 name"]').fill('sid=abc; t=1')
        headers = modal.locator('textarea.sdl-headers')
        headers.fill('not a header')
        modal.get_by_role('button', name='开始下载').click()
        page.wait_for_timeout(300)
        self.assertNotIn('aria2.addUri', self.fake.methods(), 'malformed header must block submit')
        headers.fill('Authorization: Bearer xyz\nX-Test: 1')
        modal.get_by_role('button', name='开始下载').click()
        page.wait_for_function("document.querySelectorAll('.modal.show').length === 0")
        opts = next(p for m, p in self.fake.calls if m == 'aria2.addUri')[1]
        self.assertEqual(opts['referer'], 'https://example.test/page')
        self.assertEqual(opts['header'], ['Authorization: Bearer xyz', 'X-Test: 1', 'Cookie: sid=abc; t=1'])

    def test_detail_copy_name_and_links_and_version(self):
        page = self.open()
        page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
        self.assertIn('Aria2 v1.37.0', page.locator('.sdl-foot').inner_text())
        page.locator('.sdl-item', has_text='Alpha.Show').locator('.sdl-name').click()
        modal = page.locator('.modal.show')
        modal.locator('.sdl-file [title="复制文件名"]').click()
        page.wait_for_function("navigator.clipboard.readText().then(t => t === 'Alpha.Show.S01E01.mkv')")
        modal.locator('.sdl-uri [title="复制链接"]').click()
        page.wait_for_function("navigator.clipboard.readText().then(t => t === 'https://example.test/Alpha.Show.S01E01.mkv')")
        modal.locator('.modal-header [title="复制名称"]').click()
        page.wait_for_function("navigator.clipboard.readText().then(t => t === 'Alpha.Show.S01E01.mkv')")

    def test_per_task_options_change_only_edited_values(self):
        page = self.open()
        page.locator('.sdl-item', has_text='Alpha.Show').locator('.sdl-name').click()
        modal = page.locator('.modal.show')
        modal.locator('.sdl-taskopt summary').click()
        page.wait_for_function("!document.querySelector('.modal.show .sdl-taskopt .btn-primary').disabled")
        inputs = modal.locator('.sdl-taskopt input')
        inputs.nth(0).fill('2')
        inputs.nth(2).fill('8')
        modal.locator('.sdl-taskopt').get_by_role('button', name='应用').click()
        page.wait_for_timeout(400)
        change = next(p for m, p in self.fake.calls if m == 'aria2.changeOption')
        self.assertEqual(change, ['a1', {'max-download-limit': '2048K', 'max-connection-per-server': '8'}])

    def test_waiting_queue_reorder(self):
        self.fake.tasks.append(make_task('w2', 'Second.Waiting.mkv', 'waiting', GB, 0))
        self.fake.tasks.append(make_task('w3', 'Third.Waiting.mkv', 'waiting', GB, 0))
        page = self.open()
        page.locator('.sdl-list-btn').nth(1).click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 3")
        page.locator('.sdl-item', has_text='Second.Waiting').locator('[title="置顶"]').click()
        page.wait_for_function("document.querySelector('.sdl-item .sdl-name').textContent.includes('Second.Waiting')")
        self.assertIn(['w2', 0, 'POS_SET'], [p for m, p in self.fake.calls if m == 'aria2.changePosition'])
        page.locator('.sdl-item', has_text='Third.Waiting').locator('.sdl-name').click()
        queue = page.locator('.modal.show .sdl-queue')
        for gone in ('上移', '下移', '置底'):
            self.assertEqual(queue.locator('[title="%s"]' % gone).count(), 0, gone)
        queue.locator('[title="置顶"]').click()
        page.wait_for_function("document.querySelector('.sdl-item .sdl-name').textContent.includes('Third.Waiting')")
        self.assertIn(['w3', 0, 'POS_SET'], [p for m, p in self.fake.calls if m == 'aria2.changePosition'])

    def test_waiting_queue_batch_top_keeps_relative_order(self):
        for gid in ('w2', 'w3', 'w4'):
            self.fake.tasks.append(make_task(gid, gid.upper() + '.Waiting.mkv', 'waiting', GB, 0))
        page = self.open()
        page.locator('.sdl-list-btn').nth(1).click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 4")
        order = "[...document.querySelectorAll('.sdl-item .sdl-name')].map(e => e.textContent.trim().slice(0, 2)).join(',')"
        # queue: 中文(w1), W2, W3, W4 -> select W2 and W4
        for name in ('W2.Waiting', 'W4.Waiting'):
            page.locator('.sdl-item', has_text=name).locator('.sdl-item-check').check()
        self.assertEqual(page.locator('.sdl-batch [title="批量上移"]').count(), 0)
        page.locator('.sdl-batch [title="批量置顶"]').click()
        page.wait_for_function(order + " === 'W2,W4,中文,W3'")
        self.assertEqual(page.locator('.sdl-item.selected').count(), 0, 'selection is cleared after 置顶')

    def test_stopped_list_retry_all_failed_then_removes_records(self):
        self.fake.tasks.append(make_task('e2', 'Broken.Two.mkv', 'error', GB, 0, errorCode='1'))
        bt = make_task('e3', 'Broken.Torrent', 'error', GB, 0, errorCode='1')
        bt['bittorrent'] = {'info': {'name': 'Broken.Torrent'}}
        self.fake.tasks.append(bt)
        page = self.open()
        page.locator('.sdl-list-btn').nth(2).click()
        quick = page.locator('.sdl-quick')
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 4")
        self.assertEqual(quick.get_by_role('button', name='全部开始').count(), 0)
        self.assertEqual(quick.get_by_role('button', name='全部暂停').count(), 0)
        quick.locator('[title="重试全部失败任务"]').click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 2")
        added = [p[0] for m, p in self.fake.calls if m == 'aria2.addUri']
        self.assertEqual(added, [['https://example.test/Broken.File.mkv'], ['https://example.test/Broken.Two.mkv']])
        removed = [p[0] for m, p in self.fake.calls if m == 'aria2.removeDownloadResult']
        self.assertEqual(sorted(removed), ['e1', 'e2'])
        names = page.locator('.sdl-item .sdl-name').all_inner_texts()
        self.assertTrue(any('Broken.Torrent' in n for n in names), 'BT failures are left alone')

    def test_density_defaults_to_comfortable_on_pc_and_compact_on_mobile(self):
        self.assertIn('sdl-comfortable', self.open(1280).locator('.sdl').get_attribute('class'))
        mobile = self.open(375)
        self.assertIn('sdl-compact', mobile.locator('.sdl').get_attribute('class'))
        # 旧版本默认存下的「舒适」会迁移成自动
        self.assertIn('sdl-compact', self.open(375, prefs={'density': 'comfortable'}).locator('.sdl').get_attribute('class'))
        # 用户明确保存过的「宽松」在手机上也保持
        explicit = self.open(375, prefs={'v': 2, 'density': 'comfortable'})
        self.assertIn('sdl-comfortable', explicit.locator('.sdl').get_attribute('class'))
        # compact rows are clearly shorter on phones
        rows = mobile.locator('.sdl-item').first.bounding_box()['height']
        loose = explicit.locator('.sdl-item').first.bounding_box()['height']
        self.assertLess(rows, loose * 0.8, 'compact rows should be at least 20%% shorter (%s vs %s)' % (rows, loose))

    def test_connections_and_split_allow_up_to_128(self):
        page = self.open()
        page.locator('.sdl-new').click()
        modal = page.locator('.modal.show')
        modal.locator('textarea.sdl-uris').fill('https://example.test/big.mkv')
        modal.locator('summary').click()
        modal.locator('input[type=number]').nth(0).fill('128')
        modal.locator('input[type=number]').nth(1).fill('128')
        modal.get_by_role('button', name='开始下载').click()
        page.wait_for_function("document.querySelectorAll('.modal.show').length === 0")
        opts = next(p for m, p in self.fake.calls if m == 'aria2.addUri')[1]
        self.assertEqual((opts['split'], opts['max-connection-per-server']), ('128', '128'))
        page.locator('.sdl-item', has_text='Alpha.Show').locator('.sdl-name').click()
        modal = page.locator('.modal.show')
        modal.locator('.sdl-taskopt summary').click()
        page.wait_for_function("!document.querySelector('.modal.show .sdl-taskopt .btn-primary').disabled")
        modal.locator('.sdl-taskopt input').nth(2).fill('100')
        modal.locator('.sdl-taskopt input').nth(3).fill('128')
        modal.locator('.sdl-taskopt').get_by_role('button', name='应用').click()
        page.wait_for_timeout(400)
        change = next(p for m, p in self.fake.calls if m == 'aria2.changeOption')
        self.assertEqual(change, ['a1', {'max-connection-per-server': '100', 'split': '128'}])

    def test_upload_queue_progress_fill_matches_desktop_on_mobile(self):
        from database import db, Task
        task = Task(filename='Uploading.Movie.mkv', filepath='/root/downloads/Uploading.Movie.mkv', status='uploading',  # pyright: ignore[reportCallIssue]
                    progress=40, upload_speed='12 MB/s', upload_eta='1m')
        db.session.add(task)
        db.session.commit()

        def drop():
            db.session.delete(task)
            db.session.commit()
        self.addCleanup(drop)

        def fill(width, selector):
            page = self.open(width)
            page.get_by_role('button', name='上传').first.click()
            bar = page.locator(selector).first
            bar.wait_for()
            host = bar.evaluate_handle("e => e.closest('.m-item') || e.closest('tr')")
            hb = host.as_element().bounding_box()
            b = bar.bounding_box()
            style = bar.evaluate("e => { const s = getComputedStyle(e); return {opacity: s.opacity, events: s.pointerEvents}; }")
            if width == 375 and os.environ.get('SCANNER_DOWNLOADER_SCREENSHOT'):
                page.screenshot(path=os.environ['SCANNER_DOWNLOADER_SCREENSHOT'].replace('.png', '-upload-mobile.png'))
            return b, hb, style

        b, hb, style = fill(375, '.m-item .row-progress')
        desktop_b, desktop_hb, desktop_style = fill(1280, 'tr.progress-row .row-progress')
        # same look as the desktop row: translucent fill across the full entry height, not a thin line
        self.assertGreaterEqual(b['height'], hb['height'] - 2)
        self.assertAlmostEqual(b['width'] / hb['width'], 0.4, delta=0.03)
        self.assertGreaterEqual(desktop_b['height'], desktop_hb['height'] - 2)
        self.assertEqual(style, desktop_style)
        self.assertEqual(style['events'], 'none')

    def test_new_torrent_task_uploads_base64(self):
        page = self.open()
        page.locator('.sdl-new').click()
        modal = page.locator('.modal.show')
        modal.get_by_role('button', name='种子文件').click()
        modal.locator('input[type=file]').set_input_files({'name': 'demo.torrent', 'mimeType': 'application/x-bittorrent', 'buffer': b'd4:infod4:name4:demoee'})
        modal.get_by_role('button', name='开始下载').click()
        page.wait_for_timeout(600)
        torrent = next(p for m, p in self.fake.calls if m == 'aria2.addTorrent')
        self.assertEqual(base64.b64decode(torrent[0]), b'd4:infod4:name4:demoee')

    def test_detail_modal_shows_files_and_connections(self):
        page = self.open()
        self.items(page).first.locator('.sdl-name').click()
        modal = page.locator('.modal.show')
        modal.wait_for()
        text = modal.inner_text()
        self.assertIn('Alpha.Show.S01E01.mkv', text)
        self.assertIn('/root/downloads', text)
        self.assertIn('连接数', text)
        self.assertIn('文件（1）', text)
        self.assertIn('https://example.test/Alpha.Show.S01E01.mkv', text)

    def test_download_settings_apply_and_persist(self):
        page = self.open()
        page.locator('.sdl-toolbar [title="更多"]').click()
        page.get_by_role('button', name='下载设置').click()
        modal = page.locator('.modal.show')
        inputs = modal.locator('input')
        inputs.nth(0).fill('7')
        inputs.nth(1).fill('10')
        inputs.nth(2).fill('0')
        modal.locator('textarea').fill('MyAgent/1.0')
        modal.get_by_role('button', name='保存').click()
        page.wait_for_function("document.querySelectorAll('.modal.show').length === 0")
        change = next(p for m, p in self.fake.calls if m == 'aria2.changeGlobalOption')
        self.assertEqual(change, [{'max-concurrent-downloads': '7', 'max-overall-download-limit': '10240K',
                                   'max-overall-upload-limit': '0', 'user-agent': 'MyAgent/1.0'}])
        with open(self.conf_path, encoding='utf-8') as f:
            conf = f.read()
        self.assertIn('max-concurrent-downloads=7\n', conf)
        self.assertIn('max-overall-download-limit=10240K\n', conf)
        self.assertIn('user-agent=MyAgent/1.0\n', conf)
        self.assertIn('#user-agent=Old 1.0\n', conf, 'commented lines must be left alone')
        self.assertIn('rpc-secret=keep\n', conf)

    def test_quick_action_buttons_are_visible(self):
        page = self.open()
        quick = page.locator('.sdl-quick')
        for label in ('全部开始', '全部暂停', '清理已完成', '清理全部已结束'):
            self.assertTrue(quick.get_by_role('button', name=label).is_visible(), label)
        quick.get_by_role('button', name='全部暂停').click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 0")
        self.assertIn('aria2.pauseAll', self.fake.methods())
        quick.get_by_role('button', name='全部开始').click()
        page.wait_for_function("document.querySelectorAll('.sdl-item').length === 3")
        self.assertIn('aria2.unpauseAll', self.fake.methods())

    def test_clean_completed_keeps_errors_and_clean_all_purges(self):
        page = self.open()
        page.locator('.sdl-quick').get_by_role('button', name='清理已完成').click()
        page.locator('#confirmModal .btn-danger').click()
        page.wait_for_function("document.querySelector('.sdl-list-btn:nth-child(3) .sdl-count').textContent.trim() === '1'")
        removed = [p[0] for m, p in self.fake.calls if m == 'aria2.removeDownloadResult']
        self.assertEqual(removed, ['s1'])
        self.assertNotIn('aria2.purgeDownloadResult', self.fake.methods())
        page.locator('.sdl-quick').get_by_role('button', name='清理全部已结束').click()
        page.locator('#confirmModal .btn-danger').click()
        page.wait_for_function("document.querySelector('.sdl-list-btn:nth-child(3) .sdl-count').textContent.trim() === '0'")
        self.assertIn('aria2.purgeDownloadResult', self.fake.methods())

    def test_prefs_apply_and_import_export(self):
        page = self.open(prefs={'defaultList': 'stopped', 'density': 'compact', 'sort': 'name', 'refreshInterval': 5})
        self.assertIn('active', page.locator('.sdl-list-btn').nth(2).get_attribute('class'))
        self.assertIn('sdl-compact', page.locator('.sdl').get_attribute('class'))
        page.locator('.sdl-toolbar [title="更多"]').click()
        page.get_by_role('button', name='偏好设置').click()
        modal = page.locator('.modal.show')
        modal.locator('textarea').fill('{"density":"comfortable","refreshInterval":999,"newTaskSplit":4}')
        modal.get_by_role('button', name='导入').click()
        page.wait_for_function("document.querySelector('.sdl').classList.contains('sdl-comfortable')")
        stored = json.loads(page.evaluate("localStorage.getItem('scanner.downloaderPrefs')"))
        self.assertEqual(stored['refreshInterval'], 60)
        self.assertEqual(stored['newTaskSplit'], 4)
        self.assertEqual(stored['defaultList'], 'stopped')

    def test_row_shows_total_size_not_downloaded(self):
        page = self.open()
        size = page.locator('.sdl-item', has_text='Alpha.Show').locator('.sdl-m-size')
        self.assertEqual(size.inner_text().strip(), '4.00 GB')  # 1 GB of 4 GB downloaded
        self.assertNotIn('/', size.inner_text())
        self.fake.find('a2')['totalLength'] = '0'  # e.g. a magnet before metadata arrives
        page.wait_for_function("[...document.querySelectorAll('.sdl-m-size')].some(e => e.textContent.trim() === '大小未知')")

    def test_progress_is_full_height_row_fill(self):
        for width in (1280, 375):
            with self.subTest(width=width):
                page = self.open(width)
                item = page.locator('.sdl-item', has_text='Alpha.Show')
                self.assertEqual(item.locator('.progress').count(), 0, 'no thin progress bar')
                fill = item.locator('.sdl-fill')
                fb, ib = fill.bounding_box(), item.bounding_box()
                self.assertGreaterEqual(fb['height'], ib['height'] - 3)
                self.assertAlmostEqual(fb['width'] / ib['width'], 0.25, delta=0.03)  # 1 GB of 4 GB
                look = fill.evaluate("e => { const s = getComputedStyle(e); return [s.opacity, s.pointerEvents]; }")
                self.assertEqual(look, ['0.14', 'none'])
                # the row stays clickable through the fill
                item.locator('.sdl-name').click()
                page.locator('.modal.show').wait_for()

    def test_standalone_button_and_page(self):
        page = self.open()
        link = page.locator('.sdl-foot a.sdl-open')
        self.assertEqual(link.get_attribute('href'), '/downloader')
        self.assertEqual(link.get_attribute('target'), '_blank')
        with page.context.expect_page() as popup:
            link.click()
        standalone = popup.value
        standalone.locator('.sdl-item').first.wait_for()
        self.assertEqual(standalone.locator('.sdl-open').count(), 0, 'no self-link on the standalone page')
        self.assertEqual(standalone.locator('header a[href*="github.com/jiemo9527/Video-ASR-Ad-Cleaner"]').count(), 1)
        standalone.locator('.sdl-item', has_text='Alpha.Show').locator('[title="暂停"]').click()
        standalone.wait_for_function("document.querySelector('.toast.show') !== null")
        self.assertIn('aria2.forcePause', self.fake.methods())

    def test_mobile_layout_fits_and_is_usable(self):
        for width in (320, 375, 430):
            with self.subTest(width=width):
                page = self.open(width)
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                lists = page.locator('.sdl-lists')
                self.assertFalse(lists.evaluate('e => e.scrollWidth > e.clientWidth + 1'), 'list tabs must not be cut off')
                for sel in ('.sdl-new', '.sdl-search input', '.sdl-item [title="暂停"]'):
                    box = page.locator(sel).first.bounding_box()
                    self.assertGreaterEqual(box['x'], 0, sel)
                    self.assertLessEqual(box['x'] + box['width'], width, sel)
                    self.assertGreaterEqual(box['height'], 30, sel)
                self.assertEqual(page.locator('iframe#ariaNgFrame').count(), 0)
                selbar = page.locator('.sdl-selbar').bounding_box()
                self.assertLessEqual(selbar['height'], 40, 'quick actions must share one compact row with 全选')
                quick = page.locator('.sdl-quick').bounding_box()
                self.assertLessEqual(quick['x'] + quick['width'], width, 'quick actions must stay on screen')
                if width == 375 and os.environ.get('SCANNER_DOWNLOADER_SCREENSHOT'):
                    page.screenshot(path=os.environ['SCANNER_DOWNLOADER_SCREENSHOT'].replace('.png', '-mobile.png'))
        if os.environ.get('SCANNER_DOWNLOADER_SCREENSHOT'):
            self.open(1280).screenshot(path=os.environ['SCANNER_DOWNLOADER_SCREENSHOT'])


if __name__ == '__main__':
    unittest.main()
