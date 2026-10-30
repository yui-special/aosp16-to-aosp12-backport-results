#!/usr/bin/env bash
# Resume only the generation/post-check phase after the shared branch-name bug.
set -uo pipefail

CVE=${1:?usage: resume_six_case_generation_20261030.sh <CVE> [project-name]}
PROJ=${2:-aosp-frameworks-base}
KEY_SOURCE_PID=${KEY_SOURCE_PID:-598479}

AGENT_ROOT=/data/ncwang/backport_patch/patch-hunk-agent-json-normalize-20261030/data_ncwang
ROOT=$AGENT_ROOT/backport_patch
BATCH_ROOT=/data/ncwang/backport_patch/json-normalize-six-20261030
CASE=$BATCH_ROOT/cases/$CVE
DS=$BATCH_ROOT/datasets/$CVE
OUT=$BATCH_ROOT/results/$CVE
CFG=$CASE/config.yml
PY=/data/ncwang/backport_patch/tools/patch-backporting/.venv/bin/python
SRC=$ROOT/tools/patch-backporting/src
export PYTHONPATH=/data/ncwang/backport_patch/deepseek-runs/CVE-2025-48649-20261029-2058/pydeps${PYTHONPATH:+:$PYTHONPATH}

LLM_KEY=$(tr '\0' '\n' < "/proc/$KEY_SOURCE_PID/environ" \
  | sed -n 's/^LLM_KEY=//p' | tr -d '\r')
if [ -z "$LLM_KEY" ]; then
  echo "NO_KEY from pid $KEY_SOURCE_PID" >&2
  exit 1
fi

export LLM_KEY
export LLM_BASE_URL=https://api.deepseek.com/v1
export DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
export DEEPSEEK_MODEL=deepseek-flash
export COVERAGE_MODEL=deepseek-flash
export CHAIN_CHECK_SCRIPT=$AGENT_ROOT/chain_check.py

mkdir -p "$OUT"
cp -f "$OUT/status.txt" "$OUT/status.before-branch-fix.txt" 2>/dev/null || true
cp -f "$OUT/run.log" "$OUT/run.before-branch-fix.log" 2>/dev/null || true
cp -f "$OUT/verdict.md" "$OUT/verdict.before-branch-fix.md" 2>/dev/null || true
echo "RETRY branch-fix $(date -Is)" >> "$OUT/status.txt"

REPO=$($PY -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["project_dir"])' "$CFG")
TARGET=$($PY -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["target_release"])' "$CFG")

git -C "$REPO" reset --hard >/dev/null 2>&1
cd "$SRC" || exit 1
timeout --foreground "${GENERATION_TIMEOUT:-10800}" "$PY" backporting.py \
  -c "$CFG" --cumulative --allow-incomplete-groups --debug \
  > "$OUT/run.retry-branch-fix.log" 2>&1
rc=$?
cp -f "$OUT/run.retry-branch-fix.log" "$OUT/run.log" 2>/dev/null || true
echo "RETRY_EXIT $rc $(date -Is)" >> "$OUT/status.txt"

cp -f "$DS/complete.patch" "$OUT/complete.patch" 2>/dev/null || true
cp -a "$CASE/inputs/merged_hunks/ported/." "$OUT/" 2>/dev/null || true

if [ -f "$OUT/complete.patch" ]; then
  CHAIN_CHECK_BASE_REF="$TARGET" \
    "$PY" "$AGENT_ROOT/chain_check.py" "$REPO" "$OUT/complete.patch" \
    > "$OUT/chain-report.txt" 2>&1
  echo "RETRY_CHAIN rc=$?" >> "$OUT/status.txt"
fi

"$PY" "$AGENT_ROOT/make_verdict.py" "$CASE" "$OUT" \
  >> "$OUT/status.txt" 2>&1 || true
echo "RETRY_DONE rc=$rc $(date -Is)" >> "$OUT/status.txt"
exit "$rc"
