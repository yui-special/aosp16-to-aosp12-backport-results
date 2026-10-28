#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 实验：逐 hunk LLM summary -> 机械建边 -> 连通分量
# 用法: summarize_and_group.py <patch> <out_json> [model]
import json
import os
import re
import sys
import time
import signal
import ssl
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "backport_patch/tools/patch-backporting/src"))
from tools.utils import split_patch_indexed  # noqa: E402

BASE = os.environ.get("LLM_BASE_URL", "https://endpoint.greatrouter.com/v1").rstrip("/")
SYS = (
    "你是代码补丁审阅者，能分析 Java/Kotlin/C/C++/Python/Rust 等多种语言。"
    "只输出严格 JSON，不要 markdown、不要解释。"
)
TPL = """下面是一个上游补丁里的**单个 hunk**（文件：{file}）。

请判断它在所在的函数/类里做了什么，并给出可供“目标版本语义映射”使用的结构化标签。

kind 只能取：
  declare   —— 新增/修改一个方法声明（接口方法、AIDL 方法、抽象方法、public 方法签名）
  implement —— 实现或重写某个方法（@Override、实现接口）
  call      —— 调用某个方法（尤其是把参数继续往下传）
  logic     —— 修改函数内部逻辑（条件、赋值、校验）
  noise     —— 版本号、注释、时间戳等与行为无关的改动

symbol：被 declare / implement / call 的方法名（没有就空串）
param_change：该 hunk 是否改变了这个方法的参数（"add" / "remove" / "none"）

semantic_contract 必须描述修复语义，而不是只复述代码。字段：
  goal               —— 这个改动要保证/阻止什么行为
  behavior           —— 目标版本必须保持的行为
  enclosing_role     —— 当前 hunk 所在函数/方法/符号/内层作用域的职责，以及它在修复中承担哪一步
  inputs             —— 关键输入、参数或状态
  preconditions      —— 生效前提
  state_read          —— 读取的状态/调用
  state_written       —— 写入/改变的状态/调用
  side_effects        —— 返回值、异常、权限、日志等外部效果
  failure_behavior    —— 失败/拒绝路径
  call_chain          —— 涉及的上游调用链，按调用顺序
  callers             —— hunk/上下文中能确定的调用方；不确定时留空，不要猜
  callees             —— 本改动直接调用或依赖的函数/构造器
  symbol_relations    —— 逐条写出“调用者 -> 被调用者: 数据流/条件/返回值/副作用关系”
  prerequisite_semantics —— 这个修复默认目标已经存在的旧机制、字段、入口或调用链；没有则留空
  search_terms        —— 在目标仓库中搜索等价实现时使用的 3~12 个精确标识符、方法名、类名、字符串或注释短语
  obligations         —— 移植后必须逐项满足的 1~6 条语义义务

依赖描述应聚焦本 hunk 的行为变化：区分必须同步修改的接口/状态约束和仅供理解的普通调用。
不要仅因同文件、同函数或共用 helper 就声称必须一起修改；prerequisite_semantics 只写具体必要前提。
不要猜测目标版本中一定存在哪些符号；search_terms 只用于搜索候选，不能当成等价证明。
非代码或 noise hunk 的 semantic_contract 可以字段为空，但仍要输出全部字段。

只输出严格 JSON：
{{"kind": "...", "symbol": "...", "param_change": "...", "summary": "一句中文",
  "semantic_contract": {{"goal": "...", "behavior": "...", "enclosing_role": "...", "inputs": [], "preconditions": [],
  "state_read": [], "state_written": [], "side_effects": [], "failure_behavior": "...",
  "call_chain": [], "callers": [], "callees": [], "symbol_relations": [],
  "prerequisite_semantics": [], "search_terms": [], "obligations": []}}}}

hunk：
{hunk}
"""


class _CallTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise _CallTimeout("LLM call timeout")


try:
    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.maximum_version = ssl.TLSVersion.TLSv1_2
