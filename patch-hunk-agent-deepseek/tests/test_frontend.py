"""No network: resume correctness, bounded concurrency and deterministic publication."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from concurrent.futures import wait as futures_wait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'data_ncwang/backport_patch/tools/patch-backporting/src'))
from tools.frontend import Checkpoint, run_tasks, stage_options


def valid(value):
    if not isinstance(value, dict) or type(value.get('value')) is not int:
        raise ValueError('invalid result')


class FrontendTasksTests(unittest.TestCase):
    def test_resume_only_successes_and_explicit_fresh_mode_calls_every_task(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'summaries.json'
            items = [(n, n) for n in range(3)]
            first = Checkpoint(output, {'tree': 'old', 'prompt': 'v1'})
            def work(n):
                if n == 1:
                    raise RuntimeError('request failed')
                return {'value': n}
            with self.assertRaisesRegex(RuntimeError, 'request failed'):
                list(run_tasks(items, work, first, valid, 1))
            calls = []
            def success(n):
                calls.append(n)
                return {'value': n}
            resumed = list(run_tasks(items, success, Checkpoint(output, {'tree': 'old', 'prompt': 'v1'}), valid, 1))
            self.assertEqual(calls, [1, 2])
            self.assertEqual([reused for _, _, reused in resumed], [True, False, False])
            calls.clear()
            list(run_tasks(items, success, Checkpoint(output, {'tree': 'old', 'prompt': 'v1'}, resume=False), valid, 1))
            self.assertEqual(calls, [0, 1, 2])
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_revision_configuration_and_corruption_cannot_reuse_an_old_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'mapping.json'
            identity = dict(target='tree-a', upstream='tree-b', model='m1', options={'temperature': 0})
            checkpoint = Checkpoint(output, identity)
            checkpoint.put('h0', {'value': 1})
            for field, value in [('target', 'tree-c'), ('upstream', 'tree-d'), ('model', 'm2'), ('options', {'temperature': 0.5})]:
                with self.subTest(field=field):
                    self.assertIsNone(Checkpoint(output, {**identity, field: value}).get('h0', valid))
            stored = json.loads(checkpoint.path.read_text())
            stored['entries']['h0']['value'] = {'value': 999}
            checkpoint.path.write_text(json.dumps(stored))
            self.assertIsNone(Checkpoint(output, identity).get('h0', valid))
            checkpoint.path.write_text('{half written')
            self.assertIsNone(Checkpoint(output, identity).get('h0', valid))

    def test_independent_requests_overlap_and_results_keep_original_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            barrier = threading.Barrier(2)
            active = 0
            maximum = 0
            lock = threading.Lock()
            def work(n):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(active, maximum)
                barrier.wait(timeout=5)
                with lock:
                    active -= 1
                return {'value': n}
            result = list(run_tasks([(n, n) for n in range(4)], work,
                          Checkpoint(Path(directory)/'triage.json', {}), valid, 2))
            self.assertEqual(maximum, 2)
            self.assertEqual([(key, row['value']) for key, row, _ in sorted(result)], [(n, n) for n in range(4)])

    def test_inflight_success_is_saved_after_another_task_fails_without_dispatching_more(self):
        with tempfile.TemporaryDirectory() as directory:
            barrier = threading.Barrier(2)
            observed_failure = threading.Event()
            calls = []
            def work(n):
                calls.append(n)
                barrier.wait(timeout=5)
                if n == 0:
                    raise RuntimeError('provider failure')
                if not observed_failure.wait(timeout=5):
                    raise RuntimeError('coordinator did not observe failure')
                return {'value': n}
            def wait_for_completion(*args, **kwargs):
                done, pending = futures_wait(*args, **kwargs)
                if any(f.exception() is not None for f in done):
                    observed_failure.set()
                return done, pending
            checkpoint = Checkpoint(Path(directory)/'stage.json', {})
            with patch('tools.frontend.wait', side_effect=wait_for_completion), self.assertRaises(RuntimeError):
                list(run_tasks([(n, n) for n in range(5)], work, checkpoint, valid, 2))
            self.assertEqual(sorted(calls), [0, 1])
            self.assertEqual(checkpoint.get(1, valid), {'value': 1})
            self.assertIsNone(checkpoint.get(0, valid))

    def test_invalid_and_explicitly_unresolved_results_are_never_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Checkpoint(Path(directory)/'stage.json', {})
            with self.assertRaises(ValueError):
                list(run_tasks([(1, 1)], lambda _: {}, checkpoint, valid, 1))
            self.assertIsNone(checkpoint.get(1, valid))
            result = list(run_tasks([(1, 1)], lambda _: {'value': 1}, checkpoint, valid, 1,
                                    cacheable=lambda _: False))
            self.assertEqual(result[0][1], {'value': 1})
            self.assertIsNone(checkpoint.get(1, valid))

    def test_configuration_supports_fresh_runs_and_stage_overrides(self):
        with patch.dict(os.environ, {'FRONTEND_OPTIONS': json.dumps({
                'workers': 2, 'resume': False, 'summary': {'workers': 4, 'request_options': {'temperature': 0.2}}})}, clear=True):
            self.assertEqual(stage_options('summary')['workers'], 4)
            self.assertEqual(stage_options('triage')['workers'], 2)
            self.assertFalse(stage_options('mapping')['resume'])
            self.assertEqual(stage_options('summary')['request_options'], {'temperature': 0.2})
        for options in [{'workers': 0}, {'resume': 'false'}, {'summary': []}, {'summary': {'request_options': {'messages': []}}}]:
            with patch.dict(os.environ, {'FRONTEND_OPTIONS': json.dumps(options)}, clear=True), self.assertRaises(ValueError):
                stage_options('summary')
