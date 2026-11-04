"""Minimal safety gates layered on the November 1 codebase."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'
sys.path.insert(0, str(SRC))

from agent.invoke_llm import (
    promote_pending_patch,
    quarantine_patch,
    validate_direct_apply_safety,
)


class MinimalStabilityTests(unittest.TestCase):
    def test_pending_patch_is_promoted_only_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            final = Path(directory) / 'complete.patch'
            pending = Path(str(final) + '.pending')
            pending.write_text('candidate\n', encoding='utf-8')

            self.assertFalse(final.exists())
            self.assertTrue(promote_pending_patch(str(pending), str(final)))
            self.assertFalse(pending.exists())
            self.assertEqual(final.read_text(encoding='utf-8'), 'candidate\n')

    def test_quarantine_removes_failed_patch_from_success_path(self):
        with tempfile.TemporaryDirectory() as directory:
            final = Path(directory) / 'complete.patch'
            final.write_text('bad\n', encoding='utf-8')

            quarantine_patch(str(final))

            self.assertFalse(final.exists())
            self.assertTrue(Path(str(final) + '.stale').is_file())

    def test_direct_apply_symbol_failure_is_rejected(self):
        project = SimpleNamespace(
            post_apply_symbol_check=lambda *_args, **_kwargs: {
                'status': 'FAIL', 'missing': ['Container.missing']
            },
            post_apply_null_guard_check=lambda _patch: {
                'status': 'NOT_APPLICABLE', 'unprotected': []
            },
        )

        report, accepted = validate_direct_apply_safety(
            project, 'target', '+++ b/A.java\n+Container.missing();\n'
        )

        self.assertFalse(accepted)
        self.assertEqual(report['status'], 'FAIL')

    def test_direct_apply_unknown_symbol_check_is_rejected(self):
        project = SimpleNamespace(
            post_apply_symbol_check=lambda *_args, **_kwargs: {
                'status': 'UNKNOWN', 'error': 'symbol index unavailable'
            },
            post_apply_null_guard_check=lambda _patch: {
                'status': 'NOT_APPLICABLE', 'unprotected': []
            },
        )

        report, accepted = validate_direct_apply_safety(
            project, 'target', '+++ b/A.java\n+Container.present();\n'
        )

        self.assertFalse(accepted)
        self.assertEqual(report['status'], 'UNKNOWN')

    def test_direct_apply_passes_after_symbol_and_guard_checks(self):
        project = SimpleNamespace(
            post_apply_symbol_check=lambda *_args, **_kwargs: {
                'status': 'PASS', 'missing': []
            },
            post_apply_null_guard_check=lambda _patch: {
                'status': 'PASS', 'unprotected': []
            },
        )

        report, accepted = validate_direct_apply_safety(
            project, 'target', '+++ b/A.java\n+Container.present();\n'
        )

        self.assertTrue(accepted)
        self.assertEqual(report['status'], 'PASS')

    def test_direct_apply_guard_failure_is_rejected(self):
        project = SimpleNamespace(
            post_apply_symbol_check=lambda *_args, **_kwargs: {
                'status': 'PASS', 'missing': []
            },
            post_apply_null_guard_check=lambda _patch: {
                'status': 'FAIL', 'unprotected': ['predicate']
            },
        )

        report, accepted = validate_direct_apply_safety(
            project, 'target', '+++ b/A.java\n+predicate.test(value);\n'
        )

        self.assertFalse(accepted)
        self.assertEqual(report['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
