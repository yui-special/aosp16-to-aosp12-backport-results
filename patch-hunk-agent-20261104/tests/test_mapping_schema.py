"""Mapping schema accepts architecture-level resolutions without weakening evidence."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'
MAPPER = ROOT / 'data_ncwang/backport_patch/cases/CVE-2025-48649/scripts/map_semantics.py'
EXPORTER = MAPPER.with_name('build_v6_export.py')
exporter_spec = importlib.util.spec_from_file_location(
    'mapping_exporter_under_test', EXPORTER
)
exporter = importlib.util.module_from_spec(exporter_spec)
exporter_spec.loader.exec_module(exporter)
sys.path.insert(0, str(SRC))

spec = importlib.util.spec_from_file_location('mapping_schema_under_test', MAPPER)
mapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mapper)


class MappingSchemaTests(unittest.TestCase):
    def test_hunk_mapping_runs_once_for_every_hunk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch_path = root / 'input.patch'
            summaries = root / 'summaries.json'
            output = root / 'hunk-mapping.json'
            patch_path.write_text('fixture', encoding='utf-8')
            summaries.write_text(json.dumps([
                {'hunk': 0, 'file': 'A.java', 'summary': 'production'},
                {'hunk': 1, 'file': 'tests/A.java', 'summary': 'test'},
            ]), encoding='utf-8')
            hunks = [
                {'idx': 0, 'file': 'A.java', 'text': '+guard'},
                {'idx': 1, 'file': 'tests/A.java', 'text': '+assert'},
            ]

            def direct_for(_repo, _ref, hunk, _summary):
                return {
                    'hunk': hunk['idx'],
                    'mapping_type': 'direct',
                    'mappings': [{
                        'path': hunk['file'],
                        'symbol': 'guard',
                        'evidence': 'same target method',
                    }],
                    'reason': 'direct target',
                    'candidates': [],
                }

            with patch.object(sys, 'argv', [
                str(MAPPER), '--hunks', 'repo', 'ref', str(patch_path),
                str(summaries), str(output),
            ]), patch.object(mapper, 'split_patch_indexed', return_value=('', hunks)), \
                    patch.object(mapper, 'direct_mapping', side_effect=direct_for) as direct_call:
                mapper.map_hunks_main()

            result = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(direct_call.call_count, 2)
            self.assertEqual(result['hunks'][0]['mapping_type'], 'direct')
            self.assertEqual(result['hunks'][1]['mapping_type'], 'direct')

    def test_group_projection_reuses_hunk_mapping_without_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch_path = root / 'input.patch'
            summaries = root / 'summaries.json'
            groups = root / 'groups.json'
            triage = root / 'triaged.json'
            hunk_mapping = root / 'hunk-mapping.json'
            output = root / 'semantic-mapping.json'
            patch_path.write_text('fixture', encoding='utf-8')
            summaries.write_text(json.dumps([
                {'hunk': 0}, {'hunk': 1},
            ]), encoding='utf-8')
            groups.write_text(json.dumps({
                'groups': [[0], [1]], 'config_capability_relations': [],
            }), encoding='utf-8')
            triage.write_text(json.dumps([
                {'hunk': 0, 'port': True},
                {'hunk': 1, 'port': False},
            ]), encoding='utf-8')
            direct = {
                'hunk': 0,
                'mapping_type': 'direct',
                'mappings': [{
                    'path': 'A.java', 'symbol': 'guard', 'evidence': 'same method',
                }],
                'reason': 'direct',
                'candidates': [],
            }
            skipped = {
                'hunk': 1,
                'mapping_type': 'not_applicable',
                'mappings': [],
                'reason': 'test source',
                'candidates': [],
            }
            hunk_mapping.write_text(json.dumps({
                'source_patch_sha256': 'x',
                'model': 'fixture',
                'hunks': [direct, skipped],
            }), encoding='utf-8')
            with patch.object(sys, 'argv', [
                str(MAPPER), '--project', 'repo', 'ref', str(patch_path),
                str(summaries), str(groups), str(triage),
                str(hunk_mapping), str(output),
            ]), patch.object(mapper, 'split_patch_indexed', return_value=('', [
                {'idx': 0, 'file': 'A.java'},
                {'idx': 1, 'file': 'tests/A.java'},
            ])), patch.object(mapper, 'git_exists', return_value=False):
                mapper.project_group_mapping_main()

            result = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(len(result['groups']), 1)
            self.assertEqual(result['groups'][0]['hunks'], [0])
            self.assertEqual(
                result['groups'][0]['hunk_mappings'][0]['mapping_type'], 'direct'
            )

    def test_create_method_allows_new_symbol_in_existing_file(self):
        candidates = {9: [(4, 'ActivityRecord.java', ['ActivityRecord'], [])]}
        response = {
            'hunk_mappings': [{
                'hunk': 9,
                'mapping_type': 'create_method',
                'mappings': [{
                    'path': 'ActivityRecord.java',
                    'symbol': 'deferStartingWindowRemovalForKeyguardUnoccluding',
                    'evidence': 'ActivityRecord owns starting-window lifecycle',
                }],
                'accessor_rewrites': [{
                    'source': 'mTransitionController.inTransition(this)',
                    'target': 'mAtmService.getTransitionController().inTransition(this)',
                }],
                'required_guards': ['mStartingData != null'],
                'reason': 'target needs the lifecycle method added',
            }]
        }
        with patch.object(mapper, 'git_exists', return_value=True), \
                patch.object(mapper, 'symbol_in_text', return_value=False):
            rows, errors = mapper.normalize_group_response(
                response, [9], candidates, 'repo', 'ref', hunks=None
            )

        self.assertEqual(errors, [])
        self.assertEqual(rows[0]['mapping_type'], 'create_method')
        self.assertEqual(rows[0]['mappings'][0]['symbol'],
                         'deferStartingWindowRemovalForKeyguardUnoccluding')
        self.assertEqual(rows[0]['required_guards'], ['mStartingData != null'])

    def test_inline_true_requires_a_capability_resolution(self):
        candidates = {0: []}
        response = {
            'hunk_mappings': [{
                'hunk': 0,
                'mapping_type': 'inline_true',
                'reason': 'flag is fixed on in target',
            }]
        }
        rows, _errors = mapper.normalize_group_response(
            response, [0], candidates, 'repo', 'ref', hunks=None
        )
        self.assertEqual(rows[0]['mapping_type'], 'unresolved')

        response['hunk_mappings'][0]['capability_resolutions'] = [{
            'capability': 'keepAppWindowHideWhileLocked',
            'resolution': 'INLINE_TRUE',
            'evidence': 'target behavior is unconditional',
        }]
        rows, errors = mapper.normalize_group_response(
            response, [0], candidates, 'repo', 'ref', hunks=None
        )
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]['mapping_type'], 'inline_true')

    def test_inline_true_has_precedence_but_consumer_obligation_remains(self):
        rows = [{
            'hunk': 0,
            'mapping_type': 'requires_prerequisite',
            'mappings': [],
            'required_prerequisites': ['create windowing_frontend.aconfig'],
            'required_guards': [],
            'reason': 'flag absent',
        }, {
            'hunk': 9,
            'mapping_type': 'requires_prerequisite',
            'mappings': [{'path': 'WindowState.java'}],
            'required_prerequisites': [
                'Flags.keepAppWindowHideWhileLocked accessor is missing',
                'ActivityRecord.deferStartingWindowRemovalForKeyguarding is missing',
            ],
            'required_guards': [
                'Flags.keepAppWindowHideWhileLocked() && wasShowWhenLocked',
            ],
            'reason': 'consumer',
        }]
        spec = {'config_capability_relations': [{
            'id': 'config:keepappwindowhidewhilelocked',
            'provider': 0,
            'consumer': 9,
            'declaration': 'keep_app_window_hide_while_locked',
            'accessor': 'keepAppWindowHideWhileLocked',
            'resolution': 'INLINE_TRUE',
            'evidence': 'fixed read only',
        }]}
        rows = mapper.apply_config_capability_precedence(rows, spec)

        self.assertEqual(rows[0]['mapping_type'], 'inline_true')
        self.assertEqual(rows[0]['required_prerequisites'], [])
        self.assertEqual(rows[1]['mapping_type'], 'requires_prerequisite')
        self.assertEqual(len(rows[1]['required_prerequisites']), 1)
        self.assertIn('deferStartingWindowRemoval', rows[1]['required_prerequisites'][0])
        self.assertEqual(rows[1]['required_guards'], [])

    def test_requires_prerequisite_must_include_existing_main_target(self):
        rows = [{
            'hunk': 9,
            'mapping_type': 'requires_prerequisite',
            'mappings': [{'path': 'services/ActivityRecord.java'}],
        }]
        hunks = {9: {'file': 'services/WindowState.java'}}
        with patch.object(mapper, 'git_exists', return_value=True):
            errors = mapper.mapping_completeness_errors(
                rows, hunks, 'repo', 'ref', {'config_capability_relations': []}
            )
        self.assertTrue(errors)
        self.assertIn('omits existing main target file services/WindowState.java',
                      errors[0])

    def test_exporter_also_enforces_inline_true_precedence(self):
        mapping = {
            0: {
                'hunk': 0,
                'mapping_type': 'requires_prerequisite',
                'mappings': [],
                'required_prerequisites': ['create aconfig'],
            },
            9: {
                'hunk': 9,
                'mapping_type': 'requires_prerequisite',
                'mappings': [{'path': 'WindowState.java'}],
                'required_prerequisites': [
                    'Flags.keepAppWindowHideWhileLocked missing',
                    'deferStartingWindowRemoval method missing',
                ],
                'required_guards': [],
            },
        }
        spec_data = {'config_capability_relations': [{
            'id': 'config:keepappwindowhidewhilelocked',
            'provider': 0,
            'consumer': 9,
            'declaration': 'keep_app_window_hide_while_locked',
            'accessor': 'keepAppWindowHideWhileLocked',
            'resolution': 'INLINE_TRUE',
            'evidence': 'fixed read only',
        }]}
        result = exporter.apply_capability_precedence(mapping, spec_data)
        self.assertEqual(result[0]['mapping_type'], 'inline_true')
        self.assertEqual(result[0]['required_prerequisites'], [])
        self.assertEqual(len(result[9]['required_prerequisites']), 1)
        self.assertIn('deferStartingWindowRemoval',
                      result[9]['required_prerequisites'][0])

    def test_export_adds_only_compact_architecture_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch = (
                'diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n'
                '@@ -1 +1 @@\n-old\n+new\n'
            )
            (root / 'input.patch').write_text(patch, encoding='utf-8')
            group = {
                'hunks': [0],
                'goal': 'create target lifecycle method',
                'why_together': 'one method',
                'split_review': 'atomic',
                'depends_on': [],
            }
            spec = {
                'groups': [[0]],
                'group_details': [group],
                'hunks': [{'hunk': 0, 'file': 'A.java', 'summary': 'fixture'}],
            }
            (root / 'groups.json').write_text(json.dumps(spec), encoding='utf-8')
            mapping = {'groups': [{'hunk_mappings': [{
                'hunk': 0,
                'mapping_type': 'create_method',
                'mappings': [{
                    'path': 'A.java',
                    'symbol': 'newLifecycleMethod',
                    'evidence': 'A owns the lifecycle',
                }],
                'accessor_rewrites': [{
                    'source': 'oldAccessor', 'target': 'newAccessor',
                }],
                'required_guards': ['field != null'],
                'capability_resolutions': [{
                    'capability': 'featureFlag',
                    'resolution': 'INLINE_TRUE',
                }],
                'reason': 'target needs the method',
            }]}]}
            (root / 'mapping.json').write_text(json.dumps(mapping), encoding='utf-8')
            result = subprocess.run([
                sys.executable, '-B', str(EXPORTER), str(root / 'input.patch'),
                'unused-repo', 'unused-ref', str(root / 'groups.json'),
                str(root / 'export'), str(root / 'mapping.json'),
            ], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            index = json.loads(
                (root / 'export/merged_hunks/original/index.json').read_text(encoding='utf-8')
            )
            hints = index[0]['hints']
            self.assertIn('CREATE_METHOD', hints)
            self.assertIn('newLifecycleMethod', hints)
            self.assertIn('oldAccessor -> newAccessor', hints)


if __name__ == '__main__':
    unittest.main()
