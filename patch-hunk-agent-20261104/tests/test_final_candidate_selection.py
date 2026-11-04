"""A stale resume checkpoint must never replace the current validated candidate."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'
sys.path.insert(0, str(SRC))

from agent.invoke_llm import final_candidate_sources


class FinalCandidateSelectionTests(unittest.TestCase):
    def test_live_candidate_wins_over_stale_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / 'latest-validated.patch'
            checkpoint.write_text('BAD_OLD_CHECKPOINT\n', encoding='utf-8')
            project = SimpleNamespace(
                succeeded_patches=['CORRECT_CURRENT_CANDIDATE\n']
            )
            candidates = final_candidate_sources(project, str(checkpoint))

        self.assertEqual(candidates, ['CORRECT_CURRENT_CANDIDATE\n'])

    def test_checkpoint_is_used_only_without_live_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / 'latest-validated.patch'
            checkpoint.write_text('RESUME_CHECKPOINT\n', encoding='utf-8')
            project = SimpleNamespace(succeeded_patches=[])
            candidates = final_candidate_sources(project, str(checkpoint))

        self.assertEqual(candidates, ['RESUME_CHECKPOINT\n'])


if __name__ == '__main__':
    unittest.main()
