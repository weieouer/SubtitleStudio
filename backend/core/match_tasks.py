"""为同步参考匹配提供进度、取消和有界生命周期，不保存大结果。"""
from __future__ import annotations

from collections import OrderedDict
import threading
import time


class MatchTasks:
    def __init__(self, timeout=150, history_limit=32):
        self.timeout = timeout
        self.history_limit = history_limit
        self.lock = threading.RLock()
        self.tasks = OrderedDict()
        self.active = None

    def status(self, identity):
        with self.lock:
            task = self.tasks.get(identity)
            return {key: task[key] for key in ('status', 'progress', 'message')} if task else {
                'status': 'pending', 'progress': 0, 'message': '等待匹配请求'}

    def cancel(self, identity):
        with self.lock:
            task = self.tasks.get(identity)
            if task:
                task['cancel'].set()
            return task is not None

    def run(self, identity, calculate):
        with self.lock:
            if self.active is not None:
                raise ValueError('另一个剧本匹配正在处理，请完成或取消后重试')
            if identity in self.tasks:
                raise ValueError('匹配任务标识已使用，请重新开始校验')
            task = {'status': 'running', 'progress': 0, 'message': '建立剧本索引',
                    'cancel': threading.Event()}
            self.tasks[identity] = task
            self.active = identity
            while len(self.tasks) > self.history_limit:
                self.tasks.popitem(last=False)
        deadline = time.monotonic() + self.timeout

        def checkpoint(progress=0, message='匹配剧本'):
            if task['cancel'].is_set():
                raise ValueError('剧本匹配已取消，当前字幕保持不变')
            if time.monotonic() >= deadline:
                raise ValueError('剧本匹配超过时间上限，请缩小剧本或字幕范围后重试')
            with self.lock:
                task['progress'] = max(task['progress'], progress)
                task['message'] = message

        try:
            checkpoint()
            result = calculate(checkpoint)
            checkpoint(1, '校验完成')
            with self.lock:
                task['status'] = 'done'
            return result
        except Exception as exc:
            with self.lock:
                task['status'] = 'cancelled' if task['cancel'].is_set() else 'error'
                task['message'] = str(exc)
            raise
        finally:
            with self.lock:
                self.active = None
