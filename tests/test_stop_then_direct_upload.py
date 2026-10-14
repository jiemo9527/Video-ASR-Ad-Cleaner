"""检测中手动停止后立刻直传/重试：旧检测线程收尾时不能把新请求覆盖成「已停止」。"""
import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DB = os.path.join(tempfile.gettempdir(), 'scanner_stop_direct_upload_test.db')
os.environ['SCANNER_DATABASE_URI'] = 'sqlite:///' + TMP_DB.replace('\\', '/')

import app as scanner  # noqa: E402
from core_logic import ScannerCore  # noqa: E402
from database import db, Task, User  # noqa: E402

SENTINEL = object()


class _StopWorker(BaseException):
    """结束测试用工作线程（worker 只捕获 Exception）。"""


class StoppableFrontQueue(scanner.FrontQueue):
    def get(self, *args, **kwargs):
        item = super().get(*args, **kwargs)
        if item is SENTINEL:
            raise _StopWorker()
        return item


class StoppableQueue(queue.Queue):
    def get(self, *args, **kwargs):
        item = super().get(*args, **kwargs)
        if item is SENTINEL:
            raise _StopWorker()
        return item


class StopThenRequeueTests(unittest.TestCase):
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
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        Task.query.delete()
        db.session.commit()
        self.detect_q = StoppableFrontQueue()
        self.upload_q = StoppableQueue()
        for name, value in (('detect_queue', self.detect_q), ('upload_queue', self.upload_q)):
            patcher = mock.patch.object(scanner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # 模拟音频检测中：收到停止后仍要等在途的识别请求结束才返回 cancelled
        self.in_audio = threading.Event()
        self.release = threading.Event()
        original = ScannerCore.process_file
        test = self

        def fake_process_file(core, file_path, config, *args, **kwargs):
            if config.get('direct_upload'):
                return original(core, file_path, config, *args, **kwargs)
            test.in_audio.set()
            test.release.wait(10)
            return {'status': 'cancelled'} if core._stopped else {'status': 'ready_to_upload'}

        patcher = mock.patch.object(ScannerCore, 'process_file', fake_process_file)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 两个检测线程：重新入队的任务可能被另一个线程取到，而旧线程还占着任务
        self.workers = [threading.Thread(target=self.run_worker, daemon=True) for _ in range(2)]
        for w in self.workers:
            w.start()
        self.addCleanup(self.stop_workers)

    def run_worker(self):
        try:
            scanner.detection_worker()
        except _StopWorker:
            pass

    def stop_workers(self):
        self.release.set()
        for _ in self.workers:
            self.detect_q.put(SENTINEL)
        for w in self.workers:
            w.join(5)

    def make_task(self):
        media = os.path.join(self.tmp.name, 'movie.mkv')
        with open(media, 'wb') as f:
            f.write(b'x' * 16)
        task = Task(filename='movie.mkv', filepath=media, status='pending', log='', overrides='{}')
        db.session.add(task)
        db.session.commit()
        self.detect_q.put(task.id)
        self.assertTrue(self.in_audio.wait(5), '任务没有进入检测')
        return task.id

    def wait_status(self, tid, statuses, timeout=5):
        deadline = time.time() + timeout
        task = None
        while time.time() < deadline:
            db.session.expire_all()
            task = db.session.get(Task, tid)
            if task.status in statuses and not (scanner.get_active_task_ids() & {tid}):
                return task
            time.sleep(0.05)
        self.fail(f'status={task.status if task else None}, log={task.log if task else ""}')

    def drain_upload_queue(self):
        items = []
        while True:
            try:
                items.append(self.upload_q.get_nowait())
            except queue.Empty:
                return items

    def test_direct_upload_after_stop_goes_to_upload_queue(self):
        tid = self.make_task()
        self.assertEqual(self.client.post(f'/api/cancel/{tid}').status_code, 200)
        self.assertEqual(self.client.post(f'/api/task/{tid}/direct_upload').status_code, 200)
        time.sleep(0.3)  # 另一个检测线程先取到直传请求，此时旧线程仍占着任务
        self.release.set()

        task = self.wait_status(tid, ('pending_upload',))
        self.assertTrue(json.loads(task.overrides).get('direct_upload'))
        self.assertIn('⏩ 直传模式', task.log)
        self.assertEqual(self.drain_upload_queue(), [tid])

    def test_stop_without_new_request_stays_cancelled(self):
        tid = self.make_task()
        self.client.post(f'/api/cancel/{tid}')
        self.release.set()

        task = self.wait_status(tid, ('cancelled',))
        self.assertIn('⏹ 任务已手动停止', task.log)
        time.sleep(0.3)
        db.session.expire_all()
        self.assertEqual(db.session.get(Task, tid).status, 'cancelled')
        self.assertEqual(self.drain_upload_queue(), [])

    def test_retry_detect_after_stop_runs_detection_again(self):
        tid = self.make_task()
        self.client.post(f'/api/cancel/{tid}')
        self.in_audio.clear()
        self.client.post(f'/api/retry/{tid}', json={'type': 'detect'})
        time.sleep(0.3)
        self.release.set()

        # 重试后重新检测（这次没有被停止，检测通过进入上传）
        task = self.wait_status(tid, ('pending_upload',))
        self.assertTrue(self.in_audio.is_set())
        self.assertEqual(self.drain_upload_queue(), [tid])
        self.assertEqual(task.status, 'pending_upload')


if __name__ == '__main__':
    unittest.main()
