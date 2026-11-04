#!/usr/bin/env bash
# 通用 case 跑批（仓库/目标/补丁都从 config.yml 读）
# 用法: run_case_generic.sh <dataset目录名> <CVE tag>
#   例: run_case_generic.sh aosp-external-sqlite CVE-2025-48595
set -uo pipefail

PROJ=${1:?usage: run_case_generic.sh <project-dir-name> <CVE>}
CVE=${2:?usage: run_case_generic.sh <project-dir-name> <CVE>}
ROOT=${BACKPORT_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}
SRC=$ROOT/tools/patch-backporting/src
PY=${BACKPORT_PYTHON:-$ROOT/tools/patch-backporting/.venv/bin/python}
DS=${BACKPORT_DATASET:-$ROOT/dataset/$PROJ/$CVE}
INPUT_CONFIG=${BACKPORT_CONFIG:-$DS/config.yml}
CASE=${BACKPORT_CASE:-$ROOT/cases/$CVE}
SCRIPTS=${BACKPORT_SCRIPTS:-$ROOT/cases/CVE-2025-48649/scripts}
CFG=$CASE/config.yml
OUT=${OUT:-$CASE/runs/official}
export CHAIN_CHECK_SCRIPT=${CHAIN_CHECK_SCRIPT:-$(dirname "$ROOT")/chain_check.py}

config_value() {
  "$PY" -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))[sys.argv[2]])' "$INPUT_CONFIG" "$1"
}
REPO=$(config_value project_dir) || exit 1
TARGET=$(config_value target_release) || exit 1
NEW_PATCH=$(config_value new_patch) || exit 1

mkdir -p "$CASE/groups" "$CASE/inputs" "$OUT"
echo "START $CVE $(date -Is)" > "$OUT/status.txt"
echo "repo=$REPO target=$TARGET" >> "$OUT/status.txt"

fail_stage() {
  local stage=$1 rc=$2
  echo "INFRA_FAILURE stage=$stage rc=$rc $(date -Is)" >> "$OUT/status.txt"
  exit "$rc"
}

