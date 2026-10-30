#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐 hunk 分诊：这个 hunk 到底要不要移植到目标版本。

输入：patch + summaries.json（已算好的 kind/symbol/summary/enclosing）+ CVE 描述
输出：triaged.json —— 每个 hunk 的 {port, category, reason}

用法: triage.py <patch> <summaries.json> <cve_desc> <out.json> [model]
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "backport_patch/tools/patch-backporting/src"))
from tools.utils import split_patch_indexed  # noqa: E402
from tools.hunk_dependencies import (fingerprint, review_dependencies, syntax_prerequisites)  # noqa: E402

TPL = """上游提交里混着三类内容：
  ① fix       —— 目标 CVE 的修复语义，必须移植
  ② metadata  —— 版本号/元数据/升级产物（SQLITE_VERSION、METADATA、README.version、Android.patch 等），不要移植
  ③ unrelated —— 与目标 CVE 无关的其他上游改动（独立重构、别的 bugfix、非必要的测试设施），不要移植

CVE 描述：{desc}
目标版本通常比上游旧，上游新增的设施在目标里可能不存在。
先判断该改动与修复语义是否相关；上游类名、函数名或文件在目标缺失，不构成无关的证据。
修复逻辑及其必要测试需要映射到目标的等价实现。纯粹为上游测试管理功能开关的
Rule 或测试运行器若仅服务上游设施且与修复及其验证无关，应判 unrelated。
必要导入、声明、初始化、测试预期依赖的输出也属于 fix，不能因没有独立修复行为就跳过。
kind/symbol 只是摘要线索，noise 不等于 metadata。
移除开关条件可能改变安全行为；删除已无引用的开关声明属于独立清理，不自动成为修复前提。
分诊不猜测目标是否具有某个类名，也不把目标不存在上游设施当作跳过修复的理由。

hunk 所在文件：{file}
这个 hunk 在函数里做了什么（摘要）：{summary}
类型：{kind}；符号：{symbol}；所在函数：{enclosing}

语义契约：{contract}
源码证据：{structure}

patch 内容：
```diff
{diff}
```

判断：这个 hunk 是否需要移植到目标版本？
只输出 JSON：{{"port": true 或 false, "category": "fix" 或 "metadata" 或 "unrelated", "reason": "一句话"}}
"""


def call(key, model, prompt, *, dependency=False):
    # Share response/error handling with the summary stage; options are external.
    from summarize_and_group import call_json
    options = os.environ.get("TRIAGE_REQUEST_OPTIONS", '{"temperature":0}')
    timeout = os.environ.get("TRIAGE_READ_TIMEOUT", "60")
    if dependency:
        options = os.environ.get("TRIAGE_DEPENDENCY_REQUEST_OPTIONS", options)
        timeout = os.environ.get("TRIAGE_DEPENDENCY_READ_TIMEOUT",
                                 os.environ.get("GROUP_READ_TIMEOUT", "300"))
    options, timeout = json.loads(options), float(timeout)
    return call_json(key, model, prompt, request_options=options, read_timeout=timeout)


def validate_decision(obj):
    if (not isinstance(obj, dict) or type(obj.get("port")) is not bool
            or obj.get("category") not in {"fix", "metadata", "unrelated"}
            or obj["port"] != (obj["category"] == "fix")
            or not isinstance(obj.get("reason"), str) or not obj["reason"].strip()):
        raise ValueError("triage requires boolean port, consistent category, and reason")
    return obj


def git_count(repo, ref, name, path=None):
    """符号在目标版本出现多少次（可限定文件）。"""
    cmd = ["git", "-C", repo, "grep", "-c", "-F", name, ref]
    if path:
        cmd += ["--", path]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode not in (0, 1) and path:
        # 上游路径在目标里可能不同（A16 system/stack/btm/btm_sec.cc vs A12 stack/btm/...），
        # 这时退回全仓库搜索，避免把"文件搬家"误判成"函数不存在"
        return git_count(repo, ref, name, None)
    if p.returncode not in (0, 1):
        return -1
    try:
        return int(p.stdout.strip().split(":")[-1] or 0)
    except ValueError:
        return len(p.stdout.strip().splitlines())


def postprocess(rows, hunks, summaries, repo, target_ref, reviews=None):
    """Close only evidenced prerequisites; summary names never select hunks."""
    by_id = {r["hunk"]: r for r in rows}
    for row in rows:
        row.setdefault("initial_decision", {k: row.get(k) for k in ("port", "category", "reason")})
        row.update(row["initial_decision"])
        row.pop("func_in_target", None)
        row.pop("semantic_candidates", None)
        row.pop("dependency_review", None)
        row.pop("dependency_fingerprint", None)
        row.pop("dependency_status", None)
        row["retained_by"] = []
    edges = syntax_prerequisites(summaries)
    if reviews is not None:
        edges += [{**edge, "consumer": consumer, "kind": "reviewed_prerequisite"}
                  for consumer, review in reviews.items() for edge in review["requires"]]
    unique = {}
    for edge in edges:
        unique.setdefault((edge["provider"], edge["consumer"], edge["reason"]), edge)
    edges = list(unique.values())
    # Monotonic worklist, with no arbitrary closure-depth limit.
    incoming = {}
    for edge in edges:
        incoming.setdefault(edge["consumer"], []).append(edge)
    pending = sorted(h for h, row in by_id.items() if row.get("port") is True)
    visited = set()
    while pending:
        consumer = pending.pop()
        if consumer in visited:
            continue
        visited.add(consumer)
        for edge in incoming.get(consumer, []):
            row = by_id[edge["provider"]]
            # An API failure is unresolved, even when another hunk mentions it.
            if row.get("port") is None:
                continue
            row["retained_by"].append(edge)
            if row.get("port") is not True:
                row.update(port=True, category="fix", reason=(row.get("reason") or "") +
                           " | 被 hunk %d 依赖：%s" % (consumer, edge["reason"]))
                pending.append(row["hunk"])
    for row in rows:
        row["needs_semantic_mapping"] = row.get("port") is True
    if reviews is not None:
        by_hunk = {h.get("idx", i): h for i, h in enumerate(hunks)}
        for summary in summaries:
            h = summary["hunk"]
            row = by_id[h]
            # Include syntax edges too, so grouping receives exactly the same DAG.
            review = {**reviews[h], "requires": list(reviews[h]["requires"])}
            seen = {e["provider"] for e in review["requires"]}
            for edge in incoming.get(h, []):
                if edge["provider"] not in seen:
                    review["requires"].append(edge)
                    seen.add(edge["provider"])
            row["dependency_review"] = review
            row["dependency_fingerprint"] = fingerprint(summary, by_hunk[h])
            row["dependency_status"] = "complete"
    return rows


