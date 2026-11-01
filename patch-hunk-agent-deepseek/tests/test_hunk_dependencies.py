"""Dependency closure must generalize beyond method names and one patch corpus."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'
sys.path.insert(0, str(SRC))
from tools.source_structure import Source
from tools.hunk_dependencies import (attach_reviews, review_dependencies, review_inputs,
                                     syntax_prerequisites, validate_review)

spec = importlib.util.spec_from_file_location('triage_dependencies_test', ROOT/'data_ncwang/triage.py')
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)


def fixture(source, lines):
    records, hunks = [], []
    for h, line in enumerate(lines):
        records.append({'hunk': h, 'file': source.path, 'symbol': '', 'source_structure': {
            'before_ref': 'parent', 'after_ref': 'commit',
            'before': {'status': 'ok', 'scopes': []},
            'after': source.evidence([line], line, 1)}})
        code = source.data.decode().splitlines()[line-1]
        hunks.append({'idx': h, 'file': source.path, 'text': '@@ -0,0 +%d @@\n+%s' % (line, code)})
    return records, hunks


class DependencyTests(unittest.TestCase):
    def test_import_prerequisite_survives_renaming_but_not_unused_imports(self):
        for imported in ('Verifier', 'RenamedParser', 'X'):
            with self.subTest(imported=imported):
                source = Source('Example.java', ('''import pkg.%s;
import pkg.Unused;
class Example {
 void run() {
  %s.check();
 }
}''' % (imported, imported)).encode())
                records, hunks = fixture(source, [1, 2, 5])
                rows = [{'hunk': i, 'port': i == 2, 'reason': 'initial'} for i in range(3)]
                triage.postprocess(rows, hunks, records, '', '')
                self.assertEqual([r['port'] for r in rows], [True, False, True])
                self.assertEqual([(e['provider'], e['consumer']) for e in syntax_prerequisites(records)], [(0, 2)])
                reviews = {h: {'hunk': h, 'reason': 'No extra prerequisites', 'requires': []}
                           for h in range(3)}
                triage.postprocess(rows, hunks, records, '', '', reviews)
                saved = copy.deepcopy(rows)
                replay = {r['hunk']: r['dependency_review'] for r in rows}
                triage.postprocess(rows, hunks, records, '', '', replay)
                # Cached syntax edges must not accumulate duplicate explanations.
                self.assertEqual(rows, saved)

    def test_import_name_shadowing_comments_literals_and_qualification_are_not_proof(self):
        for declaration, statement in [
                ('Object Verifier;', 'Verifier.check();'),
                ('', 'String text = "Verifier.check()";'),
                ('', '// Verifier.check();'),
                ('', 'other.Verifier.check();')]:
            with self.subTest(statement=statement):
                source = Source('Example.java', ('''import pkg.Verifier;
class Example {
 void run() {
  %s
  %s
 }
}''' % (declaration, statement)).encode())
                records, _ = fixture(source, [1, 5])
                self.assertEqual(syntax_prerequisites(records), [])

    def test_same_import_identifier_in_another_file_does_not_create_an_edge(self):
        source = Source('One.java', b'import pkg.Verifier;\nclass One { void f() { Verifier.check(); } }')
        records, _ = fixture(source, [1, 2])
        records[1]['source_structure']['after']['path'] = 'Other.java'
        self.assertEqual(syntax_prerequisites(records), [])

    def test_changed_assertion_keeps_receiver_on_unchanged_line(self):
        source = Source('ExampleTest.java', b'''class ExampleTest {
 void testOutput() {
  assertThat(value.render())
    .contains("ready: false");
 }
}''')
        evidence = source.evidence([4], 4, 1)
        self.assertIn('value.render()', evidence['statements'][0]['text'])
        self.assertIn('ready: false', evidence['statements'][0]['text'])

    def test_same_summary_symbols_and_inferred_symbols_never_promote(self):
        for symbol in ('append', 'toString', 'sharedHelper', ''):
            with self.subTest(symbol=symbol):
                rows = [{'hunk': 0, 'port': False}, {'hunk': 1, 'port': True}]
                summaries = [{'hunk': h, 'symbol': symbol, 'kind': 'declare',
                              'symbol_inferred': symbol} for h in range(2)]
                triage.postprocess(rows, [{'text': '+'+symbol}]*2, summaries, '', '')
                self.assertIs(rows[0]['port'], False)

    def test_long_dependency_chain_and_replay_have_no_depth_limit_or_prose_growth(self):
        count = 12
        source = Source('Example.java', b'class Example {}')
        records, hunks = fixture(source, [1]*count)
        rows = [{'hunk': h, 'port': h == count-1} for h in range(count)]
        reviews = {h: {'hunk': h, 'reason': 'fixture', 'requires': [] if h == 0 else [
            {'provider': h-1, 'reason': 'previous prerequisite'}]} for h in range(count)}
        triage.postprocess(rows, hunks, records, '', '', reviews)
        self.assertTrue(all(r['port'] for r in rows))
        first = copy.deepcopy(rows)
        attach_reviews(records, hunks, rows)
        triage.postprocess(rows, hunks, records, '', '', reviews)
        self.assertEqual(rows, first)

    def test_unknown_decision_cannot_be_repaired_by_a_dependency_edge(self):
        records = [{'hunk': 0}, {'hunk': 1}]
        rows = [{'hunk': 0, 'port': None}, {'hunk': 1, 'port': True}]
        reviews = {0: {'requires': []}, 1: {'requires': [{'provider': 0, 'reason': 'needed'}]}}
        triage.postprocess(rows, [{'text': '+change'}]*2, records, '', '', reviews)
        self.assertIsNone(rows[0]['port'])
        with self.assertRaisesRegex(ValueError, 'missing its reviewed prerequisite'):
            attach_reviews(records, [{'text': '+change'}]*2, rows)

    def test_review_uses_all_hunks_and_exact_two_sided_changed_code(self):
        source = Source('Example.java', b'''class Example {
 String render() { return "ready: false"; }
 void testOutput() { assertThat(value.render()).contains("ready: false"); }
}''')
        records, hunks = fixture(source, [2, 3])
        rows = [{'hunk': 0, 'port': False}, {'hunk': 1, 'port': True}]
        answer = {'reviews': [
            {'hunk': 0, 'reason': 'No prerequisites', 'requires': []},
            {'hunk': 1, 'reason': 'Asserts new output', 'requires': [{
                'provider': 0, 'reason': 'render must emit the asserted text',
                'provider_evidence': {'side': 'after', 'quote': source.data.decode().splitlines()[1]},
                'consumer_evidence': {'side': 'after', 'quote': source.data.decode().splitlines()[2]}}]}]}
        ask = Mock(return_value=answer)
        reviews = review_dependencies(rows, hunks, records, ask)
        prompt = ask.call_args.args[0]
        self.assertIn('"initial_port": false', prompt)
        triage.postprocess(rows, hunks, records, '', '', reviews)
        self.assertTrue(rows[0]['port'])
        attach_reviews(records, hunks, rows)
        records[0]['source_structure']['after_ref'] = 'different commit'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            attach_reviews(records, hunks, rows)
        # Similar output text is not an acceptable quote from the provider.
        bad = copy.deepcopy(answer)
        bad['reviews'][1]['requires'][0]['provider_evidence']['quote'] = 'String render() { return "otherReady: false"; }'
        with self.assertRaisesRegex(ValueError, 'quote must match'):
            validate_review(bad, review_inputs(rows, hunks, records))
        for invalid in ({'reviews': answer['reviews'][1:]}, {'reviews': answer['reviews']*2}):
            with self.assertRaisesRegex(ValueError, 'every original hunk'):
                validate_review(invalid, review_inputs(rows, hunks, records))

    def test_only_changed_code_can_support_a_reviewed_edge(self):
        inputs = {'hunks': [{'hunk': 0, 'diff': '@@ -1,2 +1,2 @@\n keep();\n-old();\n+new();'},
                            {'hunk': 1, 'diff': '@@ -1 +1 @@\n-x();\n+y();'}]}
        answer = {'reviews': [
            {'hunk': 0, 'reason': 'none', 'requires': []},
            {'hunk': 1, 'reason': 'needs change', 'requires': [{'provider': 0, 'reason': 'fixture',
                'provider_evidence': {'side': 'after', 'quote': 'keep();'},
                'consumer_evidence': {'side': 'after', 'quote': 'y();'}}]}]}
        with self.assertRaisesRegex(ValueError, 'changed line'):
            validate_review(answer, inputs)

    def test_invalid_review_retries_and_never_falls_back_to_bare_names(self):
        rows = [{'hunk': 0, 'port': False}, {'hunk': 1, 'port': True}]
        summaries = [{'hunk': h, 'symbol': 'same'} for h in range(2)]
        ask = Mock(return_value={'reviews': []})
        with self.assertRaisesRegex(ValueError, 'TRIAGE_DEPENDENCY_INVALID'):
            review_dependencies(rows, [{'text': '+same();'}]*2, summaries, ask, retries=1)
        self.assertEqual(ask.call_count, 2)
        self.assertIs(rows[0]['port'], False)

    def test_malformed_triage_is_not_a_skip(self):
        for value in (None, {}, {'port': 'false'}, {'port': False, 'category': 'fix', 'reason': 'x'}):
            with self.assertRaises(ValueError):
                triage.validate_decision(value)

    def test_one_failed_decision_stops_the_stage_and_is_saved_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'input.patch').write_text('fixture')
            (root/'summaries.json').write_text('[{"hunk":0}]')
            argv = ['triage.py', str(root/'input.patch'), str(root/'summaries.json'),
                    'description', str(root/'triaged.json')]
            with patch.object(sys, 'argv', argv), patch.dict(os.environ, {'LLM_KEY': 'test'}), \
                    patch.object(triage, 'split_patch_indexed', return_value=('', [
                        {'idx': 0, 'file': 'A.java', 'text': '+change();'}])), \
                    patch.object(triage, 'call', return_value=None), patch.object(triage.time, 'sleep'), \
                    self.assertRaisesRegex(SystemExit, 'TRIAGE_INFRA_FAILURE'):
                triage.main()
            self.assertIsNone(json.loads((root/'triaged.json').read_text())[0]['port'])

    def test_whole_patch_timeout_and_options_are_configurable(self):
        client = Mock(return_value={})
        with patch.dict(sys.modules, {'summarize_and_group': SimpleNamespace(call_json=client)}), \
                patch.dict(os.environ, {'GROUP_READ_TIMEOUT': '240',
                    'TRIAGE_DEPENDENCY_REQUEST_OPTIONS': '{"temperature":0.1}'}, clear=True):
            triage.call('test', 'model', 'prompt', dependency=True)
            self.assertEqual(client.call_args.kwargs['read_timeout'], 240)
            self.assertEqual(client.call_args.kwargs['request_options'], {'temperature': 0.1})
            triage.call('test', 'model', 'prompt')
            self.assertEqual(client.call_args.kwargs['read_timeout'], 60)

    def test_skipped_consumers_do_not_require_their_skipped_providers_on_replay(self):
        records = [{'hunk': 0}, {'hunk': 1}]
        hunks = [{'text': '+change();'}]*2
        rows = [{'hunk': h, 'port': False} for h in range(2)]
        reviews = {0: {'requires': []}, 1: {'requires': [{'provider': 0, 'reason': 'fixture'}]}}
        triage.postprocess(rows, hunks, records, '', '', reviews)
        attach_reviews(records, hunks, rows)
        self.assertFalse(any(r['port'] for r in rows))


if __name__ == '__main__':
    unittest.main()
