"""Triage must separate production patch work from test/debug obligations."""
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data_ncwang'
sys.path.insert(0, str(DATA))
sys.path.insert(0, str(DATA / 'backport_patch/tools/patch-backporting/src'))

spec = importlib.util.spec_from_file_location('triage_roles_under_test', DATA / 'triage.py')
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)


def hunk(path, text, idx=0):
    return {'hunk': idx, 'file': path, 'text': text}


def summary(text, idx=0):
    return {
        'hunk': idx,
        'summary': text,
        'kind': 'logic',
        'symbol': '',
        'det_enclosing': '',
        'introduced': [],
        'semantic_contract': {},
    }


class TriageRoleTests(unittest.TestCase):
    def test_mapping_evidence_sent_to_triage_is_compact(self):
        evidence = triage.compact_mapping_evidence({
            'mapping_type': 'split',
            'mappings': [
                {'path': 'A.java', 'symbol': 'one'},
                {'path': 'B.java', 'symbol': 'two'},
                {'path': 'C.java', 'symbol': 'three'},
                {'path': 'D.java', 'symbol': 'four'},
            ],
            'reason': 'mapped across existing target methods',
            'candidates': [['large candidate excerpt should not leak']],
        })

        self.assertEqual(evidence['status'], 'MAPPED')
        self.assertEqual(evidence['target_files'], ['A.java', 'B.java', 'C.java'])
        self.assertEqual(evidence['target_symbols'], ['one', 'two', 'three'])
        self.assertNotIn('mappings', evidence)
        self.assertNotIn('candidates', evidence)

    def test_unresolved_mapping_is_not_reported_as_absent(self):
        evidence = triage.compact_mapping_evidence({
            'mapping_type': 'unresolved',
            'mappings': [],
            'reason': 'no confirmed target counterpart',
        })
        self.assertEqual(evidence['status'], 'UNRESOLVED')

    def test_skipped_multi_symbol_provider_is_restored(self):
        rows = [
            {'hunk': 11, 'port': False, 'category': 'unrelated',
             'reason': 'visibility refactor'},
            {'hunk': 7, 'port': True, 'category': 'fix',
             'reason': 'security behavior'},
        ]
        hunks = [
            {'idx': 11,
             'text': '+boolean hasVisibleNotPinnedActivity(int uid) { return true; }\n'},
            {'idx': 7, 'text': '+tracker.hasVisibleNotPinnedActivity(uid);\n'},
        ]
        summaries = [
            {'hunk': 11, 'kind': 'implement', 'symbol': 'hasVisibleActivity',
             'source_structure': {'after': {'new_declarations': [{
                 'name': 'hasVisibleNotPinnedActivity'
             }]}}, 'semantic_contract': {}},
            {'hunk': 7, 'kind': 'call', 'symbol': 'shouldAbort',
             'source_structure': {}, 'semantic_contract': {}},
        ]

        triage._closure_once(rows, hunks, summaries)

        self.assertIs(rows[0]['port'], True)
        self.assertEqual(rows[0]['triage_state'], 'port_required')
        self.assertIn('被 hunk 7 依赖', rows[0]['reason'])

    def test_ambiguous_skipped_providers_remain_false(self):
        rows = [
            {'hunk': 3, 'port': False, 'category': 'unrelated', 'reason': 'a'},
            {'hunk': 11, 'port': False, 'category': 'unrelated', 'reason': 'b'},
            {'hunk': 7, 'port': True, 'category': 'fix', 'reason': 'guard'},
        ]
        hunks = [
            {'idx': 3,
             'text': '+void hasVisibleNotPinnedActivity(int uid) {}\n'},
            {'idx': 11,
             'text': '+void hasVisibleNotPinnedActivity(long uid) {}\n'},
            {'idx': 7, 'text': '+tracker.hasVisibleNotPinnedActivity(uid);\n'},
        ]
        summaries = [
            {'hunk': 3, 'kind': 'implement', 'symbol': '', 'source_structure': {}},
            {'hunk': 11, 'kind': 'implement', 'symbol': '', 'source_structure': {}},
            {'hunk': 7, 'kind': 'call', 'symbol': 'shouldAbort',
             'source_structure': {}},
        ]

        triage._closure_once(rows, hunks, summaries)

        self.assertIs(rows[0]['port'], False)
        self.assertIs(rows[1]['port'], False)
        self.assertEqual(rows[0]['triage_state'], 'dependency_ambiguous')
        self.assertEqual(rows[1]['triage_state'], 'dependency_ambiguous')

    def test_behavior_test_is_obligation_not_production_port(self):
        row = {'hunk': 0, 'port': False, 'category': 'test', 'reason': 'security test'}
        item = hunk('services/tests/wmtests/A.java', (
            '@@ -1 +1 @@\n'
            '+ if (pinned) { assertThat(verdict).isEqualTo(BLOCK); }\n'
        ))
        triage.apply_nonproduction_policy(row, item, summary('pinned activity is blocked'))

        self.assertFalse(row['port'])
        self.assertEqual(row['triage_state'], 'test_equivalence_required')
        self.assertEqual(row['test_obligation']['status'], 'TEST_EQUIVALENCE_REQUIRED')
        self.assertTrue(row['nonproduction_locked'])

    def test_test_infrastructure_is_skipped_without_obligation(self):
        row = {'hunk': 0, 'port': False, 'category': 'test', 'reason': 'rule import'}
        item = hunk('services/tests/wmtests/A.java', (
            '@@ -1 +1 @@\n+import android.platform.test.flag.junit.SetFlagsRule;\n'
        ))
        triage.apply_nonproduction_policy(row, item, summary('add test rule import'))

        self.assertFalse(row['port'])
        self.assertEqual(row['triage_state'], 'test_infrastructure_only')
        self.assertEqual(row['test_obligation']['status'], 'NONE')

    def test_new_test_file_uses_path_from_patch_header(self):
        row = {'hunk': 0, 'port': False, 'category': 'test', 'reason': 'new test'}
        item = hunk('(new file)', (
            '--- /dev/null\n'
            '+++ b/services/tests/wmtests/src/com/android/server/wm/VisibleActivityProcessTrackerTests.java\n'
            '@@ -0,0 +1 @@\n'
            '+ @Test public void pinnedWindowIsRejected() { assertFalse(allowed()); }\n'
        ))
        triage.apply_nonproduction_policy(row, item, summary('tracker security test'))

        self.assertEqual(
            row['file'],
            'services/tests/wmtests/src/com/android/server/wm/VisibleActivityProcessTrackerTests.java',
        )
        self.assertFalse(row['port'])
        self.assertEqual(row['test_obligation']['status'], 'TEST_EQUIVALENCE_REQUIRED')

    def test_test_role_does_not_override_triage_port_decision(self):
        row = {'hunk': 0, 'port': True, 'category': 'fix', 'reason': 'model says port'}
        item = hunk('services/tests/wmtests/A.java', '@@ -1 +1 @@\n+assertFalse(x);\n')
        triage.apply_nonproduction_policy(
            row,
            item,
            summary('test'),
            {
                'hunk': 0,
                'role': 'test_source',
                'subtype': 'behavior_test',
                'confidence': 'HIGH',
                'evidence': ['path/test content indicates test source'],
            },
        )
        self.assertTrue(row['port'])
        self.assertEqual(row['classification']['role'], 'test_source')
        self.assertEqual(row['test_obligation']['status'], 'TEST_EQUIVALENCE_REQUIRED')
        self.assertEqual(row['triage_state'], 'test_port_required')

    def test_only_confirmed_absence_allows_deterministic_test_skip(self):
        classification = {'role': 'test_source', 'subtype': 'behavior_test'}
        self.assertTrue(triage.can_decide_without_triage_model(
            classification, {'status': 'NOT_APPLICABLE'}
        ))
        self.assertFalse(triage.can_decide_without_triage_model(
            classification, {'status': 'MAPPED'}
        ))
        self.assertFalse(triage.can_decide_without_triage_model(
            classification, {'status': 'UNRESOLVED'}
        ))

    def test_triage_owns_deterministic_nonproduction_decisions(self):
        test_decision = triage.deterministic_classification_decision(
            hunk('services/tests/A.java', '', 3),
            {
                'hunk': 3,
                'role': 'test_source',
                'subtype': 'behavior_test',
            },
        )
        metadata_decision = triage.deterministic_classification_decision(
            hunk('README.md', '', 4),
            {
                'hunk': 4,
                'role': 'metadata',
                'subtype': 'document_or_ownership',
            },
        )
        self.assertFalse(test_decision['port'])
        self.assertEqual(test_decision['category'], 'test')
        self.assertFalse(metadata_decision['port'])
        self.assertEqual(metadata_decision['category'], 'metadata')

    def test_expected_debug_string_is_optional_test_obligation(self):
        row = {'hunk': 0, 'port': False, 'category': 'test', 'reason': 'expected text'}
        item = hunk('services/tests/wmtests/A.java', (
            '@@ -1 +1 @@\n'
            '+ + "realCallingUidHasVisibleNotPinnedActivity: false; "\n'
        ))
        triage.apply_nonproduction_policy(row, item, summary('update expected debug string'))

        self.assertFalse(row['port'])
        self.assertEqual(row['triage_state'], 'test_debug_optional')
        self.assertEqual(row['test_obligation']['status'], 'OPTIONAL')

    def test_debug_output_marker_does_not_override_triage(self):
        accepted_debug = {
            'hunk': 0, 'port': True, 'category': 'fix', 'reason': 'triage accepts obligation'
        }
        debug_hunk = hunk('services/core/A.java', (
            '@@ -1 +1 @@\n'
            '+ sb.append("; callingUidHasVisibleNotPinnedActivity: ").append(value);\n'
        ))
        triage.apply_nonproduction_policy(
            accepted_debug,
            debug_hunk,
            summary('add field to debug dump'),
            {
                'hunk': 0,
                'role': 'production_source',
                'subtype': 'debug_output',
                'confidence': 'HIGH',
                'evidence': ['changed lines are debug output only'],
            },
        )
        self.assertTrue(accepted_debug['port'])
        self.assertNotEqual(accepted_debug.get('triage_state'), 'debug_output_only')

        rejected_debug = {
            'hunk': 0, 'port': False, 'category': 'metadata', 'reason': 'triage rejects output'
        }
        triage.apply_nonproduction_policy(
            rejected_debug,
            debug_hunk,
            summary('add field to debug dump'),
            {
                'hunk': 0,
                'role': 'production_source',
                'subtype': 'debug_output',
                'confidence': 'HIGH',
                'evidence': ['changed lines are debug output only'],
            },
        )
        self.assertFalse(rejected_debug['port'])
        self.assertEqual(rejected_debug['triage_state'], 'debug_output_only')

        logic_row = {'hunk': 1, 'port': True, 'category': 'fix', 'reason': 'guard'}
        logic_hunk = hunk('services/core/A.java', (
            '@@ -1 +1 @@\n'
            '+ if (appSwitchAllowed && hasVisibleNotPinnedActivity) return allow;\n'
        ), idx=1)
        triage.apply_nonproduction_policy(logic_row, logic_hunk, summary('tighten BAL guard'))
        self.assertTrue(logic_row['port'])
        self.assertNotEqual(logic_row.get('triage_state'), 'debug_output_only')

    def test_log_word_in_summary_does_not_make_signature_debug_output(self):
        row = {'hunk': 2, 'port': True, 'category': 'fix', 'reason': 'signature'}
        signature = hunk('services/core/A.java', (
            '@@ -1 +1 @@\n'
            '+ void resetRuntimePermissions(String pkg, int userId, boolean restore);\n'
        ), idx=2)
        context = summary('no explicit log; exception and log paths are unchanged', 2)
        triage.apply_nonproduction_policy(
            row,
            signature,
            context,
            {
                'hunk': 2,
                'role': 'production_source',
                'subtype': 'behavior_or_implementation',
                'confidence': 'HIGH',
                'evidence': ['source path requires semantic review'],
            },
        )
        self.assertTrue(row['port'])
        self.assertNotEqual(row.get('triage_state'), 'debug_output_only')

        fallback_row = {'hunk': 2, 'port': True, 'category': 'fix', 'reason': 'signature'}
        triage.apply_nonproduction_policy(fallback_row, signature, context)
        self.assertTrue(fallback_row['port'])
        self.assertNotEqual(fallback_row.get('triage_state'), 'debug_output_only')

    def test_postprocess_keeps_test_hunks_out_of_active_set(self):
        hunks = [
            hunk('services/tests/wmtests/A.java',
                 '@@ -1 +1 @@\n+assertThat(value).isEqualTo(false);\n', 0),
            hunk('services/core/A.java',
                 '@@ -1 +1 @@\n+if (pinned) return block;\n', 1),
            hunk('services/tests/wmtests/A.java',
                 '@@ -1 +1 @@\n+ + "debugField: false; "\n', 2),
        ]
        summaries = [
            summary('verify security behavior', 0),
            summary('tighten security guard', 1),
            summary('update expected debug string', 2),
        ]
        rows = [
            {'hunk': 0, 'port': False, 'category': 'test', 'reason': 'test'},
            {'hunk': 1, 'port': True, 'category': 'fix', 'reason': 'guard'},
            {'hunk': 2, 'port': False, 'category': 'test', 'reason': 'debug'},
        ]

        triage.postprocess(rows, hunks, summaries, '', '')

        self.assertFalse(rows[0]['port'])
        self.assertTrue(rows[1]['port'])
        self.assertFalse(rows[2]['port'])


if __name__ == '__main__':
    unittest.main()
