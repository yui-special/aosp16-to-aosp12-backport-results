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

sys.path.insert(0, "/data/ncwang/backport_patch/tools/patch-backporting/src")
from tools.utils import split_patch_indexed  # noqa: E402

BASE = "https://endpoint.greatrouter.com/v1"
SYS = (
    "你是 AOSP 代码审阅者。只输出严格 JSON，不要 markdown、不要解释。"
)
TPL = """下面是一个 AOSP16 补丁里的**单个 hunk**（文件：{file}）。

请判断它在所在的函数/类里做了什么，并给出结构化标签。

kind 只能取：
  declare   —— 新增/修改一个方法声明（接口方法、AIDL 方法、抽象方法、public 方法签名）
  implement —— 实现或重写某个方法（@Override、实现接口）
  call      —— 调用某个方法（尤其是把参数继续往下传）
  logic     —— 修改函数内部逻辑（条件、赋值、校验）
  noise     —— 版本号、注释、时间戳等与行为无关的改动

symbol：被 declare / implement / call 的方法名（没有就空串）
param_change：该 hunk 是否改变了这个方法的参数（"add" / "remove" / "none"）

只输出 JSON：
{{"kind": "...", "symbol": "...", "param_change": "...", "summary": "一句中文"}}

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


def call(key, model, prompt):
    url = BASE.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": [
        {"role": "system", "content": SYS},
        {"role": "user", "content": prompt},
    ]}
    import httpx
    last = None
    for attempt in range(4):
        try:
            with httpx.Client(verify=_SSL_CTX or True,
                              transport=httpx.HTTPTransport(local_address="0.0.0.0",
                                                            verify=_SSL_CTX or True),
                              timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                r = client.post(url, headers={"Authorization": "Bearer " + key},
                                json=body)
                return r.json()["choices"][0]["message"]["content"]
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
    for h in hunks:
        prompt = TPL.format(file=h["file"], hunk=h["text"])
        try:
            obj = parse_json(call(key, model, prompt))
        except Exception as e:  # noqa: BLE001
            obj = {"kind": "unknown", "symbol": "", "param_change": "",
                   "summary": "CALL_FAILED: %s" % e}
        obj["hunk"] = h["idx"]
        obj["file"] = h["file"]
        rows.append(obj)
        print("  %2d  %-9s %-32s %s" % (
            h["idx"], obj.get("kind", "?"), (obj.get("symbol") or "-")[:32],
            (obj.get("summary") or "")[:60]))

    json.dump(rows, open(out, "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=2)

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
