"""Exercise generation -> real compiler -> repair despite a review outage."""
import json
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'data_ncwang/backport_patch/tools/patch-backporting/src'))
from agent import invoke_llm as runtime
from agent.coverage import artifact_tree
from tools.project import Project, create_validate_tool


@unittest.skipUnless(shutil.which('cc'), 'C compiler required')
class FinalRepairFlowTests(unittest.TestCase):
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
                self.assertIn('too few arguments', payload['compile_ret'])
                self.assertIn('FINAL_VALIDATION_UNAVAILABLE', payload['compile_ret'])
                self.assertIn('API_CHAIN_VALIDATION_FAILED', payload['compile_ret'])
                self.assertEqual(audit.read_text(), 'build\n')
                create_validate_tool(project).invoke({'ref': base, 'patch': repaired})
                self.assertTrue(project.poc_succeeded)

            repair.invoke.side_effect = fix
            with patch.object(runtime, 'CoverageReviewer', return_value=reviewer), \
                    patch.object(runtime, 'create_tool_calling_agent'), \
                    patch.object(runtime, 'AgentExecutor', return_value=repair), \
                    patch.dict(os.environ, {'CHAIN_CHECK_SCRIPT': str(ROOT/'data_ncwang/chain_check.py'),
                                           'PG_COMPILE_PER_GROUP': '0'}):
                runtime.do_backport(Mock(), project, data, Mock(), str(root/'generation.log'))
            repair.invoke.assert_called_once()
            self.assertEqual(audit.read_text(), 'build\nbuild\ntest\n')
            self.assertTrue(project.poc_succeeded)
            output = (dataset/'complete.patch').read_text()
            self.assertIn('+  changed(1, 1);', output)
            ported = dataset/'merged_hunks/ported'
            report = json.loads((ported/'global-validation.json').read_text())
            self.assertTrue(report['complete'])
            self.assertEqual(report['api_chain_rc'], 0)
            self.assertEqual((ported/'validated-checkpoints/latest-validated.patch').read_text(), output)


if __name__ == '__main__':
    unittest.main()
