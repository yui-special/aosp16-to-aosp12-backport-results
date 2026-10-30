import importlib
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
            }))
            with patch.object(runtime, 'is_commit_valid', return_value=True), \
                    patch.object(runtime, 'rev_parse_commit', side_effect=lambda ref, _: ref):
                data = runtime.load_yml(str(config))
            self.assertEqual(data.llm_model, 'deepseek-flash')
            self.assertEqual(data.llm_base_url, 'https://api.deepseek.com/v1')
            self.assertEqual(data.coverage_model, 'deepseek-flash')
            self.assertEqual(data.llm_timeout_seconds, 123)
            self.assertEqual(data.llm_max_retries, 2)
            self.assertEqual(data.coverage_review_iterations, 7)
            self.assertEqual(data.coverage_review_retries, 0)
            self.assertEqual(data.final_repair_iterations, 9)


if __name__ == '__main__':
    unittest.main()
