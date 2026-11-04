#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐 hunk 分诊：这个 hunk 到底要不要移植到目标版本。

输入：patch + summaries.json（已算好的 kind/symbol/summary/enclosing）+ CVE 描述
输出：triaged.json —— 每个 hunk 的移植决定和独立的闭包复核标记

port=True   必须修改目标生产代码。
port=False  不直接修改目标代码；测试/调试义务通过 triage_state 单独保留。
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_capabilities import promote_triage_consumers

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
测试文件默认不进入生产 Patch；若测试验证了修复行为，只记录等价测试义务。
纯粹为上游测试管理功能开关的 Rule、导入或测试运行器，不改变修复行为。

hunk 所在文件：{file}
这个 hunk 在函数里做了什么（摘要）：{summary}
类型：{kind}；符号：{symbol}；所在函数：{enclosing}

结构化输入（只作为证据；summary 可能遗漏重点）：
```json
{structured_context}
```
目标版本对应证据（精简状态和目标位置）：
```json
{mapping_evidence}
```
MAPPED 表示目标版本存在对应实现或输出位置；NOT_APPLICABLE 表示没有生产对应；
UNRESOLVED 只表示证据尚未确认，不能当成“目标不存在”。

patch 内容：
```diff
{diff}
```

判断：这个 hunk 是否需要移植到目标版本？
必须结合 diff、源码结构、相关 hunk 和关系；不能仅凭摘要措辞。
目标版本缺少同名类名、函数名或文件，不构成 upstream-unrelated 的证据。
只输出 JSON：{{"port": true 或 false, "category": "fix" 或 "metadata" 或 "unrelated", "reason": "一句话"}}
"""


def hunk_file_path(hunk):
    """Resolve new-file/deleted-file hunk markers to their actual Git path."""
    path = str(hunk.get("file") or "").strip()
    if path and path not in {"(new file)", "(deleted file)", "/dev/null"}:
        return path
    text = str(hunk.get("text") or "")
    match = re.search(r"^\+\+\+ b/(.+)$|^--- a/(.+)$", text, re.M)
    if not match:
        return path
    return (match.group(1) or match.group(2)).strip()


def classify_file_role(path):
    path = str(path or "")
    lower = path.lower()
    if lower.endswith(".aconfig") or "/flags/" in lower:
        return "config"
    if (
        "/tests/" in lower or "/test/" in lower
        or re.search(r"(?:Tests?|_test)\.(?:java|kt|cpp|cc|c)$", path)
        or re.match(r"(?:test_.+|.+_test)\.py$", os.path.basename(lower))
    ):
        return "test"
    if re.search(r"(?:^|/)(?:METADATA|OWNERS|README|CHANGELOG|VERSION)(?:\.|$)", path, re.I):
        return "metadata"
    return "production"


def _changed_code_lines(hunk):
    return [
        line[1:].strip()
        for line in str(hunk.get("text") or "").splitlines()
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    ]


def _summary_text(summary):
    return json.dumps(summary, ensure_ascii=False)


def _test_infrastructure_only(lines):
    meaningful = [line for line in lines if line]
    return bool(meaningful) and all(
        line.startswith(("import ", "@Rule", "@ClassRule"))
        or re.match(r"^(?:public|private|protected)?\s*static\s+final\s+.*Rule\b", line)
        for line in meaningful
    )


def _test_has_behavior_signal(lines, summary):
    text = "\n".join(lines) + "\n" + _summary_text(summary)
    return bool(re.search(
        r"@Test\b|\bassert(?:That|WithMessage|Equals)?\s*\(|"
        r"\bverify\s*\(|\bwhen\s*\(|\bthenReturn\s*\(",
        text,
    ))


def load_hunk_classification(path=""):
    path = path or os.environ.get("HUNK_CLASSIFICATION_FILE", "")
    if not path or not os.path.isfile(path):
        return {}
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        int(row["hunk"]): row
        for row in rows
        if isinstance(row, dict) and str(row.get("hunk", "")).isdigit()
    }


def load_hunk_mapping(path=""):
    path = path or os.environ.get("HUNK_MAPPING_FILE", "")
    if not path or not os.path.isfile(path):
        return {}
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        int(item["hunk"]): item
        for item in data.get("hunks", [])
        if isinstance(item, dict) and str(item.get("hunk", "")).isdigit()
    }


def compact_mapping_evidence(item):
    """Return only the small mapping summary that triage actually needs."""
    if not item:
        return {
            "status": "UNAVAILABLE",
            "target_files": [],
            "target_symbols": [],
            "reason": "mapping evidence unavailable",
        }
    mapping_type = str(item.get("mapping_type") or "unresolved").lower()
    if mapping_type in {
        "direct", "renamed", "moved", "split", "merged_into",
        "behavior_equivalent", "create_method", "accessor_rewrite",
        "distributed_obligation", "inline_true",
    }:
        status = "MAPPED"
    elif mapping_type == "requires_prerequisite":
        status = "REQUIRES_PREREQUISITE"
    elif mapping_type in {"absent", "not_applicable"}:
        status = "NOT_APPLICABLE"
    else:
        status = "UNRESOLVED"
    files, symbols = [], []
    for mapped in (item.get("mappings") or [])[:3]:
        path = str(mapped.get("path") or "").strip()
        symbol = str(mapped.get("symbol") or "").strip()
        if path and path not in files:
            files.append(path)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return {
        "status": status,
        "target_files": files,
        "target_symbols": symbols[:3],
        "reason": str(item.get("reason") or "")[:240],
    }


def deterministic_classification_decision(hunk, classification):
    role = classification.get("role", "unknown")
    subtype = classification.get("subtype", "unclassified")
    if role == "test_source":
        category = "test"
        reason = "triage policy: test source does not enter production patch"
    elif role in {"metadata", "generated"}:
        category = "metadata"
        reason = "triage policy: %s/%s does not enter production patch" % (role, subtype)
    else:
        raise ValueError("deterministic decision is only valid for non-production roles")
    return {
        "port": False,
        "category": category,
        "reason": reason,
        "hunk": hunk.get("idx", hunk.get("hunk")),
        "file": classification.get("file") or hunk_file_path(hunk),
        "classification": classification,
    }


def can_decide_without_triage_model(classification, mapping_evidence):
    """Only confirmed target absence permits a deterministic non-production skip."""
    return (
        classification.get("role") in {"test_source", "metadata", "generated"}
        and mapping_evidence.get("status") == "NOT_APPLICABLE"
    )


def apply_nonproduction_policy(row, hunk, summary, classification=None):
    """Annotate non-production roles without overriding the triage port decision."""
    actual_file = hunk_file_path(hunk)
    classification = classification or {}
    role = classification.get("role") or classify_file_role(actual_file)
    subtype = classification.get("subtype", "")
    row["file"] = actual_file
    row["file_role"] = role
    row["classification"] = classification or {
        "role": role,
        "subtype": subtype,
        "confidence": "HIGH",
        "evidence": ["triage fallback classification"],
    }
    row["nonproduction_locked"] = False
    row.pop("test_obligation", None)

    if role in {"test", "test_source"}:
        lines = _changed_code_lines(hunk)
        infrastructure = (
            subtype == "test_infrastructure" or _test_infrastructure_only(lines)
        )
        required = (
            subtype == "behavior_test"
            or (not subtype and _test_has_behavior_signal(lines, summary))
        ) and not infrastructure
        if infrastructure:
            row["test_obligation"] = {
                "status": "NONE",
                "required": False,
                "reason": "upstream-only test infrastructure",
            }
        elif required:
            row["test_obligation"] = {
                "status": "TEST_EQUIVALENCE_REQUIRED",
                "required": True,
                "reason": "test changes validate security behavior; adapt an equivalent target test",
            }
        else:
            row["test_obligation"] = {
                "status": "OPTIONAL",
                "required": False,
                "reason": "test-only fixture, signature, or expected-string update",
            }
        if row.get("port") is False:
            row["category"] = "test"
            row["needs_semantic_mapping"] = False
            row["review_required"] = False
            row["closure_symbols"] = []
            row["review_reason"] = ""
            row["nonproduction_locked"] = True
            if infrastructure:
                row["triage_state"] = "test_infrastructure_only"
            elif required:
                row["triage_state"] = "test_equivalence_required"
            else:
                row["triage_state"] = "test_debug_optional"
        elif row.get("port") is True:
            row["triage_state"] = "test_port_required"
        return

    if role in {"metadata", "generated"} and row.get("port") is False:
        row["needs_semantic_mapping"] = False
        row["review_required"] = False
        row["closure_symbols"] = []
        row["review_reason"] = ""
        row["nonproduction_locked"] = True
        row["triage_state"] = "%s_not_required" % role
        return

    if role in {"production", "production_source"} and subtype == "debug_output" and row.get("port") is False:
        row["category"] = "metadata"
        row["needs_semantic_mapping"] = False
        row["review_required"] = False
        row["closure_symbols"] = []
        row["review_reason"] = ""
        row["nonproduction_locked"] = True
        row["triage_state"] = "debug_output_only"
        row["reason"] = (
            (row.get("reason") or "").rstrip()
            + " | triage decision: debug output was not selected for porting"
        )


def compact_source_structure(struct):
    if not isinstance(struct, dict):
        return {}
    after = struct.get("after") or {}
    return {
        "changed_scopes": [
            {
                "name": scope.get("qualified_name") or scope.get("name"),
                "kind": scope.get("kind"),
                "signature": scope.get("signature"),
            }
            for scope in (after.get("scopes") or [])[:6]
        ],
        "signature_changes": (struct.get("signature_changes") or [])[:8],
        "added_or_changed_calls": [
            {"callee": call.get("callee"), "expression": call.get("expression")}
            for call in (after.get("calls") or [])[:12]
        ],
        "state_writes": [
            {
                "target": write.get("target"),
                "operator": write.get("operator"),
                "value": write.get("value"),
            }
            for write in (after.get("writes") or [])[:12]
        ],
        "new_declarations": [
            {
                "name": decl.get("name"),
                "qualified_name": decl.get("qualified_name"),
                "kind": decl.get("kind"),
            }
            for decl in (after.get("declarations") or [])[:8]
        ],
    }


def build_relation_candidates(summaries):
    by_symbol, by_scope = {}, {}
    for summary in summaries:
        idx = summary["hunk"]
        symbol = (summary.get("symbol_inferred") or summary.get("symbol") or "").strip()
        if symbol:
            by_symbol.setdefault(symbol, set()).add(idx)
        scope = summary.get("det_enclosing") or summary.get("enclosing") or ""
        if scope:
            by_scope.setdefault((summary.get("file"), scope), set()).add(idx)
    result = {}
    for summary in summaries:
        idx = summary["hunk"]
        symbol = (summary.get("symbol_inferred") or summary.get("symbol") or "").strip()
        scope = summary.get("det_enclosing") or summary.get("enclosing") or ""
        related = {
            "same_symbol": sorted(by_symbol.get(symbol, set()) - {idx}) if symbol else [],
            "same_file_enclosing": sorted(by_scope.get((summary.get("file"), scope), set()) - {idx}) if scope else [],
            "provides_config_capabilities": [
                {key: relation.get(key) for key in ("id", "provider", "consumer")}
                for relation in (summary.get("provides_config_capabilities") or [])
            ],
            "requires_config_capabilities": [
                {key: relation.get(key) for key in ("id", "provider", "consumer")}
                for relation in (summary.get("requires_config_capabilities") or [])
            ],
        }
        result[idx] = related
    return result


def triage_structured_context(summary, hunk, relation_candidates):
    contract = summary.get("semantic_contract") or {}
    compact_contract = {}
    for key in (
        "goal", "behavior", "failure_behavior", "preconditions",
        "call_chain", "prerequisite_semantics", "obligations",
    ):
        value = contract.get(key)
        if isinstance(value, str):
            compact_contract[key] = value[:600]
        elif isinstance(value, list):
            compact_contract[key] = [str(item)[:500] for item in value[:8]]
    return {
        "file_role": classify_file_role(hunk_file_path(hunk)),
        "source_structure": compact_source_structure(summary.get("source_structure") or {}),
        "related_hunks": relation_candidates.get(summary["hunk"], {}),
        "detected_behaviors": {
            "kind": summary.get("kind", ""),
            "symbol": summary.get("symbol_inferred") or summary.get("symbol") or "",
            "introduced": summary.get("introduced") or [],
            "semantic_contract": compact_contract,
        },
    }


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


def postprocess(rows, hunks, summaries, repo, target_ref, classification=None):
    """Mechanical safeguards around the LLM decisions."""
    classification = classification or {}
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
        apply_nonproduction_policy(
            row,
            hunks[row["hunk"]],
            summaries[row["hunk"]],
            classification.get(int(row["hunk"])),
        )

    # 规则 2/3 需要迭代到不动点：提升一个 hunk 后，它可能又拉出新的依赖
    for _round in range(5):
        changed = _closure_once(rows, hunks, summaries)
        if not changed:
            break
    promote_triage_consumers(rows, summaries)
    for _round in range(5):
        changed = _closure_once(rows, hunks, summaries)
        if not changed:
            break
    # Test/debug decisions are role-based and must not be re-promoted by symbol
    # co-occurrence or a config relation.
    for row in rows:
        if row.get("nonproduction_locked") or classify_file_role(
            hunk_file_path(hunks[row["hunk"]])
        ) != "production":
            apply_nonproduction_policy(
                row,
                hunks[row["hunk"]],
                summaries[row["hunk"]],
                classification.get(int(row["hunk"])),
            )
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
    summary_by_hunk = {
        int(item.get("hunk", i)): item for i, item in enumerate(summaries)
    }
    hunk_by_id = {
        int(item.get("idx", i)): item for i, item in enumerate(hunks)
    }

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
    providers = {}
    for i, s in enumerate(summaries):
        if rows[i].get("port") or rows[i].get("nonproduction_locked"):
            continue
        hunk = hunk_by_id.get(int(s.get("hunk", i)), hunks[i])
        symbols = _defined_symbols(s, hunk)
        for sym in symbols:
            providers.setdefault(sym, set()).add(i)
    for r in rows:
        if not r.get("port"):
            continue
        hunk = hunk_by_id.get(int(r["hunk"]))
        if hunk is None:
            continue
        added = "\n".join(
            line for line in hunk["text"].splitlines() if line.startswith("+")
        )
        for name, idxs in providers.items():
            if not re.search(r"\b%s\b" % re.escape(name), added):
                continue
            if len(idxs) > 1:
                for i in sorted(idxs):
                    if rows[i].get("port") is not False:
                        continue
                    rows[i]["review_required"] = True
                    rows[i]["triage_state"] = "dependency_ambiguous"
                    rows[i]["review_reason"] = (
                        "多个 skipped hunk 都定义 %s，不能自动选择 provider" % name
                    )
                changed = True
                continue
            for i in idxs:
                if rows[i].get("port"):
                    continue
                if rows[i].get("nonproduction_locked"):
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
            if rows[i].get("nonproduction_locked"):
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


def _defined_symbols(summary, hunk):
    """Symbols introduced by one skipped declaration/implementation hunk."""
    values = set()
    if (summary.get("kind") or "").strip() in ("declare", "implement"):
        values.update({
            (summary.get("symbol_inferred") or "").strip(),
            (summary.get("symbol") or "").strip(),
        })
    structure = summary.get("source_structure") or {}
    after = structure.get("after") if isinstance(structure, dict) else {}
    for declaration in (after or {}).get("new_declarations") or []:
        if not isinstance(declaration, dict):
            continue
        values.add(str(declaration.get("name") or "").strip())
        qualified = str(declaration.get("qualified_name") or "").strip()
        if qualified:
            values.add(qualified.rsplit(".", 1)[-1])
    added = "\n".join(
        line[1:]
        for line in (hunk.get("text") or "").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    method = re.compile(
        r"^\s*(?:(?:public|private|protected|static|final|abstract|"
        r"synchronized|native|default)\s+)*[A-Za-z_$][\w$<>\[\].?, ]*\s+"
        r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
        re.MULTILINE,
    )
    values.update(method.findall(added))
    return {
        value for value in values
        if len(value) > 3 and re.search(r"[A-Za-z_]", value)
    }


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
    relation_candidates = build_relation_candidates(summaries)

    failed_summaries = [int(s.get("hunk", i)) for i, s in enumerate(summaries)
                        if s.get("call_failed")]
    raw_limit = os.environ.get("SUMMARY_MAX_FAILURES")
    summary_failure_limit = int(raw_limit) if raw_limit is not None else 0
    if len(failed_summaries) > summary_failure_limit:
        sys.exit("TRIAGE_INFRA_FAILURE: upstream summary failed for hunks %s (limit=%d)" %
                 (failed_summaries[:20], summary_failure_limit))

    post_only = os.environ.get("TRIAGE_POST_ONLY") == "1"
    classification = load_hunk_classification()
    hunk_mapping = load_hunk_mapping()
    rows = json.load(open(out_path, encoding="utf-8")) if post_only else []
    if post_only:
        for row in rows:
            row["mapping_evidence"] = compact_mapping_evidence(
                hunk_mapping.get(int(row["hunk"]))
            )
        print("post-only：复用已有 LLM 判断，只跑后处理")
    for r, h in ([] if post_only else zip(summaries, hunks)):
        classified = classification.get(int(r["hunk"]), {})
        mapping_evidence = compact_mapping_evidence(
            hunk_mapping.get(int(r["hunk"]))
        )
        if can_decide_without_triage_model(classified, mapping_evidence):
            obj = deterministic_classification_decision(h, classified)
            obj["mapping_evidence"] = mapping_evidence
            rows.append(obj)
            print("  %2d %-5s %-10s %s" % (
                obj["hunk"], "skip", obj["category"], obj["reason"][:50]))
            continue
        prompt = TPL.format(
            desc=desc, file=h["file"], summary=r.get("summary", ""),
            kind=r.get("kind", ""), symbol=r.get("symbol") or "-",
            enclosing=r.get("det_enclosing") or r.get("enclosing") or "-",
            structured_context=json.dumps(
                triage_structured_context(r, h, relation_candidates),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            mapping_evidence=json.dumps(
                mapping_evidence,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            diff=h["text"][:4000],
        )
        obj = None
        for attempt in range(3):
            try:
                obj = parse_json(call(key, model, prompt))
                if obj and "port" in obj:
                    obj["mapping_evidence"] = mapping_evidence
                    break
            except (urllib.error.URLError, TimeoutError) as e:
                print("  retry %d: %s" % (attempt + 1, e), file=sys.stderr)
                time.sleep(3)
        if not obj or "port" not in obj:
            obj = {"port": None, "category": "unknown", "reason": "parse failed"}
        obj["hunk"] = r["hunk"]
        obj["file"] = hunk_file_path(h)
        rows.append(obj)
        print("  %2d %-5s %-10s %s" % (
            obj["hunk"], "PORT" if obj["port"] else "skip",
            obj.get("category", ""), (obj.get("reason") or "")[:50]))

    if repo and target_ref:
        postprocess(
            rows,
            hunks,
            summaries,
            repo,
            target_ref,
            classification=classification,
        )

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
