#!/usr/bin/env bash
# 通用 case 跑批（仓库/目标/补丁都从 config.yml 读）
# 用法: run_case_generic.sh <dataset目录名> <CVE tag>
#   例: run_case_generic.sh aosp-external-sqlite CVE-2025-48595
set -uo pipefail

PROJ=${1:?usage: run_case_generic.sh <project-dir-name> <CVE>}
CVE=${2:?usage: run_case_generic.sh <project-dir-name> <CVE>}
ROOT=/data/ncwang/backport_patch
SRC=$ROOT/tools/patch-backporting/src
PY=$ROOT/tools/patch-backporting/.venv/bin/python
DS=$ROOT/dataset/$PROJ/$CVE
CASE=$ROOT/cases/$CVE
SCRIPTS=$ROOT/cases/CVE-2025-48649/scripts
CFG=$CASE/config.yml
OUT=${OUT:-$CASE/runs/official}

REPO=$(grep '^project_dir:' "$DS/config.yml" | awk '{print $2}')
TARGET=$(grep '^target_release:' "$DS/config.yml" | awk '{print $2}')
NEW_PATCH=$(grep '^new_patch:' "$DS/config.yml" | awk '{print $2}')

mkdir -p "$CASE/groups" "$CASE/inputs" "$OUT"
echo "START $CVE $(date -Is)" > "$OUT/status.txt"
echo "repo=$REPO target=$TARGET" >> "$OUT/status.txt"

fail_stage() {
  local stage=$1 rc=$2
  echo "INFRA_FAILURE stage=$stage rc=$rc $(date -Is)" >> "$OUT/status.txt"
  exit "$rc"
}

