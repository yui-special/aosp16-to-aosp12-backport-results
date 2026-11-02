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
    def test_behavior_test_is_obligation_not_production_port(self):
        row = {'hunk': 0, 'port': True, 'category': 'fix', 'reason': 'security test'}
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
        row = {'hunk': 0, 'port': True, 'category': 'fix', 'reason': 'rule import'}
        item = hunk('services/tests/wmtests/A.java', (
            '@@ -1 +1 @@\n+import android.platform.test.flag.junit.SetFlagsRule;\n'
        ))
        triage.apply_nonproduction_policy(row, item, summary('add test rule import'))

        self.assertFalse(row['port'])
        self.assertEqual(row['triage_state'], 'test_infrastructure_only')
        self.assertEqual(row['test_obligation']['status'], 'NONE')

    def test_new_test_file_uses_path_from_patch_header(self):
        row = {'hunk': 0, 'port': True, 'category': 'fix', 'reason': 'new test'}
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

    def test_preclassification_overrides_model_test_port_decision(self):
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
                'action': 'TEST_EQUIVALENCE_REQUIRED',
                'confidence': 'HIGH',
                'evidence': ['path/test content indicates test source'],
            },
        )
        self.assertFalse(row['port'])
        self.assertEqual(row['classification']['role'], 'test_source')
        self.assertEqual(row['test_obligation']['status'], 'TEST_EQUIVALENCE_REQUIRED')

    def test_classifier_can_skip_triage_model_for_test_and_debug(self):
        test_decision = triage.deterministic_classification_decision(
            hunk('services/tests/A.java', '', 3),
            {
                'hunk': 3,
                'role': 'test_source',
                'subtype': 'behavior_test',
                'action': 'TEST_EQUIVALENCE_REQUIRED',
            },
        )
        debug_decision = triage.deterministic_classification_decision(
            hunk('services/core/A.java', '', 4),
            {
                'hunk': 4,
                'role': 'production_source',
                'subtype': 'debug_only',
                'action': 'SKIP_PRODUCTION',
            },
        )
        self.assertFalse(test_decision['port'])
        self.assertEqual(test_decision['category'], 'test')
        self.assertFalse(debug_decision['port'])
        self.assertEqual(debug_decision['category'], 'metadata')

    def test_expected_debug_string_is_optional_test_obligation(self):
        row = {'hunk': 0, 'port': True, 'category': 'fix', 'reason': 'expected text'}
        item = hunk('services/tests/wmtests/A.java', (
            '@@ -1 +1 @@\n'
            '+ + "realCallingUidHasVisibleNotPinnedActivity: false; "\n'
        ))
        triage.apply_nonproduction_policy(row, item, summary('update expected debug string'))

        self.assertFalse(row['port'])
        self.assertEqual(row['triage_state'], 'test_debug_optional')
        self.assertEqual(row['test_obligation']['status'], 'OPTIONAL')

    def test_production_debug_dump_is_skipped_but_security_logic_is_not(self):
        debug_row = {'hunk': 0, 'port': True, 'category': 'fix', 'reason': 'dump'}
        debug_hunk = hunk('services/core/A.java', (
            '@@ -1 +1 @@\n'
            '+ sb.append("; callingUidHasVisibleNotPinnedActivity: ").append(value);\n'
        ))
        triage.apply_nonproduction_policy(
            debug_row, debug_hunk, summary('add field to debug dump')
        )
        self.assertFalse(debug_row['port'])
        self.assertEqual(debug_row['triage_state'], 'debug_only')

        logic_row = {'hunk': 1, 'port': True, 'category': 'fix', 'reason': 'guard'}
        logic_hunk = hunk('services/core/A.java', (
            '@@ -1 +1 @@\n'
            '+ if (appSwitchAllowed && hasVisibleNotPinnedActivity) return allow;\n'
        ), idx=1)
        triage.apply_nonproduction_policy(logic_row, logic_hunk, summary('tighten BAL guard'))
        self.assertTrue(logic_row['port'])
        self.assertNotEqual(logic_row.get('triage_state'), 'debug_only')

    def test_log_word_in_summary_does_not_make_signature_debug_only(self):
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
                'action': 'SEMANTIC_PIPELINE',
                'confidence': 'HIGH',
                'evidence': ['source path requires semantic review'],
            },
        )
        self.assertTrue(row['port'])
        self.assertNotEqual(row.get('triage_state'), 'debug_only')

        fallback_row = {'hunk': 2, 'port': True, 'category': 'fix', 'reason': 'signature'}
        triage.apply_nonproduction_policy(fallback_row, signature, context)
        self.assertTrue(fallback_row['port'])
        self.assertNotEqual(fallback_row.get('triage_state'), 'debug_only')

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
            {'hunk': 0, 'port': True, 'category': 'fix', 'reason': 'test'},
            {'hunk': 1, 'port': True, 'category': 'fix', 'reason': 'guard'},
            {'hunk': 2, 'port': True, 'category': 'fix', 'reason': 'debug'},
        ]

        triage.postprocess(rows, hunks, summaries, '', '')

        self.assertFalse(rows[0]['port'])
        self.assertTrue(rows[1]['port'])
        self.assertFalse(rows[2]['port'])


if __name__ == '__main__':
    unittest.main()
