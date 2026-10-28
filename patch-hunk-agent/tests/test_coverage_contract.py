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
        reviewer = CoverageReviewer(Mock(), self.root, self.base, retries=0)
        missing = {'members': [dict(idx=8, status='missing', reason='The restrictive guard was removed.', evidence=[])]}
        executor = Mock()
        executor.invoke.side_effect = [{'output': json.dumps(self.record())}, {'output': json.dumps(missing)}]
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
        reviewer = CoverageReviewer(Mock(), self.root, self.base, retries=0)
        executor = Mock()
        executor.invoke.side_effect = [TimeoutError('fixture timeout'), {'output': json.dumps(self.record())}]
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