except Exception:
    _SSL_CTX = None


def call(key, model, prompt, request_options=None, read_timeout=60.0):
    url = BASE.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": [
        {"role": "system", "content": SYS},
        {"role": "user", "content": prompt},
    ]}
    if request_options is not None:
        if not isinstance(request_options, dict) or set(request_options) & {"model", "messages", "stream"}:
            raise ValueError("request_options must be an object without model/messages/stream overrides")
        body.update(request_options)
    if not 0 < float(read_timeout) < float("inf"):
        raise ValueError("read_timeout must be a finite positive number")
    import httpx
    last = None
    for attempt in range(4):
        try:
            with httpx.Client(verify=_SSL_CTX or True,
                              transport=httpx.HTTPTransport(local_address="0.0.0.0",
                                                            verify=_SSL_CTX or True),
                              timeout=httpx.Timeout(float(read_timeout), connect=10.0)) as client:
                r = client.post(url, headers={"Authorization": "Bearer " + key},
                                json=body)
                payload = r.json()
                choices = payload.get("choices") if isinstance(payload, dict) else None
                if not choices:
                    error = payload.get("error", {}) if isinstance(payload, dict) else {}
                    message = error.get("message", "missing choices") if isinstance(error, dict) else str(error)
                    message = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]",
                                     str(message).replace(key, "[redacted]"))[:500]
                    last = RuntimeError("LLM_RESPONSE_ERROR http=%s: %s" % (r.status_code, message))
                    code = error.get("code") if isinstance(error, dict) else None
                    no_quota = (code in {"insufficient_quota", "insufficient_balance"}
                                or "balance is insufficient" in message.lower())
                    if no_quota or (400 <= r.status_code < 500 and r.status_code != 429):
                        break
                    raise last
                return choices[0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError(str(last))


def parse_json(text):
    t = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        s, e = t.find("{"), t.rfind("}")
        return json.loads(t[s:e + 1])


CONTRACT_FIELDS = (
    "goal", "behavior", "enclosing_role", "inputs", "preconditions", "state_read",
    "state_written", "side_effects", "failure_behavior", "call_chain", "callers",
    "callees", "symbol_relations", "prerequisite_semantics", "search_terms", "obligations",
)
STRING_CONTRACT_FIELDS = {"goal", "behavior", "failure_behavior", "enclosing_role"}


def normalize_contract(obj):
    """Keep old/malformed model outputs usable without trusting their shape."""
    raw = obj.get("semantic_contract")
    if not isinstance(raw, dict):
        raw = {}
    out = {}
    for key in CONTRACT_FIELDS:
        value = raw.get(key, "" if key in STRING_CONTRACT_FIELDS else [])
        if key in STRING_CONTRACT_FIELDS:
            out[key] = str(value or "").strip()
        else:
            if isinstance(value, str):
                value = [value] if value.strip() else []
            if not isinstance(value, list):
                value = []
            out[key] = [str(item).strip() for item in value[:16]
                        if str(item).strip()][:16]
    # A fallback summary is still useful for grouping and for the later mapping stage.
    if not out["goal"]:
        out["goal"] = str(obj.get("summary") or "").strip()
    if obj.get("symbol") and obj["symbol"] not in out["search_terms"]:
        out["search_terms"].insert(0, str(obj["symbol"]).strip())
    return out


def call_json(key, model, prompt, request_options=None, read_timeout=60.0):
    """Ask for one JSON object and repair malformed output in-place."""
    raw = call(key, model, prompt, request_options=request_options, read_timeout=read_timeout)
    try:
        return parse_json(raw)
    except Exception as first_error:  # noqa: BLE001
        repair = (
            "上一条回答不是合法 JSON。请根据原任务修复格式，只输出一个完整 JSON 对象。\n"
            "解析错误：%s\n\n原任务：\n%s\n\n上一条回答：\n%s" %
            (first_error, prompt, raw[:24000])
        )
        repaired = call(key, model, repair, request_options=request_options, read_timeout=read_timeout)
        try:
            return parse_json(repaired)
        except Exception as second_error:  # noqa: BLE001
            raise ValueError("JSON parse failed after repair: %s" % second_error)


def main():
    patch, out = sys.argv[1], sys.argv[2]
    model = sys.argv[3] if len(sys.argv) > 3 else "DeepSeek-V4-Pro"
    key = os.environ.get("LLM_KEY", "")
    if not key:
        sys.exit("缺 LLM_KEY")

    text = open(patch, encoding="utf-8", errors="replace").read()
    _, hunks = split_patch_indexed(text, True)
    print("hunk 数:", len(hunks))

    rows = []
    hunk_retries = int(os.environ.get("SUMMARY_HUNK_RETRIES", "2"))
    for h in hunks:
        prompt = TPL.format(file=h["file"], hunk=h["text"])
        obj = None
        last_error = None
        for attempt in range(hunk_retries + 1):
            try:
                obj = call_json(key, model, prompt)
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < hunk_retries:
                    time.sleep(2)
        if obj is None:
            obj = {"kind": "unknown", "symbol": "", "param_change": "",
                   "summary": "CALL_FAILED: %s" % last_error, "call_failed": True,
                   "summary_attempts": hunk_retries + 1}
        obj["semantic_contract"] = normalize_contract(obj)
        obj["hunk"] = h["idx"]
        obj["file"] = h["file"]
        rows.append(obj)
        print("  %2d  %-9s %-32s %s" % (
            h["idx"], obj.get("kind", "?"), (obj.get("symbol") or "-")[:32],
            (obj.get("summary") or "")[:60]))

    json.dump(rows, open(out, "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=2)

    # A failed summary must not become `unknown` and later look like a semantic
    # skip. The runner treats a non-zero exit as an infrastructure failure.
    failures = sum(1 for row in rows if row.get("call_failed"))
    raw_limit = os.environ.get("SUMMARY_MAX_FAILURES")
    limit = int(raw_limit) if raw_limit is not None else 0
    if failures > limit:
        sys.exit("SUMMARY_INFRA_FAILURE: %d/%d hunk summaries failed (limit=%d)" % (
            failures, len(rows), limit))

    # ---- 机械建边：declare(S) <-> implement(S) / call(S) ----
    parent = list(range(len(rows)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_symbol = {}
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        by_symbol.setdefault(sym, []).append(r)

    edges = []
    for sym, group in by_symbol.items():
        decl = [r for r in group if r.get("kind") == "declare"]
        impl = [r for r in group if r.get("kind") == "implement"]
        call_ = [r for r in group if r.get("kind") == "call"]
        anchor = decl or impl
        if not anchor:
            continue
        a = anchor[0]["hunk"]
        for r in decl + impl + call_:
            if r["hunk"] != a:
                edges.append((a, r["hunk"], sym, r.get("kind")))
                union(a, r["hunk"])
        for r in decl:
            if r["hunk"] != a:
                union(a, r["hunk"])

    comps = {}
    for r in rows:
        comps.setdefault(find(r["hunk"]), []).append(r["hunk"])
    groups = sorted((sorted(v) for v in comps.values()), key=lambda g: g[0])

    print("\n=== 机械建边（%d 条）===" % len(edges))
    for a, b, sym, kind in edges:
        print("  %2d <-> %2d   %-34s (%s)" % (a, b, sym[:34], kind))

    print("\n=== 连通分量（%d 组）===" % len(groups))
    for i, g in enumerate(groups):
        print("  组 %d (%d hunks): %s" % (i + 1, len(g), g))

    print("\n=== 与 v2（11 组）/ v5（3 组）对比 ===")
    print("  summary+机械边 : %d 组，大小 %s" % (len(groups), [len(g) for g in groups]))


if __name__ == "__main__":
    main()
