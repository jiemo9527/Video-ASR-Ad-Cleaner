"""插队功能验证：FrontQueue.move_to_front 顺序与 /api/tasks/prioritize 行为。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_prioritize_test.db')

import app as scanner  # noqa: E402
from database import db, Task  # noqa: E402


class FrontQueueOrderTests(unittest.TestCase):
    def test_move_to_front_keeps_selection_order_and_counts(self):
        q = scanner.FrontQueue()
        for i in [1, 2, 3, 4, 5]:
            q.put(i)

        self.assertTrue(q.move_to_front(4))
        self.assertTrue(q.move_to_front(2))
        self.assertEqual(q.snapshot(), [2, 4, 1, 3, 5])
        self.assertFalse(q.move_to_front(99))
        self.assertEqual(q.unfinished_tasks, 5)


class PrioritizeEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TMP_DB):
            os.remove(TMP_DB)
        scanner.app.config['TESTING'] = True
        scanner.app.config['LOGIN_DISABLED'] = True
        scanner.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')
        cls.ctx = scanner.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        Task.query.delete()
        db.session.commit()
        self.ids = []
        for name in ['a.mkv', 'b.mkv', 'c.mkv', 'd.mkv']:
            t = Task(filename=name, filepath='/tmp/' + name, status='pending', log='')
            db.session.add(t)
            db.session.flush()
            self.ids.append(t.id)
        busy = Task(filename='busy.mkv', filepath='/tmp/busy.mkv', status='processing', log='')
        db.session.add(busy)
        db.session.flush()
        self.busy_id = busy.id
        db.session.commit()

        while not scanner.detect_queue.empty():
            scanner.detect_queue.get()
        for task_id in self.ids:
            scanner.detect_queue.put(task_id)
        self.client = scanner.app.test_client()

    def test_multi_select_prioritize_preserves_requested_order(self):
        a, b, c, d = self.ids
        res = self.client.post('/api/tasks/prioritize', json={'ids': [d, c]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(scanner.detect_queue.snapshot(), [d, c, a, b])

    def test_single_prioritize_moves_task_to_head(self):
        a, b, c, d = self.ids
        res = self.client.post('/api/tasks/prioritize', json={'ids': [c]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(scanner.detect_queue.snapshot(), [c, a, b, d])

    def test_prioritize_does_not_change_status_and_logs_action(self):
        target = self.ids[2]
        self.client.post('/api/tasks/prioritize', json={'ids': [target]})
        task = db.session.get(Task, target)
        self.assertEqual(task.status, 'pending')
        self.assertEqual(task.retry_count or 0, 0)
        self.assertIn('插队', task.log or '')

    def test_non_pending_task_is_rejected_and_queue_unchanged(self):
        before = scanner.detect_queue.snapshot()
        res = self.client.post('/api/tasks/prioritize', json={'ids': [self.busy_id]})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(scanner.detect_queue.snapshot(), before)

    def test_empty_selection_is_rejected(self):
        res = self.client.post('/api/tasks/prioritize', json={'ids': []})
        self.assertEqual(res.status_code, 400)

    def test_pending_task_missing_from_memory_queue_is_requeued_at_front(self):
        a, b, c, d = self.ids
        self.assertTrue(scanner.detect_queue.move_to_front(d))
        scanner.detect_queue.get()  # 模拟 d 已被取走但仍是 pending
        self.assertEqual(scanner.detect_queue.snapshot(), [a, b, c])
        res = self.client.post('/api/tasks/prioritize', json={'ids': [d]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(scanner.detect_queue.snapshot(), [d, a, b, c])

    def test_mixed_selection_reports_skipped_tasks(self):
        res = self.client.post('/api/tasks/prioritize', json={'ids': [self.ids[3], self.busy_id]})
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload['count'], 1)
        self.assertIn('跳过', payload['msg'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
