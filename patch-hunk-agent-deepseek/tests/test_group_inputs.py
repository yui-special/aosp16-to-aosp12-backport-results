"""Grouping inputs preserve the selected set and prerequisite order."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SRC = (Path(__file__).resolve().parents[1] / 'data_ncwang/backport_patch/tools/'
       'patch-backporting/src')
sys.path.insert(0, str(SRC))

from agent import invoke_llm as runtime
from tools.utils import dump_merged_hunks, group_patch, load_merged_hunk_files


SOURCE_PATCH = ''.join(
    'diff --git a/{0}.java b/{0}.java\n--- a/{0}.java\n+++ b/{0}.java\n'
    '@@ -1 +1 @@\n-old{0}\n+new{0}\n'.format(name) for name in ('A', 'B'))
PLAN = {'groups': [
    {'id': 'provider', 'hunks': [1]},
    {'id': 'consumer', 'hunks': [0], 'depends_on': [1]},
]}


class GroupInputTests(unittest.TestCase):
    def test_spec_and_export_keep_prerequisite_order(self):
        _, merged = group_patch(SOURCE_PATCH, PLAN)
        self.assertEqual([g['hunks'] for g in merged], [[1], [0]])
        with tempfile.TemporaryDirectory() as root:
            original, _ = dump_merged_hunks(merged, root)
            loaded = load_merged_hunk_files(original)
            self.assertEqual([g['gid'] for g in loaded], ['provider', 'consumer'])

    def test_index_order_wins_over_filenames(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            for name in ('merged-hunk-99.patch', 'merged-hunk-100.patch'):
                (folder / name).write_text(name)
            (folder / 'index.json').write_text(json.dumps([
                {'file': 'merged-hunk-99.patch', 'hunks': [99]},
                {'file': 'merged-hunk-100.patch', 'hunks': [100]},
            ]))
            self.assertEqual([g['hunks'] for g in load_merged_hunk_files(root)], [[99], [100]])

    def test_reexport_removes_only_obsolete_generated_inputs(self):
        _, merged = group_patch(SOURCE_PATCH, PLAN)
        with tempfile.TemporaryDirectory() as root:
            original, ported = dump_merged_hunks(merged, root)
            custom = Path(original) / 'custom.patch'
            previous_result = Path(ported) / 'merged-hunk-01.patch'
            custom.write_text('custom input')
            previous_result.write_text('previous result')
            dump_merged_hunks(merged[:1], root)
            self.assertFalse((Path(original) / 'merged-hunk-01.patch').exists())
            self.assertEqual(custom.read_text(), 'custom input')
            self.assertEqual(previous_result.read_text(), 'previous result')
            self.assertEqual([g['hunks'] for g in load_merged_hunk_files(original)], [[1]])
            dump_merged_hunks([], root)
            self.assertEqual(load_merged_hunk_files(original), [])
            self.assertFalse((Path(original) / 'merged-hunk-00.patch').exists())
            self.assertTrue(custom.exists())

    def test_index_ignores_unlisted_patch_files(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / 'current.patch').write_text('current')
            (folder / 'leftover.patch').write_text('leftover')
            (folder / 'index.json').write_text(json.dumps([{'file': 'current.patch'}]))
            self.assertEqual([g['text'] for g in load_merged_hunk_files(root)], ['current'])

    def test_bad_or_missing_indexed_input_does_not_fall_back_to_directory_scan(self):
        for index, error in [
            ('{', ValueError), ('{}', ValueError),
            ('[{"file":"missing.patch"}]', FileNotFoundError),
            ('[{"file":"../outside.patch"}]', ValueError),
            ('[{"file":"old.patch"},{"file":"old.patch"}]', ValueError),
        ]:
            with self.subTest(index=index), tempfile.TemporaryDirectory() as root:
                (Path(root) / 'old.patch').write_text('old')
                (Path(root) / 'index.json').write_text(index)
                with self.assertRaises(error):
                    load_merged_hunk_files(root)

    def test_legacy_directory_without_index_still_loads_in_filename_order(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / '02.patch').write_text('second')
            (Path(root) / '01.patch').write_text('first')
            self.assertEqual([g['text'] for g in load_merged_hunk_files(root)], ['first', 'second'])

    def test_explicit_empty_input_never_restarts_original_hunks(self):
        for indexed in (False, True):
            with self.subTest(indexed=indexed), tempfile.TemporaryDirectory() as root:
                inputs = Path(root) / 'merged_hunks/original'
                inputs.mkdir(parents=True)
                if indexed:
                    (inputs / 'index.json').write_text('[]')
                    (inputs / 'stale.patch').write_text(SOURCE_PATCH)
                data = SimpleNamespace(new_patch='source', cumulative=False,
                    merged_hunks_input_dir=str(inputs), patch_dataset_dir=root,
                    hunk_groups_spec={'groups': [], 'excluded_hunks': [0, 1]})
                project = Mock(poc_succeeded=False)
                project._get_patch.return_value = SOURCE_PATCH
                agent = Mock()
                with patch.object(runtime, 'group_patch', wraps=group_patch) as fallback, \
                        patch.object(runtime, '_try_apply') as apply, \
                        patch.object(runtime, 'CoverageReviewer') as reviewer:
                    runtime.do_backport(agent, project, data, Mock(), str(Path(root) / 'run.log'))
                fallback.assert_not_called()
                apply.assert_not_called()
                agent.invoke.assert_not_called()
                reviewer.assert_not_called()
                self.assertFalse(project.poc_succeeded)
                self.assertFalse((Path(root) / 'complete.patch').exists())


if __name__ == '__main__':
    unittest.main()
