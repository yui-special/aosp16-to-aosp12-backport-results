"""The pre-pipeline classifier separates production, tests and support files."""
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data_ncwang'
sys.path.insert(0, str(DATA / 'backport_patch/tools/patch-backporting/src'))

spec = importlib.util.spec_from_file_location(
    'hunk_classifier_under_test', DATA / 'hunk_classifier.py'
)
classifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(classifier)
summary_spec = importlib.util.spec_from_file_location(
    'summary_classifier_under_test', DATA / 'summarize_and_group.py'
)
summary_module = importlib.util.module_from_spec(summary_spec)
summary_spec.loader.exec_module(summary_module)


def item(path, text, idx=0):
    return {'idx': idx, 'file': path, 'text': text}


class HunkClassifierTests(unittest.TestCase):
    def test_production_guard_is_described_without_port_decision(self):
        row = classifier.classify(item(
            'services/core/A.java',
            '@@ -1 +1 @@\n+if (pinned) return BLOCK;\n',
        ))
        self.assertEqual(row['role'], 'production_source')
        self.assertEqual(row['subtype'], 'behavior_or_implementation')
        self.assertNotIn('action', row)
        self.assertNotIn('port', row)

    def test_behavior_test_is_classified_without_obligation_decision(self):
        row = classifier.classify(item(
            'services/tests/wmtests/A.java',
            '@@ -1 +1 @@\n+@Test public void t() { assertFalse(allowed()); }\n',
        ))
        self.assertEqual(row['role'], 'test_source')
        self.assertEqual(row['subtype'], 'behavior_test')
        self.assertNotIn('action', row)

    def test_test_infrastructure_is_only_described(self):
        row = classifier.classify(item(
            'services/tests/wmtests/A.java',
            '@@ -1 +1 @@\n+import android.platform.test.flag.junit.SetFlagsRule;\n',
        ))
        self.assertEqual(row['subtype'], 'test_infrastructure')
        self.assertNotIn('action', row)

    def test_new_test_file_path_is_recovered(self):
        row = classifier.classify(item(
            '(new file)',
            '--- /dev/null\n'
            '+++ b/services/tests/wmtests/src/com/android/server/wm/A.java\n'
            '@@ -0,0 +1 @@\n+@Test public void t() { assertTrue(allowed()); }\n',
        ))
        self.assertEqual(row['role'], 'test_source')
        self.assertEqual(
            row['file'],
            'services/tests/wmtests/src/com/android/server/wm/A.java',
        )

    def test_debug_config_and_build_roles_are_distinct(self):
        debug = classifier.classify(item(
            'services/core/A.java',
            '@@ -1 +1 @@\n+ sb.append("; state: ").append(value);\n',
        ))
        config = classifier.classify(item(
            'core/flags/windowing.aconfig',
            '@@ -1 +1 @@\n+name: "feature_flag"\n',
            1,
        ))
        build = classifier.classify(item(
            'services/Android.bp',
            '@@ -1 +1 @@\n+ static_libs: ["lib"],\n',
            2,
        ))
        self.assertEqual(debug['role'], 'production_source')
        self.assertEqual(debug['subtype'], 'debug_output')
        self.assertEqual(config['role'], 'config_capability')
        self.assertEqual(config['subtype'], 'config_declaration')
        self.assertEqual(build['role'], 'build_system')
        self.assertEqual(build['subtype'], 'build_rule')
        self.assertNotIn('action', debug)
        self.assertNotIn('action', config)
        self.assertNotIn('action', build)

    def test_nonproduction_classification_can_bypass_summary_model(self):
        result = summary_module.deterministic_summary({
            'role': 'test_source',
            'subtype': 'behavior_test',
        })
        self.assertEqual(result['kind'], 'test')
        self.assertEqual(result['summary_mode'], 'deterministic_classification')
        self.assertIn('test_source/behavior_test', result['summary'])


if __name__ == '__main__':
    unittest.main()
