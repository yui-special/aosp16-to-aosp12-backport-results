#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐 hunk 分诊：这个 hunk 到底要不要移植到目标版本。

输入：patch + summaries.json（已算好的 kind/symbol/summary/enclosing）+ CVE 描述
输出：triaged.json —— 每个 hunk 的移植决定和独立的闭包复核标记

port=True   必须修改目标代码。
port=False  不修改目标代码。
review_required=True 仅表示命中了符号闭包，仍需判断目标版本是否适用；
                     绝不把 hunk 自动加入 Patch。

用法: triage.py <patch> <summaries.json> <cve_desc> <out.json> [model]
"""
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "backport_patch/tools/patch-backporting/src"))
from tools.utils import split_patch_indexed  # noqa: E402

BASE = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
SYS = "你是安全补丁回移植审阅者，能处理多种编程语言。只输出严格 JSON，不要 markdown、不要解释。"
LEGACY_SYMBOL_CLOSURE_SUFFIX = " | 符号闭包："


class _CallTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise _CallTimeout("LLM call timeout")

TPL = """上游提交里混着三类内容：
  ① fix       —— 目标 CVE 的修复语义，必须移植
  ② metadata  —— 版本号/元数据/升级产物（SQLITE_VERSION、METADATA、README.version、Android.patch 等），不要移植
  ③ unrelated —— 与目标 CVE 无关的其他上游改动（独立重构、别的 bugfix、非必要的测试设施），不要移植

CVE 描述：{desc}
目标版本通常比上游旧，上游新增的设施在目标里可能不存在。
先判断该改动与修复语义是否相关；上游类名、函数名或文件在目标缺失，不构成无关的证据。
修复逻辑及其必要测试需要映射到目标的等价实现。纯粹为上游测试管理功能开关的
Rule、导入或测试运行器，若不改变修复行为且目标不需要该设施，应判 unrelated。

hunk 所在文件：{file}
这个 hunk 在函数里做了什么（摘要）：{summary}
类型：{kind}；符号：{symbol}；所在函数：{enclosing}

patch 内容：
```diff
{diff}
```