def main():
    patch_path, summ_path, desc, out_path = sys.argv[1:5]
    model = (sys.argv[5] if len(sys.argv) > 5
             else os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    repo = sys.argv[6] if len(sys.argv) > 6 else ""
    target_ref = sys.argv[7] if len(sys.argv) > 7 else ""
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_KEY", "")
    post_only = os.environ.get("TRIAGE_POST_ONLY") == "1"
    if not key and not post_only:
        sys.exit("缺 DEEPSEEK_API_KEY（兼容 LLM_KEY）")

    text = Path(patch_path).read_text(encoding="utf-8", errors="replace")
    _, hunks = split_patch_indexed(text, True)
    summaries = json.loads(Path(summ_path).read_text(encoding="utf-8"))
    assert len(summaries) == len(hunks)

    failed_summaries = [int(s.get("hunk", i)) for i, s in enumerate(summaries)
                        if s.get("call_failed")]
    raw_limit = os.environ.get("SUMMARY_MAX_FAILURES")
    summary_failure_limit = int(raw_limit) if raw_limit is not None else 0
    if len(failed_summaries) > summary_failure_limit:
        sys.exit("TRIAGE_INFRA_FAILURE: upstream summary failed for hunks %s (limit=%d)" %
                 (failed_summaries[:20], summary_failure_limit))

    rows = json.loads(Path(out_path).read_text(encoding="utf-8")) if post_only else []
    cached_reviews = None
    if post_only:
        print("post-only：复用已有 LLM 判断，只跑后处理")
        from tools.hunk_dependencies import attach_reviews
        attach_reviews(summaries, hunks, rows)
        if all("dependency_review" in r for r in rows):
            cached_reviews = {r["hunk"]: r["dependency_review"] for r in rows}
    for r, h in ([] if post_only else zip(summaries, hunks)):
        prompt = TPL.format(
            desc=desc, file=h["file"], summary=r.get("summary", ""),
            kind=r.get("kind", ""), symbol=r.get("symbol") or "-",
            enclosing=r.get("det_enclosing") or r.get("enclosing") or "-",
            contract=json.dumps(r.get("semantic_contract", {}), ensure_ascii=False),
            structure=json.dumps(r.get("source_structure", {}), ensure_ascii=False),
            diff=h["text"],
        )
        obj = None
        for attempt in range(3):
            try:
                obj = validate_decision(call(key, model, prompt))
                break
            except RuntimeError as e:
                # Transport/client retries have already been exhausted. Do not
                # repeat a failed account/request across every remaining hunk.
                obj = {"port": None, "category": "unknown", "reason": str(e)}
                break
            except (ValueError, TypeError, KeyError, AttributeError, TimeoutError) as e:
                print("  retry %d: %s" % (attempt + 1, e), file=sys.stderr)
                time.sleep(3)
        if obj is None:
            obj = {"port": None, "category": "unknown", "reason": "parse failed"}
        obj["hunk"] = r["hunk"]
        obj["file"] = h["file"]
        rows.append(obj)
        print("  %2d %-5s %-10s %s" % (
            obj["hunk"], "PORT" if obj["port"] is True else "skip" if obj["port"] is False else "FAIL",
            obj.get("category", ""), (obj.get("reason") or "")[:50]))
        if obj["port"] is None:
            Path(out_path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            sys.exit("TRIAGE_INFRA_FAILURE: unresolved hunk %d" % obj["hunk"])

    # Save initial decisions even if dependency review fails; no unknown decision
    # can silently become a skip or be hidden by closure.
    for row in rows:
        if "initial_decision" in row:
            row.update(row["initial_decision"])
        if not post_only:
            row["dependency_status"] = "pending"
    Path(out_path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r["hunk"] for r in rows if type(r.get("port")) is not bool]
    if failed:
        sys.exit("TRIAGE_INFRA_FAILURE: unresolved hunks %s" % failed)
    if post_only:
        reviews = cached_reviews
    else:
        reviews = review_dependencies(rows, hunks, summaries,
            lambda prompt: call(key, model, prompt, dependency=True),
            retries=max(0, int(os.environ.get("TRIAGE_DEPENDENCY_RETRIES", "1"))))
    postprocess(rows, hunks, summaries, repo, target_ref, reviews)

    Path(out_path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    n_port = sum(1 for x in rows if x.get("port"))
    n_fail = sum(1 for x in rows if x.get("port") is None)
    print("\n合计: port=%d, skip=%d, 失败=%d" % (
        n_port, len(rows) - n_port - n_fail, n_fail))


if __name__ == "__main__":
    main()
