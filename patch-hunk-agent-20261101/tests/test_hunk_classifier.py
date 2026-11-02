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
    def test_production_guard_uses_semantic_pipeline(self):
        row = classifier.classify(item(
            'services/core/A.java',
            '@@ -1 +1 @@\n+if (pinned) return BLOCK;\n',
        ))
        self.assertEqual(row['role'], 'production_source')
        self.assertEqual(row['action'], 'SEMANTIC_PIPELINE')

    def test_behavior_test_becomes_test_obligation(self):
        row = classifier.classify(item(
            'services/tests/wmtests/A.java',
            '@@ -1 +1 @@\n+@Test public void t() { assertFalse(allowed()); }\n',
        ))
        self.assertEqual(row['role'], 'test_source')
        self.assertEqual(row['subtype'], 'behavior_test')
        self.assertEqual(row['action'], 'TEST_EQUIVALENCE_REQUIRED')

    def test_test_infrastructure_is_skipped(self):
        row = classifier.classify(item(
            'services/tests/wmtests/A.java',
            '@@ -1 +1 @@\n+import android.platform.test.flag.junit.SetFlagsRule;\n',
        ))
        self.assertEqual(row['subtype'], 'test_infrastructure')
        self.assertEqual(row['action'], 'SKIP')

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

    def test_debug_config_and_build_routes_are_distinct(self):
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
        self.assertEqual(debug['action'], 'SKIP_PRODUCTION')
        self.assertEqual(config['action'], 'CONFIG_CAPABILITY_PIPELINE')
        self.assertEqual(build['action'], 'DEPENDENCY_REVIEW_REQUIRED')

    def test_nonproduction_classification_can_bypass_summary_model(self):
        result = summary_module.deterministic_summary({
            'role': 'test_source',
            'subtype': 'behavior_test',
            'action': 'TEST_EQUIVALENCE_REQUIRED',
        })
        self.assertEqual(result['kind'], 'test')
        self.assertEqual(result['summary_mode'], 'deterministic_classification')
        self.assertIn('TEST_EQUIVALENCE_REQUIRED', result['summary'])


if __name__ == '__main__':
    unittest.main()
