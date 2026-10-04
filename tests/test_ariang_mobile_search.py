"""Optional browser regression check against the official AriaNg AllInOne package.

Set SCANNER_ARIANG_TEST_INDEX to its extracted index.html and install playwright
in the test environment. SCANNER_TEST_CHROMIUM may select a local browser.
No live Aria2 connection is used; the task list below is synthetic test data.
"""
import importlib
import os
from pathlib import Path
import unittest

try:
    sync_playwright = importlib.import_module('playwright.sync_api').sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
INDEX = os.environ.get('SCANNER_ARIANG_TEST_INDEX')


@unittest.skipUnless(sync_playwright and INDEX, 'requires playwright and SCANNER_ARIANG_TEST_INDEX')
class AriaNgMobileSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert sync_playwright is not None and INDEX is not None
        cls.playwright = sync_playwright().start()
        options = {}
        if os.environ.get('SCANNER_TEST_CHROMIUM'):
            options['executable_path'] = os.environ['SCANNER_TEST_CHROMIUM']
        cls.browser = cls.playwright.chromium.launch(**options)
        cls.upstream = Path(INDEX).read_text(encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def open_page(self, width, theme=True):
        page = self.browser.new_page(viewport={'width': width, 'height': 812},
                                     is_mobile=width < 768, has_touch=width < 768)
        self.addCleanup(page.close)

        def serve(route):
            if route.request.resource_type == 'document':
                page_html = self.upstream
                if theme:
                    page_html = page_html.replace('</head>', '<link rel="stylesheet" href="/static/ariang-scanner.css"><script defer src="/static/ariang-scanner-quiet-dialogs.js"></script></head>')
                route.fulfill(content_type='text/html', body=page_html)
            elif route.request.url.endswith('/static/ariang-scanner.css'):
                route.fulfill(content_type='text/css', body=(ROOT / 'static/ariang-scanner.css').read_text(encoding='utf-8'))
            elif route.request.url.endswith('/static/ariang-scanner-quiet-dialogs.js'):
                route.fulfill(content_type='application/javascript', body=(ROOT / 'static/ariang-scanner-quiet-dialogs.js').read_text(encoding='utf-8'))
            else:
                route.abort()

        page.route('http://ariang.test/**', serve)
        page.goto('http://ariang.test/#!/downloading')
        page.locator('#task-table').wait_for(state='attached')
        return page

    def test_mobile_logo_row_is_hidden_without_leaving_a_gap(self):
        for width in (320, 375, 430, 767):
            with self.subTest(width=width):
                page = self.open_page(width)
                self.assertFalse(page.locator('.main-header > .logo').is_visible())
                self.assertEqual(page.locator('.main-header .navbar').bounding_box()['y'], 0)
                header = page.locator('.main-header').bounding_box()
                self.assertLessEqual(header['height'], 118)
                self.assertAlmostEqual(page.locator('#content-body').bounding_box()['y'],
                                       header['height'], delta=1)

    def test_mobile_search_is_visible_wide_and_filters_tasks(self):
        for width in (320, 375, 430, 767):
            with self.subTest(width=width):
                page = self.open_page(width)
                search = page.locator('#search-box')
                self.assertTrue(search.is_visible(), 'mobile search must not be hidden-xs')
                box = search.bounding_box()
                self.assertGreaterEqual(box['height'], 44)
                self.assertGreaterEqual(box['width'], width - 32)
                self.assertGreaterEqual(box['x'], 0)
                self.assertLessEqual(box['x'] + box['width'], width)
                self.assertEqual(search.evaluate('(e) => getComputedStyle(e).fontSize'), '16px')
                header = page.locator('.main-header').bounding_box()
                content = page.locator('[ng-view]').bounding_box()
                self.assertGreaterEqual(content['y'], header['y'] + header['height'])
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                page.evaluate("""() => {
                    const scope = angular.element(document.querySelector('[ng-view]')).scope();
                    scope.$apply(() => {
                        scope.taskContext.list = ['Alpha.mkv', 'Beta.mkv'].map((name, i) => ({
                            gid: String(i), taskName: name, status: 'active', totalLength: 100,
                            completedLength: 20, downloadSpeed: 0, uploadSpeed: 0, files: []
                        }));
                    });
                }""")
                rows = page.locator('#task-table .task-table-body > [data-gid]')
                self.assertEqual(rows.count(), 2)
                search.fill('Alpha')
                page.wait_for_function("document.querySelectorAll('#task-table [data-gid]').length === 1")
                self.assertIn('Alpha.mkv', rows.inner_text())
                search.fill('')
                page.wait_for_function("document.querySelectorAll('#task-table [data-gid]').length === 2")
                if width == 375 and os.environ.get('SCANNER_ARIANG_TEST_SCREENSHOT'):
                    page.screenshot(path=os.environ['SCANNER_ARIANG_TEST_SCREENSHOT'])

    def test_mobile_menu_stays_in_viewport_and_opens(self):
        for width in (320, 375, 430, 767):
            with self.subTest(width=width):
                page = self.open_page(width)
                toggle = page.locator('[data-toggle="push-menu"]')
                box = toggle.bounding_box()
                self.assertLessEqual(box['y'] + box['height'], 812,
                                     'menu toggle must remain inside the viewport')
                toggle.click()
                page.wait_for_function("document.body.classList.contains('sidebar-open')")
                page.locator('.sidebar-menu a[href="#!/waiting"]').click()
                page.wait_for_url('**/#!/waiting')
                self.assertTrue(page.locator('[data-toggle="push-menu"]').is_visible())

    def test_mobile_chinese_composition_filters_visible_tasks(self):
        page = self.open_page(375)
        page.evaluate("""() => {
            const scope = angular.element(document.querySelector('[ng-view]')).scope();
            scope.$apply(() => {
                scope.taskContext.list = ['中文电影.mkv', '其他剧集.mkv'].map((name, i) => ({
                    gid: String(i), taskName: name, status: 'active', totalLength: 100,
                    completedLength: 20, downloadSpeed: 0, uploadSpeed: 0, files: []
                }));
            });
        }""")
        search = page.locator('#search-box')
        search.click()
        search.evaluate("""e => {
            e.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
            e.value = '中文';
            e.dispatchEvent(new InputEvent('input', {bubbles: true, isComposing: true}));
            e.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true, data: '中文'}));
        }""")
        page.wait_for_function("document.querySelectorAll('#task-table [data-gid]').length === 1")
        rows = page.locator('#task-table [data-gid]:visible')
        self.assertEqual(rows.count(), 1)
        self.assertIn('中文电影', rows.inner_text())

    def test_desktop_search_geometry_is_unchanged(self):
        for width in (768, 1280):
            with self.subTest(width=width):
                original = self.open_page(width, theme=False)
                themed = self.open_page(width)
                self.assertTrue(themed.locator('#search-box').is_visible())
                for key in ('x', 'y', 'width', 'height'):
                    self.assertAlmostEqual(original.locator('#search-box').bounding_box()[key],
                                           themed.locator('#search-box').bounding_box()[key], delta=1)


if __name__ == '__main__':
    unittest.main()
