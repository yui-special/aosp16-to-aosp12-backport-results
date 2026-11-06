#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 实验：逐 hunk LLM summary -> 机械建边 -> 连通分量
# 用法: summarize_and_group.py <patch> <out_json> [model]
import json
import argparse
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
from tools.source_structure import hunk_evidence, resolve_ref, syntax_summary  # noqa: E402
from tools import source_structure
from tools.frontend import Checkpoint, implementation_id, run_tasks, stage_options, write_json

BASE = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
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

函数名、完整签名、参数变化和直接调用由程序从固定版本源码提取，见下方语法事实。
不要重新猜测这些字段。解析缺口代表未知；语法事实不证明目标版本接口等价。

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
若提供了修改后的完整作用域，以其最终代码为准。同一个测试的 setup、被测调用和断言
可能分散在多个 hunk；必须一起阅读，不得因为断言不在当前 hunk 就假定旧的期望保持不变。
测试名称不决定场景，具体布尔值、参数和最终断言决定场景。只描述本 hunk 的贡献，
但要使它与同一作用域内其他 hunk 的最终行为一致。
非代码或 noise hunk 的 semantic_contract 可以字段为空，但仍要输出全部字段。

只输出严格 JSON：
{{"kind": "...", "summary": "一句中文",
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


def validate_summary(obj):
    """Validate new model output before legacy normalization can hide omissions."""
    if (not isinstance(obj, dict) or obj.get('kind') not in
            {'declare', 'implement', 'call', 'logic', 'noise'} or obj.get('call_failed')
            or not isinstance(obj.get('summary'), str) or not obj['summary'].strip()):
        raise ValueError('summary requires a valid kind and nonempty summary')
    contract = obj.get('semantic_contract')
    if not isinstance(contract, dict):
        raise ValueError('semantic_contract must be an object')
    for field in CONTRACT_FIELDS:
        value = contract.get(field)
        if field in STRING_CONTRACT_FIELDS:
            valid = isinstance(value, str)
        else:
            valid = isinstance(value, list) and all(isinstance(s, str) and s.strip() for s in value)
        if not valid:
            raise ValueError('missing or invalid semantic_contract.' + field)
    if obj['kind'] != 'noise':
        for field in ('goal', 'behavior', 'enclosing_role', 'obligations'):
            if not contract[field]:
                raise ValueError('behavioral hunk requires semantic_contract.' + field)
        if any(not contract[f].strip() for f in ('goal', 'behavior', 'enclosing_role')):
            raise ValueError('behavioral contract fields must not be blank')
    return obj


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


def attach_syntax(obj, evidence):
    """Legacy labels come from syntax; multiple scopes are never collapsed by a model."""
    obj["symbol"], obj["param_change"] = "", "unknown"
    if evidence is None:
        obj["syntax_status"] = "unavailable_without_source_refs"
        return
    obj["source_structure"] = evidence
    complete = all(evidence[s]["status"] in {"ok", "missing"} for s in ("before", "after"))
    obj["syntax_status"] = "ok" if complete else "incomplete"
    names = sorted({d["name"] for s in ("before", "after") for d in evidence[s]["scopes"]
                    if d["kind"] == "function" and not d["parse_error"]})
    obj["symbols"] = names
    obj["symbol"] = names[0] if len(names) == 1 else ""
    if complete:
        changes = evidence["signature_changes"]
        labels = set()
        for change in changes:
            old, new = change["old_types"], change["new_types"]
            if old is None or new is None:
                labels.add("new_declaration" if old is None else "removed_declaration")
            else:
                labels.add("add" if len(new) > len(old) else "remove" if len(new) < len(old) else "replace")
        obj["param_change"] = next(iter(labels)) if len(labels) == 1 else "mixed" if labels else "none"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("patch")
    parser.add_argument("out")
    parser.add_argument("model", nargs="?", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    parser.add_argument("--repo")
    parser.add_argument("--commit")
    args = parser.parse_args()
    if bool(args.repo) != bool(args.commit):
        parser.error("--repo and --commit must be supplied together")
    patch, out, model = args.patch, args.out, args.model
    if args.repo:
        commit = resolve_ref(args.repo, args.commit)
        parent = resolve_ref(args.repo, commit + "^")
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_KEY", "")
    if not key:
        sys.exit("缺 DEEPSEEK_API_KEY（兼容 LLM_KEY）")

    text = open(patch, encoding="utf-8", errors="replace").read()
    _, hunks = split_patch_indexed(text, True)
    print("hunk 数:", len(hunks))

    options = stage_options('summary')
    checkpoint = Checkpoint(out, dict(stage='summary', patch=text,
        repo=os.path.realpath(args.repo) if args.repo else None,
        refs=[parent, commit] if args.repo else [], parser=source_structure.settings(),
        model=model, endpoint=BASE, request_options=options['request_options'],
        implementation=implementation_id(__file__, source_structure.__file__)), options['resume'])
    if os.path.exists(out):
        os.remove(out)
    rows, tasks, evidence_by_hunk = [], [], {}
    scope_context = source_structure.changed_scope_context(args.repo, parent, commit, hunks) if args.repo else None
    hunk_retries = int(os.environ.get("SUMMARY_HUNK_RETRIES", "2"))
    for h in hunks:
        evidence = hunk_evidence(args.repo, parent, commit, h) if args.repo else None
        evidence_by_hunk[h['idx']] = evidence
        prompt = TPL.format(file=h["file"], hunk=h["text"])
        prompt += "\n程序提取的语法事实：\n" + json.dumps(
            syntax_summary(evidence) if evidence else {"status": "source_refs_not_supplied"}, ensure_ascii=False)
        if scope_context:
            prompt += '\n修改后的完整作用域（含关联 hunk 的最终修改）：\n' + json.dumps(
                dict(scopes={key: scope_context['scopes'][key]
                             for key in scope_context['by_hunk'].get(h['idx'], [])},
                     diagnostic=scope_context['diagnostics'].get(h['idx'])), ensure_ascii=False)
        tasks.append((h['idx'], prompt))

    def summarize(prompt):
        feedback = ''
        for attempt in range(hunk_retries + 1):
            try:
                obj = call_json(key, model, prompt + feedback,
                                request_options=options['request_options'], read_timeout=options['read_timeout'])
                return validate_summary(obj)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                if attempt == hunk_retries:
                    raise
                feedback = '\n上次输出未通过结构检查：%s。请重新输出完整语义契约，不要用空值掩盖遗漏。' % exc
        raise ValueError('SUMMARY_HUNK_RETRIES must be nonnegative')

    by_hunk = {h['idx']: h for h in hunks}
    try:
        for idx, obj, reused in run_tasks(tasks, summarize, checkpoint, validate_summary, options['workers']):
            attach_syntax(obj, evidence_by_hunk[idx])
            # New contracts have already passed strict validation. Do not run the
            # legacy normalizer, which can coerce fields or truncate obligations.
            obj.update(hunk=idx, file=by_hunk[idx]['file'])
            rows.append(obj)
            print('  %2d %-9s %s%s' % (idx, obj['kind'], '[cached] ' if reused else '', obj['summary'][:60]), flush=True)
    except Exception as exc:
        completed = {row['hunk'] for row in rows}
        rows.extend(dict(hunk=h['idx'], file=h['file'], kind='unknown', call_failed=True,
                         summary='stage incomplete: ' + str(exc))
                    for h in hunks if h['idx'] not in completed)
        write_json(out, sorted(rows, key=lambda row: row['hunk']))
        sys.exit('SUMMARY_INFRA_FAILURE: %s; validated items retained in checkpoint' % exc)
    rows.sort(key=lambda row: row['hunk'])
    write_json(out, rows)

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
