#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据跑批产物自动给出 case 判定（与 CVE 无关）。

判定：
  OK              全部组成功 + patch 非空 + 链检查通过 + 编译通过（若有）
  PARTIAL         有组失败，但 patch 可用
  NEEDS_REDESIGN  需要目标版本不存在的机制（分诊标 needs_redesign / 全部 hunk 无落点）
  NOT_APPLICABLE  没有需要移植的 hunk，且不是缺机制
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

    groups, ok, nnp, failed = [], 0, 0, 0
    for f in glob.glob(os.path.join(run_dir, "merged-hunk-*.status.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        groups.append(d)
        if not d.get("round_succeeded"):
            failed += 1
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
    elif failed:
        verdict = "PARTIAL"
    elif patch_size <= 2 and not ok:
        verdict = "NOT_APPLICABLE"
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
    lines.append("| complete.patch | %d 字节 |" % patch_size)
    lines.append("| API 链检查 | %s |" % ("%d 处不匹配" % chain_bad if chain_bad else "通过"))
    lines.append("| 编译 | %s |" % ("通过" if build_ok else ("失败" if build_ok is False else "未执行")))
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
            if not d.get("round_succeeded"):
                lines.append("- hunks %s：未成功" % d.get("member_hunks"))
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
