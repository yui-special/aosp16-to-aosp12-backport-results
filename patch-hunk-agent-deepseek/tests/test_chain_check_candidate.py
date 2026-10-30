"""The final API-chain check must inspect the patched candidate, not the worktree."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHAIN_CHECK = ROOT / "data_ncwang/chain_check.py"


class ChainCheckCandidateTests(unittest.TestCase):
    def _git(self, repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_patch_tree_is_checked_even_when_worktree_is_stale(self):
        with tempfile.TemporaryDirectory(prefix="chain-check-test-") as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.name", "Test")
            self._git(repo, "config", "user.email", "test@example.com")

            source = repo / "A.java"
            source.write_text(
                "class A {\n"
                "  void reset(String pkg, int userId) {}\n"
                "  void first() { reset(\"a\", 1); }\n"
                "  void second() { reset(\"b\", 2); }\n"
                "}\n",
                encoding="utf-8",
            )
            self._git(repo, "add", "A.java")
            self._git(repo, "commit", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD").strip()

            source.write_text(
                "class A {\n"
                "  void reset(String pkg, int userId, boolean restore) {}\n"
                "  void first() { reset(\"a\", 1, true); }\n"
                "  void second() { reset(\"b\", 2, true); }\n"
                "}\n",
                encoding="utf-8",
            )
            patch = Path(tmp) / "candidate.patch"
            patch.write_text(self._git(repo, "diff"), encoding="utf-8")
            self._git(repo, "checkout", "--", "A.java")
            # The worktree intentionally remains on the stale base.
            env = dict(os.environ, CHAIN_CHECK_BASE_REF=base)
            result = subprocess.run(
                [sys.executable, str(CHAIN_CHECK), str(repo), str(patch)],
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("仍用旧参数个数的调用点（0）", result.stdout)

    def test_missing_tree_reference_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="chain-check-test-") as tmp:
            patch = Path(tmp) / "candidate.patch"
            patch.write_text("", encoding="utf-8")
            env = {
                key: value for key, value in os.environ.items()
                if key not in {"CHAIN_CHECK_REF", "CHAIN_CHECK_BASE_REF"}
            }
            result = subprocess.run(
                [sys.executable, str(CHAIN_CHECK), tmp, str(patch)],
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("CHAIN_CHECK_TREE_REQUIRED", result.stderr)


if __name__ == "__main__":
    unittest.main()
