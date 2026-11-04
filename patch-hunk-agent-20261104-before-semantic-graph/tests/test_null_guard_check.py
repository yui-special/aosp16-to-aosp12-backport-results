"""NULL guard preservation is a deterministic acceptance requirement."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from git import Repo


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'
sys.path.insert(0, str(SRC))

from tools.guard_check import check_null_guard_preservation
from tools.project import Project


class NullGuardCheckTests(unittest.TestCase):
    def test_removed_guard_is_rejected_when_value_is_still_used(self):
        patch = """
--- a/Tracker.java
+++ b/Tracker.java
@@ -1,2 +1,2 @@
-if (predicate == null || predicate.test(wpc)) {
+if (predicate.test(wpc)) {
"""
        report = check_null_guard_preservation(patch)

        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['unprotected'], ['predicate'])

    def test_preserved_guard_passes(self):
        patch = """
--- a/Tracker.java
+++ b/Tracker.java
@@ -1,2 +1,3 @@
-if (predicate == null || predicate.test(wpc)) {
+if (predicate == null || predicate.test(wpc)) {
+    Objects.requireNonNull(predicate);
 }
"""
        report = check_null_guard_preservation(patch)

        self.assertEqual(report['status'], 'PASS')

    def test_removed_value_is_not_a_false_positive(self):
        patch = """
--- a/Tracker.java
+++ b/Tracker.java
@@ -1,1 +1,1 @@
-if (unused != null) { old(unused); }
+return true;
"""
        report = check_null_guard_preservation(patch)

        self.assertEqual(report['status'], 'NOT_APPLICABLE')

    def test_project_validation_rejects_removed_guard_before_compile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = Repo.init(root)
            with repo.config_writer() as config:
                config.set_value('user', 'name', 'Test')
                config.set_value('user', 'email', 'test@example.invalid')
            source = root / 'Example.java'
            source.write_text(
                'class Example {\n'
                '  boolean check(Object value) {\n'
                '    return value == null || value.equals("ok");\n'
                '  }\n'
                '}\n',
                encoding='utf-8',
            )
            repo.index.add(['Example.java'])
            repo.index.commit('base')
            base = repo.head.commit.hexsha
            source.write_text(
                'class Example {\n'
                '  boolean check(Object value) {\n'
                '    return value.equals("ok");\n'
                '  }\n'
                '}\n',
                encoding='utf-8',
            )
            patch = repo.git.diff(base) + '\n'
            repo.git.reset('--hard')
            project = Project(SimpleNamespace(
                project_url='fixture',
                project_dir=str(root),
                error_message='',
                new_patch_parent=base,
                target_release=base,
            ))
            project.full_patch_base = base
            project.all_hunks_applied_succeeded = True

            try:
                result = project._validate(base, patch)
            finally:
                project.repo.close()
                repo.close()

        self.assertIn('NULL_GUARD_CHECK_FAILED', result)
        self.assertFalse(project.poc_succeeded)


if __name__ == '__main__':
    unittest.main()
