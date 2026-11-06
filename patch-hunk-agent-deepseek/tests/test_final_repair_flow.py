"""Exercise generation -> real compiler -> repair despite a review outage."""
import json
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from git import Repo
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'))
from agent import invoke_llm as runtime
from agent.coverage import artifact_tree
from tools.source_checks import candidate_diff
from tools.project import Project, FinalValidationUnavailable, create_submit_candidate_tool, create_validate_tool


def complete_submission(project):
    """Fixtures describe final content; the real repair tool receives a local delta."""
    def invoke(arguments):
        ref = getattr(project, 'repair_candidate_ref', None)
        if ref and ref != 'unavailable':
            final = artifact_tree(project.dir, project.full_patch_base, arguments['patch'])
            arguments = dict(ref=ref, patch=candidate_diff(project.dir, ref, final))
        return create_submit_candidate_tool(project).invoke(arguments)
    return SimpleNamespace(invoke=invoke)


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class FinalRepairFlowTests(unittest.TestCase):
    def test_feedback_splits_production_and_test_coverage_without_losing_full_patch_contract(self):
        findings = [
            {'idx': 11, 'file': 'services/core/Example.java', 'reason': 'missing production API'},
            {'idx': 35, 'file': 'services/tests/ExampleTests.java', 'reason': 'missing pinned test'}]
        stages = runtime._repair_feedback_stages('compile failed\nmissing production and test', findings, True)
        self.assertEqual([name for name, _ in stages], ['production', 'test_coverage'])
        self.assertIn('compile failed', stages[0][1])
        self.assertIn('production-code completeness only', stages[0][1])
        self.assertNotIn('compile failed', stages[1][1])
        self.assertIn('missing pinned test', stages[1][1])
        for _, feedback in stages:
            self.assertIn('complete replacement patch', feedback)
        single = runtime._repair_feedback_stages('test gap', findings[1:], False)
        self.assertEqual([name for name, _ in single], ['test_coverage'])
        self.assertIn('test gap', single[0][1])
        self.assertIn('complete replacement patch', single[0][1])

    def test_semantic_pass_stops_intermediate_session_without_enabling_full_validation(self):
        project = Project.__new__(Project)
        project.all_hunks_applied_succeeded = True
        project.final_artifact_check = Mock(return_value='')
        project.full_validation_allowed = False
        result = create_submit_candidate_tool(project).invoke({'ref': 'base', 'patch': 'patch'})
        self.assertIn('Stop this repair session now', result)
        self.assertTrue(project.semantic_candidate_ready)
        self.assertFalse(project.full_validation_allowed)
        self.assertFalse(project.poc_succeeded)

    def test_real_executor_stops_at_first_submission_even_with_multiple_tool_calls(self):
        project = Project.__new__(Project)
        project.all_hunks_applied_succeeded = True
        project.final_artifact_check = Mock(return_value='')
        project.full_validation_allowed = False
        probe = Mock()

        @tool
        def later_read() -> str:
            """A read that must never run after candidate submission."""
            probe()
            return 'unexpected'

        llm = FakeMessagesListChatModel(responses=[AIMessage(content='', tool_calls=[
            dict(name='submit_candidate', args={'ref': 'base', 'patch': 'first'}, id='one'),
            dict(name='submit_candidate', args={'ref': 'base', 'patch': 'second'}, id='two'),
            dict(name='later_read', args={}, id='three')]), AIMessage(content='must not be requested')])
        prompt = runtime.ChatPromptTemplate.from_messages([
            ('human', '{input}'), runtime.MessagesPlaceholder('agent_scratchpad')])
        tools = [create_submit_candidate_tool(project), later_read]
        with patch.object(type(llm), 'bind_tools', return_value=llm, create=True):
            agent = runtime.create_tool_calling_agent(llm, tools, prompt)
            executor = runtime.CandidateRepairExecutor(agent=agent, tools=tools, max_iterations=10)
            result = executor.invoke({'input': 'submit'})
        self.assertIn('CANDIDATE_SEMANTIC_CHECK_PASS', result['output'])
        project.final_artifact_check.assert_called_once_with('first')
        probe.assert_not_called()
        self.assertEqual(llm.i, 1)

    def test_read_only_repair_session_cannot_finish_as_if_it_submitted_a_candidate(self):
        with tempfile.TemporaryDirectory(prefix='final repair ') as folder:
            root = Path(folder)
            repo = Repo.init(root/'repo')
            with repo.config_writer() as cfg:
                cfg.set_value('user', 'name', 'Test')
                cfg.set_value('user', 'email', 'test@example.invalid')
            source = Path(repo.working_tree_dir)/'example.c'
            original = 'void changed(int value) {}\nint main(void) {\n  changed(1);\n  return 0;\n}\n'
            source.write_text(original)
            repo.index.add(['example.c'])
            base = repo.index.commit('target').hexsha
            source.write_text(original.replace('int value)', 'int value, int restore)'))
            repo.index.add(['example.c'])
            upstream = repo.index.commit('changed interface').hexsha
            source.write_text(source.read_text().replace('changed(1);', 'changed(1, 1);'))
            repo.git.reset('--hard')
            repo.git.checkout('-b', 'cumulative', base)
            dataset = root/'dataset'
            dataset.mkdir()
            audit = root/'build-test.log'
            (dataset/'build.sh').write_text('echo build >> ' + shlex.quote(str(audit))
                                           + '\ncc -fsyntax-only example.c\n')
            (dataset/'test.sh').write_text('echo test >> ' + shlex.quote(str(audit)) + '\n')
            data = SimpleNamespace(project_url='fixture', project_dir=repo.working_tree_dir + '/',
                error_message='', new_patch_parent=base, target_release='cumulative',
                new_patch=upstream, cumulative=True, cum_base=base, patch_dataset_dir=str(dataset)+'/')
            project = Project(data)
            project.keep_tree_after_apply = True
            reviewer = Mock()
            reviewer.review.side_effect = lambda members, candidate: dict(
                complete=False, candidate_tree='tree', method='semantic_review_failed',
                error='JSONDecodeError', missing=[m['idx'] for m in members],
                members=[dict(idx=m['idx'], file=m['file'], covered=False, status='missing')
                         for m in members])
            repair = Mock()
            repair.invoke.return_value = {}
            with patch.object(runtime, 'CoverageReviewer', return_value=reviewer), \
                    patch.object(runtime, 'create_tool_calling_agent'), \
                    patch.object(runtime, 'CandidateRepairExecutor', return_value=repair), \
                    patch.dict(os.environ, {'CHAIN_CHECK_SCRIPT': str(ROOT/'data_ncwang/chain_check.py'),
                                           'PG_COMPILE_PER_GROUP': '0'}):
                runtime.do_backport(Mock(), project, data, Mock(), str(root/'generation.log'))
            repair.invoke.assert_called_once()
            self.assertFalse(project.poc_succeeded)
            self.assertEqual(audit.read_text(), 'build\n')

    def test_unresolved_production_stage_does_not_start_test_coverage_stage(self):
        with tempfile.TemporaryDirectory(prefix='final repair ') as folder:
            root = Path(folder)
            repo = Repo.init(root/'repo')
            with repo.config_writer() as cfg:
                cfg.set_value('user', 'name', 'Test')
                cfg.set_value('user', 'email', 'test@example.invalid')
            source = Path(repo.working_tree_dir)/'example.c'
            original = 'void changed(int value) {}\nint main(void) {\n  changed(1);\n  return 0;\n}\n'
            source.write_text(original)
            repo.index.add(['example.c'])
            base = repo.index.commit('target').hexsha
            source.write_text(original.replace('int value)', 'int value, int restore)'))
            repo.index.add(['example.c'])
            upstream = repo.index.commit('changed interface').hexsha
            source.write_text(source.read_text().replace('changed(1);', 'changed(1, 1);'))
            repaired = repo.git.diff(base) + '\n'
            repo.git.reset('--hard')
            repo.git.checkout('-b', 'cumulative', base)
            dataset = root/'dataset'
            dataset.mkdir()
            audit = root/'build-test.log'
            (dataset/'build.sh').write_text('echo build >> ' + shlex.quote(str(audit))
                                           + '\ncc -fsyntax-only example.c\n')
            (dataset/'test.sh').write_text('echo test >> ' + shlex.quote(str(audit)) + '\n')
            data = SimpleNamespace(project_url='fixture', project_dir=repo.working_tree_dir + '/',
                error_message='', new_patch_parent=base, target_release='cumulative',
                new_patch=upstream, cumulative=True, cum_base=base, patch_dataset_dir=str(dataset)+'/')
            project = Project(data)
            project.keep_tree_after_apply = True
            reviewer = Mock()
            reviewer.review.side_effect = lambda members, candidate: dict(
                complete=False, candidate_tree='tree', method='semantic_review_with_verified_source_evidence',
                error=None, missing=[m['idx'] for m in members],
                members=[dict(idx=m['idx'], file=m['file'], covered=False, status='missing')
                         for m in members])
            repair = Mock()
            observed = []
            def submit(payload, callbacks):
                deferred = create_validate_tool(project).invoke({'ref': base, 'patch': repaired})
                self.assertIn('FULL_VALIDATION_DEFERRED', deferred)
                result = complete_submission(project).invoke({'ref': base, 'patch': repaired})
                observed.append((project.final_validation_unavailable,
                                 list(project.final_validation_findings)))
                return result
            repair.invoke.side_effect = submit
            with patch.object(runtime, 'CoverageReviewer', return_value=reviewer), \
                    patch.object(runtime, 'create_tool_calling_agent'), \
                    patch.object(runtime, 'CandidateRepairExecutor', return_value=repair), \
                    patch.object(runtime, '_repair_feedback_stages',
                                 return_value=[('production', 'p'), ('test_coverage', 't')]), \
                    patch.dict(os.environ, {'CHAIN_CHECK_SCRIPT': str(ROOT/'data_ncwang/chain_check.py'),
                                           'PG_COMPILE_PER_GROUP': '0'}):
                runtime.do_backport(Mock(), project, data, Mock(), str(root/'generation.log'))
            self.assertEqual(repair.invoke.call_count, 2,
                             (observed,
                              [call.args[0]['compile_ret'] for call in repair.invoke.call_args_list]))
            self.assertEqual(repair.invoke.call_args_list[0].args[0]['compile_ret'], 'p')
            retry_feedback = repair.invoke.call_args_list[1].args[0]['compile_ret']
            self.assertIn('unresolved production findings', retry_feedback)
            self.assertNotIn('test_coverage', retry_feedback)
            self.assertFalse(project.poc_succeeded)
            self.assertEqual(audit.read_text(), 'build\n')

    def test_staged_repairs_finish_before_one_final_build_and_test(self):
        with tempfile.TemporaryDirectory(prefix='staged repair ') as folder:
            root = Path(folder)
            repo = Repo.init(root/'repo')
            with repo.config_writer() as cfg:
                cfg.set_value('user', 'name', 'Test')
                cfg.set_value('user', 'email', 'test@example.invalid')
            source = Path(repo.working_tree_dir)/'example.c'
            original = 'void changed(int value) {}\nint main(void) {\n  changed(1);\n  return 0;\n}\n'
            source.write_text(original)
            repo.index.add(['example.c'])
            base = repo.index.commit('target').hexsha
            source.write_text(original.replace('int value)', 'int value, int restore)'))
            repo.index.add(['example.c'])
            upstream = repo.index.commit('changed interface').hexsha
            source.write_text(source.read_text().replace('changed(1);', 'changed(1, 1);'))
            repaired = repo.git.diff(base) + '\n'
            source.write_text(source.read_text().replace('return 0;', 'return 0; /* fixed */'))
            repaired_final = repo.git.diff(base) + '\n'
            repo.git.reset('--hard')
            repo.git.checkout('-b', 'cumulative', base)
            dataset = root/'dataset'
            dataset.mkdir()
            audit = root/'build-test.log'
            (dataset/'build.sh').write_text('echo build >> ' + shlex.quote(str(audit))
                                           + '\ncc -fsyntax-only example.c\n')
            (dataset/'test.sh').write_text('echo test >> ' + shlex.quote(str(audit)) + '\n')
            data = SimpleNamespace(project_url='fixture', project_dir=repo.working_tree_dir + '/',
                error_message='', new_patch_parent=base, target_release='cumulative',
                new_patch=upstream, cumulative=True, cum_base=base, patch_dataset_dir=str(dataset)+'/')
            project = Project(data)
            project.keep_tree_after_apply = True
            reviewer = Mock()

            def review(members, candidate):
                production_ok = '+  changed(1, 1);' in candidate
                test_ok = '/* fixed */' in candidate
                rows = [dict(idx=0, file='example.c', covered=production_ok,
                             status='satisfied' if production_ok else 'missing'),
                        dict(idx=35, file='services/tests/ExampleTests.java', covered=test_ok,
                             status='satisfied' if test_ok else 'missing')]
                return dict(complete=production_ok and test_ok,
                    candidate_tree=artifact_tree(repo.working_tree_dir, base,
                                                 candidate.rstrip('\n') + '\n'),
                    method='semantic_review_with_verified_source_evidence', error=None,
                    missing=[m['idx'] for m in rows if not m['covered']], members=rows)

            reviewer.review.side_effect = review
            repair = Mock()

            def fix(payload, callbacks):
                calls = len(repair.invoke.call_args_list)
                if calls == 1:
                    return complete_submission(project).invoke({'ref': base, 'patch': repaired})
                if calls == 2:
                    return complete_submission(project).invoke({'ref': base, 'patch': repaired_final})
                return create_validate_tool(project).invoke({'ref': base, 'patch': repaired_final})

            repair.invoke.side_effect = fix
            with patch.object(runtime, 'CoverageReviewer', return_value=reviewer), \
                    patch.object(runtime, 'create_tool_calling_agent'), \
                    patch.object(runtime, 'CandidateRepairExecutor', return_value=repair), \
                    patch.dict(os.environ, {'CHAIN_CHECK_SCRIPT': str(ROOT/'data_ncwang/chain_check.py'),
                                           'PG_COMPILE_PER_GROUP': '0'}):
                runtime.do_backport(Mock(), project, data, Mock(), str(root/'generation.log'))
            self.assertEqual(repair.invoke.call_count, 2)
            self.assertEqual(audit.read_text(), 'build\nbuild\ntest\n')
            self.assertTrue(project.poc_succeeded)
            self.assertIn('/* fixed */', (dataset/'complete.patch').read_text())

    def test_test_stage_production_regression_returns_to_production_before_final_validation(self):
        with tempfile.TemporaryDirectory(prefix='repair category switch ') as folder:
            root = Path(folder)
            repo = Repo.init(root/'repo')
            with repo.config_writer() as cfg:
                cfg.set_value('user', 'name', 'Test')
                cfg.set_value('user', 'email', 'test@example.invalid')
            source = Path(repo.working_tree_dir)/'example.c'
            original = 'void changed(int value) {}\nint main(void) {\n  changed(1);\n  return 0;\n}\n'
            source.write_text(original)
            repo.index.add(['example.c'])
            base = repo.index.commit('target').hexsha
            source.write_text(original.replace('int value)', 'int value, int restore)'))
            repo.index.add(['example.c'])
            upstream = repo.index.commit('changed interface').hexsha
            source.write_text(source.read_text().replace('changed(1);', 'changed(1, 1);'))
            repaired = repo.git.diff(base) + '\n'
            source.write_text(source.read_text().replace('return 0;', 'return 0; /* attempt 1 */'))
            repaired_attempt1 = repo.git.diff(base) + '\n'
            source.write_text(source.read_text().replace('/* attempt 1 */', '/* attempt 2 */'))
            repaired_attempt2 = repo.git.diff(base) + '\n'
            source.write_text(original.replace('return 0;', 'return 0; /* fixed */'))
            test_only_regression = repo.git.diff(base) + '\n'
            source.write_text(source.read_text().replace('int value)', 'int value, int restore)')
                              .replace('changed(1);', 'changed(1, 1);'))
            repaired_final = repo.git.diff(base) + '\n'
            repo.git.reset('--hard')
            repo.git.checkout('-b', 'cumulative', base)
            dataset = root/'dataset'
            dataset.mkdir()
            audit = root/'build-test.log'
            (dataset/'build.sh').write_text('echo build >> ' + shlex.quote(str(audit))
                                           + '\ncc -fsyntax-only example.c\n')
            (dataset/'test.sh').write_text('echo test >> ' + shlex.quote(str(audit)) + '\n')
            data = SimpleNamespace(project_url='fixture', project_dir=repo.working_tree_dir + '/',
                error_message='', new_patch_parent=base, target_release='cumulative',
                new_patch=upstream, cumulative=True, cum_base=base, patch_dataset_dir=str(dataset)+'/')
            project = Project(data)
            project.keep_tree_after_apply = True
            reviewer = Mock()

            def review(members, candidate):
                production_ok = '+  changed(1, 1);' in candidate
                test_ok = '/* fixed */' in candidate
                rows = [dict(idx=0, file='example.c', covered=production_ok,
                             status='satisfied' if production_ok else 'missing'),
                        dict(idx=35, file='services/tests/ExampleTests.java', covered=test_ok,
                             status='satisfied' if test_ok else 'missing')]
                return dict(complete=production_ok and test_ok,
                    candidate_tree=artifact_tree(repo.working_tree_dir, base,
                                                 candidate.rstrip('\n') + '\n'),
                    method='semantic_review_with_verified_source_evidence', error=None,
                    missing=[m['idx'] for m in rows if not m['covered']], members=rows)

            reviewer.review.side_effect = review
            repair = Mock()

            def fix(payload, callbacks):
                calls = len(repair.invoke.call_args_list)
                if calls in (1, 5):
                    candidate = repaired
                elif calls in (2, 6):
                    candidate = repaired_attempt1
                elif calls == 3:
                    candidate = repaired_attempt2
                elif calls == 4:
                    candidate = test_only_regression
                elif calls == 7:
                    candidate = repaired_final
                else:
                    self.assertIn('All known semantic findings are resolved', payload['compile_ret'])
                    return create_validate_tool(project).invoke({'ref': base, 'patch': repaired_final})
                if calls == 5:
                    self.assertIn('test repair exposed production-code gaps', payload['compile_ret'])
                return complete_submission(project).invoke({'ref': base, 'patch': candidate})

            repair.invoke.side_effect = fix
            with patch.object(runtime, 'CoverageReviewer', return_value=reviewer), \
                    patch.object(runtime, 'create_tool_calling_agent'), \
                    patch.object(runtime, 'CandidateRepairExecutor', return_value=repair), \
                    patch.dict(os.environ, {'CHAIN_CHECK_SCRIPT': str(ROOT/'data_ncwang/chain_check.py'),
                                           'PG_COMPILE_PER_GROUP': '0'}):
                runtime.do_backport(Mock(), project, data, Mock(), str(root/'generation.log'))
            self.assertEqual(repair.invoke.call_count, 7)
            self.assertEqual(audit.read_text(), 'build\nbuild\ntest\n')
            self.assertTrue(project.poc_succeeded)
            self.assertIn('/* fixed */', (dataset/'complete.patch').read_text())

    def test_compile_error_and_api_finding_reach_repair_when_review_is_unavailable(self):
        with tempfile.TemporaryDirectory(prefix='final repair ') as folder:
            root = Path(folder)
            repo = Repo.init(root/'repo')
            with repo.config_writer() as cfg:
                cfg.set_value('user', 'name', 'Test')
                cfg.set_value('user', 'email', 'test@example.invalid')
            source = Path(repo.working_tree_dir)/'example.c'
            original = 'void changed(int value) {}\nint main(void) {\n  changed(1);\n  return 0;\n}\n'
            source.write_text(original)
            repo.index.add(['example.c'])
            base = repo.index.commit('target').hexsha
            source.write_text(original.replace('int value)', 'int value, int restore)'))
            repo.index.add(['example.c'])
            upstream = repo.index.commit('changed interface').hexsha
            source.write_text(source.read_text().replace('changed(1);', 'changed(1, 1);'))
            repaired = repo.git.diff(base) + '\n'
            source.write_text(source.read_text().replace('return 0;', 'return 0; /* intermediate */'))
            repaired_intermediate = repo.git.diff(base) + '\n'
            source.write_text(source.read_text().replace('/* intermediate */', '/* fixed */'))
            repaired_final = repo.git.diff(base) + '\n'
            repo.git.reset('--hard')
            repo.git.checkout('-b', 'cumulative', base)
            dataset = root/'dataset'
            dataset.mkdir()
            audit = root/'build-test.log'
            (dataset/'build.sh').write_text('echo build >> ' + shlex.quote(str(audit))
                                           + '\ncc -fsyntax-only example.c\n')
            (dataset/'test.sh').write_text('echo test >> ' + shlex.quote(str(audit))
                                           + '\ngrep -q "/\* fixed \*/" example.c\n')
            data = SimpleNamespace(project_url='fixture', project_dir=repo.working_tree_dir + '/',
                error_message='', new_patch_parent=base, target_release='cumulative',
                new_patch=upstream, cumulative=True, cum_base=base, patch_dataset_dir=str(dataset)+'/')
            project = Project(data)
            project.keep_tree_after_apply = True
            reviewer = Mock()

            def review(members, candidate):
                tree = artifact_tree(repo.working_tree_dir, base, candidate.rstrip('\n') + '\n')
                missing_call = '+  changed(1, 1);' not in candidate
                return dict(complete=not missing_call, candidate_tree=tree,
                    method='semantic_review_failed' if missing_call else 'semantic_review_with_verified_source_evidence',
                    error='JSONDecodeError' if missing_call else None,
                    missing=[m['idx'] for m in members] if missing_call else [],
                    members=[dict(idx=m['idx'], file=m['file'], covered=not missing_call,
                                  status='missing' if missing_call else 'satisfied') for m in members])

            reviewer.review.side_effect = review
            repair = Mock()

            def fix(payload, callbacks):
                if len(repair.invoke.call_args_list) == 1:
                    self.assertIn('too few arguments', payload['compile_ret'])
                    self.assertIn('FINAL_VALIDATION_UNAVAILABLE', payload['compile_ret'])
                    self.assertIn('API_CHAIN_VALIDATION_FAILED', payload['compile_ret'])
                    self.assertEqual(audit.read_text(), 'build\n')
                    complete_submission(project).invoke({'ref': base, 'patch': repaired})
                    self.assertFalse(project.poc_succeeded)
                elif len(repair.invoke.call_args_list) == 2:
                    complete_submission(project).invoke({'ref': base, 'patch': repaired_intermediate})
                    self.assertFalse(project.poc_succeeded)
                else:
                    complete_submission(project).invoke({'ref': base, 'patch': repaired_final})
                    self.assertFalse(project.poc_succeeded)

            repair.invoke.side_effect = fix
            with patch.object(runtime, 'CoverageReviewer', return_value=reviewer), \
                    patch.object(runtime, 'create_tool_calling_agent'), \
                    patch.object(runtime, 'CandidateRepairExecutor', return_value=repair), \
                    patch.dict(os.environ, {'CHAIN_CHECK_SCRIPT': str(ROOT/'data_ncwang/chain_check.py'),
                                           'PG_COMPILE_PER_GROUP': '0'}):
                runtime.do_backport(Mock(), project, data, Mock(), str(root/'generation.log'))
            self.assertEqual(repair.invoke.call_count, 3)
            self.assertEqual(audit.read_text(), 'build\nbuild\ntest\nbuild\ntest\nbuild\ntest\n')
            self.assertTrue(project.poc_succeeded)
            output = (dataset/'complete.patch').read_text()
            self.assertIn('+  changed(1, 1);', output)
            ported = dataset/'merged_hunks/ported'
            report = json.loads((ported/'global-validation.json').read_text())
            self.assertTrue(report['complete'])
            self.assertEqual(report['api_chain_rc'], 0)
            self.assertEqual((ported/'validated-checkpoints/latest-validated.patch').read_text(), output)


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class FinalRepairRegressionTests(unittest.TestCase):
    def test_unlimited_backend_exceeds_configured_repair_budget_and_still_validates(self):
        self.data.unlimited_backend = True
        self.data.final_repair_retries = 0
        self.data.final_repair_iterations = 1
        candidates = [self.candidate('// attempt %d' % i) for i in range(5)]
        candidates.append(self.candidate('/* production */ /* covered */'))
        repair = self.run_repairs(candidates)
        self.assertEqual(repair.invoke.call_count, 6)
        self.assertIsNone(repair.max_iterations)
        self.assertIsNone(self.project.final_repair_reads_remaining)
        self.assertIsNone(self.project.final_repair_read_limit)
        self.assertIsNone(self.project.query_progress.stale_limit)
        self.assertTrue(self.project.poc_succeeded)

    def test_real_executor_can_correct_rejected_local_submission_within_budget(self):
        seed = self.candidate('/* production */')
        candidate = artifact_tree(self.project.dir, self.base, seed)
        final = artifact_tree(self.project.dir, self.base, self.candidate('/* production */ /* repair */'))
        delta = candidate_diff(self.project.dir, candidate, final)
        self.project.latest_candidate_patch = seed
        self.project.repair_candidate_ref = candidate
        self.project.full_patch_base = self.base
        self.project.all_hunks_applied_succeeded = True
        self.project.final_artifact_check = Mock(return_value='')
        llm = FakeMessagesListChatModel(responses=[
            AIMessage(content='', tool_calls=[dict(name='submit_candidate',
                args={'ref': self.base, 'patch': delta}, id='stale')]),
            AIMessage(content='', tool_calls=[dict(name='submit_candidate',
                args={'ref': candidate, 'patch': delta}, id='fixed')]),
            AIMessage(content='must not be requested')])
        prompt = runtime.ChatPromptTemplate.from_messages([
            ('human', '{input}'), runtime.MessagesPlaceholder('agent_scratchpad')])
        tools = [create_submit_candidate_tool(self.project)]
        with patch.object(type(llm), 'bind_tools', return_value=llm, create=True):
            agent = runtime.create_tool_calling_agent(llm, tools, prompt)
            result = runtime.CandidateRepairExecutor(agent=agent, tools=tools, max_iterations=None).invoke(
                {'input': 'repair'})
        self.assertIn('CANDIDATE_SEMANTIC_CHECK_PASS', result['output'])
        self.assertEqual(llm.i, 2)
        self.project.final_artifact_check.assert_called_once()

    def test_local_submission_preserves_unrelated_changes_and_ignores_dirty_worktree(self):
        seed = self.candidate('/* production */ /* covered */')
        candidate = artifact_tree(self.project.dir, self.base, seed)
        final = self.candidate('/* production */ /* covered */ /* local repair */')
        tree = artifact_tree(self.project.dir, self.base, final)
        delta = candidate_diff(self.project.dir, candidate, tree)
        self.project.latest_candidate_patch = seed
        self.project.repair_candidate_ref = candidate
        self.project.full_patch_base = self.base
        self.project.all_hunks_applied_succeeded = True
        self.project.final_artifact_check = Mock(return_value='')
        self.source.write_text('dirty unrelated checkout')
        before_index = self.repo.index.write_tree().hexsha
        result = create_submit_candidate_tool(self.project).invoke({'ref': candidate, 'patch': delta})
        self.assertIn('CANDIDATE_SEMANTIC_CHECK_PASS', result)
        self.assertEqual(artifact_tree(self.project.dir, self.base, self.project.latest_candidate_patch), tree)
        self.assertEqual(self.source.read_text(), 'dirty unrelated checkout')
        self.assertEqual(self.repo.index.write_tree().hexsha, before_index)
        self.project.final_artifact_check.assert_called_once()

    def test_local_submission_rejects_stale_ref_and_atomic_multifile_conflict(self):
        seed = self.candidate('/* production */')
        candidate = artifact_tree(self.project.dir, self.base, seed)
        final = artifact_tree(self.project.dir, self.base, self.candidate('/* production */ /* repair */'))
        delta = candidate_diff(self.project.dir, candidate, final)
        self.project.latest_candidate_patch = seed
        self.project.repair_candidate_ref = candidate
        self.project.full_patch_base = self.base
        self.project.all_hunks_applied_succeeded = True
        self.project.final_artifact_check = Mock(return_value='')
        tool = create_submit_candidate_tool(self.project)
        self.assertIn('CANDIDATE_SUBMISSION_INVALID', tool.invoke({'ref': self.base, 'patch': delta}))
        conflict = delta + ('diff --git a/missing.c b/missing.c\n--- a/missing.c\n+++ b/missing.c\n'
                            '@@ -1 +1 @@\n-old\n+new\n')
        self.assertIn('CANDIDATE_SUBMISSION_INVALID', tool.invoke({'ref': candidate, 'patch': conflict}))
        self.assertEqual(self.project.latest_candidate_patch, seed)
        self.project.final_artifact_check.assert_not_called()

    def test_local_submission_refuses_offset_relocation(self):
        import re
        seed = self.candidate('/* original */')
        candidate = artifact_tree(self.project.dir, self.base, seed)
        final = artifact_tree(self.project.dir, self.base, self.candidate('/* edited */'))
        delta = candidate_diff(self.project.dir, candidate, final)
        shifted = re.sub(r'@@ -(\d+)', lambda m: '@@ -' + str(int(m[1])+2), delta)
        with self.assertRaises(ValueError):
            artifact_tree(self.project.dir, candidate, shifted, strict=True)

    def test_explicit_infrastructure_failure_never_enters_llm_repair(self):
        (self.dataset/'build.sh').write_text('echo "No space left on device" >&2\nexit 1\n')
        repair = self.run_repairs([], seed=self.candidate('/* production */ /* covered */'))
        repair.invoke.assert_not_called()
        self.assertFalse(self.project.poc_succeeded)
        self.assertEqual(self.project.validation_infrastructure_failure['kind'], 'disk_full')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='repair regression ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repo.init(self.root/'repo')
        with self.repo.config_writer() as cfg:
            cfg.set_value('user', 'name', 'Test')
            cfg.set_value('user', 'email', 'test@example.invalid')
        self.source = Path(self.repo.working_tree_dir)/'example.c'
        original = 'void changed(int value) {}\nint main(void) {\n  changed(1);\n  return 0;\n}\n'
        self.source.write_text(original)
        self.repo.index.add(['example.c'])
        self.base = self.repo.index.commit('target').hexsha
        self.changed = original.replace('int value)', 'int value, int restore)')
        self.source.write_text(self.changed)
        self.repo.index.add(['example.c'])
        upstream = self.repo.index.commit('changed interface').hexsha
        self.dataset = self.root/'dataset'
        self.dataset.mkdir()
        self.audit = self.root/'build-test.log'
        (self.dataset/'build.sh').write_text('echo build >> ' + shlex.quote(str(self.audit))
                                            + '\ncc -fsyntax-only example.c\n')
        (self.dataset/'test.sh').write_text('echo test >> ' + shlex.quote(str(self.audit)) + '\n')
        self.data = SimpleNamespace(project_url='fixture', project_dir=self.repo.working_tree_dir+'/',
            error_message='', new_patch_parent=self.base, target_release='cumulative',
            new_patch=upstream, cumulative=True, cum_base=self.base, patch_dataset_dir=str(self.dataset)+'/')
        self.project = Project(self.data)
        self.project.keep_tree_after_apply = True

    def candidate(self, suffix):
        self.source.write_text(self.changed.replace('changed(1);', 'changed(1, 1);') + suffix + '\n')
        return self.repo.git.diff(self.base) + '\n'

    def run_repairs(self, candidates, seed=None, review_transform=None):
        if seed is not None:
            self.data.repair_candidate = seed
        self.repo.git.reset('--hard')
        self.repo.git.checkout('-b', 'cumulative', self.base)
        reviewer = Mock()

        def review(members, candidate):
            rows = [dict(idx=0, file='example.c', covered='/* production */' in candidate),
                    dict(idx=35, file='tests/ExampleTest.java', covered='/* covered */' in candidate)]
            for row in rows:
                row['status'] = 'satisfied' if row['covered'] else 'missing'
            result = dict(complete=all(row['covered'] for row in rows),
                candidate_tree=artifact_tree(self.repo.working_tree_dir, self.base, candidate.rstrip('\n') + '\n'),
                method='semantic_review_with_verified_source_evidence',
                missing=[row['idx'] for row in rows if not row['covered']], members=rows)
            return review_transform(result) if review_transform else result

        reviewer.review.side_effect = review
        repair, iterator = Mock(), iter(candidates)
        repair.invoke.side_effect = lambda *args: complete_submission(self.project).invoke(
            {'ref': self.base, 'patch': next(iterator)})
        with patch.object(runtime, 'CoverageReviewer', return_value=reviewer), \
                patch.object(runtime, 'create_tool_calling_agent') as create, \
                patch.object(runtime, 'CandidateRepairExecutor', return_value=repair), \
                patch.dict(os.environ, {'CHAIN_CHECK_SCRIPT': str(ROOT/'data_ncwang/chain_check.py'),
                                       'PG_COMPILE_PER_GROUP': '0'}):
            group_agent = Mock()
            runtime.do_backport(group_agent, self.project, self.data, Mock(), str(self.root/'run.log'))
        if seed is not None:
            group_agent.invoke.assert_not_called()
            ported = self.dataset / 'merged_hunks/ported'
            self.assertEqual(list(ported.glob('*.api-inventory.json')), [])
            self.assertEqual(json.loads((ported/'replay-input.json').read_text())['selected_hunks'], [0])
        if create.called:
            self.assertNotIn('validate', [tool.name for tool in create.call_args.args[1]])
        return repair

    def test_replay_uses_complete_seed_without_generating_groups(self):
        seed = self.candidate('/* production */')
        fixed = self.candidate('/* production */ /* covered */')
        repair = self.run_repairs([fixed], seed=seed)
        repair.invoke.assert_called_once()
        self.assertEqual(repair.invoke.call_args.args[0]['complete_patch'], seed)
        self.assertTrue(self.project.poc_succeeded)
        self.assertEqual(self.audit.read_text(), 'build\ntest\nbuild\ntest\n')

    def test_replay_of_valid_seed_runs_checks_without_unnecessary_repair(self):
        seed = self.candidate('/* production */ /* covered */')
        repair = self.run_repairs([], seed=seed)
        repair.invoke.assert_not_called()
        self.assertTrue(self.project.poc_succeeded)
        self.assertEqual(self.audit.read_text(), 'build\ntest\n')
        self.assertEqual((self.dataset/'complete.patch').read_text(), seed)

    def test_test_compiler_repair_routes_new_coverage_findings_back_to_coverage(self):
        (self.dataset/'test.sh').write_text(
            'echo test >> ' + shlex.quote(str(self.audit)) +
            '\ngrep -Fq "/* repaired */" example.c\n')
        seed = self.candidate('/* production */ /* covered */')
        candidates = [self.candidate('/* production */ /* repaired */'),
                      self.candidate('/* production */ /* covered */ /* repaired */')]
        repair = self.run_repairs(candidates, seed=seed)
        self.assertTrue(self.project.poc_succeeded)
        self.assertEqual(repair.invoke.call_count, 2)
        self.assertIn('test-coverage', repair.invoke.call_args_list[1].args[0]['compile_ret'])
        self.assertIn('upstream_location', repair.invoke.call_args_list[1].args[0]['compile_ret'])

    def test_partial_review_keeps_test_gap_repairable_without_inventing_production_defect(self):
        seed = self.candidate('/* production */')
        fixed = self.candidate('/* production */ /* covered */')

        def partial(result):
            if not result['complete']:
                result.update(method='semantic_review_failed', error='unknown evidence for hunk 1',
                              unreviewed=[1])
                result['members'].append(dict(idx=1, file='example.c', covered=False,
                    status='unreviewed', reason='semantic_review_unavailable', evidence=[]))
            return result

        repair = self.run_repairs([fixed], seed=seed, review_transform=partial)
        repair.invoke.assert_called_once()
        feedback = repair.invoke.call_args.args[0]['compile_ret']
        self.assertIn('SEMANTIC_VALIDATION_FAILED', feedback)
        self.assertIn('repair every current test-coverage finding', feedback)
        self.assertNotIn('production-code completeness only', feedback)
        self.assertTrue(self.project.poc_succeeded)
        self.assertEqual(self.audit.read_text(), 'build\ntest\nbuild\ntest\n')

    def test_unreviewed_hunk_alone_neither_accepts_nor_requests_code_changes(self):
        seed = self.candidate('/* production */ /* covered */')

        def unavailable(result):
            result.update(complete=False, method='semantic_review_failed', error='unknown evidence',
                          unreviewed=[0], missing=[])
            result['members'][0].update(status='unreviewed', covered=False,
                                       reason='semantic_review_unavailable')
            return result

        repair = self.run_repairs([], seed=seed, review_transform=unavailable)
        repair.invoke.assert_not_called()
        self.assertFalse(self.project.poc_succeeded)
        self.assertFalse(self.project.final_repair_required)
        self.assertEqual(self.project.final_validation_findings, [])
        self.assertEqual(self.audit.read_text(), 'build\ntest\n')

    def test_latest_unvalidated_candidate_survives_checker_failure(self):
        project = self.project
        directory = self.root/'candidates'
        project.candidate_checkpoint_dir = str(directory)
        project.full_patch_base = self.base
        project.all_hunks_applied_succeeded = True
        project.final_artifact_check = Mock(side_effect=FinalValidationUnavailable('service unavailable'))
        submitted = self.candidate('/* production */')
        project._submit_candidate('wrong-model-ref', submitted)
        self.assertEqual((directory/'latest-candidate.patch').read_text(), submitted)
        meta = json.loads((directory/'latest-candidate.json').read_text())
        self.assertEqual(meta['base_ref'], self.base)
        self.assertTrue(meta['validation_required'])
        self.assertEqual(meta['sha256'], hashlib.sha256(submitted.encode()).hexdigest())
        self.assertFalse(project.poc_succeeded)
        self.assertEqual(project.validated_patch_checkpoints, [])
        second = self.candidate('/* production */ /* covered */')
        project.final_artifact_check.side_effect = RuntimeError('unexpected checker failure')
        with self.assertRaisesRegex(RuntimeError, 'unexpected checker failure'):
            project._submit_candidate(self.base, second)
        self.assertEqual((directory/'latest-candidate.patch').read_text(), second)
        self.assertEqual(project.validated_patch_checkpoints, [])

    def test_candidate_checkpoint_replace_failure_preserves_previous_patch(self):
        directory = self.root/'candidates'
        self.project.candidate_checkpoint_dir = str(directory)
        previous = self.candidate('/* first */')
        self.project.persist_candidate(previous, self.base)
        with patch('tools.project.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.project.persist_candidate(self.candidate('/* second */'), self.base)
        self.assertEqual((directory/'latest-candidate.patch').read_text(), previous)
        self.assertEqual(sorted(p.name for p in directory.iterdir()),
                         ['latest-candidate.json', 'latest-candidate.patch'])

    def test_checkpoint_failure_cannot_inherit_an_older_validation_success(self):
        self.project.all_hunks_applied_succeeded = True
        self.project.compile_succeeded = self.project.testcase_succeeded = self.project.poc_succeeded = True
        with patch.object(self.project, 'persist_candidate', side_effect=OSError('disk unavailable')), \
                patch.object(self.project, '_compile_patch') as compile_patch:
            with self.assertRaises(OSError):
                self.project._validate(self.base, self.candidate('/* next */'))
        self.assertFalse(self.project.poc_succeeded)
        self.assertFalse(self.project.compile_succeeded)
        self.assertFalse(self.project.testcase_succeeded)
        compile_patch.assert_not_called()

    def test_production_retries_do_not_consume_test_coverage_budget(self):
        candidates = [self.candidate('// production attempt 1'),
                      self.candidate('// production attempt 2'),
                      self.candidate('/* production */'),
                      self.candidate('/* production */ // test attempt 1'),
                      self.candidate('/* production */ // test attempt 2'),
                      self.candidate('/* production */ /* covered */')]
        repair = self.run_repairs(candidates)
        self.assertEqual(repair.invoke.call_count, 6)
        self.assertIn('current test-coverage finding', repair.invoke.call_args_list[3].args[0]['compile_ret'])
        self.assertTrue(self.project.poc_succeeded)
        self.assertEqual(self.audit.read_text(), 'build\nbuild\ntest\n')

    def test_final_compile_failure_returns_to_repair_without_an_llm_validation_turn(self):
        broken = self.candidate('/* production */ /* covered */\nint = ;')
        fixed = self.candidate('/* production */ /* covered */')
        repair = self.run_repairs([broken, fixed])
        self.assertEqual(repair.invoke.call_count, 2)
        self.assertEqual(artifact_tree(self.project.dir, self.base,
                                      repair.invoke.call_args_list[1].args[0]['complete_patch']),
                         artifact_tree(self.project.dir, self.base, broken))
        self.assertIn('error:', repair.invoke.call_args_list[1].args[0]['compile_ret'])
        retry = repair.invoke.call_args_list[1].args[0]
        self.assertEqual(retry['candidate_ref'], artifact_tree(self.project.dir, self.base, broken))
        self.assertEqual(retry['target_release'], self.base)
        self.assertTrue(self.project.poc_succeeded)
        self.assertEqual(self.audit.read_text(), 'build\nbuild\nbuild\ntest\n')

    def test_runtime_test_failure_preserves_build_success_and_routes_to_test_repair(self):
        seed = self.candidate('/* production */ /* covered */')
        fixed = self.candidate('/* production */ /* covered */ /* test fixed */')
        (self.dataset/'test.sh').write_text(
            'echo test >> ' + shlex.quote(str(self.audit))
            + '\ngrep -q "test fixed" example.c\n')
        repair = self.run_repairs([fixed], seed=seed)
        feedback = repair.invoke.call_args.args[0]['compile_ret']
        self.assertIn('CURRENT STAGE: repair the reported test/PoC failure', feedback)
        self.assertIn('Compilation already passed', feedback)
        self.assertEqual(repair.invoke.call_args.args[0]['candidate_ref'],
                         artifact_tree(self.project.dir, self.base, seed))
        self.assertTrue(self.project.poc_succeeded)
        self.assertEqual(self.audit.read_text(), 'build\ntest\nbuild\ntest\n')

    def test_test_and_poc_failures_preserve_earlier_successful_stage_results(self):
        self.project.compile_succeeded = True
        (Path(self.project.dir)/'test.sh').write_text('echo failed >&2\nexit 1\n')
        self.project.testcase_succeeded = self.project.poc_succeeded = True
        with patch('tools.project.candidate_failure_context', side_effect=ValueError('bad context')):
            feedback = self.project._run_testcase()
        self.assertTrue(self.project.compile_succeeded)
        self.assertFalse(self.project.testcase_succeeded)
        self.assertFalse(self.project.poc_succeeded)
        self.assertIn('failed', feedback)
        self.assertIn('bad context', feedback)
        self.project.testcase_succeeded = self.project.poc_succeeded = True
        (Path(self.project.dir)/'poc.sh').write_text('echo bug_still_present\n')
        self.project.err_msg = 'bug_still_present'
        self.project._run_poc('candidate')
        self.assertTrue(self.project.compile_succeeded)
        self.assertTrue(self.project.testcase_succeeded)
        self.assertFalse(self.project.poc_succeeded)

    def test_same_failed_tree_with_different_diff_format_is_not_built_twice(self):
        broken = self.candidate('/* production */ /* covered */\nint = ;')
        repair = self.run_repairs([broken, broken + '\n'])
        self.assertEqual(repair.invoke.call_count, 2)
        self.assertFalse(self.project.poc_succeeded)
        self.assertEqual(self.audit.read_text(), 'build\nbuild\n')

    def test_configured_retry_budget_bounds_repeated_semantic_failures(self):
        self.data.final_repair_retries = 1
        repair = self.run_repairs([self.candidate('// first'), self.candidate('// second')])
        self.assertEqual(repair.invoke.call_count, 2)
        self.assertFalse(self.project.poc_succeeded)
        self.assertEqual(self.audit.read_text(), 'build\n')


if __name__ == '__main__':
    unittest.main()
