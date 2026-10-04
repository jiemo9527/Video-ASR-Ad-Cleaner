"""内置下载器页面：首页「下载」标签直接渲染新下载器，另有独立页面；经典 AriaNg 已移除。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_downloader_ui_test.db')
# Bind the scratch DB before importing app; db.init_app() binds at import time.
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from database import db, User  # noqa: E402


class DownloaderPageTests(unittest.TestCase):
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

    def test_index_renders_builtin_downloader_only(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('<scanner-downloader', page)
        self.assertIn('standalone-url="/downloader"', page)
        self.assertNotIn('ariaNgFrame', page)
        self.assertNotIn('AriaNg.Options', page)

    def test_header_logo_links_to_github_project(self):
        self.assertEqual(scanner.PROJECT_URL, 'https://github.com/jiemo9527/Video-ASR-Ad-Cleaner')
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('href="%s"' % scanner.PROJECT_URL, page)

    def test_standalone_downloader_page(self):
        res = self.client.get('/downloader')
        self.assertEqual(res.status_code, 200)
        page = res.get_data(as_text=True)
        self.assertIn('<scanner-downloader', page)
        self.assertIn('sdl-standalone', page)
        self.assertIn('href="%s"' % scanner.PROJECT_URL, page)

    def test_standalone_page_requires_login(self):
        # TESTING / LOGIN_DISABLED make Flask-Login skip the check; test with production settings
        for key, value in (('LOGIN_DISABLED', False), ('TESTING', False)):
            self.addCleanup(scanner.app.config.__setitem__, key, scanner.app.config.get(key))
            scanner.app.config[key] = value
        # the class-level app context is reused by requests, so drop Flask-Login's cached user
        from flask import g
        g.pop('_login_user', None)
        res = scanner.app.test_client().get('/downloader')
        g.pop('_login_user', None)
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res.headers['Location'])

    def test_legacy_ariang_routes_are_gone(self):
        res = self.client.get('/aria2/')
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers['Location'].endswith('/downloader'))
        self.assertEqual(self.client.get('/ariang/').status_code, 404)

    def test_downloader_ui_setting_removed(self):
        self.assertNotIn('downloader_ui', self.client.get('/api/settings').get_json())
        settings_page = self.client.get('/settings_page').get_data(as_text=True)
        self.assertNotIn('下载器界面', settings_page)
        self.assertNotIn("id: 'ui'", settings_page)

    def test_upload_auto_switch_defaults_on_but_preserves_saved_off(self):
        from database import Config
        key = 'upload_remote_auto_switch'
        row = db.session.get(Config, key)
        previous = row.value if row else None

        def restore():
            current = db.session.get(Config, key)
            if previous is None:
                if current:
                    db.session.delete(current)
            else:
                current.value = previous
            db.session.commit()
        self.addCleanup(restore)
        if row:
            db.session.delete(row)
            db.session.commit()
        self.assertTrue(scanner.get_final_config(None)[key])
        db.session.add(Config(key=key, value='false'))
        db.session.commit()
        self.assertFalse(scanner.get_final_config(None)[key])

    def test_startup_removes_legacy_downloader_ui_row(self):
        from database import Config
        db.session.merge(Config(key='downloader_ui', value='classic'))  # pyright: ignore[reportCallIssue]
        db.session.merge(Config(key='scan_path', value='/root/downloads'))  # pyright: ignore[reportCallIssue]
        db.session.commit()
        self.assertEqual(scanner.remove_legacy_config(), 1)
        self.assertIsNone(db.session.get(Config, 'downloader_ui'))
        self.assertIsNotNone(db.session.get(Config, 'scan_path'), 'other settings are kept')
        self.assertEqual(scanner.remove_legacy_config(), 0)


class Aria2ConfigPathTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(scanner.resolve_aria2_config_path({}), '/root/.aria2c/aria2.conf')

    def test_installer_config_dir_from_scanner_env(self):
        # install.sh writes SCANNER_ARIA2_CONFIG_DIR when /root/.aria2c belongs to another Aria2
        env = {'SCANNER_ARIA2_CONFIG_DIR': '/opt/scanner-aria2'}
        self.assertEqual(scanner.resolve_aria2_config_path(env).replace('\\', '/'), '/opt/scanner-aria2/aria2.conf')

    def test_explicit_path_wins(self):
        env = {'SCANNER_ARIA2_CONFIG_PATH': '/etc/aria2/custom.conf', 'SCANNER_ARIA2_CONFIG_DIR': '/opt/x'}
        self.assertEqual(scanner.resolve_aria2_config_path(env), '/etc/aria2/custom.conf')

    def test_blank_values_fall_back(self):
        env = {'SCANNER_ARIA2_CONFIG_PATH': ' ', 'SCANNER_ARIA2_CONFIG_DIR': ''}
        self.assertEqual(scanner.resolve_aria2_config_path(env), '/root/.aria2c/aria2.conf')


class PinnedCdnTests(unittest.TestCase):
    def test_vue_and_axios_versions_are_pinned(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')
        for name in ('index.html', 'settings.html', 'downloader.html'):
            with open(os.path.join(root, name), encoding='utf-8') as f:
                html = f.read()
            self.assertNotIn('unpkg.com/vue@3/', html, name)
            self.assertNotIn('unpkg.com/axios/', html, name)
            self.assertIn('unpkg.com/vue@3.5.43/', html, name)
            self.assertIn('unpkg.com/axios@1.20.0/', html, name)


if __name__ == '__main__':
    unittest.main()