判断：这个 hunk 是否需要移植到目标版本？
只输出 JSON：{{"port": true 或 false, "category": "fix" 或 "metadata" 或 "unrelated", "reason": "一句话"}}
"""


def call(key, model, prompt):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    # 该网关 TLS 1.3 握手会卡死，强制 1.2（与 CVE 无关的环境适配）
    try:
        ctx = ssl.create_default_context()
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    except Exception:  # noqa: BLE001
        ctx = None
    # urllib 在服务端半关闭（CLOSE-WAIT）时会永久挂起，timeout 参数不生效，
    # 用 SIGALRM 给每次调用加硬上限。
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(60)
    try:
        with urllib.request.urlopen(req, timeout=55, context=ctx) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    finally:
        signal.alarm(0)


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


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


# 识别「这个 hunk 定义/声明了哪个符号」。
# 要同时覆盖 C（static int foo(...）和 Java/AIDL（private boolean foo(...)/void foo(...);）
DEF_RE = re.compile(
    r"#define\s+(\w+)"
    r"|(?:^|\s)(?:(?:public|private|protected|static|final|abstract|synchronized|native|default|"
    r"extern|inline|SQLITE_PRIVATE|SQLITE_API)\s+)*"
    r"(?:[\w<>\[\],.?]+\s+)+(\w+)\s*\("
)


def _mapping_terms(summary):
    """Terms that can reveal a renamed/moved equivalent in the target."""
    contract = summary.get("semantic_contract") or {}
    values = []
    for key in ("search_terms", "call_chain", "callers", "callees",
                "symbol_relations", "prerequisite_semantics"):
        value = contract.get(key) or []
        if isinstance(value, str):
            value = [value]
        values.extend(str(item).strip() for item in value)
    values.extend([
        str(summary.get("det_enclosing") or "").strip(),
        str(summary.get("symbol_inferred") or summary.get("symbol") or "").strip(),
    ])
    seen, out = set(), []
    for term in values:
        # Keep identifiers/functions/class names and longer error strings. Short words
        # such as "true" or "error" are not evidence of semantic equivalence.
        if len(term) < 4 or term in seen:
            continue
        if not re.search(r"[A-Za-z_]", term):
            continue
        seen.add(term)
        out.append(term)
    return out[:12]


def postprocess(rows, hunks, summaries, repo, target_ref):
    """Mechanical safeguards around the LLM decisions."""
    # Relevance is decided here; target equivalence is checked by the dedicated
    # mapping stage. Repeating repository-wide keyword searches here both
    # conflates those decisions and promotes unrelated framework vocabulary.
    for row in rows:
        # Restore the model's own decision before recomputing mechanical closure.
        # This makes TRIAGE_POST_ONLY deterministic: an older run that promoted a
        # hunk through the former name-wide rule cannot keep that stale promotion.
        initial = row.get("initial_decision")
        if not isinstance(initial, dict):
            reason = row.get("reason") or ""
            if row.get("port") is True and LEGACY_SYMBOL_CLOSURE_SUFFIX in reason:
                # Older outputs did not persist initial_decision. The exact
                # code-owned suffix proves this row was promoted by the former
                # name-wide rule, so restore the only fact we can know safely:
                # the model did not decide to port it. Category remains unknown.
                row["initial_decision"] = {
                    "port": False,
                    "category": "unknown",
                    "reason": reason.split(LEGACY_SYMBOL_CLOSURE_SUFFIX, 1)[0],
                }
            else:
                row["initial_decision"] = {
                    k: row.get(k) for k in ("port", "category", "reason")
                }
        initial = row["initial_decision"]
        for key in ("port", "category", "reason"):
            if key in initial:
                row[key] = initial[key]
        row.pop("func_in_target", None)
        row.pop("semantic_candidates", None)
        row["needs_semantic_mapping"] = row.get("port") is True
        row["review_required"] = False
        row["closure_symbols"] = []
        row["review_reason"] = ""
        row["triage_state"] = (
            "port_required" if row.get("port") is True
            else "not_required" if row.get("port") is False
            else "undecided"
        )

    # 规则 2/3 需要迭代到不动点：提升一个 hunk 后，它可能又拉出新的依赖
    for _round in range(5):
        changed = _closure_once(rows, hunks, summaries)
        if not changed:
            break
    return rows


def _closure_once(rows, hunks, summaries):
    """跑一轮依赖闭包 + 符号闭包，返回是否有改动。

    规则 2（依赖闭包）：PORT hunk 用到的符号若由某个 skip hunk 定义 -> 提回该 hunk。
    规则 3（符号闭包）：同一 symbol 中只要有一处必须移植，其余 skip hunk
      加入独立复核集合，但不改变 port/category。共享 symbol 只是“可能需要
      一起检查”的证据，不能证明测试适配、文档或无关调用点也必须进入 Patch。
      只有规则 2 的真实依赖证明才允许把 port=False 提升为 True。
    """
    changed = False

    # 补 symbol：摘要偶尔会给空 symbol（尤其插入型 hunk）。用新增行里最常出现的
    # 已知 symbol 补上，这是纯机械推断，不依赖具体 CVE。
    known = {(s.get("symbol") or "").strip() for s in summaries} - {""}
    for h, s in zip(hunks, summaries):
        if (s.get("symbol") or "").strip() or not known:
            continue
        added = "\n".join(l for l in h["text"].split("\n") if l.startswith("+"))
        counts = {}
        for sym in known:
            n = len(re.findall(r"\b%s\b" % re.escape(sym), added))
            if n:
                counts[sym] = n
        if counts:
            s["symbol_inferred"] = max(counts, key=counts.get)

    # 只把「声明/实现」类 hunk 当作定义方；call/logic 类不算，
    # 否则调用行也会被误当成定义，导致整包被拉进来。
    defined = {}
    for i, s in enumerate(summaries):
        if rows[i].get("port"):
            continue
        if (s.get("kind") or "").strip() not in ("declare", "implement"):
            continue
        sym = (s.get("symbol_inferred") or s.get("symbol") or "").strip()
        if sym and len(sym) > 2:
            defined.setdefault(sym, []).append(i)
    for r, h in zip(rows, hunks):
        if not r.get("port"):
            continue
        added = "\n".join(l for l in h["text"].split("\n") if l.startswith("+"))
        for name, idxs in defined.items():
            if not re.search(r"\b%s\b" % re.escape(name), added):
                continue
            for i in idxs:
                if rows[i].get("port"):
                    continue
                if rows[i].get("port") is not False:
                    # A failed triage decision remains unresolved until triage
                    # succeeds; dependency mechanics cannot invent a decision.
                    continue
                rows[i]["port"] = True
                rows[i]["category"] = "fix"
                rows[i]["reason"] = (rows[i].get("reason") or "") + (
                    " | 被 hunk %d 依赖（%s）" % (r["hunk"], name))
                rows[i]["needs_semantic_mapping"] = True
                rows[i]["review_required"] = False
                rows[i]["closure_symbols"] = []
                rows[i]["review_reason"] = ""
                rows[i]["triage_state"] = "port_required"
                changed = True

    by_sym = {}
    for i, s in enumerate(summaries):
        sym = (s.get("symbol_inferred") or s.get("symbol") or "").strip()
        if sym:
            by_sym.setdefault(sym, []).append(i)
    for sym, idxs in by_sym.items():
        if not any(rows[i].get("port") for i in idxs):
            continue
        for i in idxs:
            if rows[i].get("port"):
                continue
            if rows[i].get("port") is not False:
                # An unresolved triage decision must stay unresolved; closure
                # cannot manufacture a decision from symbol-name co-occurrence.
                continue
            already = rows[i].get("closure_symbols") or []
            if sym in already:
                continue
            rows[i]["review_required"] = True
            rows[i]["triage_state"] = "review_only_closure"
            rows[i]["closure_symbols"] = sorted(set(already) | {sym})
            rows[i]["review_reason"] = (
                "符号闭包复核：%s 同链另有必须移植的 hunk；本 hunk 不自动移植"
                % sym
            )
            changed = True
    return changed


def main():
    patch_path, summ_path, desc, out_path = sys.argv[1:5]
    model = (sys.argv[5] if len(sys.argv) > 5
             else os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    repo = sys.argv[6] if len(sys.argv) > 6 else ""
    target_ref = sys.argv[7] if len(sys.argv) > 7 else ""
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_KEY", "")
    if not key:
        sys.exit("缺 DEEPSEEK_API_KEY（兼容 LLM_KEY）")

    text = open(patch_path, encoding="utf-8", errors="replace").read()
    _, hunks = split_patch_indexed(text, True)
    summaries = json.load(open(summ_path, encoding="utf-8"))
    assert len(summaries) == len(hunks)

    failed_summaries = [int(s.get("hunk", i)) for i, s in enumerate(summaries)
                        if s.get("call_failed")]
    raw_limit = os.environ.get("SUMMARY_MAX_FAILURES")
    summary_failure_limit = int(raw_limit) if raw_limit is not None else 0
    if len(failed_summaries) > summary_failure_limit:
        sys.exit("TRIAGE_INFRA_FAILURE: upstream summary failed for hunks %s (limit=%d)" %
                 (failed_summaries[:20], summary_failure_limit))

    post_only = os.environ.get("TRIAGE_POST_ONLY") == "1"
    rows = json.load(open(out_path, encoding="utf-8")) if post_only else []
    if post_only:
        print("post-only：复用已有 LLM 判断，只跑后处理")
    for r, h in ([] if post_only else zip(summaries, hunks)):
        prompt = TPL.format(
            desc=desc, file=h["file"], summary=r.get("summary", ""),
            kind=r.get("kind", ""), symbol=r.get("symbol") or "-",
            enclosing=r.get("det_enclosing") or r.get("enclosing") or "-",
            diff=h["text"][:4000],
        )
        obj = None
        for attempt in range(3):
            try:
                obj = parse_json(call(key, model, prompt))
                if obj and "port" in obj:
                    break
            except (urllib.error.URLError, TimeoutError) as e:
                print("  retry %d: %s" % (attempt + 1, e), file=sys.stderr)
                time.sleep(3)
        if not obj or "port" not in obj:
            obj = {"port": None, "category": "unknown", "reason": "parse failed"}
        obj["hunk"] = r["hunk"]
        obj["file"] = h["file"]
        rows.append(obj)
        print("  %2d %-5s %-10s %s" % (
            obj["hunk"], "PORT" if obj["port"] else "skip",
            obj.get("category", ""), (obj.get("reason") or "")[:50]))

    if repo and target_ref:
        postprocess(rows, hunks, summaries, repo, target_ref)

    json.dump(rows, open(out_path, "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=2)
    n_port = sum(1 for x in rows if x.get("port") is True)
    n_skip = sum(1 for x in rows if x.get("port") is False)
    n_review = sum(1 for x in rows
                   if x.get("port") is False and x.get("review_required") is True)
    n_fail = sum(1 for x in rows
                 if not (x.get("port") is True or x.get("port") is False))
    print("\n合计: port=%d, skip=%d（仅复核 %d）, 失败=%d" % (
        n_port, n_skip, n_review, n_fail))
    # 失败不能被当成 skip：超过 10%（至少 2 个）就判为基础设施故障
    if n_fail > max(2, len(rows) // 10):
        sys.exit("TRIAGE_INFRA_FAILURE: %d/%d hunks 调用失败" % (n_fail, len(rows)))


if __name__ == "__main__":
    main()
