import importlib
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SRC = (Path(__file__).resolve().parents[1] / 'data_ncwang/backport_patch/tools/'
       'patch-backporting/src')
sys.path.insert(0, str(SRC))


class RuntimeConfigTests(unittest.TestCase):
    def test_invalid_candidate_replay_is_rejected_before_touching_worktree(self):
        runtime = importlib.import_module('backporting')
        with tempfile.TemporaryDirectory() as directory:
            seed = Path(directory)/'candidate.patch'
            seed.write_text('')
            for extra in ([], ['--only-group', '0']):
                with self.subTest(extra=extra), \
                        patch.object(sys, 'argv', ['backporting.py', '-c', 'fixture.yml',
                                                  '--repair-from', str(seed), *extra]), \
                        patch.object(runtime, 'load_yml', return_value=SimpleNamespace()), \
                        patch.object(runtime, 'Project') as project, \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        runtime.main()
                    project.assert_not_called()

    def test_list_groups_preserves_worktree_and_does_not_export_inputs(self):
        from git import Actor, Repo
        runtime = importlib.import_module('backporting')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = Repo.init(root / 'repo')
            source = root / 'repo/source.java'
            ignored = root / 'repo/private.log'
            untracked = root / 'repo/notes.txt'
            source.write_text('old\n')
            (root / 'repo/.gitignore').write_text('*.log\n')
            repo.index.add(['source.java', '.gitignore'])
            actor = Actor('Test', 'test@example.invalid')
            base = repo.index.commit('target', author=actor, committer=actor).hexsha
            source.write_text('new\n')
            repo.index.add(['source.java'])
            upstream = repo.index.commit('patch', author=actor, committer=actor).hexsha
            source.write_text('user edit\n')
            ignored.write_text('keep ignored\n')
            untracked.write_text('keep notes\n')
            dataset = root / 'dataset'
            dataset.mkdir()
            data = SimpleNamespace(project='fixture', tag='case', project_url='fixture',
                project_dir=str(root / 'repo'), patch_dataset_dir=str(dataset),
                new_patch=upstream, new_patch_parent=base, target_release=base,
                error_message='', hunk_groups_spec=None, merged_hunks_input_dir='')
            before = repo.git.status('--porcelain', '--ignored')
            output = io.StringIO()
            with patch.object(sys, 'argv', ['backporting.py', '-c', 'fixture.yml', '--list-groups']), \
                    patch.object(runtime, 'load_yml', return_value=data), \
                    patch.object(runtime, 'add_file_handler'), \
                    patch.object(runtime.os, 'makedirs'), \
                    patch.object(runtime, 'initial_agent') as initialize, \
                    contextlib.redirect_stdout(output):
                runtime.main()
            self.assertIn('original_hunks=[0]', output.getvalue())
            self.assertEqual(repo.git.status('--porcelain', '--ignored'), before)
            self.assertEqual(source.read_text(), 'user edit\n')
            self.assertEqual(ignored.read_text(), 'keep ignored\n')
            self.assertEqual(untracked.read_text(), 'keep notes\n')
            self.assertEqual(repo.head.commit.hexsha, upstream)
            self.assertFalse((dataset / 'merged_hunks').exists())
            initialize.assert_not_called()

    def test_missing_explicit_inputs_are_errors_instead_of_legacy_fallback(self):
        import yaml
        runtime = importlib.import_module('backporting')
        with tempfile.TemporaryDirectory() as directory:
            for field in ('hunk_groups', 'merged_hunks_dir'):
                with self.subTest(field=field):
                    config = Path(directory) / 'config.yml'
                    config.write_text(yaml.safe_dump({
                        'project_dir': directory, 'patch_dataset_dir': directory,
                        'new_patch': 'source', 'new_patch_parent': 'parent',
                        'target_release': 'target', field: 'missing-input',
                    }))
                    with self.assertRaises(FileNotFoundError):
                        runtime.load_yml(str(config))

    def test_generation_exit_status_requires_final_acceptance(self):
        runtime = importlib.import_module('backporting')
        for accepted, expected in [(False, 2), (True, 0)]:
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as directory:
                data = SimpleNamespace(project='fixture', tag='case', cumulative=False,
                                       patch_dataset_dir=directory)
                project = Mock(poc_succeeded=accepted)
                with patch.object(sys, 'argv', ['backporting.py', '-c', 'fixture.yml']), \
                        patch.object(runtime, 'load_yml', return_value=data), \
                        patch.object(runtime, 'Project', return_value=project), \
                        patch.object(runtime, 'initial_agent', return_value=(Mock(), Mock())), \
                        patch.object(runtime, 'do_backport'), \
                        patch.object(runtime, 'add_file_handler'), \
                        patch.object(runtime.os, 'makedirs'), \
                        patch.object(runtime.shutil, 'copy'), \
                        patch.object(runtime.time, 'sleep'):
                    self.assertEqual(runtime.main(), expected)

    def test_generation_model_and_endpoint_come_from_config(self):
        import yaml
        runtime = importlib.import_module('backporting')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yml'
            config.write_text(yaml.safe_dump({
                'project_dir': directory, 'patch_dataset_dir': directory,
                'new_patch': 'source', 'new_patch_parent': 'parent', 'target_release': 'target',
                'llm_model': 'deepseek-flash',
                'llm_base_url': 'https://api.deepseek.com/v1',
                'coverage_model': 'deepseek-flash',
                'llm_timeout_seconds': 123, 'llm_max_retries': 2,
                'coverage_review_iterations': 7, 'coverage_review_retries': 0,
                'final_repair_iterations': 9,
                'final_repair_retries': 1, 'final_repair_category_switches': 0,
                'query_stale_limit': 3, 'ledger_chars': 12000,
                'context_query_limit': 6, 'context_query_chars': 18000,
                'context_inline_chars': 0,
                'unlimited_backend': True,
            }))
            with patch.object(runtime, 'is_commit_valid', return_value=True), \
                    patch.object(runtime, 'rev_parse_commit', side_effect=lambda ref, _: ref):
                data = runtime.load_yml(str(config))
            self.assertEqual(data.llm_model, 'deepseek-flash')
            self.assertIs(data.unlimited_backend, True)
            self.assertEqual(data.llm_base_url, 'https://api.deepseek.com/v1')
            self.assertEqual(data.coverage_model, 'deepseek-flash')
            self.assertEqual(data.coverage_temperature, 0.0)
            self.assertEqual(data.llm_timeout_seconds, 123)
            self.assertEqual(data.llm_max_retries, 2)
            self.assertEqual(data.coverage_review_iterations, 7)
            self.assertEqual(data.coverage_review_retries, 0)
            self.assertEqual(data.final_repair_iterations, 9)
            self.assertEqual(data.final_repair_retries, 1)
            self.assertEqual(data.final_repair_category_switches, 0)
            self.assertEqual(data.query_stale_limit, 3)
            self.assertEqual(data.ledger_chars, 12000)
            self.assertEqual(data.context_query_limit, 6)
            self.assertEqual(data.context_query_chars, 18000)
            self.assertEqual(data.context_inline_chars, 0)

    def test_context_limits_reject_invalid_configuration(self):
        import yaml
        runtime = importlib.import_module('backporting')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yml'
            for field, value in [('query_stale_limit', 0), ('query_stale_limit', True),
                                 ('coverage_temperature', True), ('coverage_temperature', float('nan')),
                                 ('coverage_temperature', -0.1), ('coverage_temperature', 2.1),
                                 ('ledger_chars', -1), ('ledger_chars', '6000'),
                                 ('context_query_limit', 0), ('context_query_chars', 1999),
                                 ('context_inline_chars', -1), ('context_inline_chars', True),
                                 ('final_repair_retries', -1), ('final_repair_retries', True),
                                 ('final_repair_category_switches', -1), ('unlimited_backend', 'true')]:
                with self.subTest(field=field, value=value):
                    config.write_text(yaml.safe_dump({field: value}))
                    with self.assertRaisesRegex(ValueError, field):
                        runtime.load_yml(str(config))


if __name__ == '__main__':
    unittest.main()
