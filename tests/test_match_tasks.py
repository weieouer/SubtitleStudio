"""参考匹配的取消、进度、有界历史及失败释放。"""
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from core.match_tasks import MatchTasks
from core.reference_match import match_reference
from core.reference_match import ReferenceIndex


class MatchTaskTests(unittest.TestCase):
    def test_range_sampling_matches_previous_slice_then_sample(self):
        positions = list(range(1200))
        for lo, hi in ((0, 1200), (20, 1190), (110, 110), (4, 50), (100, 149)):
            sliced = positions[lo:hi]
            expected = sliced if len(sliced) <= 48 else [
                sliced[round(i * (len(sliced)-1)/47)] for i in range(48)]
            self.assertEqual(ReferenceIndex._sample(positions, start=lo, end=hi), expected)
    def test_cancel_active_match_and_reject_parallel_work(self):
        tasks = MatchTasks()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def calculate(check):
            check(.25, '第一段')
            entered.set()
            release.wait(3)
            check(.5, '第二段')
        def run():
            try: tasks.run('first', calculate)
            except ValueError as exc: errors.append(str(exc))
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(tasks.status('first')['progress'], .25)
            with self.assertRaisesRegex(ValueError, '另一个'):
                tasks.run('second', lambda check: None)
            self.assertTrue(tasks.cancel('first'))
        finally:
            release.set(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn('已取消', errors[0])
        self.assertEqual(tasks.status('first')['status'], 'cancelled')
        self.assertEqual(tasks.run('second', lambda check: 42), 42)

    def test_history_is_bounded_and_failure_releases_slot(self):
        tasks = MatchTasks(history_limit=2)
        for identity in ('one', 'two', 'three'):
            tasks.run(identity, lambda check: {})
        self.assertEqual(len(tasks.tasks), 2)
        self.assertEqual(tasks.status('one')['status'], 'pending')
        with self.assertRaises(RuntimeError):
            tasks.run('bad', lambda check: (_ for _ in ()).throw(RuntimeError('failure')))
        self.assertEqual(tasks.status('bad')['status'], 'error')
        self.assertEqual(tasks.run('next', lambda check: True), True)

    def test_time_budget_stops_work(self):
        tasks = MatchTasks(timeout=1)
        with patch('core.match_tasks.time.monotonic', side_effect=[0, 0, 2]):
            with self.assertRaisesRegex(ValueError, '时间上限'):
                tasks.run('limited', lambda check: check(.1))
        self.assertIsNone(tasks.active)

    def test_real_match_checks_cancellation_and_reports_progress(self):
        updates = []
        def check(progress, message):
            updates.append(progress)
            if progress >= .05:
                raise ValueError('cancelled')
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            match_reference([{'text': '你好世界', 'start': 0, 'end': 1}],
                            {'text': '你好世界'}, checkpoint=check)
        self.assertEqual(updates, [0, .05])
