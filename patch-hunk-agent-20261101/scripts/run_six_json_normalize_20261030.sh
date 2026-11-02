#!/usr/bin/env bash
set -uo pipefail

CODE_ROOT=/data/ncwang/backport_patch/patch-hunk-agent-json-normalize-20261030/data_ncwang/backport_patch
RUNNER="$CODE_ROOT/cases/run_case_generic.sh"
SOURCE_ROOT=/data/ncwang/backport_patch
BATCH="$SOURCE_ROOT/json-normalize-six-20261030"
PY=/data/ncwang/backport_patch/tools/patch-backporting/.venv/bin/python
PYDEPS=/data/ncwang/backport_patch/deepseek-runs/CVE-2025-48649-20261029-2058/pydeps

test ! -e "$BATCH"
mkdir -p "$BATCH"/{cases,datasets,results,worktrees,logs}
exec 9>"$BATCH/batch.lock"
flock -n 9 || { echo "batch already running" >&2; exit 1; }

export PYTHONPATH="$PYDEPS${PYTHONPATH:+:$PYTHONPATH}"
export LLM_BASE_URL=https://api.deepseek.com/v1
export DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
export DEEPSEEK_MODEL=deepseek-flash
export SUMMARY_MODEL=deepseek-flash
export GROUP_MODEL=deepseek-flash
export TRIAGE_MODEL=deepseek-flash
export MAP_MODEL=deepseek-flash
export ENABLE_TRIAGE=1
export GENERATION_TIMEOUT=10800
export BACKPORT_PYTHON="$PY"
export BACKPORT_ROOT="$CODE_ROOT"

CASES=(
  CVE-2025-32348
  CVE-2025-48649
  CVE-2025-48533
  CVE-2025-48550
  CVE-2025-48595
  CVE-2026-0045
)

dataset_source() {
  case "$1" in
    CVE-2025-32348|CVE-2025-48649|CVE-2025-48533|CVE-2025-48550)
      echo "$SOURCE_ROOT/dataset/aosp-frameworks-base/$1" ;;
    CVE-2025-48595)
      echo "$SOURCE_ROOT/dataset/aosp-external-sqlite/$1" ;;
    CVE-2026-0045)
      echo "$SOURCE_ROOT/dataset/aosp-system-bt/$1" ;;
    *) return 1 ;;
  esac
}

config_value() {
  "$PY" -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

record() {
  printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$BATCH/batch.log"
}

record "BATCH_START pid=$$"
overall=0
for cve in "${CASES[@]}"; do
  src=$(dataset_source "$cve") || { record "SETUP_FAIL $cve bad_dataset"; overall=1; continue; }
  src_cfg="$src/config.yml"
  repo=$(config_value "$src_cfg" project_dir)
  target=$(config_value "$src_cfg" target_release)
  dataset="$BATCH/datasets/$cve"
  worktree="$BATCH/worktrees/$cve"
  case_dir="$BATCH/cases/$cve"
  out="$BATCH/results/$cve"
  case_log="$BATCH/logs/$cve.batch.log"

  record "CASE_SETUP_START $cve repo=$repo target=$target"
  test -f "$src/real.patch" || { record "SETUP_FAIL $cve missing_real.patch"; overall=1; continue; }
  git -C "$repo" cat-file -e "$target^{commit}" || { record "SETUP_FAIL $cve missing_target"; overall=1; continue; }

  if ! git -C "$repo" worktree add --detach "$worktree" "$target" >"$BATCH/logs/$cve.worktree.log" 2>&1; then
    record "SETUP_FAIL $cve worktree"
    overall=1
    continue
  fi

  mkdir -p "$dataset" "$case_dir" "$out"
  cp -a "$src/." "$dataset/"
  case "$dataset" in
    "$BATCH"/datasets/*) ;;
    *) record "REFUSE_UNSAFE_DATASET $dataset"; exit 1 ;;
  esac
  find "$dataset" -maxdepth 1 -type f -name 'complete.patch*' -delete

  "$PY" - "$src_cfg" "$dataset/config.yml" "$worktree" "$dataset/" <<'PY'
import sys, yaml
src, dst, worktree, dataset_dir = sys.argv[1:]
data = yaml.safe_load(open(src, encoding='utf-8'))
for key in ('openai_key', 'api_key', 'deepseek_api_key', 'azure_endpoint',
            'azure_deployment', 'azure_api_version'):
    data.pop(key, None)
data.update({
    'project_dir': worktree,
    'patch_dataset_dir': dataset_dir,
    'llm_base_url': 'https://api.deepseek.com/v1',
    'llm_model': 'deepseek-flash',
    'coverage_model': 'deepseek-flash',
    'use_azure': False,
})
with open(dst, 'w', encoding='utf-8') as fh:
    yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
PY

  record "CASE_START $cve"
  (
    export BACKPORT_DATASET="$dataset"
    export BACKPORT_CONFIG="$dataset/config.yml"
    export BACKPORT_CASE="$case_dir"
    export OUT="$out"
    exec bash "$RUNNER" "$(config_value "$dataset/config.yml" project)" "$cve"
  ) >"$case_log" 2>&1
  rc=$?
  record "CASE_END $cve rc=$rc"
  if [ "$rc" -ne 0 ]; then overall=1; fi
done

"$PY" - "$BATCH" <<'PY'
import json, os, sys, yaml
root = sys.argv[1]
rows = []
for cve in sorted(os.listdir(os.path.join(root, 'results'))):
    out = os.path.join(root, 'results', cve)
    status = os.path.join(out, 'status.txt')
    verdict = os.path.join(out, 'verdict.md')
    patch = os.path.join(out, 'complete.patch')
    cfg = os.path.join(root, 'datasets', cve, 'config.yml')
    target = None
    if os.path.exists(cfg):
        target = yaml.safe_load(open(cfg, encoding='utf-8')).get('target_release')
    rows.append({
        'cve': cve,
        'target_release': target,
        'status': open(status, encoding='utf-8').read().strip() if os.path.exists(status) else '',
        'complete_patch': patch if os.path.exists(patch) else None,
        'complete_patch_bytes': os.path.getsize(patch) if os.path.exists(patch) else 0,
        'verdict': open(verdict, encoding='utf-8').read().strip() if os.path.exists(verdict) else '',
    })
with open(os.path.join(root, 'batch-summary.json'), 'w', encoding='utf-8') as fh:
    json.dump(rows, fh, ensure_ascii=False, indent=2)
with open(os.path.join(root, 'batch-summary.md'), 'w', encoding='utf-8') as fh:
    fh.write('# Six-CVE JSON-normalization regression\n\n')
    fh.write('| CVE | Patch bytes | Verdict |\n|---|---:|---|\n')
    for row in rows:
        first = next((line for line in row['verdict'].splitlines() if line.startswith('# case')), '')
        fh.write('| %s | %d | %s |\n' % (row['cve'], row['complete_patch_bytes'], first.replace('# case 判定：', '')))
PY

record "BATCH_END overall_rc=$overall"
exit "$overall"
