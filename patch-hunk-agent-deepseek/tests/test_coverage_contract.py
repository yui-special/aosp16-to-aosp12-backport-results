"""Semantic decisions must retain relevance and prove the final candidate's coverage."""
import importlib.util
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'
sys.path.insert(0, str(SRC))
from agent.coverage import (CoverageReviewer, _judge_evidence, _retry_evidence,
                            artifact_tree, read_blob, verify_review, repair_findings)
from tools.source_structure import hunk_evidence, changed_scope_context
from agent.invoke_llm import evaluate_global_artifact
from tools.project import Project, FinalValidationUnavailable
from tools.utils import group_patch, split_patch_indexed, uncovered_hunks
from git import Repo

spec = importlib.util.spec_from_file_location('triage_coverage_test', ROOT / 'data_ncwang/triage.py')
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)


class TriageRelevanceTests(unittest.TestCase):
    def run_postprocess(self, decision, hits):
        rows = [dict(hunk=0, port=decision, category='fix' if decision else 'unrelated', reason='original')]
        summaries = [dict(det_enclosing='AbsentType', kind='logic', symbol='',
                          semantic_contract={'search_terms': ['@Rule', 'JUnit4']})]
        with patch.object(triage, 'git_count', side_effect=lambda repo, ref, term: hits.get(term, 0)):
            triage.postprocess(rows, [{'text': '+change;'}], summaries, '/repo', 'base')
        return rows[0]

    def test_generic_target_terms_cannot_promote_unrelated_test_infrastructure(self):
        row = self.run_postprocess(False, {'@Rule': 10, 'JUnit4': 20})
        self.assertIs(row['port'], False)
        self.assertEqual(row['category'], 'unrelated')
        self.assertEqual(row['initial_decision']['port'], False)

    def test_absent_names_cannot_discard_a_relevant_fix(self):
        row = self.run_postprocess(True, {})
        self.assertIs(row['port'], True)
        self.assertTrue(row['needs_semantic_mapping'])
        self.assertNotIn('func_in_target', row)

    def test_pure_aconfig_cleanup_is_decided_without_a_model(self):
        hunks = [
            {'idx': 0, 'file': 'windowing_frontend.aconfig', 'text':
                '@@ -1 +1 @@\n-flag {\n-    name: "keep_app_window_hide_while_locked"\n-    is_fixed_read_only: true'},
            {'idx': 1, 'file': 'WindowState.java', 'text':
                '@@ -1 +1 @@\n-if (keepAppWindowHideWhileLocked()) action();\n+action();'},
        ]
        with patch.object(triage, 'git_count', return_value=2):
            decision = triage.mechanical_triage_decision(hunks[0], hunks, '/repo', 'base')
        self.assertIs(decision['port'], False)
        self.assertEqual(decision['category'], 'metadata')
        self.assertEqual(decision['decision_source'], 'deterministic_config_cleanup')
        self.assertEqual(decision['config_evidence']['target_occurrences'],
                         {'keep_app_window_hide_while_locked': 2, 'keepAppWindowHideWhileLocked': 2})
        self.assertIsNone(triage.mechanical_triage_decision(hunks[1], hunks, '', ''))
        self.assertIsNone(triage.mechanical_triage_decision(hunks[0], hunks[:1], '', ''))

        rows = [dict(hunk=0, port=False, initial_decision=decision),
                dict(hunk=1, port=True, category='fix', reason='behavior')]
        summaries = [{'hunk': 0}, {'hunk': 1}]
        from tools.hunk_dependencies import review_inputs, validate_review
        inputs = review_inputs(rows, hunks, summaries)
        answer = {'reviews': [
            {'hunk': 0, 'reason': 'cleanup follows behavior', 'requires': []},
            {'hunk': 1, 'reason': 'wrong reversed dependency', 'requires': [
                {'provider': 0, 'reason': 'incorrectly requires cleanup first',
                 'provider_evidence': {'side': 'before', 'quote': 'name: "keep_app_window_hide_while_locked"'},
                 'consumer_evidence': {'side': 'after', 'quote': 'action();'}}]}]}
        with self.assertRaisesRegex(ValueError, 'config cleanup cannot be a prerequisite'):
            validate_review(answer, inputs)

    def test_failed_model_decision_is_not_fabricated_from_keyword_hits(self):
        row = self.run_postprocess(None, {'@Rule': 10})
        self.assertIsNone(row['port'])

    def test_reviewed_selected_call_can_retain_its_required_definition(self):
        rows = [dict(hunk=0, port=False, reason='helper'), dict(hunk=1, port=True, reason='fix')]
        hunks = [{'text': '@@ -0,0 +1 @@\n+void requiredHelper() {}'},
                 {'text': '@@ -0,0 +1 @@\n+requiredHelper();'}]
        summaries = [dict(hunk=0, symbol='requiredHelper', kind='declare'),
                     dict(hunk=1, symbol='caller', kind='logic')]
        from tools.hunk_dependencies import review_inputs, validate_review
        reviews = validate_review({'reviews': [
            {'hunk': 0, 'reason': 'Definition has no prerequisites', 'requires': []},
            {'hunk': 1, 'reason': 'Caller requires definition', 'requires': [
                {'provider': 0, 'reason': 'Direct call of the newly defined helper',
                 'provider_evidence': {'side': 'after', 'quote': 'void requiredHelper() {}'},
                 'consumer_evidence': {'side': 'after', 'quote': 'requiredHelper();'}}]}]},
            review_inputs(rows, hunks, summaries))
        triage.postprocess(rows, hunks, summaries, '', 'base', reviews)
        self.assertTrue(rows[0]['port'])
        self.assertTrue(rows[0]['needs_semantic_mapping'])
        self.assertIn('被 hunk 1 依赖', rows[0]['reason'])


    def test_shared_symbol_requires_review_but_never_forces_a_skip_to_port(self):
        rows = [
            dict(hunk=0, port=True, category='fix', reason='production fix'),
            dict(hunk=1, port=False, category='unrelated', reason='test-only adaptation'),
        ]
        hunks = [
            {'text': '+clearApplicationUserData(caller, userId, restore);'},
            {'text': '+clearApplicationUserData(caller, observer, userId, restore);'},
        ]
        summaries = [
            dict(symbol='clearApplicationUserData', kind='logic'),
            dict(symbol='clearApplicationUserData', kind='logic'),
        ]
        triage.postprocess(rows, hunks, summaries, '', 'base')
        self.assertIs(rows[1]['port'], False)
        self.assertEqual(rows[1]['category'], 'unrelated')
        self.assertTrue(rows[1]['review_required'])
        self.assertEqual(rows[1]['triage_state'], 'review_only_closure')
        self.assertEqual(rows[1]['closure_symbols'], ['clearApplicationUserData'])
        self.assertEqual(rows[1]['initial_decision']['port'], False)

        snapshot = json.loads(json.dumps(rows, ensure_ascii=False))
        triage.postprocess(rows, hunks, summaries, '', 'base')
        self.assertEqual(rows, snapshot)

    def test_postprocess_repairs_a_stale_name_wide_promotion(self):
        rows = [
            dict(hunk=0, port=True, category='fix', reason='production fix'),
            dict(
                hunk=1,
                port=True,
                category='fix',
                reason='test-only adaptation | 符号闭包：clearApplicationUserData 这条 API 链必须整体移植',
            ),
        ]
        hunks = [{'text': '+clearApplicationUserData(caller, restore);'},
                 {'text': '+clearApplicationUserData(caller, observer, restore);'}]
        summaries = [dict(symbol='clearApplicationUserData', kind='logic'),
                     dict(symbol='clearApplicationUserData', kind='logic')]
        triage.postprocess(rows, hunks, summaries, '', 'base')
        self.assertIs(rows[1]['port'], False)
        self.assertEqual(rows[1]['category'], 'unknown')
        self.assertEqual(rows[1]['reason'], 'test-only adaptation')
        self.assertTrue(rows[1]['review_required'])

    def test_shared_symbol_does_not_turn_an_undecided_hunk_into_a_decision(self):
        rows = [
            dict(hunk=0, port=True, category='fix', reason='production fix'),
            dict(hunk=1, port=None, category='unknown', reason='triage failed'),
        ]
        hunks = [{'text': '+useApi();'}, {'text': '+useApiWithExtraArg();'}]
        summaries = [dict(symbol='useApi', kind='logic'),
                     dict(symbol='useApi', kind='logic')]
        triage.postprocess(rows, hunks, summaries, '', 'base')
        self.assertIsNone(rows[1]['port'])
        self.assertFalse(rows[1]['review_required'])
        self.assertEqual(rows[1]['triage_state'], 'undecided')

    def test_dependency_closure_does_not_turn_an_undecided_hunk_into_a_port(self):
        rows = [
            dict(hunk=0, port=None, category='unknown', reason='triage failed'),
            dict(hunk=1, port=True, category='fix', reason='production fix'),
        ]
        hunks = [{'text': '+void requiredHelper() {}'}, {'text': '+requiredHelper();'}]
        summaries = [dict(symbol='requiredHelper', kind='declare'),
                     dict(symbol='caller', kind='logic')]
        triage.postprocess(rows, hunks, summaries, '', 'base')
        self.assertIsNone(rows[0]['port'])
        self.assertFalse(rows[0]['needs_semantic_mapping'])
        self.assertEqual(rows[0]['triage_state'], 'undecided')

    def test_triage_excluded_hunk_is_not_reinserted_by_group_fallback(self):
        patch = (
            'diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n'
            '@@ -1 +1 @@\n-oldA\n+newA\n'
            'diff --git a/B.java b/B.java\n--- a/B.java\n+++ b/B.java\n'
            '@@ -1 +1 @@\n-oldB\n+newB\n'
        )
        _, hunks = split_patch_indexed(patch, True)
        spec = {'groups': [{'hunks': [0]}], 'excluded_hunks': [1]}
        self.assertEqual(uncovered_hunks(hunks, spec), [])
        _, merged = group_patch(patch, spec, True)
        self.assertEqual([group['hunks'] for group in merged], [[0]])


