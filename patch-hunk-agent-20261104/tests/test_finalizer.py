"""End-of-session finalization must submit a complete candidate to validate."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'
sys.path.insert(0, str(SRC))

from agent.invoke_llm import _extract_candidate_patch, finalize_and_validate_candidate


PATCH = (
    'diff --git a/A.java b/A.java\n'
    '--- a/A.java\n'
    '+++ b/A.java\n'
    '@@ -1 +1 @@\n'
    '-old\n'
    '+new\n'
)


class FinalizerTests(unittest.TestCase):
    def test_extracts_fenced_diff_from_agent_output(self):
        value = {'output': 'reasoning\n```diff\n' + PATCH + '```\nmore prose'}
        self.assertEqual(_extract_candidate_patch(value), PATCH)

    def test_agent_output_is_validated_without_extra_model_call(self):
        project = SimpleNamespace(round_succeeded=False, _validate=Mock())
        project._validate.side_effect = lambda _ref, _patch, _evidence: setattr(
            project, 'round_succeeded', True
        ) or 'OK'
        llm = Mock()
        group = {
            'hunks': [1],
            'files': ['A.java'],
            'concern': 'fixture',
            'why': 'fixture',
        }
        ok, result = finalize_and_validate_candidate(
            llm, project, 'base', group, PATCH, {'output': PATCH}
        )
        self.assertTrue(ok)
        self.assertEqual(result, 'OK')
        project._validate.assert_called_once()
        llm.invoke.assert_not_called()

    def test_finalizer_no_candidate_does_not_validate(self):
        project = SimpleNamespace(round_succeeded=False, _validate=Mock())
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content='NO_PATCH')
        group = {
            'hunks': [1],
            'files': ['A.java'],
            'concern': 'fixture',
            'why': 'fixture',
        }
        ok, result = finalize_and_validate_candidate(
            llm, project, 'base', group, PATCH, {'output': ''}
        )
        self.assertFalse(ok)
        self.assertEqual(result, 'FINALIZER_NO_CANDIDATE')
        project._validate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
