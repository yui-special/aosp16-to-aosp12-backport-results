#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐 hunk 分诊：这个 hunk 到底要不要移植到目标版本。

输入：patch + summaries.json（已算好的 kind/symbol/summary/enclosing）+ CVE 描述
输出：triaged.json —— 每个 hunk 的 {port, category, reason}

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

sys.path.insert(0, "/data/ncwang/backport_patch/tools/patch-backporting/src")
from tools.utils import split_patch_indexed  # noqa: E402

BASE = os.environ.get("LLM_BASE_URL", "https://endpoint.greatrouter.com/v1").rstrip("/")
SYS = "你是 AOSP 安全补丁的回移植审阅者。只输出严格 JSON，不要 markdown、不要解释。"


class _CallTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise _CallTimeout("LLM call timeout")

TPL = """上游 A16 提交里混着三类内容：
  ① fix       —— 目标 CVE 的修复语义，必须移植
  ② metadata  —— 版本号/元数据/升级产物（SQLITE_VERSION、METADATA、README.version、Android.patch 等），不要移植
  ③ unrelated —— 与目标 CVE 无关的其他上游改动（重构、别的 bugfix、依赖目标版本不存在的新设施），不要移植

CVE 描述：{desc}
目标版本比上游旧，上游新增的设施在目标里往往不存在。

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
    for key in ("search_terms", "call_chain"):
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
    # Rule 1: a missing upstream function name is NOT enough to skip. Before skipping,
    # search the semantic-contract terms. A hit means this hunk needs target mapping;
    # only the absence of all strong terms permits a skip.
    for r, h, s in zip(rows, hunks, summaries):
        enc = (s.get("det_enclosing") or s.get("enclosing") or "").strip()
        if not enc or enc in ("-", "(new file)") or not repo:
            continue
        cnt = git_count(repo, target_ref, enc)
        r["func_in_target"] = cnt
        if cnt != 0:
            continue

        term_hits = {}
        for term in _mapping_terms(s):
            if term == enc:
                continue
            term_count = git_count(repo, target_ref, term)
            if term_count > 0:
                term_hits[term] = term_count
        r["semantic_candidates"] = term_hits
        if term_hits:
            # The name disappeared, but the behavior may have been renamed/moved or
            # split across old target functions. Never silently turn this into skip.
            r["port"] = True
            r["category"] = "fix"
            r["needs_semantic_mapping"] = True
            r["reason"] = (r.get("reason") or "") + (
                " | 目标无原名 %s，但语义搜索命中 %s；必须做等价映射" %
                (enc, ", ".join(sorted(term_hits)[:6]))
            )
        elif r.get("port"):
            r["port"] = False
            r["category"] = "unrelated"
            r["reason"] = (r.get("reason") or "") + (
                " | 目标版本不存在函数 %s，且语义搜索无候选" % enc
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
    规则 3（符号闭包）：同一 symbol 的 hunk 必须整体同进同退。
      这是 API 链（声明/AIDL/实现/调用点）完整性的机械保证：
      只要链上有一个 hunk 要移植，其余 hunk 就不能被单独丢掉。
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
                rows[i]["port"] = True
                rows[i]["category"] = "fix"
                rows[i]["reason"] = (rows[i].get("reason") or "") + (
                    " | 被 hunk %d 依赖（%s）" % (r["hunk"], name))
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
            rows[i]["port"] = True
            rows[i]["category"] = "fix"
            rows[i]["reason"] = (rows[i].get("reason") or "") + (
                " | 符号闭包：%s 这条 API 链必须整体移植" % sym)
            changed = True
    return changed


def main():
    patch_path, summ_path, desc, out_path = sys.argv[1:5]
    model = sys.argv[5] if len(sys.argv) > 5 else "DeepSeek-V4-Pro"
    repo = sys.argv[6] if len(sys.argv) > 6 else ""
    target_ref = sys.argv[7] if len(sys.argv) > 7 else ""
    key = __import__("os").environ.get("LLM_KEY", "")
    if not key:
        sys.exit("缺 LLM_KEY")

    text = open(patch_path, encoding="utf-8", errors="replace").read()
    _, hunks = split_patch_indexed(text, True)
    summaries = json.load(open(summ_path, encoding="utf-8"))
    assert len(summaries) == len(hunks)

    failed_summaries = [int(s.get("hunk", i)) for i, s in enumerate(summaries)
                        if s.get("call_failed")]
    if failed_summaries:
        sys.exit("TRIAGE_INFRA_FAILURE: upstream summary failed for hunks %s" %
                 failed_summaries[:20])

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
    n_port = sum(1 for x in rows if x.get("port"))
    n_fail = sum(1 for x in rows if x.get("port") is None)
    print("\n合计: port=%d, skip=%d, 失败=%d" % (
        n_port, len(rows) - n_port, n_fail))
    # 失败不能被当成 skip：超过 10%（至少 2 个）就判为基础设施故障
    if n_fail > max(2, len(rows) // 10):
        sys.exit("TRIAGE_INFRA_FAILURE: %d/%d hunks 调用失败" % (n_fail, len(rows)))


if __name__ == "__main__":
    main()
