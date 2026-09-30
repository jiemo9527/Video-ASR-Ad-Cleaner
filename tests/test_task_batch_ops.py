"""批量直传与“运行中先停止再删除”。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_batch_ops_test.db')

# The scratch DB MUST be selected before importing app; see test_prioritize_queue.py.
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from database import db, Task  # noqa: E402


def is_scratch_database(path):
    if not path:
        return False
    resolved = os.path.normcase(os.path.realpath(path))
    temp_dir = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
    name = os.path.basename(resolved)
    return (os.path.dirname(resolved) == temp_dir
            and name.startswith('scanner_') and name.endswith('_test.db'))


class FakeCore:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


_ctx = None


def setUpModule():
    global _ctx
    if os.path.exists(TMP_DB):
        os.remove(TMP_DB)
    scanner.app.config['TESTING'] = True
    scanner.app.config['LOGIN_DISABLED'] = True
    _ctx = scanner.app.app_context()
    _ctx.push()
    if not is_scratch_database(db.engine.url.database):
        _ctx.pop()
        raise RuntimeError('Refusing to run destructive tests against %s' % db.engine.url)
    db.create_all()


def tearDownModule():
    db.session.remove()
    _ctx.pop()


class BatchOpsTestBase(unittest.TestCase):
    def setUp(self):
        Task.query.delete()
        db.session.commit()
        while not scanner.detect_queue.empty():
            scanner.detect_queue.get()
        self.tmp = tempfile.TemporaryDirectory()
        self.client = scanner.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def add_task(self, name, status, overrides=None):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'wb') as f:
            f.write(b'x')
        task = Task(filename=name, filepath=path, status=status, log='',
                    overrides=json.dumps(overrides) if overrides else None)
        db.session.add(task)
        db.session.commit()
        return task.id

    def task(self, task_id):
        db.session.expire_all()
        return Task.query.get(task_id)


class BatchDirectUploadTests(BatchOpsTestBase):
    def test_marks_selected_detect_tasks_for_direct_upload(self):
        queued = self.add_task('queued.mkv', 'pending')
        scanner.detect_queue.put(99999)
        scanner.detect_queue.put(queued)
        dirty = self.add_task('dirty.mkv', 'dirty')
        busy = self.add_task('busy.mkv', 'processing')
        uploading = self.add_task('up.mkv', 'pending_upload', {'direct_upload': True})

        res = self.client.post('/api/tasks/batch', json={'type': 'detect', 'action': 'direct_upload',
                                                         'ids': [queued, dirty, busy, uploading]})

        self.assertEqual(res.status_code, 200)
        self.assertIn('操作了 2 个任务', res.get_json()['msg'])
        self.assertIn('跳过 2 个', res.get_json()['msg'])
        for task_id in (queued, dirty):
            task = self.task(task_id)
            self.assertEqual(task.status, 'pending')
            self.assertTrue(json.loads(task.overrides)['direct_upload'])
        self.assertEqual(self.task(busy).status, 'processing')
        # 已排队的被提到队首，重新激活的也在队首，且不会重复入队
        queue = scanner.detect_queue.snapshot()
        self.assertEqual(sorted(queue[:2]), sorted([queued, dirty]))
        self.assertEqual(queue.count(queued), 1)

    def test_requires_selection(self):
        self.add_task('a.mkv', 'dirty')
        res = self.client.post('/api/tasks/batch', json={'type': 'detect', 'action': 'direct_upload'})
        self.assertEqual(res.status_code, 400)


class StopThenDeleteTests(BatchOpsTestBase):
    def start_fake_run(self, task_id):
        core = FakeCore()
        with scanner.task_state_lock:
            scanner.running_tasks[task_id] = core
            scanner.active_detect_tasks.add(task_id)
        return core

    def finish_fake_run(self, task_id, delay):
        def worker():
            time.sleep(delay)
            with scanner.app.app_context():
                with scanner.task_state_lock:
                    scanner.running_tasks.pop(task_id, None)
                    scanner.active_detect_tasks.discard(task_id)
                scanner.finish_pending_delete(task_id)
        thread = threading.Thread(target=worker)
        thread.start()
        return thread

    def tearDown(self):
        with scanner.task_state_lock:
            scanner.running_tasks.clear()
            scanner.active_detect_tasks.clear()
            scanner.pending_delete_ids.clear()
        super().tearDown()

    def test_running_task_is_stopped_then_deleted_in_same_request(self):
        task_id = self.add_task('run.mkv', 'processing')
        path = self.task(task_id).filepath
        core = self.start_fake_run(task_id)
        thread = self.finish_fake_run(task_id, 0.3)

        res = self.client.post(f'/api/task/{task_id}/delete')
        thread.join()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['msg'], '任务已停止并删除')
        self.assertTrue(core.stopped)
        self.assertIsNone(self.task(task_id))
        self.assertFalse(os.path.exists(path))

    def test_slow_stopping_task_is_deleted_when_worker_exits(self):
        idle = self.add_task('idle.mkv', 'error')
        slow = self.add_task('slow.mkv', 'uploading')
        self.start_fake_run(slow)
        original_stop_and_delete = scanner.stop_and_delete_tasks
        scanner.stop_and_delete_tasks = lambda tasks: original_stop_and_delete(tasks, wait_seconds=0)
        try:
            res = self.client.post('/api/tasks/batch_delete', json={'ids': [idle, slow]})
        finally:
            scanner.stop_and_delete_tasks = original_stop_and_delete

        msg = res.get_json()['msg']
        self.assertIn('已删除 1 个任务', msg)
        self.assertIn('1 个运行中任务已停止，结束后自动删除', msg)
        self.assertIsNone(self.task(idle))
        self.assertEqual(self.task(slow).status, 'cancelled')

        self.finish_fake_run(slow, 0).join()
        self.assertIsNone(self.task(slow))


if __name__ == '__main__':
    unittest.main()
