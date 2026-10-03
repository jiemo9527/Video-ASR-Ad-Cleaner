"""Read-only Scanner status probe and Aria2 paused-download handling."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_status_test.db')
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from database import db  # noqa: E402


class StatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scanner.app.config['TESTING'] = True
        cls.ctx = scanner.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = scanner.app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def status(self, active, waiting, statuses=None, fail_waiting=False):
        statuses = statuses or []

        def rpc(method, params):
            if method == 'aria2.getGlobalStat':
                return {'numActive': str(active), 'numWaiting': str(waiting)}
            self.assertEqual(method, 'aria2.tellWaiting')
            if fail_waiting:
                raise RuntimeError('RPC unavailable')
            offset, length, fields = params
            self.assertEqual(fields, ['status'])
            return [{'status': status} for status in statuses[offset:offset + length]]

        empty_query = mock.Mock()
        empty_query.filter.return_value.count.return_value = 0
        with mock.patch.object(scanner, 'get_scanner_api_token', return_value='test-token'), \
             mock.patch.object(scanner, 'call_aria2_rpc', side_effect=rpc), \
             mock.patch.object(scanner, 'get_active_task_ids', return_value=set()), \
             mock.patch.object(scanner.Task, 'query', empty_query):
            response = self.client.get('/api/status', headers={'X-API-Token': 'test-token'})
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_only_paused_waiting_downloads_are_idle_across_pages(self):
        result = self.status(0, 248, ['paused'] * 248)
        self.assertTrue(result['idle'])
        self.assertEqual(result['aria2_waiting'], 248)
        self.assertEqual(result['aria2_paused'], 248)
        self.assertEqual(result['aria2_waiting_runnable'], 0)

    def test_runnable_waiting_download_remains_busy(self):
        result = self.status(0, 2, ['paused', 'waiting'])
        self.assertTrue(result['busy'])
        self.assertEqual(result['aria2_waiting_runnable'], 1)

    def test_active_download_remains_busy(self):
        self.assertTrue(self.status(1, 0)['busy'])

    def test_waiting_lookup_failure_is_busy(self):
        result = self.status(0, 2, fail_waiting=True)
        self.assertTrue(result['busy'])
        self.assertFalse(result['aria2_ok'])

    def test_incomplete_waiting_list_is_busy(self):
        result = self.status(0, 2, ['paused'])
        self.assertTrue(result['busy'])
        self.assertFalse(result['aria2_ok'])


if __name__ == '__main__':
    unittest.main()