# Stage 0：统一使用 DeepSeek 的 OpenAI-compatible API。
export DEEPSEEK_BASE_URL=${DEEPSEEK_BASE_URL:-https://api.deepseek.com/v1}
export LLM_BASE_URL=${LLM_BASE_URL:-$DEEPSEEK_BASE_URL}
LLM_KEY=${DEEPSEEK_API_KEY:-${LLM_KEY:-}}
export LLM_KEY
code=$(timeout 12 curl -s --tlsv1.2 --tls-max 1.2 -o /dev/null -w "%{http_code}" \
  "${LLM_ENDPOINT:-$LLM_BASE_URL/models}" 2>/dev/null)
if [ "$code" != "200" ] && [ "$code" != "401" ]; then
  echo "INFRA_FAILURE endpoint=${code:-DOWN} $(date -Is)" >> "$OUT/status.txt"
  exit 2
fi
echo "preflight endpoint=$code" >> "$OUT/status.txt"

export DEEPSEEK_MODEL=${DEEPSEEK_MODEL:-deepseek-flash}
export SUMMARY_MODEL=${SUMMARY_MODEL:-$DEEPSEEK_MODEL}
export GROUP_MODEL=${GROUP_MODEL:-$SUMMARY_MODEL}
export TRIAGE_MODEL=${TRIAGE_MODEL:-$DEEPSEEK_MODEL}
export MAP_MODEL=${MAP_MODEL:-$DEEPSEEK_MODEL}
if [ -z "${LLM_KEY:-}" ]; then echo "NO_KEY" >> "$OUT/status.txt"; exit 1; fi

echo "[0/8] detect languages" | tee -a "$OUT/status.txt"
"$PY" "$SCRIPTS/v6_group.py" --detect-only "$DS/real.patch" > "$CASE/groups/languages.txt" 2>&1
rc=$?
echo "detect rc=$rc -> $(grep -c 'parser=' "$CASE/groups/languages.txt") 种类型" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage detect "$rc"

echo "[1/8] classify hunks" | tee -a "$OUT/status.txt"
CLASS_FILE="$CASE/groups/classified.json"
"$PY" "$(dirname "$ROOT")/hunk_classifier.py" "$DS/real.patch" "$CLASS_FILE" \
  > "$CASE/groups/classification.log" 2>&1
rc=$?
echo "classification rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage classification "$rc"
export HUNK_CLASSIFICATION_FILE="$CLASS_FILE"

echo "[2/8] summarize + semantic contract" | tee -a "$OUT/status.txt"
"$PY" "$(dirname "$ROOT")/summarize_and_group.py" "$DS/real.patch" "$CASE/groups/summaries.json" \
  "$SUMMARY_MODEL" > "$CASE/groups/summarize.log" 2>&1
rc=$?
echo "summary rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage summary "$rc"

TRI_ARG=()
TRI_FILE="-"
HUNK_MAPPING="$CASE/groups/hunk-mapping.json"
SEMANTIC_MAPPING="$CASE/groups/semantic-mapping.json"
# Exact enclosing is required for mapping even when triage is disabled.
echo "[2.5/8] enclosing for mapping" | tee -a "$OUT/status.txt"
"$PY" "$SCRIPTS/v6_group.py" --enclosing-only "$REPO" "$NEW_PATCH" "$TARGET" "$DS/real.patch" \
  "$CASE/groups/summaries.json" "$CASE/groups" > "$CASE/groups/group0.log" 2>&1
rc=$?
echo "mapping-input enclosing rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage mapping_input_enclosing "$rc"

echo "[3/8] map hunks once" | tee -a "$OUT/status.txt"
"$PY" "$SCRIPTS/map_semantics.py" --hunks "$REPO" "$TARGET" "$DS/real.patch" \
  "$CASE/groups/summaries_with_enclosing.json" "$HUNK_MAPPING" "$MAP_MODEL" \
  > "$CASE/groups/map-semantics.log" 2>&1
rc=$?
echo "hunk-mapping rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage hunk_mapping "$rc"
export HUNK_MAPPING_FILE="$HUNK_MAPPING"

if [ "${ENABLE_TRIAGE:-0}" = "1" ]; then
  TRI_FILE="$CASE/groups/triaged.json"
  echo "[4/8] triage with mapping evidence" | tee -a "$OUT/status.txt"
  "$PY" "$(dirname "$ROOT")/triage.py" "$DS/real.patch" "$CASE/groups/summaries_with_enclosing.json" \
    "${CVE_DESC:-只保留该 CVE 的修复语义，版本/元数据/无关上游改动不移植}" \
    "$TRI_FILE" "$TRIAGE_MODEL" "$REPO" "$TARGET" \
    > "$CASE/groups/triage.log" 2>&1
  trc=$?
  echo "triage rc=$trc" >> "$OUT/status.txt"
  [ "$trc" = 0 ] || fail_stage triage "$trc"
  TRI_ARG=(--triage "$TRI_FILE")
fi

echo "[5/8] group" | tee -a "$OUT/status.txt"
"$PY" "$SCRIPTS/v6_group.py" "${TRI_ARG[@]}" "$REPO" "$NEW_PATCH" "$TARGET" "$DS/real.patch" \
  "$CASE/groups/summaries.json" "$CASE/groups" > "$CASE/groups/group.log" 2>&1
rc=$?
echo "group rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage grouping "$rc"

echo "[6/8] project mapping onto final groups" | tee -a "$OUT/status.txt"
"$PY" "$SCRIPTS/map_semantics.py" --project "$REPO" "$TARGET" "$DS/real.patch" \
  "$CASE/groups/summaries_with_enclosing.json" "$CASE/groups/groups.json" "$TRI_FILE" \
  "$HUNK_MAPPING" "$SEMANTIC_MAPPING" \
  >> "$CASE/groups/map-semantics.log" 2>&1
rc=$?
echo "semantic-mapping rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage semantic_mapping "$rc"

echo "[7/8] export" | tee -a "$OUT/status.txt"
rm -rf "$CASE/inputs/merged_hunks"
"$PY" "$SCRIPTS/build_v6_export.py" "$DS/real.patch" "$REPO" "$TARGET" \
  "$CASE/groups/groups.json" "$CASE/inputs" "$SEMANTIC_MAPPING" \
  > "$CASE/inputs/export.log" 2>&1
rc=$?
echo "export rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage export "$rc"

if [ "${STOP_AFTER_EXPORT:-0}" = "1" ]; then
  echo "READY_AFTER_EXPORT $(date -Is)" >> "$OUT/status.txt"
  exit 0
fi

cp -f "$INPUT_CONFIG" "$CFG" || fail_stage config 1
grep -vE '^(openai_key|api_key|deepseek_api_key|llm_base_url|llm_model|coverage_model|use_azure|azure_endpoint|azure_deployment|azure_api_version|merged_hunks_dir)[[:space:]]*:' \
  "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
{
  echo "llm_base_url: $LLM_BASE_URL"
  echo "llm_model: $DEEPSEEK_MODEL"
  echo "coverage_model: $DEEPSEEK_MODEL"
  echo "use_azure: false"
  echo "merged_hunks_dir: $CASE/inputs/merged_hunks/original"
  echo "semantic_mapping_path: $CASE/groups/semantic-mapping.json"
  echo "capability_resolutions_path: $CASE/inputs/merged_hunks/ported/config-capability-resolutions.json"
} >> "$CFG"

echo "[8/8] generate" | tee -a "$OUT/status.txt"
git -C "$REPO" reset --hard >/dev/null 2>&1
cd "$SRC" || exit 1
timeout --foreground "${GENERATION_TIMEOUT:-10800}" "$PY" backporting.py -c "$CFG" --cumulative --allow-incomplete-groups --debug \
  > "$OUT/run.log" 2>&1
rc=$?
echo "EXIT $rc $(date -Is)" >> "$OUT/status.txt"

if [ "$rc" -eq 0 ] && [ -s "$DS/complete.patch" ]; then
  cp -f "$DS/complete.patch" "$OUT/complete.patch"
else
  rm -f "$OUT/complete.patch"
  if [ -s "$DS/complete.patch.pending" ]; then
    cp -f "$DS/complete.patch.pending" "$OUT/complete.patch.pending"
  fi
  echo "NO_FINAL_ARTIFACT generation_rc=$rc" >> "$OUT/status.txt"
fi
cp -a "$CASE/inputs/merged_hunks/ported/." "$OUT/" 2>/dev/null || true

# API 链完整性检查（改签名后是否还有调用点用旧参数个数）
if [ -f "$OUT/complete.patch" ]; then
  CHAIN_CHECK_BASE_REF="$TARGET" \
    "$PY" "$(dirname "$ROOT")/chain_check.py" "$REPO" "$OUT/complete.patch" \
    > "$OUT/chain-report.txt" 2>&1
  echo "chain rc=$?" >> "$OUT/status.txt"
fi

"$PY" "$(dirname "$ROOT")/make_verdict.py" "$CASE" "$OUT" >> "$OUT/status.txt" 2>&1 || true

echo "DONE rc=$rc $(date -Is)" >> "$OUT/status.txt"
exit "$rc"
