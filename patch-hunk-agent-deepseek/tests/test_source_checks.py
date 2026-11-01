"""Deterministic facts must survive target-only callers, overloads and dirty trees."""
import importlib.util
import io
import json
import os
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
from tools.source_checks import ApiCatalog, api_inventory, structural_delta
from tools.project import Project, create_find_references_tool, creat_locate_symbol_tool
from tools.porting_context import QueryProgress
from tools.utils import split_patch_indexed, hunk_coverage_report


def script(name):
    path = ROOT / ('data_ncwang/' + name)
    spec = importlib.util.spec_from_file_location('test_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceChecksTests(unittest.TestCase):
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

    def test_failed_candidate_fact_collection_rolls_back(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.target))
        project.keep_tree_after_apply = True
        with patch('tools.project.candidate_api_inventory', side_effect=RuntimeError('index failed')):
            with self.assertRaisesRegex(RuntimeError, 'index failed'):
                project._apply_hunk(self.target, self.patch_text)
        self.assertEqual(self.repo.git.status('--porcelain'), '')
        self.assertFalse(project.round_succeeded)

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
        self.assertIn('int user', project._viewcode('HEAD', 'Api.java', 1, 2))
        self.repo.git.reset('--hard')
        self.write_commit({'Api.java': self.new}, 'new ref')
        self.assertIn('boolean restore', project._viewcode('HEAD', 'Api.java', 1, 2))
        self.assertIn('outside the file', project._viewcode('HEAD', 'Api.java', 300, 400))

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
            self.assertIn('[TARGET API FACTS]', description)
            self.assertIn('Caller.preferences', description)
            self.assertIn('manager.reset(1)', description)
            self.assertIn('"argument_count": 1', description)
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
        self.assertIn('manager.reset(1, true)', (dataset/'complete.patch').read_text())

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
