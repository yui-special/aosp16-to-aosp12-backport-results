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
  "$PY" -c 'import sys,yaml; d=yaml.safe_load(open(sys.argv[1])); print(next((d[k] for k in sys.argv[2:] if d.get(k)), ""))' "$INPUT_CONFIG" "$@"
}
REPO=$(config_value project_dir) || exit 1
TARGET=$(config_value target_release) || exit 1
TARGET=$(git -C "$REPO" rev-parse --verify "$TARGET^{commit}") || exit 1
NEW_PATCH=$(config_value new_patch) || exit 1

mkdir -p "$CASE/groups" "$CASE/inputs" "$OUT"
echo "START $CVE $(date -Is)" > "$OUT/status.txt"
echo "repo=$REPO target=$TARGET" >> "$OUT/status.txt"

fail_stage() {
  local stage=$1 rc=$2
  echo "INFRA_FAILURE stage=$stage rc=$rc $(date -Is)" >> "$OUT/status.txt"
  exit "$rc"
}

# Stage 0：保留配置与阶段覆盖，未配置时使用 DeepSeek。
export DEEPSEEK_BASE_URL=${DEEPSEEK_BASE_URL:-https://api.deepseek.com/v1}
export LLM_BASE_URL=${LLM_BASE_URL:-$(config_value llm_base_url)}
export LLM_BASE_URL=${LLM_BASE_URL:-$DEEPSEEK_BASE_URL}
LLM_KEY=${DEEPSEEK_API_KEY:-${LLM_KEY:-$(config_value deepseek_api_key api_key openai_key)}}
export LLM_KEY
code=$(timeout 12 curl -s --tlsv1.2 --tls-max 1.2 -o /dev/null -w "%{http_code}" \
  "${LLM_ENDPOINT:-$LLM_BASE_URL/models}" 2>/dev/null)
if [ "$code" != "200" ] && [ "$code" != "401" ]; then
  echo "INFRA_FAILURE endpoint=${code:-DOWN} $(date -Is)" >> "$OUT/status.txt"
  exit 2
fi
echo "preflight endpoint=$code" >> "$OUT/status.txt"

export DEEPSEEK_MODEL=${DEEPSEEK_MODEL:-deepseek-flash}
export LLM_MODEL=${LLM_MODEL:-$(config_value llm_model)}
export LLM_MODEL=${LLM_MODEL:-$DEEPSEEK_MODEL}
export COVERAGE_MODEL=${COVERAGE_MODEL:-$(config_value coverage_model)}
export COVERAGE_MODEL=${COVERAGE_MODEL:-$LLM_MODEL}
export SUMMARY_MODEL=${SUMMARY_MODEL:-$(config_value summary_model)}
export SUMMARY_MODEL=${SUMMARY_MODEL:-$LLM_MODEL}
export GROUP_MODEL=${GROUP_MODEL:-$(config_value group_model)}
export GROUP_MODEL=${GROUP_MODEL:-$SUMMARY_MODEL}
export TRIAGE_MODEL=${TRIAGE_MODEL:-$(config_value triage_model)}
export TRIAGE_MODEL=${TRIAGE_MODEL:-$LLM_MODEL}
export MAP_MODEL=${MAP_MODEL:-$(config_value map_model)}
export MAP_MODEL=${MAP_MODEL:-$LLM_MODEL}
if [ -z "${LLM_KEY:-}" ]; then echo "NO_KEY" >> "$OUT/status.txt"; exit 1; fi

echo "[0/6] detect languages" | tee -a "$OUT/status.txt"
"$PY" "$SCRIPTS/v6_group.py" --detect-only "$DS/real.patch" > "$CASE/groups/languages.txt" 2>&1
rc=$?
echo "detect rc=$rc -> $(grep -c 'parser=' "$CASE/groups/languages.txt") 种类型" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage detect "$rc"

echo "[1/6] summarize + semantic contract" | tee -a "$OUT/status.txt"
"$PY" "$(dirname "$ROOT")/summarize_and_group.py" "$DS/real.patch" "$CASE/groups/summaries.json" \
  "$SUMMARY_MODEL" > "$CASE/groups/summarize.log" 2>&1
rc=$?
echo "summary rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage summary "$rc"

TRI_ARG=()
TRI_FILE="-"
if [ "${ENABLE_TRIAGE:-0}" = "1" ]; then
  # 只提取精确 enclosing（不调分组模型），再分诊，最后做语义分组
  echo "[1.5/6] enclosing for triage" | tee -a "$OUT/status.txt"
  "$PY" "$SCRIPTS/v6_group.py" --enclosing-only "$REPO" "$NEW_PATCH" "$TARGET" "$DS/real.patch" \
    "$CASE/groups/summaries.json" "$CASE/groups" > "$CASE/groups/group0.log" 2>&1
  rc=$?
  echo "pre-triage group rc=$rc" >> "$OUT/status.txt"
  [ "$rc" = 0 ] || fail_stage pre_triage_group "$rc"
  TRI_FILE="$CASE/groups/triaged.json"
  echo "[2/6] triage" | tee -a "$OUT/status.txt"
  "$PY" "$(dirname "$ROOT")/triage.py" "$DS/real.patch" "$CASE/groups/summaries_with_enclosing.json" \
    "${CVE_DESC:-只保留该 CVE 的修复语义，版本/元数据/无关上游改动不移植}" \
    "$TRI_FILE" "$TRIAGE_MODEL" "$REPO" "$TARGET" \
    > "$CASE/groups/triage.log" 2>&1
  trc=$?
  echo "triage rc=$trc" >> "$OUT/status.txt"
  [ "$trc" = 0 ] || fail_stage triage "$trc"
  TRI_ARG=(--triage "$TRI_FILE")
fi

echo "[3/6] group" | tee -a "$OUT/status.txt"
"$PY" "$SCRIPTS/v6_group.py" "${TRI_ARG[@]}" "$REPO" "$NEW_PATCH" "$TARGET" "$DS/real.patch" \
  "$CASE/groups/summaries.json" "$CASE/groups" > "$CASE/groups/group.log" 2>&1
rc=$?
echo "group rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage grouping "$rc"

echo "[4/6] map" | tee -a "$OUT/status.txt"
"$PY" "$SCRIPTS/map_semantics.py" "$REPO" "$TARGET" "$DS/real.patch" \
  "$CASE/groups/summaries_with_enclosing.json" "$CASE/groups/groups.json" "$TRI_FILE" \
  "$CASE/groups/semantic-mapping.json" "$MAP_MODEL" \
  > "$CASE/groups/map-semantics.log" 2>&1
rc=$?
echo "semantic-mapping rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage semantic_mapping "$rc"

echo "[5/6] export" | tee -a "$OUT/status.txt"
rm -rf "$CASE/inputs/merged_hunks"
"$PY" "$SCRIPTS/build_v6_export.py" "$DS/real.patch" "$REPO" "$TARGET" \
  "$CASE/groups/groups.json" "$CASE/inputs" "$CASE/groups/semantic-mapping.json" \
  > "$CASE/inputs/export.log" 2>&1
rc=$?
echo "export rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage export "$rc"

if [ "${STOP_AFTER_EXPORT:-0}" = "1" ]; then
  echo "READY_AFTER_EXPORT $(date -Is)" >> "$OUT/status.txt"
  exit 0
fi

"$PY" - "$INPUT_CONFIG" "$CFG" "$CASE/inputs/merged_hunks/original" "$TARGET" <<'PY'
import os, sys, yaml
source, destination, merged_hunks, target = sys.argv[1:]
with open(source, encoding='utf-8') as handle:
    data = yaml.safe_load(handle)
for key in ('openai_key', 'api_key', 'deepseek_api_key', 'azure_endpoint',
            'azure_deployment', 'azure_api_version'):
    data.pop(key, None)
data.update(llm_base_url=os.environ['LLM_BASE_URL'], llm_model=os.environ['LLM_MODEL'],
            coverage_model=os.environ['COVERAGE_MODEL'], use_azure=False,
            merged_hunks_dir=merged_hunks, target_release=target)
with open(destination, 'w', encoding='utf-8') as handle:
    yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
PY
[ "$?" = 0 ] || fail_stage config 1

echo "[6/6] generate" | tee -a "$OUT/status.txt"
git -C "$REPO" reset --hard >/dev/null 2>&1
cd "$SRC" || exit 1
timeout --foreground "${GENERATION_TIMEOUT:-10800}" "$PY" backporting.py -c "$CFG" --cumulative --allow-incomplete-groups --debug \
  > "$OUT/run.log" 2>&1
rc=$?
echo "EXIT $rc $(date -Is)" >> "$OUT/status.txt"

cp -f "$DS/complete.patch" "$OUT/complete.patch" 2>/dev/null
cp -a "$CASE/inputs/merged_hunks/ported/." "$OUT/" 2>/dev/null || true

# API 链完整性检查（改签名后是否还有调用点用旧参数个数）
if [ -f "$OUT/complete.patch" ]; then
  "$PY" "$CHAIN_CHECK_SCRIPT" "$REPO" "$OUT/complete.patch" --base "$TARGET" \
    > "$OUT/chain-report.txt" 2>&1
  chain_rc=$?
  echo "chain rc=$chain_rc" >> "$OUT/status.txt"
  if [ "$rc" = 0 ] && [ "$chain_rc" != 0 ]; then rc=$chain_rc; fi
fi

"$PY" "$(dirname "$ROOT")/make_verdict.py" "$CASE" "$OUT" >> "$OUT/status.txt" 2>&1 || true

echo "DONE rc=$rc $(date -Is)" >> "$OUT/status.txt"
exit "$rc"
