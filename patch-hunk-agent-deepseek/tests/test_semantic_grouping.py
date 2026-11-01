"""Semantic partitions must cover the inputs without numerical slicing."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import MagicMock, Mock, patch

SCRIPT = (Path(__file__).resolve().parents[1] /
          'data_ncwang/backport_patch/cases/CVE-2025-48649/scripts/v6_group.py')


def load_module():
    utils = ModuleType('tools.utils')
    utils.dump_merged_hunks = utils.split_patch_indexed = None
    spec = importlib.util.spec_from_file_location('semantic_grouping_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'tools.utils': utils}), patch.object(sys, 'path', sys.path[:]):
        spec.loader.exec_module(module)
    return module


def entry(hunks, dependencies=()):
    return {'hunks': hunks, 'goal': 'Maintain one API contract',
            'why_together': 'Declaration and affected callers must agree',
            'split_review': 'Changing either side alone breaks this signature',
            'depends_on': list(dependencies)}


def load_client():
    utils = ModuleType('tools.utils')
    utils.split_patch_indexed = None
    spec = importlib.util.spec_from_file_location('summary_client_test', SCRIPT.parents[4] / 'summarize_and_group.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'tools.utils': utils}), patch.object(sys, 'path', sys.path[:]):
        spec.loader.exec_module(module)
    return module


class SemanticGroupingTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_large_atomic_group_is_not_sliced_at_an_old_environment_limit(self):
        members = list(range(12))
        with patch.dict(os.environ, {'MAX_GROUP_HUNKS': '1'}):
            plan = self.module.validate_group_plan({'groups': [entry(members)]}, members)
        self.assertEqual([g['hunks'] for g in plan], [members])

    def test_shared_graph_component_can_be_partitioned_by_semantics(self):
        summaries = [{'hunk': i, 'file': 'Same.java', 'symbol': 'sameMethod',
                      'source_structure': {}} for i in range(6)]
        hunks = [{'idx': i, 'text': '+change%d' % i} for i in range(6)]
        proposal = {'reference_reviews': [], 'test_reviews': [],
                    'requires': [{'provider': 0, 'consumer': 2, 'reason': 'new helper'}]}
        ask = Mock(return_value=proposal)
        plan = self.module.semantic_groups(summaries, hunks, [list(range(6))],
                                          [('E2', 0, i, 'sameMethod') for i in range(1, 6)], ask)
        self.assertEqual([g['hunks'] for g in plan], [[i] for i in range(6)])
        self.assertEqual(plan[2]['depends_on'], [0])
        self.assertNotIn('"candidate_edges":', ask.call_args.args[0])
        self.assertEqual(ask.call_count, 1)

    def test_prefilter_dependency_review_is_reused_as_order_not_forced_merge(self):
        summaries = [{'hunk': h, 'source_structure': {}, 'dependency_review': {
            'reason': 'Reviewed before filtering', 'requires': [] if h != 2 else [
                {'provider': 0, 'reason': 'Concrete behavior required by assertion'}]}}
            for h in range(3)]
        hunks = [{'idx': h, 'text': '+change'} for h in range(3)]
        ask = Mock(side_effect=AssertionError('must not guess dependencies a second time'))
        plan = self.module.semantic_groups(summaries, hunks, [], [], ask)
        self.assertEqual([g['hunks'] for g in plan], [[0], [1], [2]])
        self.assertEqual(plan[2]['depends_on'], [0])
        ask.assert_not_called()

    def test_import_setup_is_bound_only_to_an_evidenced_consumer(self):
        from tools.source_structure import Source
        source = Source('A.java', b'''import pkg.Validator;
class A {
 void first() { unrelated(); }
 void second() { Validator.check(); }
}''')
        lines = [1, 3, 4]
        summaries = [{'hunk': h, 'file': 'A.java', 'source_structure': {
            'before': {'scopes': []}, 'after': source.evidence([line], line, 1)},
            'dependency_review': {'requires': []}} for h, line in enumerate(lines)]
        hunks = [{'idx': h, 'text': '+'+source.data.decode().splitlines()[line-1]}
                 for h, line in enumerate(lines)]
        plan = self.module.semantic_groups(summaries, hunks, [], [], Mock())
        self.assertIn([0, 2], [g['hunks'] for g in plan])
        self.assertIn([1], [g['hunks'] for g in plan])

    def test_missing_duplicate_unknown_and_forward_dependency_are_rejected(self):
        invalid = [
            {'groups': [entry([0])]},
            {'groups': [entry([0, 0]), entry([1])]},
            {'groups': [entry([0]), entry([0, 1])]},
            {'groups': [entry([0, 1, 2])]},
            {'groups': [entry([True, 1])]},
            {'groups': [entry([0], [1]), entry([1])]},
            {'groups': [entry([0, 1], [0])]},
            {'groups': [{'hunks': [0, 1]}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.module.validate_group_plan(payload, [0, 1])

    def test_invalid_plan_is_retried_with_feedback_not_mechanically_repaired(self):
        summaries = [{'hunk': i, 'source_structure': {}} for i in [0, 1]]
        hunks = [{'idx': i, 'text': '+change'} for i in [0, 1]]
        partial = {'reference_reviews': [{'id': 'invented'}], 'test_reviews': [], 'requires': []}
        complete = {'reference_reviews': [], 'test_reviews': [], 'requires': []}
        ask = Mock(side_effect=[partial, complete])
        with patch.dict(os.environ, {'GROUP_RETRIES': '1'}):
            result = self.module.semantic_groups(summaries, hunks, [[0, 1]], [], ask)
        self.assertEqual([g['hunks'] for g in result], [[0], [1]])
        self.assertIn('review each reference candidate', ask.call_args_list[1].args[0])
        ask = Mock(return_value=partial)
        with patch.dict(os.environ, {'GROUP_RETRIES': '0'}), self.assertRaisesRegex(ValueError, 'GROUPING_INVALID'):
            self.module.semantic_groups(summaries, hunks, [[0, 1]], [], ask)

    def test_build_returns_uncapped_components_and_handles_empty_triage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [{'hunk': i, 'file': 'Same.java', 'symbol': 'sameMethod'} for i in range(12)]
            hunks = [{'idx': i, 'file': 'Same.java', 'text': '@@ -1 +1 @@\n-a\n+b'} for i in range(12)]
            (root / 'summaries.json').write_text(json.dumps(records))
            (root / 'input.patch').write_text('fixture')
            with patch.object(self.module, 'split_patch_indexed', return_value=('', hunks)), \
                    patch.object(self.module, 'resolve_ref', side_effect=lambda repo, ref: ref), \
                    patch.object(self.module, 'hunk_evidence', return_value={
                        'before': {'scopes': [], 'range': [1, 1]}, 'after': {'scopes': []}}), \
                    patch.dict(os.environ, {'MAX_GROUP_HUNKS': '2'}):
                built = self.module.build('repo', 'commit', str(root/'input.patch'), str(root/'summaries.json'))
                self.assertEqual(built[2], [list(range(12))])
                empty = self.module.build('repo', 'commit', str(root/'input.patch'), str(root/'summaries.json'), keep=set())
                self.assertEqual(empty[0:4], ([], [], [], []))

    def test_enclosing_only_does_not_call_grouping_model(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ['v6_group.py', '--enclosing-only', 'repo', 'commit', 'target', 'patch', 'summary', directory]
            with patch.object(sys, 'argv', args), \
                    patch.object(self.module, 'build', return_value=([{'hunk': 0}], [], [[0]], [], {})), \
                    patch.object(self.module, 'semantic_groups') as planner:
                self.module.main()
            planner.assert_not_called()
            self.assertTrue((Path(directory)/'summaries_with_enclosing.json').is_file())
            self.assertFalse((Path(directory)/'groups.json').exists())

    def test_empty_triage_keeps_explicit_exclusions_and_review_hints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            triage = root/'triaged.json'
            triage.write_text(json.dumps([
                {'hunk': 0, 'port': False, 'review_required': True, 'closure_symbols': ['shared']},
                {'hunk': 1, 'port': False}]))
            args = ['v6_group.py', '--triage', str(triage), 'repo', 'commit', 'target',
                    'patch', 'summary', directory]
            with patch.object(sys, 'argv', args), \
                    patch.object(self.module, 'build', return_value=([], [], [], [], {})), \
                    patch.object(self.module, 'semantic_groups') as planner:
                self.module.main()
            planner.assert_not_called()
            spec = json.loads((root/'groups.json').read_text())
            self.assertEqual(spec['groups'], [])
            self.assertEqual(spec['excluded_hunks'], [0, 1])
            self.assertEqual(spec['review_only_hunks'], [0])
            self.assertEqual(spec['skipped'][0]['closure_symbols'], ['shared'])

    def test_signature_self_audit_cannot_contradict_the_partition(self):
        audit = [{'signature': 'I.send(x) -> I.send(x, mode)',
                  'definition_hunks': [0], 'affected_call_hunks': [1]}]
        with self.assertRaisesRegex(ValueError, 'signature audit split'):
            self.module.validate_group_plan({'signature_audit': audit,
                'groups': [entry([0]), entry([1], [0])]}, [0, 1])
        plan = self.module.validate_group_plan({'signature_audit': audit,
                'groups': [entry([0, 1])]}, [0, 1])
        self.assertEqual(plan[0]['signature_audit'], audit)

    def test_model_receives_source_evidence_without_component_anchoring(self):
        evidence = {'before': {'status': 'unsupported'}, 'after': {'status': 'unsupported'}}
        ask = Mock(return_value={'reference_reviews': [], 'test_reviews': [], 'requires': []})
        self.module.semantic_groups([{'hunk': i, 'source_structure': evidence} for i in [0, 1]],
                                    [{'idx': i, 'text': '+x'} for i in [0, 1]], [[0, 1]], [], ask)
        prompt = ask.call_args.args[0]
        self.assertIn('"scopes":', prompt)
        self.assertNotIn('"candidate_components":', prompt)

    def test_single_hunk_requires_no_partition_decision_or_model_call(self):
        ask = Mock()
        result = self.module.semantic_groups([{'hunk': 81, 'summary': 'One change'}],
                                             [{'idx': 81, 'text': '+x'}], [[81]], [], ask)
        self.assertEqual([g['hunks'] for g in result], [[81]])
        ask.assert_not_called()

    def test_only_cycles_merge_and_dependencies_have_canonical_reduction(self):
        # Renumbered, unordered input: a real test/signature cycle merges 40/10/70,
        # but the helper and two later guard paths remain separate.
        ids = [90, 70, 10, 30, 40, 20]
        inputs = {'hunks': [{'hunk': h} for h in ids], 'parameter_prerequisites': [],
                  'anchors': [{'id': 'S1', 'kind': 'signature', 'name': 'send', 'members': [10, 40],
                               'definitions': [{'hunk': 40}], 'calls': [{'hunk': 10}]}]}
        payload = {'goals': [{'hunk': h, 'goal': str(h)} for h in ids],
                   'signature_reviews': [{'anchor': 'S1', 'calls': [10], 'rejected_calls': []}],
                   'bindings': [{'anchor': 'S1', 'hunks': [40, 10], 'reason': 'signature and test adaptation'}],
                   'requires': [{'provider': a, 'consumer': b, 'reason': 'concrete prerequisite'}
                                for a, b in [(20, 40), (40, 70), (70, 10), (40, 30), (40, 90), (20, 90)]]}
        with patch.dict(os.environ, {'MAX_GROUP_HUNKS': '1'}):
            plan = self.module.plan_from_relations(payload, inputs)
        self.assertEqual([p['hunks'] for p in plan], [[20], [10, 40, 70], [30], [90]])
        self.assertEqual([p['depends_on'] for p in plan], [[], [20], [10], [10]])
        payload['bindings'][0]['hunks'].append(90)
        with self.assertRaisesRegex(ValueError, 'subset of one supplied anchor'):
            self.module.plan_from_relations(payload, inputs)

    def test_signature_candidates_must_be_reviewed_and_bound(self):
        inputs = {'hunks': [{'hunk': h} for h in [4, 8, 12]], 'parameter_prerequisites': [],
                  'anchors': [{'id': 'S1', 'kind': 'signature', 'name': 'send', 'members': [4, 8, 12],
                               'definitions': [{'hunk': 4}], 'calls': [{'hunk': 8}, {'hunk': 12}]}]}
        payload = {'goals': [{'hunk': h, 'goal': 'change'} for h in [4, 8, 12]],
                   'signature_reviews': [{'anchor': 'S1', 'calls': [8], 'rejected_calls': []}]}
        with self.assertRaisesRegex(ValueError, 'classify every call'):
            self.module.plan_from_relations(payload, inputs)
        payload['signature_reviews'][0]['rejected_calls'] = [{'hunk': 12, 'reason': 'different receiver overload'}]
        self.assertEqual([p['hunks'] for p in self.module.plan_from_relations(payload, inputs)], [[4, 8], [12]])

    def test_local_reviews_cannot_silently_omit_a_late_assertion(self):
        inputs = {'hunks': [{'hunk': h} for h in [10, 20, 50]], 'parameter_prerequisites': [],
                  'anchors': [{'id': 'L1', 'kind': 'local', 'members': [10, 20, 50]}]}
        payload = {'local_reviews': [{'anchor': 'L1', 'units': [
            {'hunks': [10, 20], 'reason': 'limit declaration and clamp'}]}]}
        with self.assertRaisesRegex(ValueError, 'partition every member'):
            self.module.plan_from_relations(payload, inputs)
        payload['local_reviews'][0]['units'][0]['hunks'].append(50)
        plan = self.module.plan_from_relations(payload, inputs)
        self.assertEqual([p['hunks'] for p in plan], [[10, 20, 50]])

    def test_private_overload_binding_requires_shared_parameter_and_delegation(self):
        from tools.source_structure import Source, signature_changes
        before = Source('Mailbox.java', b'''class Mailbox {
 private void dispatch(int x) {
  dispatch(x, null);
 }
 private void dispatch(int x, Callback done) {}
}''')
        after = Source('Mailbox.java', b'''class Mailbox {
 private void dispatch(int x, boolean policy) {
  dispatch(x, policy, null);
 }
 private void dispatch(int x, boolean policy, Callback done) {}
}''')
        records, hunks = [], []
        for h, line in [(11, 2), (44, 3), (97, 5)]:
            evidence = {'before': before.evidence([line], line, 1),
                        'after': after.evidence([line], line, 1)}
            evidence['signature_changes'] = signature_changes(before, after, evidence)
            records.append({'hunk': h, 'file': 'Mailbox.java', 'source_structure': evidence})
            hunks.append({'idx': h, 'text': '-old\n+new'})
        anchors = self.module.relation_inputs(records, hunks)['anchors']
        families = [a for a in anchors if a.get('fixed')]
        self.assertEqual([a['members'] for a in families], [[11, 97]])
        self.assertEqual(families[0]['added_parameters'], ['policy'])
        records[1]['source_structure']['after']['calls'] = []
        self.assertFalse(any(a.get('fixed') for a in self.module.relation_inputs(records, hunks)['anchors']))

    def test_two_affected_call_expressions_in_one_hunk_are_one_member(self):
        summaries = [{'hunk': h, 'file': 'A.java', 'source_structure': {}} for h in [10, 90]]
        hunks = [{'idx': h, 'text': '+change'} for h in [10, 90]]
        inputs = {'hunks': [{'hunk': h, 'file': 'A.java', 'diff': '+change',
                            'source_structure': self.module.compact_structure({})} for h in [10, 90]],
                  'parameter_prerequisites': [], 'anchors': [
                      {'id': 'S1', 'kind': 'signature', 'name': 'dispatch', 'members': [10, 90],
                       'definitions': [{'hunk': 10}], 'calls': [{'hunk': 90}]}]}
        ask = Mock(side_effect=[{'calls': [{'hunk': 90, 'expression': 'dispatch(a)'},
                                         {'hunk': 90, 'expression': 'dispatch(b)'}],
                                'rejected_calls': [{'hunk': 90, 'reason': 'A third call in this hunk is newly added'}]},
                                {'reference_reviews': [], 'test_reviews': [], 'requires': []}])
        with patch.object(self.module, 'relation_inputs', return_value=inputs):
            plan = self.module.semantic_groups(summaries, hunks, [], [], ask)
        self.assertEqual([p['hunks'] for p in plan], [[10, 90]])
        self.assertEqual(len(plan[0]['signature_reviews'][0]['other_calls_in_affected_hunks']), 1)
        self.assertEqual(ask.call_count, 2)

    def test_parameter_named_test_mode_does_not_turn_production_code_into_a_test(self):
        from tools.source_structure import Source
        for signature, expected in [('void calculate(boolean testMode)', False),
                                    ('@Test void runScenario()', True)]:
            with self.subTest(signature=signature):
                src = Source('A.java', ('class A {\n '+signature+' {\n first();\n second();\n }\n}').encode())
                records = [{'hunk': h, 'file': 'A.java', 'source_structure': {
                    side: src.evidence([line], line, 1) for side in ['before', 'after']}}
                    for h, line in [(17, 3), (61, 4)]]
                inputs = self.module.relation_inputs(records, [{'idx': h, 'text': '+change'} for h in [17, 61]])
                local = [a for a in inputs['anchors'] if a['kind'] == 'local']
                self.assertEqual([a['fixed'] for a in local], [expected])

    def test_constructor_initialization_copy_and_alternative_branches_form_one_unit(self):
        from tools.source_structure import Source
        source = Source('A.java', b'''class A {
 int a, b;
 A(boolean choose) {
  a = readValue();
  if (choose) {
   b = a;
  } else {
   b = 0;
  }
 }
}''')
        records, hunks = [], []
        for h, line, text in [(17, 4, '+a = readValue();'), (61, 6, '+b = a;'), (88, 8, '+b = 0;')]:
            records.append({'hunk': h, 'file': 'A.java', 'source_structure': {
                'before': source.evidence([], line, 1), 'after': source.evidence([line], line, 1)}})
            hunks.append({'idx': h, 'text': text})
        inputs = self.module.relation_inputs(records, hunks)
        local = next(a for a in inputs['anchors'] if a['kind'] == 'local')
        self.assertTrue(local['constructor'])
        payload = {'local_reviews': [{'anchor': local['id'], 'units': [
            {'hunks': [h], 'reason': 'proposed fragment'} for h in [17, 61, 88]]}]}
        self.assertEqual([g['hunks'] for g in self.module.plan_from_relations(payload, inputs)], [[17, 61, 88]])
        records[1]['source_structure']['after']['writes'][0]['value'] = 'unrelatedValue'
        inputs = self.module.relation_inputs(records, hunks)
        self.assertEqual([g['hunks'] for g in self.module.plan_from_relations(payload, inputs)], [[17], [61, 88]])

    def test_removed_config_reference_precedes_definition_and_reverse_is_retried(self):
        summaries = [{'hunk': h, 'file': name, 'source_structure': {}}
                     for h, name in [(80, 'flags.aconfig'), (13, 'caller.cc')]]
        hunks = [{'idx': 80, 'text': '-flag {\n- name: "sample_mode"\n-}'},
                 {'idx': 13, 'text': '-if (feature::sample_mode()) action();\n+action();'}]
        first = {'reference_reviews': [{'id': 'R1', 'required': True, 'reason': 'remove reference first'}],
                 'test_reviews': [], 'requires': [{'provider': 80, 'consumer': 13, 'reason': 'wrong direction'}]}
        second = {**first, 'requires': []}
        ask = Mock(side_effect=[first, second])
        with patch.dict(os.environ, {'GROUP_RETRIES': '1'}):
            result = self.module.semantic_groups(summaries, hunks, [], [], ask)
        self.assertEqual([g['hunks'] for g in result], [[13], [80]])
        self.assertEqual(result[1]['depends_on'], [13])
        self.assertIn('reversed deletion order', ask.call_args_list[1].args[0])

    def test_test_assertions_cannot_precede_the_behavior_they_verify(self):
        audit = [{'test_hunks': [1], 'implementation_hunks': [2],
                  'reason': 'The added assertion needs the flag guard, not only its parameter'}]
        with self.assertRaisesRegex(ValueError, 'test hunk 1 needs behavior hunk 2'):
            self.module.validate_group_plan({'behavior_audit': audit,
                'groups': [entry([0, 1]), entry([2], [0])]}, [0, 1, 2])
        self.module.validate_group_plan({'behavior_audit': audit,
            'groups': [entry([0, 1, 2])]}, [0, 1, 2])
        self.module.validate_group_plan({'behavior_audit': audit,
            'groups': [entry([0, 2]), entry([1], [2])]}, [0, 1, 2])

    def test_export_preserves_group_goals_and_prerequisite_hunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch_text = ("diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n"
                          "@@ -1 +1 @@\n-oldA\n+newA\n"
                          "diff --git a/B.java b/B.java\n--- a/B.java\n+++ b/B.java\n"
                          "@@ -1 +1 @@\n-oldB\n+newB\n"
                          "diff --git a/C.java b/C.java\n--- a/C.java\n+++ b/C.java\n"
                          "@@ -1 +1 @@\n-oldC\n+newC\n")
            (root/'input.patch').write_text(patch_text)
            details = [entry([0]), entry([1], [0])]
            details[0]['goal'] = 'Introduce prerequisite'
            details[1]['goal'] = 'Use existing prerequisite'
            spec = {'groups': [[0], [1]], 'group_details': details,
                    'excluded_hunks': [2], 'review_only_hunks': [2],
                    'hunks': [{'hunk': i, 'file': name, 'summary': 'fixture'}
                              for i, name in enumerate(['A.java', 'B.java'])]}
            (root/'groups.json').write_text(json.dumps(spec))
            mapping = {'groups': [{'hunk_mappings': [
                {'hunk': i, 'mapping_type': 'direct', 'mappings': [{'path': name}]}
                for i, name in enumerate(['A.java', 'B.java'])]}]}
            (root/'mapping.json').write_text(json.dumps(mapping))
            result = subprocess.run([sys.executable, '-B', str(SCRIPT.with_name('build_v6_export.py')),
                str(root/'input.patch'), 'unused-repo', 'unused-ref', str(root/'groups.json'),
                str(root/'export'), str(root/'mapping.json')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            index = json.loads((root/'export/merged_hunks/original/index.json').read_text())
            self.assertEqual(len(index), 2)
            for actual, expected in zip(index, details):
                self.assertEqual(actual['hunks'], expected['hunks'])
                self.assertTrue(actual['concern'].startswith(expected['goal']))
                self.assertEqual(actual['why'], expected['why_together'])
            self.assertIn('Prerequisites in earlier groups: hunks [0]', index[1]['hints'])
            exported = json.loads((root/'export/hunk_groups.json').read_text())
            self.assertEqual(exported['excluded_hunks'], [2])
            self.assertEqual(exported['review_only_hunks'], [2])
            from tools.utils import group_patch
            _, loaded = group_patch(patch_text, exported, True)
            self.assertEqual([g['hunks'] for g in loaded], [[0], [1]])

    def test_provider_quota_error_is_reported_without_key_or_retries(self):
        module = load_client()
        response = Mock(status_code=429)
        response.json.return_value = {'error': {'code': 'insufficient_quota',
            'message': 'Your account balance is insufficient: private-test-key'}}
        httpx = MagicMock()
        client = httpx.Client.return_value.__enter__.return_value
        client.post.return_value = response
        with patch.dict(sys.modules, {'httpx': httpx}), patch.object(module.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'LLM_RESPONSE_ERROR http=429') as failure:
                module.call('private-test-key', 'fixture-model', 'fixture prompt')
        self.assertNotIn('private-test-key', str(failure.exception))
        self.assertIn('[redacted]', str(failure.exception))
        self.assertEqual(client.post.call_count, 1)
        sleep.assert_not_called()

    def test_provider_options_and_timeout_survive_json_repair(self):
        module = load_client()
        options = {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'}
        with patch.object(module, 'call', side_effect=['broken', '{"groups": []}']) as call:
            self.assertEqual(module.call_json('test-key', 'configured-model', 'prompt',
                             request_options=options, read_timeout=300), {'groups': []})
        self.assertEqual(call.call_count, 2)
        for invocation in call.call_args_list:
            self.assertEqual(invocation.kwargs, {'request_options': options, 'read_timeout': 300})
        for options in [{'messages': []}, {'model': 'different'}, {'stream': True}, []]:
            with self.assertRaises(ValueError):
                module.call('test-key', 'configured-model', 'prompt', request_options=options)


if __name__ == '__main__':
    unittest.main()
