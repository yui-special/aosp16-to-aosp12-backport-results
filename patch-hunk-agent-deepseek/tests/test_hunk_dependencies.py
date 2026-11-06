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
from tools.hunk_dependencies import (attach_reviews, canonical_prerequisites, mechanical_prerequisites,
                                     review_dependencies, review_inputs, syntax_prerequisites, validate_review)

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

    def test_annotation_type_is_an_explicit_import_use(self):
        source = Source('Example.java', b'''import org.junit.ClassRule;
class Example {
 @ClassRule static Rule rule;
}''')
        records, _ = fixture(source, [1, 3])
        self.assertEqual([(0, 1)], [(e['provider'], e['consumer'])
                                    for e in syntax_prerequisites(records)])
        qualified = Source('Example.java', b'''import org.junit.ClassRule;
class Example {
 @org.junit.ClassRule static Rule rule;
}''')
        records, _ = fixture(qualified, [1, 3])
        self.assertEqual(syntax_prerequisites(records), [])

    def test_same_import_identifier_in_another_file_does_not_create_an_edge(self):
        source = Source('One.java', b'import pkg.Verifier;\nclass One { void f() { Verifier.check(); } }')
        records, _ = fixture(source, [1, 2])
        records[1]['source_structure']['after']['path'] = 'Other.java'
        self.assertEqual(syntax_prerequisites(records), [])

    def test_scoped_declarations_and_definitions_ignore_other_owners(self):
        source = Source('Example.java', b'''class Example {
 int count;
 void read() { System.out.println(count); }
 void added() {}
 void call() { added(); }
}
class Other {
 void call() { added(); }
}''')
        records, hunks = fixture(source, [2, 3, 4, 5, 8])
        records[2]['source_structure']['signature_changes'] = [{
            'kind': 'addition', 'name': 'added', 'scope': 'Example.added',
            'new_types': [], 'parse_error': False}]
        edges = mechanical_prerequisites(records, hunks)
        self.assertIn((0, 1, 'declaration_prerequisite'),
                      [(e['provider'], e['consumer'], e['kind']) for e in edges])
        self.assertIn((2, 3, 'definition_prerequisite'),
                      [(e['provider'], e['consumer'], e['kind']) for e in edges])
        self.assertFalse(any(e['consumer'] == 4 and e['provider'] in (0, 2) for e in edges))

    def test_direct_signature_adaptation_requires_lexical_owner_and_same_receiver(self):
        new = Source('Example.java', b'''class Example {
 void fetch(int flags) {}
 void call() {
  fetch(1);
  other.fetch(1);
 }
}
class Other {
 void call() { fetch(1); }
}''')
        old = Source('Example.java', b'''class Example {
 void fetch() {}
 void call() {
  fetch();
  this.fetch();
 }
}
class Other {
 void call() { fetch(); }
}''')
        records, hunks = fixture(new, [2, 4, 5, 9])
        records[0]['source_structure']['signature_changes'] = [{
            'kind': 'replacement', 'name': 'fetch', 'scope': 'Example.fetch',
            'old_types': [], 'new_types': ['int'], 'parse_error': False}]
        for h, line in ((1, 4), (2, 5), (3, 9)):
            records[h]['source_structure']['before'] = old.evidence([line], line, 1)
        edges = mechanical_prerequisites(records, hunks)
        self.assertEqual([(0, 1, 'signature_prerequisite')],
                         [(e['provider'], e['consumer'], e['kind']) for e in edges])

    def test_owner_this_signature_adaptation_is_lexically_bound(self):
        new = Source('Example.java', b'''class Example {
 void fetch(int flags) {}
 class Nested { void call() { Example.this.fetch(1); } }
}''')
        old = Source('Example.java', b'''class Example {
 void fetch() {}
 class Nested { void call() { Example.this.fetch(); } }
}''')
        records, hunks = fixture(new, [2, 3])
        records[0]['source_structure']['signature_changes'] = [{
            'kind': 'replacement', 'name': 'fetch', 'scope': 'Example.fetch',
            'old_types': [], 'new_types': ['int'], 'parse_error': False}]
        records[1]['source_structure']['before'] = old.evidence([3], 3, 1)
        edges = mechanical_prerequisites(records, hunks)
        self.assertEqual([(0, 1, 'signature_prerequisite')],
                         [(e['provider'], e['consumer'], e['kind']) for e in edges])

    def test_new_overload_adaptation_retains_the_old_entry_without_duplicate_definition(self):
        new = Source('Example.java', b'''class Example {
 void fetch(int value, boolean mode) {}
 void call() { fetch(1, true); }
}''')
        old = Source('Example.java', b'''class Example {
 void fetch(int value) {}
 void call() { fetch(1); }
}''')
        records, hunks = fixture(new, [2, 3])
        records[0]['source_structure']['signature_changes'] = [{
            'kind': 'addition', 'name': 'fetch', 'scope': 'Example.fetch',
            'old_types': None, 'new_types': ['int', 'boolean'], 'parse_error': False}]
        records[1]['source_structure']['before'] = old.evidence([3], 3, 1)
        edges = mechanical_prerequisites(records, hunks)
        self.assertEqual([(0, 1, 'signature_prerequisite')],
                         [(e['provider'], e['consumer'], e['kind']) for e in edges])
        self.assertTrue(edges[0]['retain_consumer'])
        rows = [{'hunk': 0, 'port': True}, {'hunk': 1, 'port': False}]
        reviews = {0: {'requires': []}, 1: {'requires': edges}}
        triage.postprocess(rows, hunks, records, '', '', reviews)
        self.assertEqual([r['port'] for r in rows], [True, True])

    def test_aconfig_cleanup_follows_reference_removal_and_cannot_be_repeated(self):
        hunks = [
            {'idx': 13, 'file': 'Flags.java', 'text': '@@ -1 +1 @@\n-if (Flags.my_feature()) {\n+if (true) {'},
            {'idx': 80, 'file': 'flags.aconfig', 'text': '@@ -1,3 +0,0 @@\n-flag {\n- name: "my_feature"\n- }'}]
        records = [{'hunk': h['idx'], 'file': h['file'], 'source_structure': {}} for h in hunks]
        edges = mechanical_prerequisites(records, hunks)
        self.assertEqual([(13, 80, 'deletion_prerequisite')],
                         [(e['provider'], e['consumer'], e['kind']) for e in edges])
        rows = [{'hunk': h['idx'], 'port': True} for h in hunks]
        inputs = review_inputs(rows, hunks, records)
        answer = {'reviews': [
            {'hunk': 13, 'reason': 'none', 'requires': []},
            {'hunk': 80, 'reason': 'duplicate', 'requires': [{
                'provider': 13, 'reason': 'duplicate mechanical edge',
                'provider_evidence': {'side': 'before', 'quote': 'if (Flags.my_feature()) {'},
                'consumer_evidence': {'side': 'before', 'quote': 'name: "my_feature"'}}]}]}
        with self.assertRaisesRegex(ValueError, 'cannot be repeated'):
            validate_review(answer, inputs)

    def test_review_combines_locked_and_behavior_edges_without_replay_duplicates(self):
        source = Source('Example.java', b'''import pkg.Verifier;
class Example {
 void run() {
  prepare();
  Verifier.check();
 }
}''')
        records, hunks = fixture(source, [1, 4, 5])
        rows = [{'hunk': h, 'port': h == 2, 'reason': 'initial'} for h in range(3)]
        answer = {'reviews': [
            {'hunk': 0, 'reason': 'none', 'requires': []},
            {'hunk': 1, 'reason': 'none', 'requires': []},
            {'hunk': 2, 'reason': 'behavior setup', 'requires': [{
                'provider': 1, 'reason': 'prepare must run first',
                'provider_evidence': {'side': 'after', 'quote': 'prepare();'},
                'consumer_evidence': {'side': 'after', 'quote': 'Verifier.check();'}}]}]}
        reviews = review_dependencies(rows, hunks, records, Mock(return_value=answer))
        self.assertEqual([(0, 'import_prerequisite'), (1, 'reviewed_prerequisite')],
                         [(e['provider'], e.get('kind', 'reviewed_prerequisite'))
                          for e in reviews[2]['requires']])
        triage.postprocess(rows, hunks, records, '', '', reviews)
        saved = copy.deepcopy(rows)
        replay = {r['hunk']: r['dependency_review'] for r in rows}
        triage.postprocess(rows, hunks, records, '', '', replay)
        self.assertEqual(rows, saved)

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

    def test_canonical_prerequisites_preserve_reachability_locks_and_retention(self):
        edges = [
            {'kind': 'reviewed_prerequisite', 'provider': 0, 'consumer': 1, 'reason': 'first step'},
            {'kind': 'reviewed_prerequisite', 'provider': 1, 'consumer': 2, 'reason': 'second step'},
            {'kind': 'reviewed_prerequisite', 'provider': 0, 'consumer': 2, 'reason': 'redundant shortcut'},
            {'kind': 'import_prerequisite', 'provider': 0, 'consumer': 2, 'reason': 'locked fact'},
            {'kind': 'reviewed_prerequisite', 'provider': 0, 'consumer': 2,
             'reason': 'required adaptation', 'retain_consumer': True}]
        result = canonical_prerequisites(edges)
        self.assertEqual([(0, 1, 'first step'), (0, 2, 'locked fact'),
                          (0, 2, 'required adaptation'), (1, 2, 'second step')],
                         [(e['provider'], e['consumer'], e['reason']) for e in result])

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
        self.assertIn('未列出不代表不存在依赖', prompt)
        self.assertIn('外部 receiver、AIDL 声明与调用', prompt)
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

    def test_evidenced_review_can_retain_a_skipped_consumer_without_name_closure(self):
        records = [{'hunk': 0}, {'hunk': 1}]
        hunks = [{'text': '+change();'}]*2
        reviews = {0: {'requires': []}, 1: {'requires': [
            {'provider': 0, 'reason': 'old entry must delegate to the new overload',
             'retain_consumer': True}]}}
        rows = [{'hunk': 0, 'port': True}, {'hunk': 1, 'port': False}]
        triage.postprocess(rows, hunks, records, '', '', reviews)
        self.assertEqual([r['port'] for r in rows], [True, True])
        self.assertTrue(rows[1]['retained_by'][0]['retain_consumer'])
        saved = copy.deepcopy(rows)
        replay = {r['hunk']: r['dependency_review'] for r in rows}
        triage.postprocess(rows, hunks, records, '', '', replay)
        self.assertEqual(rows, saved)
        rows = [{'hunk': 0, 'port': True}, {'hunk': 1, 'port': False}]
        reviews[1]['requires'][0].pop('retain_consumer')
        triage.postprocess(rows, hunks, records, '', '', reviews)
        self.assertEqual([r['port'] for r in rows], [True, False])

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
