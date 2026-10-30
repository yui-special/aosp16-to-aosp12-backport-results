"""Semantic decisions must retain relevance and prove the final candidate's coverage."""
import importlib.util
import json
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
from agent.coverage import CoverageReviewer, artifact_tree, read_blob, verify_review
from agent.invoke_llm import evaluate_global_artifact
from tools.project import Project
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

    def test_failed_model_decision_is_not_fabricated_from_keyword_hits(self):
        row = self.run_postprocess(None, {'@Rule': 10})
        self.assertIsNone(row['port'])

    def test_actual_selected_call_can_retain_its_required_definition(self):
        rows = [dict(hunk=0, port=False, reason='helper'), dict(hunk=1, port=True, reason='fix')]
        hunks = [{'text': '+void requiredHelper() {}'}, {'text': '+requiredHelper();'}]
        summaries = [dict(symbol='requiredHelper', kind='declare'), dict(symbol='caller', kind='logic')]
        triage.postprocess(rows, hunks, summaries, '', 'base')
        self.assertTrue(rows[0]['port'])
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

    def record(self):
        return {'members': [dict(idx=8, status='satisfied', reason='The actual target guard blocks this path.',
                                evidence=[dict(path='Example.java', start_line=2, end_line=2,
                                               quote='  boolean allowed() { return false; }')])]}

    def review(self, llm, executor, retries=0):
        reviewer = CoverageReviewer(llm, '.', 'base', retries=retries)
        with patch('agent.coverage.artifact_tree', return_value='candidate-tree'), \
                patch('agent.coverage.read_blob',
                      return_value='class Example {\n  boolean allowed() { return false; }\n}\n'), \
                patch('agent.coverage.create_tool_calling_agent'), \
                patch('agent.coverage.AgentExecutor', return_value=executor):
            return reviewer.review(self.members, self.candidate)

    def test_tool_evidence_and_final_json_judge_are_separate(self):
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(
            content='review follows\n```json\n' + json.dumps(self.record()) + '\n```',
            response_metadata={'finish_reason': 'stop'})
        executor = Mock()
        executor.invoke.return_value = {
            'output': 'Hunk 8 uses candidate evidence and the target guard is present.',
            'intermediate_steps': [],
        }
        result = self.review(llm, executor)
        self.assertTrue(result['complete'])
        self.assertEqual(executor.invoke.call_count, 1)
        self.assertTrue(executor.invoke.call_args.kwargs['return_intermediate_steps'])
        self.assertEqual(llm.invoke.call_count, 1)
        messages = llm.invoke.call_args.args[0]
        self.assertEqual([message.type for message in messages], ['system', 'human'])

    def test_prose_verdict_uses_a_separate_tool_free_json_formatter(self):
        llm = Mock()
        llm.invoke.side_effect = [
            SimpleNamespace(
                content='Hunk 8 is satisfied. Evidence Efixture supports the target guard.',
                response_metadata={'finish_reason': 'stop'}),
            SimpleNamespace(
                content=json.dumps(self.record()),
                response_metadata={'finish_reason': 'stop'}),
        ]
        executor = Mock()
        executor.invoke.return_value = {
            'output': 'Evidence collection is complete.',
            'intermediate_steps': [],
        }
        result = self.review(llm, executor, retries=0)
        self.assertTrue(result['complete'])
        self.assertEqual(executor.invoke.call_count, 1)
        self.assertEqual(llm.invoke.call_count, 2)
        formatter_messages = llm.invoke.call_args_list[1].args[0]
        self.assertEqual([message.type for message in formatter_messages], ['system', 'human'])
        self.assertIn('JSON transport formatter', formatter_messages[0].content)
        formatter_input = json.loads(formatter_messages[1].content)
        self.assertEqual(formatter_input['required_hunk_ids'], [8])
        self.assertIn('Evidence Efixture', formatter_input['original_review_response'])
        self.assertEqual(result['review_json_normalization']['method'], 'model_format_only')
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
        self.assertEqual(llm.invoke.call_count, 2)

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

    def test_semantic_review_does_not_reuse_a_different_artifacts_acceptance(self):
        missing = {'members': [dict(idx=8, status='missing', reason='The restrictive guard was removed.', evidence=[])]}
        llm = Mock()
        llm.invoke.side_effect = [
            SimpleNamespace(content=json.dumps(self.record())),
            SimpleNamespace(content=json.dumps(missing)),
        ]
        reviewer = CoverageReviewer(llm, self.root, self.base, retries=0)
        executor = Mock()
        executor.invoke.side_effect = [
            {'output': 'Evidence memo for the first candidate.', 'intermediate_steps': []},
            {'output': 'Evidence memo for the changed candidate.', 'intermediate_steps': []},
        ]
        with patch('agent.coverage.create_tool_calling_agent'), \
                patch('agent.coverage.AgentExecutor', return_value=executor):
            first = reviewer.review(self.members, self.candidate)
            self.assertTrue(first['complete'])
            self.assertTrue(reviewer.review(self.members, self.candidate)['complete'])
            self.source.write_text('class Example {\n  boolean allowed() { return true; } // still vulnerable\n}\n')
            another = self.repo.git.diff() + '\n'
            self.assertFalse(reviewer.review(self.members, another)['complete'])
        self.assertEqual(executor.invoke.call_count, 2)

    def test_review_service_failure_is_closed_and_not_cached_as_a_code_decision(self):
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content=json.dumps(self.record()))
        reviewer = CoverageReviewer(llm, self.root, self.base, retries=0)
        executor = Mock()
        executor.invoke.side_effect = [
            TimeoutError('fixture timeout'),
            {'output': 'Evidence memo after retry.', 'intermediate_steps': []},
        ]
        with patch('agent.coverage.create_tool_calling_agent'), \
                patch('agent.coverage.AgentExecutor', return_value=executor):
            failed = reviewer.review(self.members, self.candidate)
            self.assertFalse(failed['complete'])
            self.assertEqual(failed['method'], 'semantic_review_failed')
            self.assertIn('TimeoutError', failed['errors'][0])
            self.assertTrue(reviewer.review(self.members, self.candidate)['complete'])
        self.assertEqual(executor.invoke.call_count, 2)

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

    def test_semantic_failure_prevents_actual_build(self):
        project = Project(SimpleNamespace(project_url='fixture', project_dir=str(self.root),
            error_message='', new_patch_parent=self.base, target_release=self.base))
        project.full_patch_base = self.base
        project.all_hunks_applied_succeeded = True
        project.final_artifact_check = lambda _: 'SEMANTIC_VALIDATION_FAILED: missing regression test'
        (self.root/'build.sh').write_text('touch build-was-run\n')
        self.assertIn('SEMANTIC_VALIDATION_FAILED', project._validate(self.base, self.candidate))
        self.assertFalse((self.root/'build-was-run').exists())
        self.assertFalse(project.compile_succeeded)


if __name__ == '__main__':
    unittest.main()
