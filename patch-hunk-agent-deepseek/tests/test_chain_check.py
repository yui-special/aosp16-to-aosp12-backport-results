"""Post-run API checks must inspect the artifact, never the mutable checkout."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CHECKER = Path(__file__).resolve().parents[1] / 'data_ncwang/chain_check.py'


class ChainCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chain candidate ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.source = self.repo / 'Example.java'
        self.original = ('class Example {\n  void changed(int value) {}\n'
                         '  void caller() {\n    changed(1);\n  }\n}\n')
        self.fixed = self.original.replace('int value)', 'int value, boolean restore)').replace(
            'changed(1);', 'changed(1, true);')
        self.source.write_text(self.original)
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.artifact = self.root / 'candidate.patch'

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def candidate(self, contents):
        self.source.write_text(contents)
        self.artifact.write_text(self.git('diff', self.base))
        self.git('reset', '--hard')

    def check(self, base=None):
        before = (self.git('rev-parse', 'HEAD'), self.git('diff'), self.git('diff', '--cached'),
                  self.source.read_bytes())
        result = subprocess.run([sys.executable, str(CHECKER), str(self.repo), str(self.artifact),
                                 '--base', base or self.base], capture_output=True, text=True,
                                env={**os.environ, 'CHAIN_CHECK_REF': 'stale-nonexistent-tree'})
        self.assertEqual(before, (self.git('rev-parse', 'HEAD'), self.git('diff'),
                                 self.git('diff', '--cached'), self.source.read_bytes()))
        return result

    def test_fixed_patch_passes_when_later_task_changed_index_and_worktree(self):
        self.candidate(self.fixed)
        self.source.write_text('unrelated staged contents\n')
        self.git('add', '.')
        self.source.write_text('unrelated working contents\n')
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('candidate_tree=', result.stdout)

    def test_bad_patch_is_rejected_even_when_worktree_has_fixed_caller(self):
        self.candidate(self.original.replace('int value)', 'int value, boolean restore)'))
        self.source.write_text(self.fixed)
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn('changed(1);', result.stdout)

    def test_bad_base_and_unappliable_patch_never_fall_back_to_worktree(self):
        self.candidate(self.fixed)
        self.assertEqual(self.check('missing-base').returncode, 2)
        self.artifact.write_text('not a patch\n')
        self.assertEqual(self.check().returncode, 2)

    def test_invalid_explicit_candidate_is_an_error_even_without_signature_changes(self):
        for patch_text in ('', 'not a patch\n'):
            self.artifact.write_text(patch_text)
            result = subprocess.run([sys.executable, str(CHECKER), str(self.repo), str(self.artifact)],
                capture_output=True, text=True, env={**os.environ, 'CHAIN_CHECK_REF': 'missing-tree'})
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('调用点（0）', result.stdout)

    def test_explicit_base_overrides_stale_but_valid_candidate(self):
        self.candidate(self.original.replace('int value)', 'int value, boolean restore)'))
        self.source.write_text(self.fixed)
        self.git('add', '.')
        stale = self.git('write-tree').strip()
        result = subprocess.run([sys.executable, str(CHECKER), str(self.repo), str(self.artifact),
                                 '--base', self.base], capture_output=True, text=True,
                                env={**os.environ, 'CHAIN_CHECK_REF': stale})
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)


if __name__ == '__main__':
    unittest.main()
