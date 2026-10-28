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
            config.write_text(yaml.safe_dump({'project_dir': str(repo), 'target_release': 'HEAD',
                'new_patch': 'fixture', 'patch_dataset_dir': str(dataset), 'openai_key': 'test-placeholder'}))
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
 raise SystemExit(23)
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
                   'LLM_BASE_URL': 'http://127.0.0.1:9', 'LLM_ENDPOINT': 'http://127.0.0.1:9/models',
                   'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
            result = subprocess.run(['bash', str(RUNNER), 'configured-project', 'configured-case'],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
            self.assertEqual((out / 'complete.patch').read_text(), 'partial artifact\n')
            self.assertFalse((out / 'config.used.yml').exists())
            stages = [json.loads(line)[0] for line in audit.read_text().splitlines()]
            self.assertEqual(stages.count('v6_group.py'), 3)
            calls = [json.loads(line) for line in audit.read_text().splitlines()]
            grouping_calls = [args for name, args in calls if name == 'v6_group.py']
            self.assertEqual(grouping_calls[1][0], '--enclosing-only')
            self.assertIn('map_semantics.py', stages)
            self.assertIn('backporting.py', stages)


if __name__ == '__main__':
    unittest.main()
