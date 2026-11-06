"""Deterministic facts must survive target-only callers, overloads and dirty trees."""
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from git import Repo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'))
from tools.source_structure import Source, SymbolIndex, hunk_evidence, compact_evidence
from tools.source_checks import (ApiCatalog, api_inventory, structural_delta,
                                 inspect_worktree_patch, candidate_api_inventory,
                                 candidate_failure_context, artifact_tree, compiler_diagnostics,
                                 candidate_diagnostic_context, infrastructure_failure)
from tools.project import (Project, create_find_references_tool, creat_locate_symbol_tool,
                           create_query_api_context_tool, create_validate_tool)
from tools.porting_context import ApiContext, QueryProgress, target_context
from tools.utils import split_patch_indexed, hunk_coverage_report


def script(name):
    path = ROOT / ('data_ncwang/' + name)
    spec = importlib.util.spec_from_file_location('test_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceChecksTests(unittest.TestCase):
    def test_compiler_diagnostics_retain_real_javac_details_and_parameter_mismatch(self):
        output = ('build command\nA.java:83: error: cannot find symbol\n'
                  '        return match(uid, WindowContainer.alwaysTruePredicate());\n'
                  '                                         ^\n'
                  '  symbol:   method alwaysTruePredicate()\n'
                  '  location: class WindowContainer\n1 error\n'
                  'B.java:12: error: method reset cannot be applied to given types;\n'
                  '  required: int,boolean\n  found:    int\n'
                  '  reason: actual and formal argument lists differ in length\n'
                  'ninja: build stopped: subcommand failed.\n')
        text = compiler_diagnostics(output)
        for fact in ['alwaysTruePredicate()', 'location: class WindowContainer',
                     'required: int,boolean', 'found:    int', 'reason: actual and formal']:
            self.assertIn(fact, text)
        self.assertNotIn('build command', text)
        self.assertIn('truncated', compiler_diagnostics(output, max_chars=30))
        self.assertIsNone(infrastructure_failure(output))
        self.assertEqual(infrastructure_failure('adb: error: device offline')['kind'], 'device_offline')
        self.assertIsNone(infrastructure_failure('AssertionError: device offline is expected'))
        self.assertIsNone(infrastructure_failure('  throw new Error("No space left on device");'))

    def test_failure_lines_read_candidate_scope_instead_of_dirty_or_original_source(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repo.init(directory)
            path = Path(directory)/'tests/p/ExampleTest.java'
            path.parent.mkdir(parents=True)
            path.write_text('package p; class ExampleTest { void test() {} }\n')
            repo.index.add(['tests/p/ExampleTest.java'])
            source = ('package p;\nclass ExampleTest {\n void helper() {}\n'
                      ' void test() {\n  check("prior setup");\n'
                      '  check("concrete scenario");\n }\n}\n')
            path.write_text(source)
            repo.index.add(['tests/p/ExampleTest.java'])
            tree = repo.index.write_tree().hexsha
            path.write_text('dirty checkout with wrong line numbers\n')
            output = ('at p.ExampleTest.test(ExampleTest.java:6)\n'
                      'at org.junit.Runner.run(Runner.java:12)\n'
                      'at p.ExampleTest.test(ExampleTest.java:6)')
            context = candidate_failure_context(directory, tree, output)
            self.assertEqual(len(context['frames']), 1)
            frame = context['frames'][0]
            self.assertEqual(frame['line'], 6)
            self.assertTrue(frame['scope_complete'])
            self.assertIn('5:   check("prior setup");', frame['quote'])
            self.assertIn('6:   check("concrete scenario");', frame['quote'])
            self.assertEqual([c['arguments'] for c in frame['calls_at_location']],
                             [['"concrete scenario"'], ['"prior setup"']])
            self.assertIn('not proven', frame['call_order'])
            self.assertEqual(path.read_text(), 'dirty checkout with wrong line numbers\n')
            self.assertEqual(context['unresolved'][0]['reason'], 'source_absent')
            bounded = candidate_failure_context(directory, tree, output, max_lines=2)
            self.assertFalse(bounded['frames'][0]['scope_complete'])
            self.assertLessEqual(len(bounded['frames'][0]['quote'].splitlines()), 2)

    def test_failure_context_does_not_guess_duplicate_paths_or_invalid_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repo.init(directory)
            for name in ['one/p/A.java', 'two/p/A.java', 'one/p/B.java']:
                path = Path(directory)/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('package p; class A {}\n')
                repo.index.add([name])
            tree = repo.index.write_tree().hexsha
            context = candidate_failure_context(directory, tree,
                'at p.A.fail(A.java:1)\nat p.B.fail(B.java:999)')
            self.assertEqual(context['frames'], [])
            self.assertEqual([f['reason'] for f in context['unresolved']],
                             ['ambiguous_path', 'line_outside_candidate'])

    def delta(self, before, after):
        return structural_delta(Source('A.java', before.encode()), Source('A.java', after.encode()))

    def test_definitions_exclude_calls_comments_and_wrong_owners(self):
        source = Source('A.java', b'''package p;
class A {
 // void fake(int x)
 void run() { fake(1); }
 void send(int x) {}
 void send(String x) {}
}''')
        self.assertEqual(source.definitions('fake'), [])
        self.assertEqual(source.definitions('B.send'), [])
        self.assertEqual(len(source.definitions('p.A.send')), 2)
        self.assertEqual(len(source.definitions('A#send')), 2)

    def test_call_candidates_include_receiver_multiline_args_and_method_references(self):
        source = Source('A.java', b'''class A {
 void run() {
  service.send(lookup(1, 2), /* explanation */ true);
  other.send(
     1);
  Runnable r = service::send;
  // service.send(99);
 }
}''')
        calls = source.references('send')
        self.assertEqual([c['argument_count'] for c in calls], [2, 1, None])
        self.assertEqual(calls[0]['receiver'], 'service')
        self.assertEqual(calls[0]['arguments'], ['lookup(1, 2)', 'true'])
        self.assertEqual(calls[1]['scope'], 'A.run')
        self.assertEqual(calls[2]['receiver'], 'service')
        self.assertTrue(all(c['resolution'] == 'syntactic_candidate' for c in calls))

    def test_new_duplicates_block_but_overloads_constructors_and_nested_classes_do_not(self):
        base = 'class A { A(int x) {} void A(int x) {} void f(int x) {} }'
        valid = base[:-1] + ' void f(String x) {} class B { void f(int x) {} } }'
        self.assertEqual(self.delta(base, valid)['errors'], [])
        invalid = base[:-1] + ' int f(int renamed) { return 0; } }'
        errors = self.delta(base, invalid)['errors']
        self.assertEqual([e['kind'] for e in errors], ['duplicate_method'])
        errors = self.delta(base, base[:-1] + ' void g(int x, String... x) {} }')['errors']
        self.assertIn('duplicate_parameter', [e['kind'] for e in errors])
        arrays = 'class A { void f(int[] a) {} }'
        self.assertEqual(self.delta(arrays, arrays[:-1] + ' void f(int a[]) {} }')['errors'][0]['kind'],
                         'duplicate_method')

    def test_preexisting_duplicates_and_independent_anonymous_classes_are_not_new_errors(self):
        duplicate = 'class A { void f(int a) {} void f(int b) {} }'
        self.assertEqual(self.delta(duplicate, '\n' + duplicate)['errors'], [])
        after = '''class A {
 Object x = new Object() { void f(int a) {} };
 Object y = new Object() { void f(int a) {} };
}'''
        self.assertEqual(self.delta('class A {}', after)['errors'], [])
        # Distinct method type-variable bounds can yield distinct erased signatures.
        after = 'class A { <T extends Number> void f(T x) {} <T extends String> void f(T x) {} }'
        self.assertEqual(self.delta('class A {}', after)['errors'], [])

    def test_if_else_ownership_is_reported_without_claiming_equivalence(self):
        before = 'class A { void f() { if (old) { if (restore) grant(); } else revoke(); } }'
        after = 'class A { void f() { if (old && restore) grant(); else revoke(); } }'
        facts = self.delta(before, after)
        self.assertEqual(facts['errors'], [])
        removed = facts['control_flow']['removed']
        self.assertEqual(removed[1]['ancestors'], [{'condition': '(old)', 'branch': 'then'}])
        self.assertTrue(facts['control_flow']['added'][0]['has_else'])
        self.assertEqual(self.delta(before, '\n' + before)['control_flow'], {'removed': [], 'added': []})
        self.assertEqual(self.delta(before, 'class A { broken(')['parse_status']['after'], 'partial')

    def test_no_distinctive_token_means_unknown_not_complete(self):
        hunks = [{'idx': i, 'file': 'A.java', 'text': '-before();\n+after();'} for i in (0, 1)]
        report = hunk_coverage_report(hunks, '+unrelated();', False)
        self.assertFalse(report['complete'])
        self.assertEqual(report['missing'], [0, 1])
        self.assertEqual(report['members'][0]['reason'], 'unconfirmed_no_distinctive_identifier')


class InventoryIntegrationTests(unittest.TestCase):
    def test_reference_tool_finds_constants_locals_and_fields_without_declaration_inventory(self):
        self.write_commit({'Usage.java': '''class Usage {
 static final int STOP = 1;
 int state;
 int first() {
  int result = STOP;
  state = result;
  return result;
 }
 int second() { int result = 2; return result; }
 // STOP result state
 String label = "STOP result state";
 int STOP_MORE, $STOP, STOP$;
 int alphaSTOP, STOPbeta;
}'''.replace('alphaSTOP', 'αSTOP').replace('STOPbeta', 'STOPβ')}, 'identifiers')
        index = SymbolIndex(self.root)
        tool = create_find_references_tool(SimpleNamespace(source_index=index))
        query = {'ref': 'HEAD', 'path': 'Usage.java'}
        constant = json.loads(tool.invoke(dict(query, symbol='STOP')))
        self.assertEqual(constant['references'], [])
        self.assertNotIn('definitions', constant)
        self.assertEqual([r['line'] for r in constant['identifier_occurrences']], [2, 5])
        self.assertEqual(constant['non_code_match_count'], 2)
        self.assertEqual(constant['text_occurrences'], [])
        self.assertEqual(constant['query_status'], 'matches')
        locals_ = json.loads(tool.invoke(dict(query, symbol='result')))
        self.assertEqual({r['scope'] for r in locals_['identifier_occurrences']}, {'Usage.first', 'Usage.second'})
        self.assertEqual(len(locals_['identifier_occurrences']), 5)
        self.assertEqual({r['resolution'] for r in locals_['identifier_occurrences']}, {'lexical_identifier'})
        fields = json.loads(tool.invoke(dict(query, symbol='Usage.state')))
        self.assertEqual([r['line'] for r in fields['identifier_occurrences']], [3, 6])
        self.assertEqual(fields['identifier_occurrences'][1]['text'], '  state = result;')
        # Adding navigation evidence must not alter the API closure's call schema.
        self.assertNotIn('identifier_occurrences', index.inspect('HEAD', 'state'))

    def test_filename_queries_are_rejected_before_search_even_in_unlimited_mode(self):
        project = SimpleNamespace(source_index=Mock(), unlimited_backend=True)
        invalid = ('BackgroundLaunchProcessControllerTests.java', 'Test.kt', 'test.py',
                   'Api.java:42', 'src/Api.java', r'src\Api.java', 'dir/Api',
                   '', 'WindowProcessController app')
        for factory in (create_find_references_tool, creat_locate_symbol_tool):
            tool = factory(project)
            for symbol in invalid:
                with self.subTest(tool=tool.name, symbol=symbol):
                    result = json.loads(tool.invoke({'ref': 'HEAD', 'symbol': symbol}))
                    self.assertEqual(result['query_status'], 'invalid_query')
                    self.assertFalse(result['searched'])
                    self.assertNotIn('references', result)
        project.source_index.inspect.assert_not_called()

    def test_index_itself_rejects_filenames_before_git_resolution(self):
        from tools import source_structure
        index = SymbolIndex(self.root)
        with patch.object(source_structure, 'resolve_source_ref') as resolve:
            with self.assertRaisesRegex(ValueError, 'filename or path'):
                index.inspect('HEAD', 'Example.java', include_occurrences=True)
            resolve.assert_not_called()

    def test_qualified_symbols_and_real_java_identifier_still_work(self):
        self.write_commit({'Example.java': '''class Example {
 void java() {}
 void run() { java(); }
}'''}, 'extension-like identifier')
        project = SimpleNamespace(source_index=SymbolIndex(self.root))
        for symbol in ('Example.run', 'Example::run', 'Example#run()', 'Example.java()'):
            result = json.loads(creat_locate_symbol_tool(project).invoke(
                {'ref': 'HEAD', 'symbol': symbol, 'path': 'Example.java'}))
            self.assertEqual(len(result['definitions']), 1)
            self.assertEqual(result['definitions'][0]['path'], 'Example.java')
        result = json.loads(create_find_references_tool(project).invoke(
            {'ref': 'HEAD', 'symbol': 'java', 'path': 'Example.java'}))
        self.assertEqual(len(result['references']), 1)
        self.assertEqual(result['references'][0]['scope'], 'Example.run')

    def test_missing_path_does_not_silently_search_elsewhere_or_look_like_no_usage(self):
        index = SymbolIndex(self.root)
        project = SimpleNamespace(source_index=index)
        from tools import source_structure
        for factory in (create_find_references_tool, creat_locate_symbol_tool):
            with patch.object(source_structure, 'snapshot', wraps=source_structure.snapshot) as parse:
                result = json.loads(factory(project).invoke(
                    {'ref': 'HEAD', 'symbol': 'reset', 'path': 'Missing.java'}))
            parse.assert_not_called()
            self.assertEqual(result['query_status'], 'missing_path')
            self.assertIn('without this path', result['next_action'])
        found = json.loads(create_find_references_tool(project).invoke(
            {'ref': 'HEAD', 'symbol': 'absent', 'path': 'Api.java'}))
        self.assertEqual(found['query_status'], 'no_matches')
        self.assertEqual(found['scope_status'], 'file')
        self.assertEqual(found['searched_path'], 'Api.java')

    def test_call_names_are_not_repeated_but_same_name_arguments_and_receivers_survive(self):
        self.write_commit({'Usage.java': '''class Usage {
 Object send;
 void send(Object value) {}
 void f() {
  service.send(send);
  send.send(send);
 }
}'''}, 'overlap')
        result = SymbolIndex(self.root).inspect('HEAD', 'send', path='Usage.java', include_occurrences=True)
        self.assertEqual(len(result['references']), 2)
        self.assertEqual([(r['line'], r['column']) for r in result['identifier_occurrences']],
                         [(2, 9), (3, 7), (5, 16), (6, 3), (6, 13)])
        self.assertEqual(result['references'][1]['receiver'], 'send')
        self.assertEqual(result['references'][1]['arguments'], ['send'])

    def test_reference_source_lines_join_the_read_ledger_only_when_delivered_completely(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.query_progress = QueryProgress(stale_limit=None)
        tool = create_find_references_tool(project)
        result = json.loads(tool.invoke({'ref': self.target, 'symbol': 'user', 'path': 'Api.java'}))
        self.assertIn('void reset', result['identifier_occurrences'][0]['text'])
        self.assertIn('NO_NEW_SOURCE', project._viewcode(self.target, 'Api.java', 2, 2))
        project.query_progress = QueryProgress(stale_limit=None)
        project.final_repair_read_limit = 100
        self.assertIn('TRUNCATED', tool.invoke({'ref': self.target, 'symbol': 'user', 'path': 'Api.java'}))
        self.assertEqual(project.query_progress.source_seen, set())

    def test_nested_and_inherited_declarations_are_found_without_guessing_filenames(self):
        self.write_commit({'Harness.java': '''class Harness {
 static class Builder { int uid = 42; }
}
class Base { boolean visible() { return true; } }
class Child extends Base {}
'''}, 'nested')
        tool = creat_locate_symbol_tool(SimpleNamespace(source_index=SymbolIndex(self.root)))
        missing = json.loads(tool.invoke({'ref': 'HEAD', 'symbol': 'Builder', 'path': 'Builder.java'}))
        self.assertEqual(missing['query_status'], 'missing_path')
        found = json.loads(tool.invoke({'ref': 'HEAD', 'symbol': 'Builder'}))
        self.assertEqual([(d['path'], d['qualified_name']) for d in found['definitions']],
                         [('Harness.java', 'Harness.Builder')])
        inherited = json.loads(tool.invoke({'ref': 'HEAD', 'symbol': 'Child.visible'}))
        self.assertEqual(inherited['definitions'], [])
        # A same-name declaration is a candidate, not a type-bound inherited method.
        self.assertEqual([d['qualified_name'] for d in inherited['other_owner_candidates']], ['Base.visible'])

    def test_navigation_reports_parser_gaps_and_only_uses_text_as_unproven_evidence(self):
        self.write_commit({'I.aidl': 'interface I { const int LIMIT = 3; }',
                           'Broken.java': 'class Broken { void f() {',
                           'Note.txt': 'LIMIT is documentation, not code'}, 'gaps')
        tool = create_find_references_tool(SimpleNamespace(source_index=SymbolIndex(self.root)))
        with patch.dict(os.environ, {'SOURCE_STRUCTURE_CONFIG': ''}):
            aidl = json.loads(tool.invoke({'ref': 'HEAD', 'symbol': 'LIMIT', 'path': 'I.aidl'}))
        self.assertEqual(aidl['query_status'], 'parse_gap')
        self.assertEqual(aidl['identifier_occurrences'], [])
        self.assertEqual(len(aidl['text_occurrences']), 1)
        self.assertIn('unparsed_text', aidl['text_occurrences'][0]['resolution'])
        broken = json.loads(tool.invoke({'ref': 'HEAD', 'symbol': 'notPresent', 'path': 'Broken.java'}))
        self.assertEqual(broken['query_status'], 'parse_gap')
        self.assertEqual(broken['diagnostics'][0]['status'], 'partial')
        note = json.loads(tool.invoke({'ref': 'HEAD', 'symbol': 'LIMIT', 'path': 'Note.txt'}))
        self.assertEqual(note['identifier_occurrences'], [])
        self.assertEqual(note['diagnostics'][0]['status'], 'unsupported')
        self.assertEqual(len(note['text_occurrences']), 1)

    def test_parser_unavailability_returns_a_diagnostic_and_explicit_text_fallback(self):
        from tools import source_structure
        index = SymbolIndex(self.root)
        with patch.object(source_structure, 'snapshot', side_effect=RuntimeError('Tree-sitter grammar unavailable')):
            result = index.inspect('HEAD', 'reset', path='Api.java', include_occurrences=True)
        self.assertEqual(result['diagnostics'][0]['status'], 'parser_unavailable')
        self.assertEqual(result['references'], [])
        self.assertEqual(result['identifier_occurrences'], [])
        self.assertEqual(len(result['text_occurrences']), 1)
        self.assertEqual(result['query_status'], 'parse_gap')

    def test_identifier_lookup_uses_fixed_candidate_tree_and_does_not_mutate_checkout(self):
        (self.root/'Api.java').write_text('class Api { int state; int f() { return state; } }\n')
        self.repo.index.add(['Api.java'])
        tree = self.repo.index.write_tree().hexsha
        (self.root/'Api.java').write_text('dirty state without source syntax\n')
        index = SymbolIndex(self.root)
        before = self.repo.git.status('--porcelain')
        candidate = index.inspect(tree, 'state', path='Api.java', include_occurrences=True)
        self.assertEqual(len(candidate['identifier_occurrences']), 2)
        self.assertIn('return state', candidate['identifier_occurrences'][1]['text'])
        old = index.inspect(self.target, 'state', path='Api.java', include_occurrences=True)
        self.assertEqual(old['query_status'], 'no_matches')
        self.assertEqual(self.repo.git.status('--porcelain'), before)
        self.assertEqual((self.root/'Api.java').read_text(), 'dirty state without source syntax\n')

    def test_scoped_lookup_does_not_parse_other_files_and_returns_unique_definition_source(self):
        self.write_commit({'Other.java': 'class Other { void reset(String s) {} }'}, 'other')
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.query_progress = QueryProgress()
        from tools import source_structure
        with patch.object(source_structure, 'snapshot', wraps=source_structure.snapshot) as parse:
            result = json.loads(creat_locate_symbol_tool(project).invoke(
                {'ref': 'HEAD', 'symbol': 'reset', 'path': 'Api.java'}))
        self.assertEqual({call.args[2] for call in parse.call_args_list}, {'Api.java'})
        self.assertIn('void reset(int user)', result['source'])
        self.assertEqual(len(result['definitions']), 1)
        reread = project._viewcode(result['ref'], 'Api.java', 2, 2)
        self.assertIn('already provided', reread)
        self.assertNotIn('void reset(int user)', reread)

    def test_exhausted_read_budget_stops_before_git_search_or_source_loading(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.final_repair_reads_remaining = 0
        with patch.object(project.source_index, 'inspect') as search:
            for tool in (creat_locate_symbol_tool(project), create_find_references_tool(project)):
                self.assertIn('READ_BUDGET_EXHAUSTED', tool.invoke({'ref': 'invalid', 'symbol': 'reset'}))
            search.assert_not_called()
        self.assertIn('READ_BUDGET_EXHAUSTED', project._viewcode('invalid', 'missing', 1, 20))

    def test_compiler_context_uses_candidate_despite_dirty_checkout(self):
        tree = artifact_tree(self.root, self.target, self.patch_text)
        (self.root/'Api.java').write_text('dirty')
        context = candidate_diagnostic_context(self.root, tree, '/build/src/Api.java:2: error: bad call')
        self.assertEqual(context['frames'][0]['path'], 'Api.java')
        self.assertIn('boolean restore', context['frames'][0]['quote'])
        self.assertEqual((self.root/'Api.java').read_text(), 'dirty')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repo.init(self.root)
        with self.repo.config_writer() as config:
            config.set_value('user', 'name', 'Test')
            config.set_value('user', 'email', 'test@example.invalid')
        self.old = 'class Api {\n void reset(int user) {}\n}\n'
        self.new = 'class Api {\n void reset(int user, boolean restore) {}\n}\n'
        self.write_commit({'Api.java': self.old}, 'base')
        self.base = self.repo.head.commit.hexsha
        self.write_commit({'Api.java': self.new}, 'upstream')
        self.upstream = self.repo.head.commit.hexsha
        self.patch_text = self.repo.git.diff(self.base, self.upstream) + '\n'
        _, self.hunks = split_patch_indexed(self.patch_text, True)
        self.repo.git.checkout('--detach', self.base)
        self.write_commit({'Caller.java': 'class Caller {\n void preferences() { manager.reset(1); }\n}\n'}, 'target-only caller')
        self.target = self.repo.head.commit.hexsha

    def write_commit(self, files, message):
        for name, text in files.items():
            (self.root / name).write_text(text)
        self.repo.index.add(list(files))
        self.repo.index.commit(message)

    def test_inventory_finds_target_only_caller_and_ignores_dirty_checkout(self):
        index = SymbolIndex(self.root)
        (self.root/'Caller.java').write_text('dirty worktree')
        result = api_inventory(index, self.target, self.base, self.upstream, self.hunks)
        calls = result['apis'][0]['references']
        self.assertEqual([(c['path'], c['scope'], c['argument_count']) for c in calls],
                         [('Caller.java', 'Caller.preferences', 1)])
        self.assertEqual(result['apis'][0]['ref'], self.target)
        self.assertEqual((self.root/'Caller.java').read_text(), 'dirty worktree')
        tool = create_find_references_tool(SimpleNamespace(source_index=index))
        self.assertEqual(json.loads(tool.invoke({'ref': self.target, 'symbol': 'reset'}))['references'], calls)
        self.assertEqual(json.loads(tool.invoke({'ref': self.target, 'symbol': 'reset',
                                                'path': 'Other.java'}))['references'], [])
        self.repo.git.reset('--hard')
        self.write_commit({'Caller.java': 'class Caller { void f() { manager.reset(2, true); } }'}, 'move ref')
        self.assertEqual(index.inspect('HEAD', 'reset')['references'][0]['argument_count'], 2)
        self.assertEqual(index.inspect(self.target, 'reset')['references'][0]['argument_count'], 1)

    def test_qualified_lookup_preserves_owners_and_returns_ranges_without_fuzzy_redirect(self):
        self.write_commit({'Other.java': 'class Other { void reset(String s) {} }',
                           'ApiServiceTest.java': 'class ApiServiceTest {}'}, 'other owner')
        index = SymbolIndex(self.root)
        for symbol in ('Api.reset', 'Api::reset', 'Api#reset(int)'):
            self.assertEqual(index.locate('HEAD', symbol), [('Api.java', 2)])
        tool = creat_locate_symbol_tool(SimpleNamespace(source_index=index))
        result = json.loads(tool.invoke({'ref': 'HEAD', 'symbol': 'Api.reset'}))
        self.assertEqual(result['definitions'][0]['signature'], 'void reset(int user)')
        self.assertEqual(result['definitions'][0]['end'], 2)
        result = json.loads(tool.invoke({'ref': 'HEAD', 'symbol': 'Missing.reset'}))
        self.assertEqual(result['definitions'], [])
        self.assertEqual({d['qualified_name'] for d in result['other_owner_candidates']},
                         {'Api.reset', 'Other.reset'})
        self.assertNotIn('ApiServiceTest', json.dumps(result))

    def test_catalog_follows_related_changed_apis_before_porting_and_handles_cycles(self):
        self.repo.git.checkout('--detach', self.base)
        api = 'class Api {\n void reset(int user) { facade.clear(user); }\n}\n'
        facade = 'class Facade {\n void clear(int user) { api.reset(user); }\n}\n'
        self.write_commit({'Api.java': api, 'Facade.java': facade}, 'upstream base')
        parent = self.repo.head.commit.hexsha
        self.write_commit({'Api.java': api.replace('int user)', 'int user, boolean restore)')
                           .replace('clear(user)', 'clear(user, restore)'),
                           'Facade.java': facade.replace('int user)', 'int user, boolean restore)')
                           .replace('reset(user)', 'reset(user, restore)')}, 'signature chain')
        commit = self.repo.head.commit.hexsha
        _, hunks = split_patch_indexed(self.repo.git.diff(parent, commit) + '\n', True)
        self.repo.git.checkout('--detach', parent)
        self.write_commit({'Preferences.java': 'class Preferences { void f() { api.reset(7); } }'},
                          'target-only caller')
        group = [h for h in hunks if h['file'] == 'Facade.java']
        catalog = ApiCatalog(SymbolIndex(self.root), parent, commit, hunks)
        result = catalog.inventory('HEAD', group, ['Facade.clear'])
        self.assertEqual([api['symbol'] for api in result['apis']], ['clear', 'reset'])
        reset = result['apis'][1]
        self.assertIn('Preferences.f', [c['scope'] for c in reset['references']])
        self.assertTrue(all(c['resolution'] == 'syntactic_candidate' for c in reset['references']))
        self.assertEqual({(e['from'], e['to']) for e in result['related_api_candidates']},
                         {('clear', 'reset'), ('reset', 'clear')})

    def test_catalog_does_not_expand_generic_enclosing_run_globally(self):
        self.repo.git.checkout('--detach', self.base)
        api = '''class Api {
 void reset(int user) {}
 void f() {
  Runnable r = new Runnable() {
   public void run() { manager.reset(1); }
  };
 }
}
'''
        self.write_commit({'Api.java': api}, 'anonymous runner base')
        parent = self.repo.head.commit.hexsha
        changed = api.replace('void reset(int user)', 'void reset(int user, boolean restore)')
        changed = changed.replace('manager.reset(1)', 'manager.reset(1, true)')
        self.write_commit({'Api.java': changed}, 'change reset')
        commit = self.repo.head.commit.hexsha
        _, hunks = split_patch_indexed(self.repo.git.diff(parent, commit) + '\n', True)
        self.repo.git.checkout('--detach', parent)
        self.write_commit({'Other.java': 'class Other { void run() {} }\n'}, 'target')
        result = ApiCatalog(SymbolIndex(self.root), parent, commit, hunks).inventory('HEAD', hunks)
        self.assertEqual([api['symbol'] for api in result['apis']], ['reset'])
        self.assertNotIn('run', result['changed_api_names'])

    def test_candidate_feedback_covers_actual_signatures_and_untouched_callers(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.keep_tree_after_apply = True
        feedback = project._apply_hunk(self.target, self.patch_text)
        self.assertTrue(project.round_succeeded)
        facts = json.loads(feedback.split('CANDIDATE_API_FACTS:', 1)[1].split('\n', 1)[1])
        apis = facts['apis']
        self.assertEqual(facts['signature_changes'][0]['old_types'], ['int'])
        self.assertIn('boolean restore', apis[0]['definitions'][0]['signature'])
        self.assertEqual(apis[0]['references'][0]['scope'], 'Caller.preferences')
        self.assertEqual(apis[0]['references'][0]['argument_count'], 1)
        # An overload is still a candidate relation, never a blanket caller error.
        self.repo.git.reset('--hard')
        (self.root/'Api.java').write_text(self.old[:-2] + ' void reset(int user, boolean restore) {}\n}\n')
        overload = self.repo.git.diff() + '\n'
        self.repo.git.reset('--hard')
        feedback = project._apply_hunk(self.target, overload)
        self.assertTrue(project.round_succeeded, feedback)

    def test_offset_signature_and_api_inventory_use_applied_tree_not_dirty_checkout(self):
        original = 'class Api {\n' + ' // padding\n' * 10 + ' void reset(int user) {}\n}\n'
        self.write_commit({'Api.java': original}, 'longer target')
        base = self.repo.head.commit.hexsha
        (self.root/'Api.java').write_text(original.replace('int user)', 'int user, boolean restore)'))
        canonical = self.repo.git.diff() + '\n'
        shifted = re.sub(r'^@@ -(\d+),(\d+) \+(\d+),(\d+) @@', lambda m:
            '@@ -%d,%s +%d,%s @@' % (int(m[1])+1, m[2], int(m[3])+1, m[4]), canonical, flags=re.M)
        # The inventory must not pick up an unrelated uncommitted API or caller edit.
        (self.root/'Api.java').write_text('class Api { void wrong() {} }\n')
        (self.root/'Caller.java').write_text('class Caller { void dirty() {} }\n')
        before = self.repo.git.diff()
        facts = inspect_worktree_patch(str(self.root), base, shifted)
        self.assertEqual(facts['signature_changes'][0]['new_types'], ['int', 'boolean'])
        rows = candidate_api_inventory(SymbolIndex(self.root), base, shifted, facts['signature_changes'],
                                       tree=facts['candidate_tree'])
        self.assertIn('boolean restore', rows[0]['definitions'][0]['signature'])
        self.assertEqual(rows[0]['references'][0]['scope'], 'Caller.preferences')
        self.assertEqual(self.repo.git.diff(), before)

    def test_failed_candidate_fact_collection_rolls_back(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.keep_tree_after_apply = True
        with patch('tools.project.candidate_api_inventory', side_effect=RuntimeError('index failed')):
            with self.assertRaisesRegex(RuntimeError, 'index failed'):
                project._apply_hunk(self.target, self.patch_text)
        self.assertEqual(self.repo.git.status('--porcelain'), '')
        self.assertFalse(project.round_succeeded)

    def test_candidate_application_diagnostics_return_to_the_same_group(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.keep_tree_after_apply = True
        error = subprocess.CalledProcessError(1, ['git', 'apply'], stderr='error: Api.java does not apply')
        with patch('tools.project.inspect_worktree_patch', side_effect=error):
            feedback = project._apply_hunk(self.target, self.patch_text)
        self.assertIn('CANDIDATE_INSPECTION_FAILED', feedback)
        self.assertIn('Api.java does not apply', feedback)
        self.assertEqual(self.repo.git.status('--porcelain'), '')
        self.assertFalse(project.round_succeeded)
        self.assertEqual(project.succeeded_patches, [])

    def test_traditional_multi_file_diff_survives_independent_candidate_application(self):
        self.write_commit({'A.java': 'class A { int value = 1; }\n',
                           'B.java': 'class B { int value = 1; }\n'}, 'two files')
        base = self.repo.head.commit.hexsha
        candidate = ''.join('--- a/{0}.java\n+++ b/{0}.java\n@@ -1 +1 @@\n'
            '-class {0} {{ int value = 1; }}\n+class {0} {{ int value = 2; }}\n'.format(name)
            for name in ('A', 'B'))
        before = self.repo.git.status('--porcelain')
        tree = artifact_tree(self.root, base, candidate)
        for name in ('A', 'B'):
            self.assertIn('value = 2', self.repo.git.show('%s:%s.java' % (tree, name)))
        self.assertEqual(self.repo.git.status('--porcelain'), before)
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=base, target_release=base))
        self.assertIn('Patch applied successfully', project._apply_hunk(base, candidate))
        self.assertTrue(project.round_succeeded)

    def test_removed_api_file_keeps_untouched_callers_in_candidate_feedback(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        self.repo.git.rm('Api.java')
        removed = self.repo.git.diff('HEAD') + '\n'
        self.repo.git.reset('--hard')
        feedback = project._apply_hunk(self.target, removed)
        self.assertTrue(project.round_succeeded, feedback)
        facts = json.loads(feedback.split('CANDIDATE_API_FACTS:', 1)[1].split('\n', 1)[1])
        self.assertEqual(facts['signature_changes'][0]['kind'], 'removal')
        self.assertEqual(facts['apis'][0]['definitions'], [])
        self.assertEqual(facts['apis'][0]['references'][0]['scope'], 'Caller.preferences')

    def test_reads_detect_overlapping_ranges_but_allow_new_ranges_and_revisions(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.query_progress = QueryProgress(stale_limit=2)
        first = project._viewcode('HEAD', 'Api.java', 1, 2)
        self.assertIn('2:  void reset(int user)', first)
        project._viewcode('HEAD', 'Api.java', 2, 2)
        self.assertIn('QUERY STALLED', project._viewcode('HEAD', 'Api.java', 1, 2))
        self.assertNotIn('QUERY STALLED', project._viewcode('HEAD', 'Api.java', 3, 3))
        (self.root/'Api.java').write_text('dirty file')
        repeated = project._viewcode('HEAD', 'Api.java', 1, 2)
        self.assertIn('already provided', repeated)
        self.assertNotIn('dirty file', repeated)
        self.repo.git.reset('--hard')
        self.write_commit({'Api.java': self.new}, 'new ref')
        self.assertIn('boolean restore', project._viewcode('HEAD', 'Api.java', 1, 2))
        self.assertIn('outside the file', project._viewcode('HEAD', 'Api.java', 300, 400))

    def test_repair_tools_read_candidate_only_symbols_without_changing_checkout(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        (self.root/'Api.java').write_text(self.new)
        added = self.root/'CandidateOnly.java'
        added.write_text('class CandidateOnly { void probe() { new Api().reset(1, false); } }\n')
        self.repo.index.add(['Api.java', 'CandidateOnly.java'])
        candidate = self.repo.git.diff(self.target) + '\n'
        tree = artifact_tree(self.root, self.target, candidate)
        self.repo.git.reset('--hard', self.target)
        (self.root/'Api.java').write_text('dirty unrelated source\n')
        before = self.repo.git.status('--porcelain')
        self.assertIn('boolean restore', project._viewcode(tree, 'Api.java', 1, 4))
        self.assertNotIn('boolean restore', project._viewcode(self.target, 'Api.java', 1, 4))
        self.assertIn('probe', project._viewcode(tree, 'CandidateOnly.java', 1, 1))
        definitions = json.loads(creat_locate_symbol_tool(project).invoke(
            {'ref': tree, 'symbol': 'CandidateOnly.probe'}))
        self.assertEqual(definitions['ref'], tree)
        self.assertEqual(definitions['definitions'][0]['path'], 'CandidateOnly.java')
        refs = json.loads(create_find_references_tool(project).invoke(
            {'ref': tree, 'symbol': 'reset', 'path': 'CandidateOnly.java'}))
        self.assertEqual(refs['references'][0]['arguments'], ['1', 'false'])
        self.assertEqual(self.repo.git.status('--porcelain'), before)
        self.assertFalse(added.exists())
        with self.assertRaises(subprocess.CalledProcessError):
            project._viewcode('nonexistent-candidate-ref', 'Api.java', 1, 4)

    def test_final_repair_bounds_read_output_without_limiting_normal_group_reads(self):
        index = SymbolIndex(self.root)
        project = SimpleNamespace(source_index=index)
        output = creat_locate_symbol_tool(project).invoke({'ref': 'HEAD', 'symbol': 'reset'})
        self.assertNotIn('READ OUTPUT TRUNCATED', output)
        project.final_repair_read_limit = 100
        output = creat_locate_symbol_tool(project).invoke({'ref': 'HEAD', 'symbol': 'reset'})
        self.assertIn('READ OUTPUT TRUNCATED: 100 of', output)
        self.assertIn('qualified symbol', output)
        self.assertLess(len(output), 300)

    def test_final_repair_read_budget_reserves_iterations_for_candidate_submission(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.final_repair_reads_remaining = 1
        self.assertIn('void reset', project._viewcode('HEAD', 'Api.java', 1, 2))
        blocked = project._viewcode('HEAD', 'Api.java', 2, 3)
        self.assertIn('READ_BUDGET_EXHAUSTED', blocked)
        self.assertIn('submit_candidate', blocked)
        self.assertNotIn('boolean restore', blocked)

    def test_prompt_evidence_keeps_scopes_and_semantic_source_facts(self):
        evidence = hunk_evidence(str(self.root), self.base, self.upstream, self.hunks[0])
        compact = compact_evidence(evidence)
        self.assertEqual(compact['signature_changes'], evidence['signature_changes'])
        self.assertEqual(compact['after']['scopes'][0]['signature'], 'void reset(int user, boolean restore)')
        self.assertNotIn('content_sha256', json.dumps(compact))
        self.assertLess(len(json.dumps(compact)), len(json.dumps(evidence)))

    def test_parse_gaps_are_not_reported_as_proof_of_no_callers(self):
        self.write_commit({'Broken.java': 'class Broken { void f() { reset(1); }',
                           'I.aidl': 'interface I { void reset(int user); }'}, 'gaps')
        with patch.dict(os.environ, {'SOURCE_STRUCTURE_CONFIG': ''}):
            result = SymbolIndex(self.root).inspect('HEAD', 'reset')
        self.assertEqual({d['path']: d['status'] for d in result['diagnostics']},
                         {'Broken.java': 'partial', 'I.aidl': 'unsupported'})

    def test_mapping_requires_a_real_declaration(self):
        mapper = script('backport_patch/cases/CVE-2025-48649/scripts/map_semantics.py')
        defs, status = mapper.declaration_evidence(str(self.root), self.target, 'Caller.java', 'reset')
        self.assertEqual((defs, status), ([], 'ok'))
        defs, _ = mapper.declaration_evidence(str(self.root), self.target, 'Api.java', 'Api.reset')
        self.assertEqual(defs[0]['signature'], 'void reset(int user)')
        self.assertEqual(defs[0]['start'], 2)



    def test_group_context_reuses_revision_scoped_queries_and_current_method_source(self):
        index = SymbolIndex(self.root)
        index.inspect(self.target, 'Absent')
        group = dict(text='reset Absent', target_symbols=['Api.reset'],
                     target_locations=[dict(path='Api.java', symbol='Api.reset')])
        (self.root/'Api.java').write_text('dirty unrelated source')
        note = target_context(index, self.repo, self.target, group)
        self.assertIn('[KNOWN TARGET FACTS]', note)
        self.assertIn('void reset(int user)', note)
        self.assertIn('Absent', note)
        self.assertNotIn('dirty unrelated source', note)
        cached = len(index.inspections)
        self.assertEqual(target_context(index, self.repo, self.target, group), note)
        self.assertEqual(len(index.inspections), cached)
        self.write_commit({'Api.java': 'class Api { void reset(String user) {} }\n'}, 'changed')
        changed = self.repo.head.commit.hexsha
        updated = target_context(index, self.repo, changed, group)
        self.assertIn('void reset(String user)', updated)
        self.assertNotIn('void reset(int user)', updated)
        self.assertNotIn('"symbol": "Absent"', updated)


    def test_summary_facts_override_model_labels_and_grouping_reuses_matching_evidence(self):
        client = script('summarize_and_group.py')
        evidence = hunk_evidence(str(self.root), self.base, self.upstream, self.hunks[0])
        obj = {'symbol': 'invented', 'param_change': 'remove'}
        client.attach_syntax(obj, evidence)
        self.assertEqual((obj['symbol'], obj['param_change']), ('reset', 'add'))
        obj.update(hunk=0, file='Api.java', kind='declare')
        patch_file, summary = self.root/'source.patch', self.root/'summary.json'
        patch_file.write_text(self.patch_text)
        summary.write_text(json.dumps([obj]))
        grouping = script('backport_patch/cases/CVE-2025-48649/scripts/v6_group.py')
        with patch.object(grouping, 'hunk_evidence', wraps=hunk_evidence) as extract:
            grouping.build(str(self.root), self.upstream, str(patch_file), str(summary))
            extract.assert_not_called()
            obj['source_structure']['input_fingerprint'] = 'stale'
            summary.write_text(json.dumps([obj]))
            grouping.build(str(self.root), self.upstream, str(patch_file), str(summary))
            self.assertEqual(extract.call_count, 1)

    def test_group_validation_rejects_duplicates_rolls_back_and_preserves_prior_group(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.keep_tree_after_apply = True
        duplicate = self.old[:-2] + ' void reset(int renamed) {}\n}\n'
        # Build an actual diff; the prior target-only caller must survive rollback.
        (self.root/'Api.java').write_text(duplicate)
        bad_patch = self.repo.git.diff() + '\n'
        self.repo.git.reset('--hard')
        result = project._apply_hunk(self.target, bad_patch)
        self.assertIn('STRUCTURAL_VALIDATION_FAILED', result)
        self.assertFalse(project.round_succeeded)
        self.assertEqual(project.succeeded_patches, [])
        self.assertEqual((self.root/'Api.java').read_text(), self.old)
        self.assertTrue((self.root/'Caller.java').exists())
        self.assertEqual(self.repo.git.status('--porcelain'), '')
        result = project._apply_hunk(self.target, self.patch_text)
        self.assertTrue(project.round_succeeded, result)
        self.assertIn('boolean restore', (self.root/'Api.java').read_text())

    def test_target_only_call_inventory_reaches_initial_port_and_retry_inputs(self):
        from agent import invoke_llm as runtime
        # The target uses another parameter name, forcing the actual porting path.
        self.write_commit({'Api.java': self.old.replace('int user', 'int userId')}, 'target divergence')
        target = self.repo.head.commit.hexsha
        (self.root/'Api.java').write_text(self.new.replace('int user', 'int userId'))
        (self.root/'Caller.java').write_text('class Caller {\n void preferences() { manager.reset(1, true); }\n}\n')
        candidate = self.repo.git.diff() + '\n'
        self.repo.git.reset('--hard')
        dataset = self.root/'dataset'
        dataset.mkdir()
        data = SimpleNamespace(project_url='fixture', project_dir=str(self.root), error_message='',
            new_patch_parent=self.base, new_patch=self.upstream, target_release=target,
            patch_dataset_dir=str(dataset), coverage_retries=1)
        project = Project(data)
        agent = Mock()
        seen = []

        def port(payload, callbacks):
            description = payload['group_desc']
            self.assertIn('[TARGET API CONTEXT]', description)
            self.assertIn('[REQUIRED READS — COMPLETE IN ORDER]', description)
            self.assertIn('query_api_context', description)
            self.assertIn('Caller.preferences', description)
            self.assertIn('manager.reset(1)', description)
            self.assertEqual(project.api_context.read_report()['missing'], [])
            self.assertIn('[KNOWN TARGET FACTS]', description)
            repeated, facts = project.query_progress.source_excerpt(
                target, 'Api.java', 2, [' void reset(int userId) {}'])
            self.assertEqual(facts, [])
            self.assertIn('NO_NEW_SOURCE', repeated)
            self.assertIn((target, 'Api.java'), project.api_context.source_reads)
            project._validate(target, candidate)
            self.assertTrue(project.round_succeeded)
            seen.append(description)

        agent.invoke.side_effect = port
        # Exercise retry wiring; semantic acceptance is outside this test's boundary.
        coverage = [dict(complete=False, missing=[0], members=[], need_not_ported=False),
                    dict(complete=True, missing=[], members=[], need_not_ported=False)]
        with patch.object(runtime, 'hunk_coverage_report', side_effect=coverage), \
                patch.object(runtime, 'CoverageReviewer', side_effect=StopIteration('review boundary')):
            with self.assertRaisesRegex(StopIteration, 'review boundary'):
                runtime.do_backport(agent, project, data, Mock(), str(dataset/'run.log'))
        self.assertEqual(len(seen), 2)
        inventory = json.loads((dataset/'merged_hunks/ported/merged-hunk-00.api-inventory.json').read_text())
        self.assertEqual(inventory['apis'][0]['ref'], target)
        self.assertEqual(inventory['apis'][0]['references'][0]['scope'], 'Caller.preferences')
        status = json.loads((dataset/'merged_hunks/ported/merged-hunk-00.status.json').read_text())
        self.assertEqual(status['context_reads']['missing'], [])
        self.assertIn('manager.reset(1, true)', (dataset/'complete.patch').read_text())

    def test_api_context_is_bounded_and_required_before_validate(self):
        inventory = api_inventory(SymbolIndex(self.root), self.target,
                                  self.base, self.upstream, self.hunks)
        context = ApiContext(inventory, 'group.json', page_limit=1, max_chars=2000)
        project = SimpleNamespace(api_context=context, query_progress=None,
                                  poc_succeeded=False, _validate=Mock(return_value='validated'))
        validate = create_validate_tool(project)
        blocked = validate.invoke({'ref': self.target, 'patch': 'candidate'})
        self.assertIn('CONTEXT_READS_INCOMPLETE', blocked)
        project._validate.assert_not_called()
        query = create_query_api_context_tool(project)
        rejected = json.loads(query.invoke({'symbol': '../../secret', 'record_type': 'all'}))
        self.assertIn('outside this group', rejected['error'])
        result = json.loads(query.invoke({'symbol': 'reset', 'record_type': 'all', 'limit': 1}))
        self.assertEqual(result['pages']['definitions']['total'], 1)
        self.assertEqual(result['pages']['references']['total'], 1)
        self.assertEqual(context.read_report()['missing'], [])
        self.assertEqual(validate.invoke({'ref': self.target, 'patch': 'candidate'}), 'validated')
        missing = json.loads(query.invoke({'symbol': 'reset', 'path': '../../secret'}))
        self.assertEqual(missing['definitions'], [])
        self.assertEqual(missing['references'], [])
        too_many = json.loads(query.invoke({'symbol': 'reset', 'limit': 2}))
        self.assertIn('limit must be between 1 and 1', too_many['error'])

    def test_api_context_omits_oversized_records_without_cutting_json(self):
        inventory = {'apis': [{'symbol': 'reset', 'definitions': [],
                               'references': [{'path': 'A.java', 'line': 1, 'end_line': 2,
                                               'expression': 'x' * 5000}]}],
                     'upstream_signature_changes': []}
        context = ApiContext(inventory, 'group.json', max_chars=2000)
        result = context.query('reset')
        self.assertEqual(result['references'], [])
        self.assertEqual(result['oversized'][0]['path'], 'A.java')
        self.assertNotIn('x' * 5000, json.dumps(result))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 2000)
        self.assertEqual(context.read_report()['missing'], ['reset'])

    def test_undelivered_api_query_cannot_complete_required_reads(self):
        inventory = {'apis': [dict(symbol='reset', ref='base', definitions=[], references=[
            dict(path='A.java', line=1, end_line=1, expression='reset(1)')])]}
        context = ApiContext(inventory, 'group.json')
        project = SimpleNamespace(api_context=context, query_progress=QueryProgress(),
                                  final_repair_reads_remaining=0)
        query = create_query_api_context_tool(project)
        self.assertIn('READ_BUDGET_EXHAUSTED', query.invoke({'symbol': 'reset'}))
        self.assertEqual(context.read_report()['missing'], ['reset'])
        project.final_repair_reads_remaining = 2
        project.final_repair_read_limit = 100
        self.assertIn('READ OUTPUT TRUNCATED', query.invoke({'symbol': 'reset'}))
        self.assertEqual(context.read_report()['missing'], ['reset'])
        project.final_repair_read_limit = 40000
        query.invoke({'symbol': 'reset'})
        self.assertEqual(context.read_report()['missing'], [])

    def test_viewcode_completes_only_delivered_oversized_source_ranges(self):
        from tools.project import creat_viewcode_tool
        inventory = api_inventory(SymbolIndex(self.root), self.target,
                                  self.base, self.upstream, self.hunks)
        for row in inventory['apis'][0]['references']:
            row['expression'] = 'x' * 5000
        context = ApiContext(inventory, 'group.json', max_chars=2000)
        project = Project.__new__(Project)
        project.repo = self.repo
        project.api_context = context
        project.query_progress = QueryProgress(stale_limit=1)
        context.query('reset')
        view = creat_viewcode_tool(project)
        record = inventory['apis'][0]['references'][0]
        args = dict(ref=inventory['apis'][0]['ref'], path=record['path'],
                    startline=record['line'], endline=record['end_line'])
        project.final_repair_read_limit = 10
        self.assertIn('TRUNCATED', view.invoke(args))
        self.assertEqual(context.read_report()['missing'], ['reset'])
        project.final_repair_read_limit = 40000
        view.invoke(args)
        self.assertEqual(context.read_report()['missing'], [])

    def test_summary_model_receives_syntax_and_cannot_replace_program_fields(self):
        client = script('summarize_and_group.py')
        source_patch, output = self.root/'input.patch', self.root/'summary.json'
        source_patch.write_text(self.patch_text)

        def summarize(key, model, prompt):
            facts = json.loads(prompt.split('程序提取的语法事实：\n', 1)[1])
            self.assertEqual(facts['signature_changes'][0]['old_types'], ['int'])
            self.assertEqual(facts['signature_changes'][0]['new_types'], ['int', 'boolean'])
            return dict(kind='declare', summary='preserve reset behavior',
                        symbol='inventedByModel', param_change='remove', semantic_contract={})

        with patch.object(client, 'call_json', side_effect=summarize) as llm, \
                patch.object(sys, 'argv', ['summary', str(source_patch), str(output), 'fixture-model',
                                         '--repo', str(self.root), '--commit', self.upstream]), \
                patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'fixture-no-network'}), \
                patch('sys.stdout', new_callable=io.StringIO):
            client.main()
        self.assertEqual(llm.call_count, 1)
        result = json.loads(output.read_text())[0]
        self.assertEqual((result['symbol'], result['param_change']), ('reset', 'add'))
        self.assertEqual(result['source_structure']['after_ref'], self.upstream)






    def test_mapping_rejects_call_occurrence_and_preserves_actual_declaration(self):
        mapper = script('backport_patch/cases/CVE-2025-48649/scripts/map_semantics.py')
        response = {'hunk_mappings': [dict(hunk=0, mapping_type='direct', mappings=[
            dict(path='Caller.java', symbol='reset', evidence='name occurs here')])]}
        candidates = {0: [(1, 'Caller.java', [], []), (1, 'Api.java', [], [])]}
        rows, errors = mapper.normalize_group_response(response, [0], candidates, str(self.root), self.target)
        self.assertEqual(rows[0]['mapping_type'], 'unresolved')
        self.assertEqual(rows[0]['mappings'], [])
        self.assertTrue(any('declaration not established' in error for error in errors))
        response['hunk_mappings'][0]['mappings'][0].update(path='Api.java', symbol='Api.reset')
        rows, errors = mapper.normalize_group_response(response, [0], candidates, str(self.root), self.target)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]['mappings'][0]['definitions'][0]['signature'], 'void reset(int user)')

    def test_complete_candidate_duplicate_check_runs_before_build(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        (self.root/'Api.java').write_text(self.old[:-2] + ' void reset(int renamed) {}\n}\n')
        duplicate = self.repo.git.diff() + '\n'
        self.repo.git.reset('--hard')
        (self.root/'build.sh').write_text('touch build-ran\n')
        project.full_patch_base = self.target
        result = project._compile_patch(self.target, duplicate)
        self.assertIn('STRUCTURAL_VALIDATION_FAILED', result)
        self.assertFalse(project.compile_succeeded)
        self.assertFalse((self.root/'build-ran').exists())
        self.assertEqual((self.root/'Api.java').read_text(), self.old)

    def test_rejected_patch_with_new_file_removes_only_its_new_file(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.keep_tree_after_apply = True
        (self.root/'unrelated.txt').write_text('keep user file')
        (self.root/'Added.java').write_text('class Added { void f(int x) {} void f(int y) {} }\n')
        self.repo.index.add(['Added.java'])
        candidate = self.repo.git.diff('--cached') + '\n'
        self.repo.git.reset('--hard')
        result = project._apply_hunk(self.target, candidate)
        self.assertIn('STRUCTURAL_VALIDATION_FAILED', result)
        self.assertFalse((self.root/'Added.java').exists())
        self.assertEqual((self.root/'unrelated.txt').read_text(), 'keep user file')
        self.assertTrue((self.root/'Caller.java').exists())

    @unittest.skipUnless(shutil.which('javac'), 'Java compiler required')
    def test_duplicate_check_agrees_with_java_compiler_for_supported_cases(self):
        samples = [
            ('class A { void f(int x) {} void f(int y) {} }', False),
            ('class A { void f(int x, String x) {} }', False),
            ('class A { void f(int[] a) {} void f(int a[]) {} }', False),
            ('class A { A(int x) {} void A(int x) {} void f(int x) {} void f(String x) {} }', True),
            ('class A { <T extends Number> void f(T x) {} <T extends String> void f(T x) {} }', True),
        ]
        for code, valid in samples:
            with self.subTest(code=code):
                source = self.root/'A.java'
                source.write_text(code)
                compile_result = subprocess.run(['javac', '-d', str(self.root/'classes'), str(source)],
                                                capture_output=True, text=True, timeout=30)
                self.assertEqual(compile_result.returncode == 0, valid, compile_result.stderr)
                delta = structural_delta(Source('A.java', b'class A {}'), Source('A.java', code.encode()))
                self.assertEqual(not bool(delta['errors']), valid)


if __name__ == '__main__':
    unittest.main()
