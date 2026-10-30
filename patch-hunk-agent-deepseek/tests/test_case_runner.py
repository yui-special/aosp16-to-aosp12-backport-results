"""Exercise the real shell pipeline with local stand-ins for model stages."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

RUNNER = (Path(__file__).resolve().parents[1]
          / 'data_ncwang/backport_patch/cases/run_case_generic.sh')


class CaseRunnerTests(unittest.TestCase):
    def test_configured_stage_paths_with_spaces_and_generation_exit_status(self):
        self.run_pipeline()

    def test_configured_models_survive_and_postcheck_failure_is_propagated(self):
        self.run_pipeline(configured=True, generation_rc=0, chain_rc=1)

    def run_pipeline(self, configured=False, generation_rc=23, chain_rc=0):
        with tempfile.TemporaryDirectory(prefix='configured pipeline ') as directory:
            base = Path(directory)
            root, scripts, dataset, case, out, repo, bin_dir = [base / name for name in
                ('backport_patch', 'stage scripts', 'dataset', 'case', 'results', 'repo', 'bin')]
            src = root / 'tools/patch-backporting/src'
            for p in (root, scripts, dataset, case, out, repo, bin_dir, src):
                p.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
            (dataset / 'real.patch').write_text('fixture patch\n')
            config = base / 'input config.yml'
            settings = {'project_dir': str(repo), 'target_release': 'HEAD',
                'new_patch': 'fixture', 'patch_dataset_dir': str(dataset), 'openai_key': 'test-placeholder'}
            if configured:
                settings.update(llm_model='generation-fixture', coverage_model='review-fixture',
                                summary_model='summary-fixture', triage_model='triage-fixture',
                                map_model='mapping-fixture', llm_base_url='http://127.0.0.1:9/custom')
            config.write_text(yaml.safe_dump(settings))
            curl = bin_dir / 'curl'
            curl.write_text('#!/usr/bin/env sh\nprintf 200\n')
            curl.chmod(0o755)
            stub = '''from pathlib import Path
import json,os,sys,yaml
name=Path(__file__).name
with open(os.environ['STAGE_AUDIT'],'a') as log: log.write(json.dumps([name,sys.argv[1:]])+'\\n')
args=sys.argv[1:]
if name=='summarize_and_group.py': Path(args[1]).write_text('[]')
elif name=='triage.py': Path(args[3]).write_text('[]')
elif name=='v6_group.py':
 if args[0]=='--detect-only': print('parser=fixture')
 else:
  if args[0]=='--enclosing-only': args=args[1:]
  if args[0]=='--triage':
   assert Path(args[1]).is_file()
   args=args[2:]
  folder=Path(args[-1]); folder.mkdir(exist_ok=True)
  for f in ['summaries_with_enclosing.json','groups.json']: (folder/f).write_text('[]')
elif name=='map_semantics.py': Path(args[6]).write_text('{}')
elif name=='build_v6_export.py': (Path(args[4])/'merged_hunks/original').mkdir(parents=True)
elif name=='backporting.py':
 config=yaml.safe_load(Path(args[1]).read_text())
 assert Path(config['merged_hunks_dir']).is_dir()
 (Path(config['patch_dataset_dir'])/'complete.patch').write_text('partial artifact\\n')
 raise SystemExit(int(os.environ['GENERATION_FIXTURE_RC']))
elif name=='chain_check.py': raise SystemExit(int(os.environ['CHAIN_FIXTURE_RC']))
'''
            for path in [base / 'summarize_and_group.py', base / 'triage.py',
                         base / 'chain_check.py', base / 'make_verdict.py',
                         *[scripts / name for name in ('v6_group.py', 'map_semantics.py', 'build_v6_export.py')],
                         src / 'backporting.py']:
                path.write_text(stub)
            audit = base / 'audit.jsonl'
            env = {**os.environ, 'BACKPORT_ROOT': str(root), 'BACKPORT_PYTHON': sys.executable,
                   'BACKPORT_SCRIPTS': str(scripts), 'BACKPORT_DATASET': str(dataset),
                   'BACKPORT_CONFIG': str(config), 'BACKPORT_CASE': str(case), 'OUT': str(out),
                   'ENABLE_TRIAGE': '1', 'STOP_AFTER_EXPORT': '0', 'STAGE_AUDIT': str(audit),
                   'GENERATION_FIXTURE_RC': str(generation_rc), 'CHAIN_FIXTURE_RC': str(chain_rc),
                   'LLM_BASE_URL': 'http://127.0.0.1:9', 'LLM_ENDPOINT': 'http://127.0.0.1:9/models',
                   'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
            for key in ('DEEPSEEK_MODEL', 'LLM_MODEL', 'COVERAGE_MODEL', 'SUMMARY_MODEL', 'GROUP_MODEL',
                        'TRIAGE_MODEL', 'MAP_MODEL', 'DEEPSEEK_API_KEY', 'LLM_KEY', 'CHAIN_CHECK_SCRIPT'):
                env.pop(key, None)
            if configured:
                env.pop('LLM_BASE_URL')
            result = subprocess.run(['bash', str(RUNNER), 'configured-project', 'configured-case'],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, generation_rc or chain_rc, result.stdout + result.stderr)
            self.assertEqual((out / 'complete.patch').read_text(), 'partial artifact\n')
            self.assertFalse((out / 'config.used.yml').exists())
            stages = [json.loads(line)[0] for line in audit.read_text().splitlines()]
            self.assertEqual(stages.count('v6_group.py'), 3)
            calls = [json.loads(line) for line in audit.read_text().splitlines()]
            grouping_calls = [args for name, args in calls if name == 'v6_group.py']
            self.assertEqual(grouping_calls[1][0], '--enclosing-only')
            self.assertIn('map_semantics.py', stages)
            self.assertIn('backporting.py', stages)
            summary_call = next(args for name, args in calls
                                if name == 'summarize_and_group.py')
            triage_call = next(args for name, args in calls if name == 'triage.py')
            mapping_call = next(args for name, args in calls
                                if name == 'map_semantics.py')
            self.assertEqual(summary_call[2], settings.get('summary_model', 'deepseek-flash'))
            self.assertEqual(triage_call[4], settings.get('triage_model', 'deepseek-flash'))
            self.assertEqual(mapping_call[7], settings.get('map_model', 'deepseek-flash'))
            used_config = yaml.safe_load((case / 'config.yml').read_text())
            self.assertEqual(used_config['llm_model'], settings.get('llm_model', 'deepseek-flash'))
            self.assertEqual(used_config['coverage_model'], settings.get('coverage_model', 'deepseek-flash'))
            self.assertEqual(used_config['llm_base_url'], settings.get('llm_base_url', env['LLM_ENDPOINT'].removesuffix('/models')))
            self.assertFalse(used_config['use_azure'])
            self.assertNotIn('openai_key', used_config)
            chain_args = next(args for name, args in calls if name == 'chain_check.py')
            target = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            self.assertEqual(chain_args[-2:], ['--base', target])
            self.assertEqual(used_config['target_release'], target)


if __name__ == '__main__':
    unittest.main()
