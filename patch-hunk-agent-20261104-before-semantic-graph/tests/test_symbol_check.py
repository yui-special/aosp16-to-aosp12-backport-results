"""Post-apply symbol checks must reject new references absent from the candidate tree."""
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'
sys.path.insert(0, str(SRC))

from tools.project import Project


class SymbolCheckTests(unittest.TestCase):
    def _git(self, repo, *args):
        return subprocess.run(
            ['git', '-C', str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def _repo(self, directory):
        repo = Path(directory) / 'repo'
        repo.mkdir()
        self._git(repo, 'init')
        self._git(repo, 'config', 'user.name', 'Test')
        self._git(repo, 'config', 'user.email', 'test@example.com')
        self._git(repo, 'config', 'core.autocrlf', 'false')
        (repo / 'WindowContainer.java').write_text(
            'class WindowContainer { }\n', encoding='utf-8'
        )
        (repo / 'Tracker.java').write_text(
            'class Tracker {\n'
            '  boolean hasVisibleActivity(int uid) {\n'
            '    return match(uid, null);\n'
            '  }\n'
            '  boolean match(int uid, Object predicate) { return predicate != null; }\n'
            '}\n',
            encoding='utf-8',
        )
        self._git(repo, 'add', '.')
        self._git(repo, 'commit', '-m', 'base')
        return repo, self._git(repo, 'rev-parse', 'HEAD').strip()

    def test_missing_existing_type_method_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, base = self._repo(directory)
            project = Project(SimpleNamespace(
                project_url='fixture',
                project_dir=str(repo),
                error_message='',
                new_patch_parent=base,
                target_release=base,
            ))
            (repo / 'Tracker.java').write_text(
                'class Tracker {\n'
                '  boolean hasVisibleActivity(int uid) {\n'
                '    return WindowContainer.alwaysTruePredicate();\n'
                '  }\n'
                '  boolean match(int uid, Object predicate) { return predicate != null; }\n'
                '}\n',
                encoding='utf-8',
            )
            patch = self._git(repo, 'diff')
            self._git(repo, 'checkout', '--', 'Tracker.java')
            report = project.post_apply_symbol_check(base, patch)
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('WindowContainer.alwaysTruePredicate', report['missing'])

    def test_inline_expression_has_no_missing_static_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, base = self._repo(directory)
            project = Project(SimpleNamespace(
                project_url='fixture',
                project_dir=str(repo),
                error_message='',
                new_patch_parent=base,
                target_release=base,
            ))
            (repo / 'Tracker.java').write_text(
                'class Tracker {\n'
                '  boolean hasVisibleActivity(int uid) {\n'
                '    return match(uid, value -> true);\n'
                '  }\n'
                '  boolean match(int uid, Object predicate) { return predicate != null; }\n'
                '}\n',
                encoding='utf-8',
            )
            patch = self._git(repo, 'diff')
            self._git(repo, 'checkout', '--', 'Tracker.java')
            report = project.post_apply_symbol_check(base, patch)
        self.assertNotEqual(report['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
