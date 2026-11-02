#!/usr/bin/env bash
# 第五轮：剩余 6 个 CVE（48649 已单独跑完），2 条泳道，全部启用分诊
set -u

ROOT=/data/ncwang/backport_patch
RUN=$ROOT/cases/run_case_generic.sh
BASE=$ROOT/cases/_round5
mkdir -p "$BASE"

run() {
  echo "BEGIN $3 $(date -Is)" >> "$BASE/progress.txt"
  OUT="$BASE/$3" ENABLE_TRIAGE=1 CVE_DESC="$4" \
    bash "$RUN" "$1" "$2" > "$BASE/$3.nohup" 2>&1
  echo "END   $3 rc=$? $(date -Is)" >> "$BASE/progress.txt"
  sleep 15
}

D32348="CVE-2025-32348: PiP/固定窗口绕过后台启动限制，修复为可见 Activity 必须非 pinned。"
D48533="CVE-2025-48533: 锁屏时 splash screen 移除过早导致应用内容可见；修复为延迟移除。"
D48550="CVE-2025-48550: SlicePermissionManager 未校验包名，可越权操作他人 slice。"
D48595="CVE-2025-48595: SQLite 聚合项索引整数溢出（i16 iAgg），可导致代码执行。"
D46054="CVE-2026-46054: SELinux backing file 的 mmap 权限检查缺失。"
D0045="CVE-2026-0045: Bluetooth 安全升级链路未强制认证。"

echo "START round5 $(date -Is)" > "$BASE/progress.txt"

(
  run aosp-frameworks-base CVE-2025-32348 r5-32348 "$D32348"
  run aosp-frameworks-base CVE-2025-48533 r5-48533 "$D48533"
  run aosp-frameworks-base CVE-2025-48550 r5-48550 "$D48550"
) &

(
  run aosp-external-sqlite CVE-2025-48595 r5-48595 "$D48595"
  run aosp-kernel-linux CVE-2026-46054 r5-46054 "$D46054"
  run aosp-system-bt CVE-2026-0045 r5-0045 "$D0045"
) &

wait
echo "DONE round5 $(date -Is)" >> "$BASE/progress.txt"
