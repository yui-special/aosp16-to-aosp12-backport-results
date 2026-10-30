#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据跑批产物自动给出 case 判定（与 CVE 无关）。

判定：
  OK              全部组成功 + patch 非空 + 链检查通过 + 编译通过（若有）
  PARTIAL         有组失败，但 patch 可用
  NEEDS_REDESIGN  需要目标版本不存在的机制（分诊标 needs_redesign / 全部 hunk 无落点）
  NEEDS_EVIDENCE  空补丁或全部 need_not_ported，但没有可审计的调用路径证明
  INVALID_ARTIFACT 组声称成功，但最终 complete.patch 缺失或为空
  NOT_APPLICABLE  仅用于已由上游流程明确排除、无需 PortGPT 判断的 case
  INFRA_FAILURE   端点/环境故障

用法: make_verdict.py <case_dir> <run_dir>
"""
import glob
import json
import os
import re
import sys


def read(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def main():
    case_dir, run_dir = sys.argv[1], sys.argv[2]
    status = read(os.path.join(run_dir, "status.txt"))
    patch_path = os.path.join(run_dir, "complete.patch")
    patch_size = os.path.getsize(patch_path) if os.path.exists(patch_path) else 0
    global_path = os.path.join(run_dir, "global-validation.json")
    global_result = None
    if os.path.isfile(global_path):
        try:
            global_result = json.load(open(global_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            global_result = None

    groups, ok, nnp, failed, strict_failed = [], 0, 0, 0, 0
    for f in glob.glob(os.path.join(run_dir, "merged-hunk-*.status.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        groups.append(d)
        effective_complete = d.get("global_covered", d.get("complete"))
        effective_success = d.get("round_succeeded") or d.get("global_covered", False)
        if not effective_success or not effective_complete:
            failed += 1
            if d.get("strict_requires_patch") or not effective_complete:
                strict_failed += 1
        elif d.get("need_not_ported"):
            nnp += 1
        else:
            ok += 1

    chain = read(os.path.join(run_dir, "chain-report.txt"))
    m = re.search(r"仍用旧参数个数的调用点（(\d+)）", chain)
    chain_bad = int(m.group(1)) if m else 0

    build_ok = None
    m = re.search(r"build rc=(\d+)", status)
    if m:
        build_ok = m.group(1) == "0"

    # 分诊结果（若跑过分诊）
    triage_path = os.path.join(case_dir, "groups", "triaged.json")
    tri = []
    if os.path.isfile(triage_path):
        try:
            tri = json.load(open(triage_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            tri = []

    if "INFRA_FAILURE" in status:
        verdict = "INFRA_FAILURE"
    elif patch_size <= 2 and ok:
        verdict = "INVALID_ARTIFACT"
    elif global_result is not None and not global_result.get("complete"):
        verdict = "NEEDS_EVIDENCE" if patch_size <= 2 and not ok else "PARTIAL"
    elif failed:
        verdict = "PARTIAL"
    elif patch_size <= 2 and not ok:
        verdict = "NEEDS_EVIDENCE"
    elif chain_bad or build_ok is False:
        verdict = "PARTIAL"
    else:
        verdict = "OK"

    lines = ["# %s 判定：%s" % (os.path.basename(case_dir), verdict), ""]
    lines.append("## 统计")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append("| 分组总数 | %d |" % len(groups))
    lines.append("| 成功产出 | %d |" % ok)
    lines.append("| 判定无需移植 | %d |" % nnp)
    lines.append("| 失败 | %d |" % failed)
    lines.append("| 严格映射未完成 | %d |" % strict_failed)
    lines.append("| complete.patch | %d 字节 |" % patch_size)
    lines.append("| API 链检查 | %s |" % ("%d 处不匹配" % chain_bad if chain_bad else "通过"))
    lines.append("| 编译 | %s |" % ("通过" if build_ok else ("失败" if build_ok is False else "未执行")))
    if global_result is not None:
        lines.append("| 全局 hunk 覆盖 | %s |" % (
            "通过" if (global_result.get("coverage") or {}).get("complete") else "失败"))
        lines.append("| 全局 API 链 | %s |" % (
            "通过" if global_result.get("api_chain_rc") == 0 else "失败"))
    if tri:
        n_port = sum(1 for x in tri if x.get("port"))
        lines.append("| 分诊 port/skip | %d / %d |" % (n_port, len(tri) - n_port))
    lines.append("")
    if chain_bad:
        lines.append("## API 链问题")
        lines.append("")
        lines.append("```")
        lines.append(chain.strip())
        lines.append("```")
        lines.append("")
    if failed:
        lines.append("## 失败的组")
        lines.append("")
        for d in groups:
            effective_complete = d.get("global_covered", d.get("complete"))
            effective_success = d.get("round_succeeded") or d.get("global_covered", False)
            if not effective_success or not effective_complete:
                lines.append("- hunks %s：未成功；mapping=%s；完整=%s" % (
                    d.get("member_hunks"), d.get("mapping_status"), effective_complete))
        lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("本判定由 make_verdict.py 依据产物机械生成：组状态、patch 体积、")
    lines.append("API 链检查、编译结果。不依赖具体 CVE。")
    open(os.path.join(run_dir, "verdict.md"), "w", encoding="utf-8", newline="\n").write(
        "\n".join(lines) + "\n")
    print("verdict:", verdict)


if __name__ == "__main__":
    main()