# Stage 0：端点预检。端点挂了就直接判 INFRA_FAILURE，
# 绝不能让"调用失败"被后续阶段当成"无需移植"。
export LLM_BASE_URL=${LLM_BASE_URL:-https://endpoint.greatrouter.com/v1}
code=$(timeout 12 curl -s --tlsv1.2 --tls-max 1.2 -o /dev/null -w "%{http_code}" \
  "${LLM_ENDPOINT:-$LLM_BASE_URL/models}" 2>/dev/null)
if [ "$code" != "200" ] && [ "$code" != "401" ]; then
  echo "INFRA_FAILURE endpoint=${code:-DOWN} $(date -Is)" >> "$OUT/status.txt"
  exit 2
fi
echo "preflight endpoint=$code" >> "$OUT/status.txt"

export LLM_KEY=$(grep '^openai_key:' "$DS/config.yml" | sed 's/^openai_key: *//' | tr -d '"'"'"' ')
if [ -z "${LLM_KEY:-}" ]; then echo "NO_KEY" >> "$OUT/status.txt"; exit 1; fi

echo "[0/6] detect languages"
$PY "$SCRIPTS/v6_group.py" --detect-only "$DS/real.patch" > "$CASE/groups/languages.txt" 2>&1
rc=$?
echo "detect rc=$rc -> $(grep -c 'ctags kinds' "$CASE/groups/languages.txt") 种类型" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage detect "$rc"

echo "[1/6] summarize + semantic contract"
$PY /data/ncwang/summarize_and_group.py "$DS/real.patch" "$CASE/groups/summaries.json" \
  "${SUMMARY_MODEL:-DeepSeek-V4-Pro}" > "$CASE/groups/summarize.log" 2>&1
rc=$?
echo "summary rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage summary "$rc"

TRI_ARG=""
TRI_FILE="-"
if [ "${ENABLE_TRIAGE:-0}" = "1" ]; then
  # 先跑一次分组只为拿到精确 enclosing（不调模型），再分诊，最后带 --triage 正式分组
  echo "[1.5/6] enclosing for triage"
  $PY "$SCRIPTS/v6_group.py" "$REPO" "$NEW_PATCH" "$TARGET" "$DS/real.patch" \
    "$CASE/groups/summaries.json" "$CASE/groups" > "$CASE/groups/group0.log" 2>&1
  rc=$?
  echo "pre-triage group rc=$rc" >> "$OUT/status.txt"
  [ "$rc" = 0 ] || fail_stage pre_triage_group "$rc"
  TRI_FILE="$CASE/groups/triaged.json"
  echo "[2/6] triage"
  $PY /data/ncwang/triage.py "$DS/real.patch" "$CASE/groups/summaries_with_enclosing.json" \
    "${CVE_DESC:-只保留该 CVE 的修复语义，版本/元数据/无关上游改动不移植}" \
    "$TRI_FILE" "${TRIAGE_MODEL:-DeepSeek-V4-Pro}" "$REPO" "$TARGET" \
    > "$CASE/groups/triage.log" 2>&1
  trc=$?
  echo "triage rc=$trc" >> "$OUT/status.txt"
  [ "$trc" = 0 ] || fail_stage triage "$trc"
  TRI_ARG="--triage $TRI_FILE"
fi

echo "[3/6] dependency grouping"
$PY "$SCRIPTS/v6_group.py" $TRI_ARG "$REPO" "$NEW_PATCH" "$TARGET" "$DS/real.patch" \
  "$CASE/groups/summaries.json" "$CASE/groups" > "$CASE/groups/group.log" 2>&1
rc=$?
echo "group rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage grouping "$rc"

echo "[4/6] semantic target mapping"
$PY "$SCRIPTS/map_semantics.py" "$REPO" "$TARGET" "$DS/real.patch" \
  "$CASE/groups/summaries_with_enclosing.json" "$CASE/groups/groups.json" "$TRI_FILE" \
  "$CASE/groups/semantic-mapping.json" "${MAP_MODEL:-DeepSeek-V4-Pro}" \
  > "$CASE/groups/map-semantics.log" 2>&1
rc=$?
echo "semantic-mapping rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage semantic_mapping "$rc"

echo "[5/6] export"
rm -rf "$CASE/inputs/merged_hunks"
$PY "$SCRIPTS/build_v6_export.py" "$DS/real.patch" "$REPO" "$TARGET" \
  "$CASE/groups/groups.json" "$CASE/inputs" "$CASE/groups/semantic-mapping.json" \
  > "$CASE/inputs/export.log" 2>&1
rc=$?
echo "export rc=$rc" >> "$OUT/status.txt"
[ "$rc" = 0 ] || fail_stage export "$rc"

if [ "${STOP_AFTER_EXPORT:-0}" = "1" ]; then
  echo "READY_AFTER_EXPORT $(date -Is)" >> "$OUT/status.txt"
  exit 0
fi

cp -f "$DS/config.yml" "$CFG"
grep -v '^merged_hunks_dir' "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
echo "merged_hunks_dir: $CASE/inputs/merged_hunks/original" >> "$CFG"

echo "[6/6] backport"
git -C "$REPO" reset --hard >/dev/null 2>&1
cd "$SRC" || exit 1
PATH=/data/ncwang/tools/ctags/bin:$PATH \
  timeout 10800 "$PY" backporting.py -c "$CFG" --cumulative --allow-incomplete-groups --debug \
  > "$OUT/run.log" 2>&1
rc=$?
echo "EXIT $rc $(date -Is)" >> "$OUT/status.txt"

cp -f "$DS/complete.patch" "$OUT/complete.patch" 2>/dev/null
cp -f "$CASE/inputs/merged_hunks/ported"/* "$OUT/" 2>/dev/null
cp -f "$CFG" "$OUT/config.used.yml" 2>/dev/null

# API 链完整性检查（改签名后是否还有调用点用旧参数个数）
if [ -f "$OUT/complete.patch" ]; then
  $PY /data/ncwang/chain_check.py "$REPO" "$OUT/complete.patch" \
    > "$OUT/chain-report.txt" 2>&1
  echo "chain rc=$?" >> "$OUT/status.txt"
fi

$PY /data/ncwang/make_verdict.py "$CASE" "$OUT" >> "$OUT/status.txt" 2>&1 || true

echo "DONE $(date -Is)" >> "$OUT/status.txt"