class SplitReviewJudgeTests(unittest.TestCase):
    def setUp(self):
        self.members = [dict(idx=8, file='Renamed.java', text='-allowAny();\n+restrict();')]
        self.candidate = 'diff --git a/Example.java b/Example.java\n'
        identity = json.dumps(['candidate-tree', 'Example.java', 2, 2])
        self.evidence_id = 'E' + hashlib.sha256(identity.encode()).hexdigest()[:16]

    def record(self):
        return {'members': [dict(idx=8, status='satisfied', reason='The actual target guard blocks this path.',
                                evidence=[dict(evidence_id=self.evidence_id)])]}

    def evidence_builder(self):
        builder = Mock()
        builder.registry = {self.evidence_id: dict(
            kind='candidate_source', path='Example.java', start_line=2, end_line=2,
            quote='  boolean allowed() { return false; }')}
        builder.build.return_value = dict(method='deterministic_candidate_tree_v1',
                                         candidate_changes=[], upstream_searches=[])
        return builder

    def review(self, llm, executor=None, retries=0, builder=None):
        reviewer = CoverageReviewer(llm, '.', 'base', retries=retries)
        builder = self.evidence_builder() if builder is None else builder
        with patch('agent.coverage.artifact_tree', return_value='candidate-tree'), \
                patch('agent.coverage.candidate_diff', return_value=self.candidate), \
                patch('agent.coverage.read_blob',
                      return_value='class Example {\n  boolean allowed() { return false; }\n}\n'), \
                patch('agent.coverage.CandidateEvidenceBuilder', return_value=builder):
            return reviewer.review(self.members, self.candidate)

    def test_deterministic_evidence_and_final_json_judge_are_separate(self):
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(
            content='review follows\n```json\n' + json.dumps(self.record()) + '\n```',
            response_metadata={'finish_reason': 'stop'})
        result = self.review(llm)
        self.assertTrue(result['complete'])
        self.assertEqual(llm.invoke.call_count, 1)
        messages = llm.invoke.call_args.args[0]
        self.assertEqual([message.type for message in messages], ['system', 'human'])
        judge = json.loads(messages[1].content)
        self.assertEqual(judge['deterministic_evidence']['method'],
                         'deterministic_candidate_tree_v1')
        self.assertNotIn('evidence_memo', judge)
        self.assertNotIn('tool_trace', judge)

    def test_prose_verdict_uses_local_conversion_without_another_model_call(self):
        llm = Mock()
        llm.invoke.side_effect = [
            SimpleNamespace(
                content=f'Hunk 8 is satisfied. Evidence {self.evidence_id} supports the target guard.',
                response_metadata={'finish_reason': 'stop'}),
            SimpleNamespace(
                content=json.dumps(self.record()),
                response_metadata={'finish_reason': 'stop'}),
        ]
        result = self.review(llm, retries=0)
        self.assertTrue(result['complete'])
        self.assertEqual(llm.invoke.call_count, 1)
        self.assertEqual(result['review_json_normalization']['method'], 'deterministic_explicit_verdicts')
        normalized = json.loads(result['review_json_normalization']['normalized_text'])
        self.assertEqual(normalized['members'][0]['status'], 'satisfied')

    def test_formatter_cannot_invent_a_verdict_for_an_unsupported_hunk(self):
        llm = Mock()
        llm.invoke.side_effect = [
            SimpleNamespace(content='The available prose does not decide hunk 8 clearly.'),
            SimpleNamespace(content=json.dumps({'members': []})),
        ]
        executor = Mock()
        executor.invoke.return_value = {
            'output': 'Evidence memo has no explicit decision.',
            'intermediate_steps': [],
        }
        result = self.review(llm, executor, retries=0)
        self.assertFalse(result['complete'])
        self.assertEqual(result['method'], 'semantic_review_failed')
        self.assertEqual(llm.invoke.call_count, 1)

    def test_formatter_preserves_an_explicit_missing_verdict(self):
        missing = {'members': [dict(
            idx=8,
            status='missing',
            reason='The target guard is absent.',
            evidence=[])]}
        llm = Mock()
        llm.invoke.side_effect = [
            SimpleNamespace(content='Hunk 8 is missing because the target guard is absent.'),
            SimpleNamespace(content=json.dumps(missing)),
        ]
        executor = Mock()
        executor.invoke.return_value = {
            'output': 'Evidence memo identifies the missing guard.',
            'intermediate_steps': [],
        }
        result = self.review(llm, executor, retries=0)
        self.assertFalse(result['complete'])
        self.assertEqual(result['members'][0]['status'], 'missing')
        self.assertEqual(result['missing'], [8])

    def test_formatter_output_cannot_bypass_candidate_evidence_validation(self):
        invented = {'members': [dict(
            idx=8,
            status='satisfied',
            reason='The target guard is present.',
            evidence=[{'evidence_id': 'Einvented'}])]}
        llm = Mock()
        llm.invoke.side_effect = [
            SimpleNamespace(content='Hunk 8 is satisfied by Einvented.'),
            SimpleNamespace(content=json.dumps(invented)),
        ]
        executor = Mock()
        executor.invoke.return_value = {
            'output': 'Evidence memo mentions a candidate guard.',
            'intermediate_steps': [],
        }
        result = self.review(llm, executor, retries=0)
        self.assertFalse(result['complete'])
        self.assertEqual(result['method'], 'semantic_review_failed')
        self.assertIn('Unknown candidate evidence ID', result['error'])

    def test_empty_judge_output_is_not_sent_to_a_guess_formatter(self):
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content='')
        executor = Mock()
        executor.invoke.return_value = {
            'output': 'Evidence memo is available.',
            'intermediate_steps': [],
        }
        result = self.review(llm, executor, retries=0)
        self.assertFalse(result['complete'])
        self.assertEqual(result['method'], 'semantic_review_failed')
        self.assertEqual(llm.invoke.call_count, 1)

    def test_registered_source_evidence_is_transferred_to_final_judge(self):
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps(self.record()))
        result = self.review(llm)
        self.assertTrue(result['complete'])
        judge = json.loads(llm.invoke.call_args.args[0][1].content)
        self.assertIn(self.evidence_id, judge['evidence_registry'])
        evidence = judge['evidence_registry'][self.evidence_id]
        self.assertEqual(evidence['kind'], 'candidate_source')
        self.assertEqual(judge['candidate_sources'][evidence['source_id']]['quote'],
                         '  boolean allowed() { return false; }')

    def test_string_evidence_ids_are_normalized_locally(self):
        payload = self.record()
        payload['members'][0]['evidence'] = [self.evidence_id]
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps(payload))
        result = self.review(llm)
        self.assertTrue(result['complete'])
        self.assertEqual(result['members'][0]['evidence'][0]['evidence_id'], self.evidence_id)
        llm.invoke.assert_called_once()

    def test_partial_review_retries_only_invalid_rows_and_preserves_missing(self):
        self.members.append(dict(self.members[0], idx=9))
        missing = dict(idx=8, status='missing', reason='The caller guard is absent.', evidence=[])
        invalid = dict(self.record()['members'][0], idx=9, evidence=['Einvented'])
        valid = dict(self.record()['members'][0], idx=9)
        llm = Mock()
        llm.invoke.side_effect = [
            SimpleNamespace(content=json.dumps({'members': [missing, invalid]})),
            SimpleNamespace(content=json.dumps({'members': [valid]}))]
        result = self.review(llm, retries=1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['method'], 'semantic_review_with_verified_source_evidence')
        self.assertEqual(result['missing'], [8])
        self.assertEqual(result['unreviewed'], [])
        second = json.loads(llm.invoke.call_args_list[1].args[0][1].content)
        self.assertEqual([row['idx'] for row in second['upstream_hunks']], [9])
        self.assertEqual([row['idx'] for row in second['upstream_context']], [8])

    def test_partial_review_retains_diagnostics_when_retry_is_unavailable(self):
        self.members.append(dict(self.members[0], idx=9))
        missing = dict(idx=8, status='missing', reason='The caller guard is absent.', evidence=[])
        invalid = dict(self.record()['members'][0], idx=9, evidence=['Einvented'])
        llm = Mock()
        llm.invoke.side_effect = [SimpleNamespace(content=json.dumps({'members': [missing, invalid]})),
                                  TimeoutError('review outage')]
        result = self.review(llm, retries=1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['method'], 'semantic_review_failed')
        self.assertEqual(result['missing'], [8])
        self.assertEqual(result['unreviewed'], [9])
        self.assertEqual(result['members'][0]['reason'], missing['reason'])
        self.assertEqual(result['members'][1]['status'], 'unreviewed')

    def test_unlimited_unreviewed_stops_when_scope_expansion_adds_no_evidence(self):
        llm = Mock()
        row = dict(self.record()['members'][0], status='unreviewed', reason='Need discriminating source.')
        llm.invoke.return_value = SimpleNamespace(content=json.dumps({'members': [row]}))
        result = self.review(llm, retries=None)
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['unreviewed'], [8])
        self.assertEqual(result['stop_reason'], 'no_progress')
        self.assertIn('Additional source evidence', result['error'])
        llm.invoke.assert_called_once()

    def test_scope_expansion_allows_review_to_continue_with_new_evidence(self):
        builder = self.evidence_builder()
        builder.registry[self.evidence_id]['scope_coverage'] = [dict(
            name='Example.allowed', kind='function', start=1, end=3, complete=False, omitted_ranges=[[1, 1], [3, 3]])]
        def expand(ids):
            self.assertIn(self.evidence_id, ids)
            builder.registry['Ecomplete'] = dict(kind='candidate_source', path='Example.java',
                start_line=1, end_line=3, quote='class Example {\n  boolean allowed() { return false; }\n}')
            builder.registry[self.evidence_id]['expanded_evidence_ids'] = ['Ecomplete']
        builder.expand_scopes.side_effect = expand
        row = dict(self.record()['members'][0], status='unreviewed', reason='Need the full scope.')
        llm = Mock()
        llm.invoke.side_effect = [SimpleNamespace(content=json.dumps({'members': [row]})),
                                 SimpleNamespace(content=json.dumps(self.record()))]
        result = self.review(llm, retries=None, builder=builder)
        self.assertTrue(result['complete'])
        self.assertEqual(llm.invoke.call_count, 2)
        request = json.loads(llm.invoke.call_args_list[1].args[0][1].content)
        self.assertIn('Ecomplete', request['evidence_registry'])

    def test_identical_invalid_responses_stop_but_new_corrections_are_verified(self):
        invalid = dict(self.record()['members'][0], reason='')
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps({'members': [invalid]}))
        result = self.review(llm, retries=None)
        self.assertEqual(result['stop_reason'], 'no_progress')
        self.assertEqual(result['unreviewed'], [8])
        self.assertEqual(llm.invoke.call_count, 2)
        other_invalid = dict(self.record()['members'][0], evidence=['Einvented'])
        llm = Mock()
        llm.invoke.side_effect = [SimpleNamespace(content=json.dumps({'members': [row]})) for row in
                                 (invalid, other_invalid, self.record()['members'][0])]
        self.assertTrue(self.review(llm, retries=None)['complete'])
        self.assertEqual(llm.invoke.call_count, 3)

    def test_invalid_row_keeps_its_registered_cross_file_evidence_on_retry(self):
        self.members.append(dict(idx=9, file='OtherUpstream.java', text='other obligation'))
        builder = self.evidence_builder()
        builder.registry['Eother'] = dict(builder.registry[self.evidence_id], path='Equivalent.java')
        builder.build.return_value['upstream_searches'] = [dict(
            idx=idx, candidate_path='Example.java', searches=[{'evidence_id': self.evidence_id}]) for idx in (8, 9)]
        invalid = dict(idx=9, status='satisfied', reason='', evidence=['Eother', 'Einvented'])
        llm = Mock()
        responses = [self.record()['members'] + [invalid],
                     [dict(invalid, reason='Exact cross-file implementation.', evidence=['Eother'])]]
        def judge(messages):
            request = json.loads(messages[1].content)
            if len(responses) == 1:
                self.assertEqual([h['idx'] for h in request['upstream_hunks']], [9])
                self.assertIn('Eother', request['evidence_registry'])
                self.assertNotIn('Einvented', request['evidence_registry'])
                self.assertEqual(request['previous_decisions'], [invalid])
            return SimpleNamespace(content=json.dumps({'members': responses.pop(0)}))
        llm.invoke.side_effect = judge
        result = self.review(llm, retries=1, builder=builder)
        self.assertTrue(result['complete'])
        self.assertEqual(llm.invoke.call_count, 2)
        self.assertEqual(result['members'][1]['evidence'][0]['path'], 'Equivalent.java')

    def test_resolution_error_does_not_discard_later_valid_members(self):
        self.members.append(dict(self.members[0], idx=9))
        invalid = self.record()['members'][0]
        valid = dict(invalid, idx=9)
        corrected = dict(invalid, resolution=dict(basis='corrected_interpretation',
                         reason='The exact guard already covers the scenario.', evidence=[self.evidence_id]))
        llm = Mock()
        llm.invoke.side_effect = [
            SimpleNamespace(content=json.dumps({'members': [invalid, valid]})),
            SimpleNamespace(content=json.dumps({'members': [corrected]}))]
        real_check = CoverageReviewer._verify_resolution
        def check(reviewer, row, previous, registry):
            # A previously missing first record needs resolution; the second is new.
            prior = dict(tree='previous', registry=registry,
                         result={'members': [dict(idx=8, status='missing')]})
            return real_check(reviewer, row, prior, registry)
        with patch.object(CoverageReviewer, '_verify_resolution', check):
            result = self.review(llm, retries=1)
        self.assertTrue(result['complete'])
        second = json.loads(llm.invoke.call_args_list[1].args[0][1].content)
        self.assertEqual([h['idx'] for h in second['upstream_hunks']], [8])
        self.assertEqual(second['previous_decisions'], [invalid])
        self.assertIn('explicit evidenced resolution', second['previous_review_feedback'])

    def test_retry_can_accept_only_after_every_hunk_has_verified_evidence(self):
        self.members.append(dict(self.members[0], idx=9))
        valid = self.record()['members'][0]
        invalid = dict(valid, idx=9, evidence=['Einvented'])
        llm = Mock()
        llm.invoke.side_effect = [SimpleNamespace(content=json.dumps({'members': [valid, invalid]})),
                                  SimpleNamespace(content=json.dumps({'members': [dict(valid, idx=9)]}))]
        result = self.review(llm, retries=1)
        self.assertTrue(result['complete'])
        self.assertEqual(result['unreviewed'], [])
        self.assertEqual([row['idx'] for row in result['members']], [8, 9])
        self.assertTrue(all(row['evidence'] for row in result['members']))
        second = json.loads(llm.invoke.call_args_list[1].args[0][1].content)
        self.assertEqual([row['idx'] for row in second['upstream_hunks']], [9])

    def test_duplicate_verdicts_invalidate_only_the_ambiguous_hunk(self):
        self.members.append(dict(self.members[0], idx=9))
        valid = self.record()['members'][0]
        missing = dict(idx=9, status='missing', reason='A separate guard is absent.', evidence=[])
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps({'members': [valid, valid, missing]}))
        result = self.review(llm)
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing'], [9])
        self.assertEqual(result['unreviewed'], [8])

    def test_invalid_string_id_cannot_be_promoted_or_discarded(self):
        payload = self.record()
        payload['members'][0]['evidence'] = [self.evidence_id, 'Einvented']
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps(payload))
        result = self.review(llm)
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['unreviewed'], [8])
        self.assertIn('Unknown candidate evidence ID', result['error'])

    def test_insufficient_context_is_unreviewed_not_a_code_defect(self):
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps({'members': [dict(
            idx=8, status='unreviewed', reason='The enclosing return path was omitted.',
            evidence=[self.evidence_id])]}))
        result = self.review(llm, retries=0)
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['unreviewed'], [8])
        self.assertEqual(result['members'][0]['reason'], 'The enclosing return path was omitted.')

    def test_formatter_cannot_promote_missing_or_undecided_hunk(self):
        for original in ['Hunk 8 is missing. Evidence ' + self.evidence_id + '.',
                         'The available evidence is inconclusive.']:
            with self.subTest(original=original):
                llm = Mock()
                llm.invoke.side_effect = [SimpleNamespace(content=original),
                                         SimpleNamespace(content=json.dumps(self.record()))]
                executor = Mock()
                executor.invoke.return_value = {'output': 'memo', 'intermediate_steps': []}
                result = self.review(llm, executor)
                self.assertFalse(result['complete'])
                self.assertEqual(llm.invoke.call_count, 1)
                if original.startswith('Hunk'):
                    self.assertEqual(result['members'][0]['status'], 'missing')
                else:
                    self.assertIn('omitted hunks', result['error'])

    def test_formatter_cannot_borrow_another_hunks_evidence(self):
        llm = Mock()
        llm.invoke.side_effect = [SimpleNamespace(content='Hunk 8 is satisfied. No evidence ID supplied.\n'
            'Hunk 9 is satisfied. Evidence ' + self.evidence_id + '.'),
            SimpleNamespace(content=json.dumps(self.record()))]
        result = self.review(llm)
        self.assertFalse(result['complete'])
        self.assertIn('requires candidate evidence', result['error'])

    def test_local_conversion_rejects_duplicate_and_out_of_scope_verdicts(self):
        valid = f'Hunk 8 is satisfied. Evidence {self.evidence_id}.'
        for original in (valid + '\nHunk 8 is missing.',
                         valid + f'\nHunk 9 is satisfied. Evidence {self.evidence_id}.'):
            with self.subTest(original=original):
                llm, executor = Mock(), Mock()
                llm.invoke.return_value = SimpleNamespace(content=original)
                executor.invoke.return_value = {'output': 'memo', 'intermediate_steps': []}
                result = self.review(llm, executor)
                self.assertFalse(result['complete'])
                self.assertEqual(result['method'], 'semantic_review_failed')
                self.assertEqual(llm.invoke.call_count, 1)

    def test_local_conversion_does_not_drop_unknown_ids_after_valid_evidence(self):
        for separator in (', ', ' and '):
            with self.subTest(separator=separator):
                llm, executor = Mock(), Mock()
                llm.invoke.return_value = SimpleNamespace(content=
                    f'Hunk 8 is satisfied. Evidence {self.evidence_id}{separator}Einvented.')
                executor.invoke.return_value = {'output': 'memo', 'intermediate_steps': []}
                result = self.review(llm, executor)
                self.assertFalse(result['complete'])
                self.assertIn('Unknown candidate evidence ID', result['error'])
                self.assertEqual(llm.invoke.call_count, 1)


class CandidateCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repo.init(self.root)
        with self.repo.config_writer() as config:
            config.set_value('user', 'name', 'Test')
            config.set_value('user', 'email', 'test@example.invalid')
        self.source = self.root / 'Example.java'
        self.source.write_text('class Example {\n  boolean allowed() { return true; }\n}\n')
        self.repo.index.add(['Example.java'])
        self.repo.index.commit('base')
        self.base = self.repo.head.commit.hexsha
        self.source.write_text('class Example {\n  boolean allowed() { return false; }\n}\n')
        self.candidate = self.repo.git.diff() + '\n'
        self.repo.git.reset('--hard')
        self.members = [dict(idx=8, file='Renamed.java', text='-allowAny();\n+restrict();')]

    def record(self, status='satisfied', quote='  boolean allowed() { return false; }'):
        return {'members': [dict(idx=8, status=status, reason='The actual target guard blocks this path.',
                                evidence=[dict(path='Example.java', start_line=2, end_line=2, quote=quote)])]}

    def test_private_artifact_snapshot_does_not_mutate_worktree_or_index(self):
        self.source.write_text('unrelated uncommitted user content\n')
        before = self.repo.git.status('--porcelain')
        tree = artifact_tree(self.root, self.base, self.candidate)
        self.assertIn('return false', read_blob(self.root, tree, 'Example.java'))
        self.assertEqual(self.source.read_text(), 'unrelated uncommitted user content\n')
        self.assertEqual(self.repo.git.status('--porcelain'), before)
        self.assertEqual(self.repo.head.commit.hexsha, self.base)

    def test_review_uses_applied_coordinates_for_offset_and_recounted_hunks(self):
        original = ('class Example {\n' + ''.join('  // first padding %d\n' % n for n in range(10))
                    + '  boolean allowed() { return true; }\n'
                    + ''.join('  // second padding %d\n' % n for n in range(12))
                    + '  int limit() { return 10; }\n}\n')
        changed = original.replace('return true;', 'return false;').replace('return 10;', 'return 2;')
        self.source.write_text(original)
        self.repo.index.add(['Example.java'])
        base = self.repo.index.commit('separate methods').hexsha
        self.source.write_text(changed)
        clean_patch = self.repo.git.diff('--unified=3') + '\n'
        _, upstream = split_patch_indexed(clean_patch, False)
        self.assertEqual(len(upstream), 2)
        records = []
        for hunk, fragment in zip(upstream, ('return false;', 'return 2;')):
            line = next(i for i, text in enumerate(changed.splitlines(), 1) if fragment in text)
            records.append(dict(idx=hunk['idx'], status='satisfied', reason='The method has the new behavior.',
                evidence=[dict(path='Example.java', start_line=line, end_line=line,
                               quote=changed.splitlines()[line-1])]))
        # Both staged and unstaged user content must remain untouched by normalization.
        self.source.write_text('staged user content\n')
        self.repo.index.add(['Example.java'])
        self.source.write_text('unstaged user content\n')
        original_index, original_worktree = self.repo.git.diff('--cached'), self.repo.git.diff()
        for offset, recount in ((-1, False), (2, False), (1, True)):
            with self.subTest(offset=offset, recount=recount):
                submitted = re.sub(r'^@@ -(\d+),(\d+) \+(\d+),(\d+) @@',
                    lambda m: '@@ -%d,%d +%d,%d @@' % (
                        int(m[1])+offset, int(m[2])+(2 if recount else 0),
                        int(m[3])+offset, int(m[4])+(3 if recount else 0)), clean_patch, flags=re.M)
                tree = artifact_tree(self.root, base, submitted)
                self.assertEqual(read_blob(self.root, tree, 'Example.java'), changed)
                _, raw_hunks = split_patch_indexed(submitted, False)
                with self.assertRaisesRegex(ValueError, 'Patch/source mismatch'):
                    hunk_evidence(str(self.root), base, tree, raw_hunks[0])
                llm = Mock()
                llm.invoke.return_value = SimpleNamespace(content=json.dumps({'members': records}))
                audit = self.root / ('review-%d-%s' % (offset, recount))
                result = CoverageReviewer(llm, self.root, base, retries=0, artifact_dir=audit).review(
                    upstream, submitted)
                self.assertTrue(result['complete'], result)
                llm.invoke.assert_called_once()
                judge = json.loads(llm.invoke.call_args.args[0][1].content)
                canonical = judge['complete_candidate_patch']
                self.assertNotEqual(canonical, submitted)
                self.assertEqual(artifact_tree(self.root, base, canonical), tree)
                self.assertEqual(len(judge['deterministic_evidence']['candidate_changes']), 2)
                saved = next(d for d in (json.loads(p.read_text()) for p in audit.glob('*.json'))
                             if d['stage'] == 'deterministic_evidence')
                self.assertEqual(saved['submitted_patch'], submitted)
                self.assertEqual(saved['canonical_patch'], canonical)
                self.assertEqual(result['patch_sha256'], hashlib.sha256(submitted.encode()).hexdigest())
                self.assertEqual(self.repo.git.diff('--cached'), original_index)
                self.assertEqual(self.repo.git.diff(), original_worktree)
                self.assertEqual(self.repo.head.commit.hexsha, base)

    def test_failed_diff_generation_does_not_fall_back_to_untrusted_coordinates(self):
        llm = Mock()
        with patch('agent.coverage.candidate_diff', side_effect=subprocess.CalledProcessError(1, 'git diff')):
            result = CoverageReviewer(llm, self.root, self.base, retries=0).review(self.members, self.candidate)
        self.assertEqual(result['method'], 'semantic_review_failed')
        self.assertEqual(result['unreviewed'], [8])
        self.assertEqual(result['missing'], [])
        llm.invoke.assert_not_called()

    def test_renamed_behavior_can_be_covered_without_upstream_identifier_overlap(self):
        tree = artifact_tree(self.root, self.base, self.candidate)
        result = verify_review(self.record(), self.members, lambda p: read_blob(self.root, tree, p))
        self.assertTrue(result['complete'])

    def test_missing_duplicate_and_boolean_ids_are_rejected(self):
        read = lambda _: self.source.read_text()
        for payload in ({'members': []}, {'members': self.record()['members'] * 2},
                        {'members': [dict(self.record()['members'][0], idx=True)]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                verify_review(payload, self.members, read)

    def test_skip_without_positive_target_evidence_is_rejected(self):
        p = self.record('not_applicable')
        p['members'][0]['evidence'] = []
        with self.assertRaises(ValueError):
            verify_review(p, self.members, lambda _: self.source.read_text())

    def test_fabricated_or_stale_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'does not match'):
            verify_review(self.record(), self.members, lambda _: self.source.read_text())

    def test_tool_evidence_ids_resolve_to_verified_candidate_lines(self):
        tree = artifact_tree(self.root, self.base, self.candidate)
        payload = self.record()
        evidence = payload['members'][0]['evidence'][0]
        payload['members'][0]['evidence'] = [{'evidence_id': 'Efixture'}]
        read = lambda p: read_blob(self.root, tree, p)
        result = verify_review(payload, self.members, read, {'Efixture': evidence})
        self.assertTrue(result['complete'])
        self.assertEqual(result['members'][0]['evidence'][0]['quote'], evidence['quote'])
        with self.assertRaisesRegex(ValueError, 'Unknown candidate evidence'):
            verify_review(payload, self.members, read, {})
        with self.assertRaisesRegex(ValueError, 'does not match'):
            verify_review(payload, self.members, lambda _: self.source.read_text(), {'Efixture': evidence})

    def test_registered_search_evidence_supports_absence_but_never_satisfied(self):
        registry = {'Esearch': dict(kind='candidate_search', query='UpstreamOnly',
                                   path='', matches=[], truncated=False)}
        payload = self.record('not_applicable')
        payload['members'][0]['evidence'] = [{'evidence_id': 'Esearch'}]
        read = lambda p: read_blob(self.root, self.base, p)
        result = verify_review(payload, self.members, read, registry)
        self.assertTrue(result['complete'])
        self.assertEqual(result['members'][0]['status'], 'not_applicable')
        payload['members'][0]['status'] = 'missing'
        result = verify_review(payload, self.members, read, registry)
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing'], [8])
        payload['members'][0]['status'] = 'satisfied'
        with self.assertRaisesRegex(ValueError, 'only supports registered not_applicable'):
            verify_review(payload, self.members, read, registry)
        payload['members'][0].update(status='not_applicable', evidence=list(registry.values()))
        with self.assertRaisesRegex(ValueError, 'only supports registered not_applicable'):
            verify_review(payload, self.members, read, registry)

    def test_review_uses_real_deterministic_structure_and_search_evidence(self):
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps(self.record()))
        result = CoverageReviewer(llm, self.root, self.base, retries=0).review(
            self.members, self.candidate)
        self.assertTrue(result['complete'])
        self.assertEqual(llm.invoke.call_count, 1)
        judge = json.loads(llm.invoke.call_args.args[0][1].content)
        evidence = judge['deterministic_evidence']
        self.assertEqual(len(evidence['candidate_changes']), 1)
        scopes = evidence['candidate_changes'][0]['after']['scopes']
        self.assertTrue(scopes)
        self.assertIn('evidence_id', scopes[0])
        self.assertIn(scopes[0]['evidence_id'], judge['evidence_registry'])
        searches = evidence['upstream_searches'][0]['searches']
        searches = [judge['evidence_registry'][row['evidence_id']] for row in searches]
        self.assertEqual([row['query'] for row in searches], ['allowAny', 'restrict'])
        self.assertTrue(all(row['kind'] == 'candidate_search' for row in searches))

    def test_shared_source_excerpts_preserve_every_original_range_and_search(self):
        sources = {'One.java': 'one\ntwo\nthree\nfour\nfive\n', 'Two.java': 'different\n'}
        registry = {}
        for idx, (path, start, end) in enumerate([
                ('One.java', 1, 3), ('One.java', 2, 4), ('One.java', 2, 2), ('Two.java', 1, 1)]):
            registry['E%d' % idx] = dict(kind='candidate_source', path=path,
                start_line=start, end_line=end, quote='\n'.join(sources[path].splitlines()[start-1:end]))
        registry['Esearch'] = dict(kind='candidate_search', query='guard', matches=[], truncated=False)
        evidence = dict(candidate_changes=[{'file': 'One.java'}], upstream_searches=[
            dict(idx=8, searches=[dict(registry['Esearch'], evidence_id='Esearch')])])
        original = json.dumps(registry, sort_keys=True)
        view, compact, excerpts = _judge_evidence(evidence, registry, sources.__getitem__)
        self.assertEqual(json.dumps(registry, sort_keys=True), original)
        self.assertEqual(len(excerpts), 2)
        for evidence_id, item in registry.items():
            if item['kind'] == 'candidate_source':
                excerpt = excerpts[compact[evidence_id]['source_id']]
                start, end = item['start_line']-excerpt['start_line'], item['end_line']-excerpt['start_line']+1
                self.assertEqual('\n'.join(excerpt['quote'].splitlines()[start:end]), item['quote'])
        self.assertEqual(compact['Esearch'], registry['Esearch'])
        self.assertEqual(view['upstream_searches'][0]['searches'], [{'evidence_id': 'Esearch'}])
        self.assertEqual(view['candidate_changes'], evidence['candidate_changes'])

    def test_long_scope_is_marked_and_expanded_for_an_explicit_context_request(self):
        original = ('class Example {\n  boolean allowed() {\n    boolean ok = true;\n'
                    + '    // unmodified context\n' * 510 + '    return ok;\n  }\n}\n')
        self.source.write_text(original)
        self.repo.index.add(['Example.java'])
        base = self.repo.index.commit('long method').hexsha
        self.source.write_text(original.replace('ok = true', 'ok = false'))
        candidate = self.repo.git.diff() + '\n'
        self.repo.git.reset('--hard')
        _, members = split_patch_indexed(candidate, False)
        requests = []

        def judge(messages):
            request = json.loads(messages[1].content)
            requests.append(request)
            scopes = [(key, item, scope) for key, item in request['evidence_registry'].items()
                      for scope in item.get('scope_coverage', []) if scope['name'] == 'Example.allowed']
            if len(requests) == 1:
                evidence_id, item, scope = next(row for row in scopes if not row[2]['complete'])
                self.assertTrue(scope['omitted_ranges'])
                excerpt = request['candidate_sources'][item['source_id']]['quote']
                self.assertNotIn('return ok;', excerpt)
                status, reason = 'unreviewed', 'Need the omitted return path.'
            else:
                evidence_id, item, scope = next(row for row in scopes if row[2]['complete'])
                self.assertEqual(scope['omitted_ranges'], [])
                self.assertIn('return ok;', request['candidate_sources'][item['source_id']]['quote'])
                status, reason = 'satisfied', 'The false value reaches the return.'
            return SimpleNamespace(content=json.dumps({'members': [dict(
                idx=members[0]['idx'], status=status, reason=reason, evidence=[evidence_id])]}))

        llm = Mock()
        llm.invoke.side_effect = judge
        result = CoverageReviewer(llm, self.root, base, retries=1).review(members, candidate)
        self.assertTrue(result['complete'], result)
        self.assertEqual(llm.invoke.call_count, 2)

    def test_focused_retry_omits_unrelated_source_but_keeps_complete_candidate(self):
        other = self.root/'Other.java'
        old = ('class Other {\n boolean separate() {\n boolean ok = true;\n'
               + ' // unrelated details ' + 'x' * 100 + '\n')
        old += (' // unrelated details ' + 'x' * 100 + '\n') * 250 + ' return ok;\n }\n}\n'
        other.write_text(old)
        self.repo.index.add(['Other.java'])
        base = self.repo.index.commit('unrelated method').hexsha
        self.source.write_text('class Example {\n  boolean allowed() { return false; }\n}\n')
        other.write_text(old.replace('ok = true', 'ok = false'))
        candidate = self.repo.git.diff() + '\n'
        _, members = split_patch_indexed(candidate, False)
        self.repo.git.reset('--hard')
        requests = []

        def judge(messages):
            request = json.loads(messages[1].content)
            requests.append(request)
            rows = []
            for member in request['upstream_hunks']:
                evidence_id = next(key for key, item in request['evidence_registry'].items()
                    if item['kind'] == 'candidate_source' and item['path'] == member['file'])
                if len(requests) == 1 and member['file'] == 'Example.java':
                    evidence_id = 'Eunknown'
                rows.append(dict(idx=member['idx'], status='satisfied', reason='Target behavior present.',
                                 evidence=[evidence_id]))
            return SimpleNamespace(content=json.dumps({'members': rows}))

        llm = Mock()
        llm.invoke.side_effect = judge
        result = CoverageReviewer(llm, self.root, base, retries=1).review(members, candidate)
        self.assertTrue(result['complete'], result)
        self.assertEqual(len(requests), 2)
        first, second = requests
        self.assertEqual([h['file'] for h in second['upstream_hunks']], ['Example.java'])
        self.assertEqual(second['complete_candidate_patch'], first['complete_candidate_patch'])
        self.assertFalse(any(item.get('path') == 'Other.java' for item in second['evidence_registry'].values()))
        self.assertLess(len(json.dumps(second)), len(json.dumps(first)) / 2)
        self.assertNotIn('text', second['upstream_context'][0])

    def test_retry_keeps_cross_file_helper_evidence_referenced_by_pending_changes(self):
        registry = {'Esearch': dict(kind='candidate_search', path='Caller.java', matches=[
                        dict(evidence_id='Ecaller', path='Caller.java')]),
                    'Ecaller': dict(kind='candidate_source', path='Caller.java'),
                    'Ehelper': dict(kind='candidate_source', path='Helper.java', scope_coverage=[
                        dict(name='Helper.guard', kind='function')]),
                    'Eother': dict(kind='candidate_source', path='Other.java')}
        evidence = dict(upstream_searches=[dict(idx=8, candidate_path='Caller.java', searches=[
                        dict(evidence_id='Esearch')])], candidate_changes=[
                        dict(file='Caller.java', source_evidence_ids=['Ecaller'], after=dict(calls=[dict(callee='guard')])),
                        dict(file='Other.java', source_evidence_ids=['Eother'])])
        _, selected = _retry_evidence(evidence, registry, [dict(idx=8, file='Caller.java')])
        self.assertIn('Ehelper', selected)
        self.assertNotIn('Eother', selected)
        # A semantic mapping can need a scope that syntax/name matching did not connect.
        _, selected = _retry_evidence(evidence, registry, [dict(idx=8, file='Caller.java')],
                                      required_evidence={'Eother'})
        self.assertIn('Eother', selected)

    def test_exact_post_change_hunk_is_evidence_not_an_acceptance_shortcut(self):
        _, hunks = split_patch_indexed(self.candidate, False)
        member = dict(hunks[0], idx=8)
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps(self.record()))
        result = CoverageReviewer(llm, self.root, self.base, retries=0).review(
            [member], self.candidate)
        self.assertTrue(result['complete'])
        self.assertEqual(result['method'], 'semantic_review_with_verified_source_evidence')
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['members'][0]['status'], 'satisfied')
        self.assertIn('return false', result['members'][0]['evidence'][0]['quote'])
        llm.invoke.assert_called_once()
        judge = json.loads(llm.invoke.call_args.args[0][1].content)
        self.assertTrue(judge['deterministic_evidence']['upstream_searches'][0]['exact_text_matches'])

    def test_text_matches_do_not_remove_obligations_from_semantic_review(self):
        _, hunks = split_patch_indexed(self.candidate, False)
        exact = dict(hunks[0], idx=8)
        pending = dict(self.members[0], idx=9)
        response = {'members': [dict(self.record()['members'][0], idx=idx) for idx in (8, 9)]}
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps(response))
        result = CoverageReviewer(llm, self.root, self.base, retries=0).review(
            [exact, pending], self.candidate)
        self.assertTrue(result['complete'])
        self.assertEqual([row['idx'] for row in result['members']], [8, 9])
        judge = json.loads(llm.invoke.call_args.args[0][1].content)
        self.assertEqual([row['idx'] for row in judge['upstream_hunks']], [8, 9])
        self.assertNotIn('mechanical_verdicts', judge['deterministic_evidence'])

    def test_identical_block_in_another_method_does_not_bypass_review(self):
        # Git's canonical three-line context must also be identical in both owners.
        block = ('    int value = 1;\n' + '    // before\n' * 3 + '    value++;\n'
                 + '    // after\n' * 3 + '    return value;\n')
        original = 'class Example {\n  int sensitive() {\n' + block + '  }\n\n  int unrelated() {\n' + block + '  }\n}\n'
        self.source.write_text(original)
        self.repo.index.add(['Example.java'])
        base = self.repo.index.commit('two equal blocks').hexsha
        self.source.write_text(original.replace('value++;', 'value += 2;', 1))
        upstream = self.repo.git.diff('--unified=3') + '\n'
        _, members = split_patch_indexed(upstream, False)
        # Keep sensitive() unchanged; modify only the second copy of the block.
        before, last = original.rsplit('value++;', 1)
        self.source.write_text(before + 'value += 2;' + last)
        candidate = self.repo.git.diff('--unified=3') + '\n'
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps({'members': [dict(
            idx=members[0]['idx'], status='missing', reason='sensitive() remains unchanged', evidence=[])]}))
        result = CoverageReviewer(llm, self.root, base, retries=0).review(members, candidate)
        self.assertFalse(result['complete'])
        llm.invoke.assert_called_once()
        judge = json.loads(llm.invoke.call_args.args[0][1].content)
        self.assertTrue(judge['deterministic_evidence']['upstream_searches'][0]['exact_text_matches'])

    def test_same_hunk_id_with_changed_obligation_cannot_reuse_cached_verdict(self):
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps(self.record()))
        reviewer = CoverageReviewer(llm, self.root, self.base, retries=0)
        reviewer.review(self.members, self.candidate)
        changed = [dict(self.members[0], text='-oldBehavior();\n+anotherBehavior();')]
        reviewer.review(changed, self.candidate)
        self.assertEqual(llm.invoke.call_count, 2)

    def test_semantic_review_does_not_reuse_a_different_artifacts_acceptance(self):
        missing = {'members': [dict(idx=8, status='missing', reason='The restrictive guard was removed.', evidence=[])]}
        llm = Mock()
        llm.invoke.side_effect = [
            SimpleNamespace(content=json.dumps(self.record())),
            SimpleNamespace(content=json.dumps(missing)),
        ]
        reviewer = CoverageReviewer(llm, self.root, self.base, retries=0)
        first = reviewer.review(self.members, self.candidate)
        self.assertTrue(first['complete'])
        self.assertTrue(reviewer.review(self.members, self.candidate)['complete'])
        self.source.write_text('class Example {\n  boolean allowed() { return true; } // still vulnerable\n}\n')
        another = self.repo.git.diff() + '\n'
        self.assertFalse(reviewer.review(self.members, another)['complete'])
        self.assertEqual(llm.invoke.call_count, 2)

    def test_review_service_failure_is_retried_without_cached_code_decision(self):
        llm = Mock()
        llm.invoke.side_effect = [
            TimeoutError('fixture timeout'),
            SimpleNamespace(content=json.dumps(self.record())),
        ]
        reviewer = CoverageReviewer(llm, self.root, self.base, retries=1)
        result = reviewer.review(self.members, self.candidate)
        self.assertTrue(result['complete'])
        self.assertEqual(llm.invoke.call_count, 2)

        self.assertTrue(reviewer.review(self.members, self.candidate)['complete'])
        self.assertEqual(llm.invoke.call_count, 2)

    def test_unlimited_review_retries_until_a_valid_decision(self):
        llm = Mock()
        llm.invoke.side_effect = [TimeoutError('transient')] * 4 + [
            SimpleNamespace(content=json.dumps(self.record()))]
        reviewer = CoverageReviewer(llm, self.root, self.base, retries=None)
        self.assertTrue(reviewer.review(self.members, self.candidate)['complete'])
        self.assertEqual(llm.invoke.call_count, 5)

    def test_permanent_provider_error_does_not_loop_in_unlimited_review(self):
        error = RuntimeError('insufficient balance')
        error.status_code = 402
        llm = Mock()
        llm.invoke.side_effect = error
        result = CoverageReviewer(llm, self.root, self.base, retries=None).review(self.members, self.candidate)
        self.assertFalse(result['complete'])
        self.assertEqual(result['unreviewed'], [8])
        self.assertEqual(llm.invoke.call_count, 1)

    def test_global_status_distinguishes_missing_and_unreviewed(self):
        project = SimpleNamespace(dir=str(self.root), coverage_reviewer=Mock())
        project.coverage_reviewer.review.return_value = dict(
            complete=False, candidate_tree='fixed-tree', method='semantic_review_failed',
            missing=[8], unreviewed=[9], error='invalid evidence ID for hunk 9',
            members=[dict(idx=8, file='Renamed.java', covered=False, status='missing', reason='Guard absent'),
                     dict(idx=9, file='Renamed.java', covered=False, status='unreviewed', reason='Review unavailable')])
        status = self.root / 'merged-hunk-00.status.json'
        status.write_text(json.dumps({'member_hunks': [8, 9], 'complete': True}))
        members = {8: self.members[0], 9: dict(self.members[0], idx=9)}
        with patch('agent.invoke_llm.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')):
            complete, _ = evaluate_global_artifact(project, self.candidate, '/unused', str(self.root),
                members, {8, 9}, set(), True, True)
        self.assertFalse(complete)
        state = json.loads(status.read_text())
        self.assertEqual(state['missing_hunks'], [8])
        self.assertEqual(state['unreviewed_hunks'], [9])
        self.assertFalse(state['global_validation_complete'])

    def test_final_semantic_rejection_overrides_heuristic_and_old_skip_claim(self):
        project = SimpleNamespace(dir=str(self.root), coverage_reviewer=Mock())
        project.coverage_reviewer.review.return_value = dict(complete=False, missing=[8], method='semantic_review',
            members=[dict(idx=8, file='Renamed.java', covered=False, status='missing', reason='Guard absent')])
        status = self.root / 'merged-hunk-00.status.json'
        status.write_text(json.dumps({'member_hunks': [8], 'complete': True}))
        with patch('agent.invoke_llm.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')):
            complete, result = evaluate_global_artifact(project, self.candidate, '/unused', str(self.root),
                {8:self.members[0]}, {8}, {8}, False, True)
        self.assertFalse(complete)
        self.assertEqual(result['accepted_need_not_ported_hunks'], [])
        self.assertFalse(json.loads(status.read_text())['complete'])

    def test_bare_skip_cannot_report_success_and_evidence_remains_pending(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.base))
        self.assertIn('EVIDENCE_REQUIRED', project._validate(self.base, 'need not ported'))
        self.assertFalse(project.round_succeeded)
        response = project._validate(self.base, 'need not ported', 'Example.java: earlier group already supplies the guard')
        self.assertIn('NO_CHANGE_RECORDED', response)
        self.assertNotIn('Patch applied successfully', response)
        self.assertEqual(project.no_change_evidence['status'], 'pending_final_semantic_review')
        self.assertFalse(project.poc_succeeded)

    def test_semantic_failure_blocks_acceptance_after_actual_build_and_test(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.base))
        project.full_patch_base = self.base
        project.all_hunks_applied_succeeded = True
        project.final_artifact_check = lambda _: 'SEMANTIC_VALIDATION_FAILED: missing regression test'
        (self.root/'build.sh').write_text('touch build-was-run\n')
        (self.root/'test.sh').write_text('test -f build-was-run && touch test-was-run\n')
        self.assertIn('SEMANTIC_VALIDATION_FAILED', project._validate(self.base, self.candidate))
        self.assertTrue((self.root/'build-was-run').exists())
        self.assertTrue((self.root/'test-was-run').exists())
        self.assertTrue(project.compile_succeeded)
        self.assertTrue(project.testcase_succeeded)
        self.assertTrue(project.final_repair_required)
        self.assertFalse(project.poc_succeeded)

    def test_unavailable_review_preserves_build_error_and_requires_repair(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.base))
        project.full_patch_base = self.base
        project.all_hunks_applied_succeeded = True
        project.final_artifact_check = Mock(side_effect=FinalValidationUnavailable('JSON decode failed'))
        (self.root/'build.sh').write_text('echo "build stdout"\necho "error: missing argument" >&2\nexit 1\n')
        result = project._validate(self.base, self.candidate)
        self.assertIn('error: missing argument', result)
        self.assertIn('FINAL_VALIDATION_UNAVAILABLE', result)
        self.assertTrue(project.final_validation_unavailable)
        self.assertTrue(project.final_repair_required)
        self.assertFalse(project.poc_succeeded)

    def test_review_outage_alone_does_not_request_code_rewrite_or_accept_patch(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.base))
        project.full_patch_base = self.base
        project.all_hunks_applied_succeeded = True
        (self.root/'build.sh').write_text('exit 0\n')
        (self.root/'test.sh').write_text('exit 0\n')
        project.final_artifact_check = Mock(side_effect=FinalValidationUnavailable('invalid response'))
        project._validate(self.base, self.candidate)
        self.assertTrue(project.compile_succeeded)
        self.assertTrue(project.testcase_succeeded)
        self.assertFalse(project.final_repair_required)
        self.assertFalse(project.poc_succeeded)
        self.assertEqual(project.validated_patch_checkpoints, [])
        project.final_artifact_check = Mock(side_effect=FinalValidationUnavailable(
            'invalid response', 'API_CHAIN_VALIDATION_FAILED: caller() lacks argument'))
        result = project._validate(self.base, self.candidate)
        self.assertIn('caller() lacks argument', result)
        self.assertTrue(project.final_repair_required)
        self.assertFalse(project.poc_succeeded)
        project.final_artifact_check = lambda _: ''
        project._validate(self.base, self.candidate)
        self.assertFalse(project.final_validation_unavailable)
        self.assertTrue(project.poc_succeeded)

    def test_review_parse_failure_keeps_raw_output_and_deterministic_evidence(self):
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content='invalid final review sk-fixture-secret')
        reviewer = CoverageReviewer(llm, self.root, self.base, retries=0,
                                    artifact_dir=self.root/'review-attempts')
        result = reviewer.review(self.members, self.candidate)
        self.assertEqual(result['method'], 'semantic_review_failed')
        records = {record['stage']: record for record in (
            json.loads(path.read_text()) for path in (self.root/'review-attempts').glob('*.json'))}
        evidence = records['deterministic_evidence']
        self.assertEqual(evidence['status'], 'complete')
        self.assertEqual(evidence['candidate_hunks'], 1)
        self.assertGreater(evidence['evidence_count'], 0)
        judge = records['final_json']
        self.assertEqual(judge['output'], 'invalid final review [REDACTED_KEY]')
        self.assertNotIn('tool_steps', judge)
        self.assertIn('ValueError', judge['error'])


class TestScenarioEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repo.init(self.root)
        self.path = self.root / 'UpstreamTest.java'
        before = ('class UpstreamTest {\n @Test void testVisibility() {\n'
                  '  boolean visible = true;\n  boolean nonPinned = true;\n'
                  + ''.join('  // separate setup from assertion %d\n' % n for n in range(8))
                  + '  boolean result = allowed(visible, nonPinned);\n  assertEquals(true, result);\n }\n}\n')
        self.path.write_text(before)
        self.repo.index.add(['UpstreamTest.java'])
        self.base = self.repo.index.commit('before').hexsha
        self.after = before.replace('nonPinned = true', 'nonPinned = false').replace('assertEquals(true', 'assertEquals(false')
        self.path.write_text(self.after)
        self.repo.index.add(['UpstreamTest.java'])
        self.upstream = self.repo.index.commit('setup and assertion in different hunks').hexsha
        self.patch = self.repo.git.diff(self.base, self.upstream, '--unified=0') + '\n'
        _, self.members = split_patch_indexed(self.patch, False)

    def test_full_scope_joins_setup_and_final_assertion_across_hunks(self):
        self.assertEqual(len(self.members), 2)
        context = changed_scope_context(str(self.root), self.base, self.upstream, self.members)
        self.assertEqual(context['by_hunk'][0], context['by_hunk'][1])
        scope = context['scopes'][context['by_hunk'][0][0]]
        self.assertEqual(scope['related_hunks'], [0, 1])
        self.assertIn('nonPinned = false', scope['quote'])
        self.assertIn('assertEquals(false, result)', scope['quote'])
        self.assertTrue(any(w['target'] == 'nonPinned' and w['value'] == 'false' for w in scope['writes']))
        self.assertTrue(any(c['callee'] == 'assertEquals' and c['arguments'][0] == 'false' for c in scope['calls']))

    def test_new_test_file_is_classified_by_its_diff_path_not_legacy_placeholder(self):
        (self.root/'NewTest.java').write_text('class NewTest { @Test void testBlock() { assertFalse(allowed()); } }\n')
        self.repo.index.add(['NewTest.java'])
        commit = self.repo.index.commit('new test').hexsha
        _, members = split_patch_indexed(self.repo.git.diff(self.upstream, commit) + '\n', False)
        self.assertEqual(members[0]['file'], '(new file)')
        reviewer = CoverageReviewer(Mock(), self.root, self.upstream,
                                    upstream_parent=self.upstream, upstream_commit=commit)
        enriched, scopes = reviewer._upstream_context(members)
        self.assertEqual(enriched[0]['file'], 'NewTest.java')
        self.assertEqual(len(enriched[0]['test_scope_ids']), 1)
        scope = scopes[enriched[0]['test_scope_ids'][0]]
        self.assertIn('assertFalse', scope['quote'])

    def test_satisfied_test_requires_all_three_verified_witness_roles(self):
        scope = dict(quote=self.after)
        member = dict(idx=0, file='UpstreamTest.java', test_scope_ids=['U1'])
        registry = {'E1': dict(kind='candidate_source', path='TargetTest.java', start_line=1,
                              end_line=len(self.after.splitlines()), quote=self.after)}
        row = dict(idx=0, status='satisfied', reason='Equivalent scenario', evidence=['E1'])
        read = lambda path: self.after
        with self.assertRaisesRegex(ValueError, 'setup/exercise/assertion'):
            verify_review({'members': [row]}, [member], read, registry, upstream_scopes={'U1': scope})
        witness = dict(upstream_scope_id='U1', distinguishes_behavior='Visible true and nonPinned false must block.')
        for role, quote in [('setup', 'boolean nonPinned = false;'),
                            ('exercise', 'allowed(visible, nonPinned)'), ('assertion', 'assertEquals(false, result)')]:
            witness[role] = dict(upstream_quote=quote, target_evidence_id='E1',
                                 target_quote=quote, explanation='Same concrete scenario in the target.')
        row['test_witnesses'] = [witness]
        result = verify_review({'members': [row]}, [member], read, registry, upstream_scopes={'U1': scope})
        self.assertTrue(result['complete'])
        # A mixed hunk need not recreate an absent upstream-only test adaptation.
        member['test_scope_ids'] = ['U1', 'U2']
        adaptation = dict(upstream_scope_id='U2', status='not_applicable',
                          reason='This scope only adapts a call in absent upstream test infrastructure.',
                          upstream_quote='allowed(visible, nonPinned)', evidence=['E1'])
        row['test_witnesses'] = [witness, adaptation]
        self.assertTrue(verify_review({'members': [row]}, [member], read, registry,
                                     upstream_scopes={'U1': scope, 'U2': scope})['complete'])
        row['test_witnesses'] = [dict(adaptation, upstream_scope_id='U1'), adaptation]
        with self.assertRaisesRegex(ValueError, 'No applicable test scenario'):
            verify_review({'members': [row]}, [member], read, registry, upstream_scopes={'U1': scope, 'U2': scope})
        member['test_scope_ids'] = ['U1']
        row['test_witnesses'] = [witness]
        witness['setup']['target_quote'] = 'boolean nonPinned = visible;'
        with self.assertRaisesRegex(ValueError, 'exact candidate source'):
            verify_review({'members': [row]}, [member], read, registry, upstream_scopes={'U1': scope})
        witness['setup']['target_quote'] = 'boolean nonPinned = false;'
        registry['E1']['path'] = 'Production.java'
        with self.assertRaisesRegex(ValueError, 'production-code branch'):
            verify_review({'members': [row]}, [member], read, registry, upstream_scopes={'U1': scope})

    def test_historical_gap_cannot_disappear_after_an_unrelated_edit(self):
        reviewer = CoverageReviewer(Mock(), self.root, self.base, retries=0)
        old_source = dict(kind='candidate_source', path='UpstreamTest.java', quote='boolean visible = true;')
        previous = dict(tree=self.base, registry={'Eold': old_source},
                        result={'members': [dict(idx=0, status='missing', reason='scenario absent')]})
        row = dict(idx=0, status='satisfied', covered=True)
        current = {'Enew': dict(old_source)}
        with self.assertRaisesRegex(ValueError, 'explicit evidenced resolution'):
            reviewer._verify_resolution(row, previous, current)
        row['resolution'] = dict(basis='candidate_change', reason='fixed', evidence=['Enew'])
        with self.assertRaisesRegex(ValueError, 'unchanged'):
            reviewer._verify_resolution(row, previous, current)
        row['resolution']['basis'] = 'new_evidence'
        with self.assertRaisesRegex(ValueError, 'previously supplied'):
            reviewer._verify_resolution(row, previous, current)
        row['resolution']['basis'] = 'candidate_change'
        current['Enew']['quote'] = 'boolean nonPinned = false;'
        reviewer._verify_resolution(row, previous, current)

    def test_final_judge_receives_complete_scopes_and_rejects_old_unwitnessed_pass(self):
        llm = Mock()
        def judge(messages):
            payload = json.loads(messages[1].content)
            self.assertTrue(payload['upstream_scopes'])
            self.assertEqual(payload['decision_requirements'], [dict(idx=h['idx'],
                test_scope_ids=h['test_scope_ids'], resolution_required=False)
                for h in payload['upstream_hunks']])
            self.assertIn('"test_witnesses":', messages[0].content)
            evidence = next(key for key, item in payload['evidence_registry'].items()
                            if item['kind'] == 'candidate_source')
            return SimpleNamespace(content=json.dumps({'members': [dict(idx=h['idx'],
                status='satisfied', reason='A test exists', evidence=[evidence])
                for h in payload['upstream_hunks']]}))
        llm.invoke.side_effect = judge
        candidate = self.repo.git.diff(self.base, self.upstream) + '\n'
        result = CoverageReviewer(llm, self.root, self.base, retries=0,
            upstream_parent=self.base, upstream_commit=self.upstream).review(self.members, candidate)
        self.assertFalse(result['complete'])
        self.assertEqual(result['unreviewed'], [0, 1])
        self.assertIn('setup/exercise/assertion', result['error'])

    def test_repair_feedback_does_not_label_upstream_paths_as_edit_locations(self):
        evidence = dict(kind='candidate_source', path='TargetTest.java', start_line=10,
                        end_line=20, quote='actual candidate test')
        row = repair_findings([dict(idx=1, file='MissingUpstreamTest.java', upstream_path_present=False,
                                   status='missing', reason='scenario absent', evidence=[evidence])])[0]
        self.assertNotIn('file', row)
        self.assertEqual(row['upstream_location'], dict(path='MissingUpstreamTest.java', path_present_in_candidate=False))
        self.assertEqual(row['target_locations'][0]['path'], 'TargetTest.java')
        self.assertEqual(row['target_location_status'], 'evidenced')
        unknown = repair_findings([dict(idx=2, file='Missing.java', evidence=[])])[0]
        self.assertEqual(unknown['target_locations'], [])
        self.assertEqual(unknown['target_location_status'], 'unresolved')

    def test_prior_missing_decision_survives_an_unjustified_later_pass(self):
        llm = Mock()
        member = [self.members[0]]
        calls = []
        def judge(messages):
            payload = json.loads(messages[1].content)
            calls.append(payload)
            evidence = next(key for key, item in payload['evidence_registry'].items()
                            if item['kind'] == 'candidate_source' and item['path'] == 'UpstreamTest.java')
            return SimpleNamespace(content=json.dumps({'members': [dict(idx=0,
                status='missing' if len(calls) == 1 else 'satisfied',
                reason='Scenario is absent' if len(calls) == 1 else 'A related test already exists',
                evidence=[evidence])]}))
        llm.invoke.side_effect = judge
        reviewer = CoverageReviewer(llm, self.root, self.base, retries=0)
        first = reviewer.review(member, self.repo.git.diff(self.base, self.upstream) + '\n')
        self.assertEqual(first['missing'], [0])
        (self.root/'Unrelated.java').write_text('class Unrelated {}\n')
        self.repo.index.add(['Unrelated.java'])
        second = reviewer.review(member, self.repo.git.diff(self.base) + '\n')
        self.assertFalse(second['complete'])
        self.assertIn('explicit evidenced resolution', second['error'])
        self.assertEqual(calls[1]['previous_findings'][0]['idx'], 0)
        self.assertIn('Unrelated.java', calls[1]['changed_since_previous_review'])
        self.assertEqual(reviewer.previous_review['result']['missing'], [0])


if __name__ == '__main__':
    unittest.main()
