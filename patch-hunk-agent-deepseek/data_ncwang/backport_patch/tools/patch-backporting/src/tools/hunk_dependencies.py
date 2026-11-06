"""Dependencies over the complete patch, before relevance filtering.

Mechanical evidence proves scoped declaration, signature and cleanup dependencies.
Behavioral edges require quotes from both changed hunks and are carried into
grouping, rather than independently guessed again. These edges express
prerequisites, not instructions to merge whole API families.
"""
import hashlib
import json
import re

from tools.source_structure import structure_relations


REVIEW_PROMPT = """核对整份补丁尚未确定的直接行为依赖。输入包含所有原始 hunk，包括初步判为 skip 的 hunk。
initial_port 只是相关性初判，不是依赖证据。摘要用于解释意图，以 diff 和 source_structure 为准。
mechanical_prerequisites 是程序已经锁定的事实边，不需要也不能重复、删除或反向改写。
只有其中逐条列出的边已机械处理；未列出不代表不存在依赖，也不表示该关系已被程序否定。
跨文件接口与实现、外部 receiver、AIDL 声明与调用等未锁定签名关系仍必须审查，不得假设已覆盖。
只判断剩余问题：实施该 hunk 的行为时，哪些其他 hunk 的改动必须已经存在？

规则：
- requires 只列 mechanical_prerequisites 之外的新边，是单向先后依赖，不表示必须合组。
  只共享名称、所在方法或修复目标不能建立依赖，也不能重复已锁定的 provider -> consumer。
- 若 consumer 的 initial_port=false，但已选 provider 的修复只有同步实施该 consumer 才完整，
  例如新增重载还要求旧入口转调，则在该边设置 retain_consumer=true；默认 false。
  只能标记直接必要的适配，不能因同 API、同文件或测试设施批量保留 consumer。
- 调用行为依赖要核对接收者、作用域、签名和实参；不允许按常见方法的裸名称连接整个补丁。
- 初始化或输出修改本身可能不修复漏洞，但若被保留逻辑的**行为/测试**需要，必须列为前提。
  已列出的声明和导入由程序处理；未锁定的必要声明仍需审查。无关日志和测试设施不因同文件就保留。
- 测试逐条查看完整断言和预期值，指出真正实现该可观察行为的修改。
  声明/赋值不自动等于输出该值；不同接收者、字段或输出文本不能互相替代。
  断言输出文本时，核对生产代码实际输出的精确文本以及被测对象；不能只找相似字段名。
- 移除功能开关的条件判断可以是修复；删除不再被引用的开关声明通常是独立清理。
  顺序是先移除引用，再删除声明，不能倒置依赖，让修复强制依赖清理。
- unsupported/partial/missing 表示缺少语法证据，不表示无依赖；继续核对 diff，不能猜出类型解析结果。
- 不存在额外跨 hunk 前提时 requires=[] 并说明原因。不要为了让所有修改相关而补边。
- 每条新增依赖引用 provider 和 consumer 各自 diff 中的真实代码，去掉 diff 行首 +/-/空格标记。
  quote 必须是完整连续代码行，并至少包含该侧一条修改行；不要引用摘要、hunk 标题或省略号。
  通常使用 after；删除引用等场景可使用 before。reason 说明为什么两段代码构成直接行为前提。

只输出 JSON，reviews 必须覆盖每个输入 hunk 恰好一次：
{"reviews":[{"hunk":0,"reason":"除机械边外的前提判断", "requires":[
  {"provider":1,"reason":"具体行为依赖关系",
   "provider_evidence":{"side":"after","quote":"完整代码行"},
   "consumer_evidence":{"side":"after","quote":"完整代码行"}}
]}]}
"""


def fingerprint(summary, hunk):
    structure = summary.get("source_structure", {})
    data = [summary["hunk"], hunk.get("file"), hunk["text"],
            structure.get("before_ref"), structure.get("after_ref")]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def syntax_prerequisites(summaries):
    result = [{**r, "reason": "Scoped parameter: " + ", ".join(r["parameters"])}
              for r in structure_relations(summaries) if r["kind"] == "parameter_prerequisite"]
    for provider in summaries:
        ev = provider.get("source_structure", {})
        old, new = ev.get("before", {}), ev.get("after", {})
        if new.get("status") != "ok":
            continue
        previous = {i["qualified_name"] for i in old.get("imports", [])}
        for entry in new.get("imports", []):
            if entry["qualified_name"] in previous:
                continue
            for consumer in summaries:
                use = consumer.get("source_structure", {}).get("after", {})
                if (provider["hunk"] == consumer["hunk"] or use.get("status") != "ok"
                        or use.get("path") != new.get("path")):
                    continue
                matching = [u for u in use.get("import_uses", [])
                            if u["qualified_name"] == entry["qualified_name"]]
                if matching:
                    result.append({"kind": "import_prerequisite", "provider": provider["hunk"],
                                   "consumer": consumer["hunk"], "import": entry,
                                   "uses": matching, "reason": "Explicit import: " + entry["qualified_name"]})
    return sorted(result, key=lambda r: (r["provider"], r["consumer"], r["kind"]))


def _named_calls(evidence, name):
    if evidence.get("status") != "ok":
        return []
    return [call for call in evidence.get("calls", [])
            if not call.get("parse_error")
            and re.split(r"[.:>]", call.get("callee", ""))[-1] == name]


def _matching_calls(evidence, name, count):
    return [call for call in _named_calls(evidence, name)
            if len(call.get("arguments") or []) == count]


def _consumer_owners(evidence):
    owners = evidence.get("enclosing_functions") or [
        d for d in evidence.get("scopes", []) if d.get("kind") == "function"]
    return {d.get("qualified_name", "").rsplit(".", 1)[0]
            for d in owners if "." in d.get("qualified_name", "")}


def declaration_prerequisites(summaries):
    """Prove only newly introduced, lexically scoped declaration uses."""
    result = []
    for provider in summaries:
        structure = provider.get("source_structure", {})
        before, after = structure.get("before", {}), structure.get("after", {})
        if before.get("status") != "ok" or after.get("status") != "ok":
            continue
        old_declarations = {d["qualified_name"] for d in before.get("declarations", [])}
        for declaration in after.get("declarations", []):
            qualified = declaration.get("qualified_name", "")
            name = declaration.get("name", "")
            if not name or qualified in old_declarations or "." not in qualified:
                continue
            owner = qualified.rsplit(".", 1)[0]
            for consumer in summaries:
                if provider["hunk"] == consumer["hunk"]:
                    continue
                evidence = consumer.get("source_structure", {})
                old, new = evidence.get("before", {}), evidence.get("after", {})
                if (new.get("path") != after.get("path") or owner not in _consumer_owners(new)
                        or name not in new.get("identifiers", [])
                        or name in old.get("identifiers", [])):
                    continue
                result.append({"kind": "declaration_prerequisite", "provider": provider["hunk"],
                               "consumer": consumer["hunk"], "symbol": qualified,
                               "reason": "New scoped declaration used by this hunk: " + qualified})
        for change in structure.get("signature_changes", []):
            if change.get("kind") != "addition" or change.get("parse_error"):
                continue
            name, types = change.get("name", ""), change.get("new_types")
            scope = change.get("scope", "")
            if not name or types is None or "." not in scope:
                continue
            owner = scope.rsplit(".", 1)[0]
            short_owner = owner.rsplit(".", 1)[-1]
            allowed_receivers = {"", "this", short_owner, short_owner + ".this"}
            for consumer in summaries:
                if provider["hunk"] == consumer["hunk"]:
                    continue
                evidence = consumer.get("source_structure", {})
                old, new = evidence.get("before", {}), evidence.get("after", {})
                if (new.get("path") != after.get("path") or owner not in _consumer_owners(new)
                        or any((call.get("receiver") or "") in allowed_receivers
                               for call in _named_calls(old, name))):
                    continue
                calls = [call for call in _matching_calls(new, name, len(types))
                         if (call.get("receiver") or "") in allowed_receivers]
                if calls:
                    result.append({"kind": "definition_prerequisite", "provider": provider["hunk"],
                                   "consumer": consumer["hunk"], "symbol": scope,
                                   "calls": [call["expression"] for call in calls],
                                   "reason": "New scoped method definition called by this hunk: " + scope})
    return result


def signature_prerequisites(summaries):
    """Bind direct replacements and overload adaptations in a lexical owner."""
    result = []
    for provider in summaries:
        structure = provider.get("source_structure", {})
        after = structure.get("after", {})
        for change in structure.get("signature_changes", []):
            kind = change.get("kind")
            old_types, new_types = change.get("old_types"), change.get("new_types")
            name, scope = change.get("name", ""), change.get("scope", "")
            if (kind not in {"replacement", "addition"} or change.get("parse_error")
                    or new_types is None or not name or "." not in scope
                    or (kind == "replacement" and old_types is None)
                    or (kind == "addition" and old_types is not None)):
                continue
            owner = scope.rsplit(".", 1)[0]
            short_owner = owner.rsplit(".", 1)[-1]
            allowed_receivers = {"", "this", short_owner + ".this"}
            for consumer in summaries:
                if provider["hunk"] == consumer["hunk"]:
                    continue
                evidence = consumer.get("source_structure", {})
                old, new = evidence.get("before", {}), evidence.get("after", {})
                if (new.get("path") != after.get("path") or not any(
                        owner == candidate or candidate.startswith(owner + ".")
                        for candidate in _consumer_owners(new))):
                    continue
                old_calls = (_matching_calls(old, name, len(old_types)) if kind == "replacement"
                             else _named_calls(old, name))
                new_calls = _matching_calls(new, name, len(new_types))
                direct = [(old, new) for old in old_calls for new in new_calls
                          if old.get("receiver") == new.get("receiver")
                          and new.get("receiver") in allowed_receivers
                          and (old.get("arguments") or []) == (new.get("arguments") or [])[:len(old.get("arguments") or [])]]
                if direct:
                    edge = {"kind": "signature_prerequisite", "provider": provider["hunk"],
                            "consumer": consumer["hunk"], "symbol": scope,
                            "calls": [new["expression"] for _, new in direct],
                            "reason": "Lexically bound old-to-new %s adaptation: %s" % (
                                "signature" if kind == "replacement" else "overload", scope)}
                    if kind == "addition":
                        edge["retain_consumer"] = True
                    result.append(edge)
    return result


def _changed_lines(hunk, marker):
    return [line[1:] for line in hunk.get("text", "").splitlines()
            if line.startswith(marker) and not line.startswith(marker * 3)]


def deletion_prerequisites(hunks):
    """Prove aconfig declaration cleanup follows removal of its code reference."""
    result = []
    for declaration in hunks:
        if not declaration.get("file", "").endswith(".aconfig") or _changed_lines(declaration, "+"):
            continue
        names = set(re.findall(r'\bname\s*:\s*"([A-Za-z_]\w*)"',
                               "\n".join(_changed_lines(declaration, "-"))))
        if len(names) != 1:
            continue
        flag = names.pop()
        parts = flag.split("_")
        variants = [flag, parts[0] + "".join(part.capitalize() for part in parts[1:])]
        consumer = declaration.get("idx", hunks.index(declaration))
        for provider in hunks:
            if provider is declaration or provider.get("file", "").endswith(".aconfig"):
                continue
            old = "\n".join(_changed_lines(provider, "-"))
            new = "\n".join(_changed_lines(provider, "+"))
            if any(re.search(r"\b" + re.escape(name) + r"\b", old)
                   and not re.search(r"\b" + re.escape(name) + r"\b", new)
                   for name in variants):
                result.append({"kind": "deletion_prerequisite",
                               "provider": provider.get("idx", hunks.index(provider)),
                               "consumer": consumer, "symbol": flag,
                               "reason": "Removed flag reference precedes declaration cleanup: " + flag})
    return result


def mechanical_prerequisites(summaries, hunks):
    unique = {}
    for edge in (syntax_prerequisites(summaries) + declaration_prerequisites(summaries)
                 + signature_prerequisites(summaries) + deletion_prerequisites(hunks)):
        unique.setdefault((edge["provider"], edge["consumer"], edge["kind"], edge["reason"]), edge)
    return sorted(unique.values(), key=lambda e: (e["provider"], e["consumer"], e["kind"], e["reason"]))


def _reaches(edges, start, target):
    adjacency = {}
    for edge in edges:
        adjacency.setdefault(edge["provider"], set()).add(edge["consumer"])
    pending, seen = [start], set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(adjacency.get(current, set()) - seen)
    return False


def canonical_prerequisites(edges):
    """Remove only reviewed edges whose reachability is preserved by another path."""
    result = list(edges)
    candidates = sorted((edge for edge in result
                         if edge.get("kind", "reviewed_prerequisite") == "reviewed_prerequisite"
                         and not edge.get("retain_consumer")),
                        key=lambda e: (e["provider"], e["consumer"], e["reason"]))
    for edge in candidates:
        remaining = [candidate for candidate in result if candidate is not edge]
        if len(remaining) != len(result) and _reaches(remaining, edge["provider"], edge["consumer"]):
            result = remaining
    return sorted(result, key=lambda e: (e["provider"], e["consumer"], e["kind"], e["reason"]))


def review_inputs(rows, hunks, summaries):
    by_hunk = {h.get("idx", i): h for i, h in enumerate(hunks)}
    decisions = {r["hunk"]: r.get("initial_decision", r).get("port") for r in rows}
    sources = {r["hunk"]: r.get("initial_decision", r).get("decision_source") for r in rows}
    records = []
    for i, summary in enumerate(summaries):
        h = summary.get("hunk", i)
        source = summary.get("source_structure", {})
        records.append({"hunk": h, "file": by_hunk[h].get("file", summary.get("file")),
                        "initial_port": decisions[h], "decision_source": sources[h],
                        "summary": summary.get("summary", ""),
                        "semantic_contract": summary.get("semantic_contract", {}),
                        "diff": by_hunk[h]["text"], "source_structure": {
                            side: {k: source.get(side, {}).get(k) for k in
                                   ("status", "reason", "scopes", "calls", "declarations", "writes",
                                    "imports", "import_uses", "statements", "context", "context_truncated")}
                            for side in ("before", "after")}})
    return {"hunks": records, "mechanical_prerequisites": mechanical_prerequisites(summaries, hunks)}


def _check_quote(evidence, diff):
    if not isinstance(evidence, dict) or evidence.get("side") not in {"before", "after"}:
        raise ValueError("dependency evidence needs before/after side")
    quote = evidence.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("dependency evidence needs a code quote")
    marker = "-" if evidence["side"] == "before" else "+"
    source, changed = [], set()
    active = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            active = True
        elif active and line[:1] in {" ", marker}:
            source.append(line[1:].strip())
            if line.startswith(marker) and line[1:].strip():
                changed.add(line[1:].strip())
    wanted = [line.strip() for line in quote.strip().splitlines()]
    if (not changed.intersection(wanted) or
            not any(source[i:i+len(wanted)] == wanted for i in range(len(source)-len(wanted)+1))):
        raise ValueError("dependency quote must match contiguous diff code and include a changed line")


def validate_review(answer, inputs):
    records = {r["hunk"]: r for r in inputs["hunks"]}
    locked = {(e["provider"], e["consumer"]) for e in inputs.get("mechanical_prerequisites", [])}
    reviews = answer.get("reviews") if isinstance(answer, dict) else None
    if not isinstance(reviews, list):
        raise ValueError("dependency review must contain reviews")
    ids = [r.get("hunk") for r in reviews if isinstance(r, dict)]
    if (len(ids) != len(reviews) or any(type(h) is not int for h in ids)
            or len(ids) != len(set(ids)) or set(ids) != set(records)):
        raise ValueError("review every original hunk exactly once")
    for row in reviews:
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("dependency review requires a reason, including empty prerequisites")
        if not isinstance(row.get("requires"), list):
            raise ValueError("requires must be a list")
        seen = set()
        for edge in row["requires"]:
            provider = edge.get("provider")
            if (type(provider) is not int or provider not in records or provider == row["hunk"]
                    or provider in seen):
                raise ValueError("invalid or duplicate prerequisite hunk")
            if (provider, row["hunk"]) in locked:
                raise ValueError("mechanical prerequisite cannot be repeated by the model: %s -> %s" %
                                 (provider, row["hunk"]))
            if (records[provider].get("decision_source") == "deterministic_config_cleanup"
                    and records[row["hunk"]].get("decision_source") != "deterministic_config_cleanup"):
                raise ValueError("config cleanup cannot be a prerequisite of the behavior fix")
            seen.add(provider)
            if type(edge.get("retain_consumer", False)) is not bool:
                raise ValueError("retain_consumer must be boolean")
            if not isinstance(edge.get("reason"), str) or not edge["reason"].strip():
                raise ValueError("prerequisite requires a concrete reason")
            _check_quote(edge.get("provider_evidence"), records[provider]["diff"])
            _check_quote(edge.get("consumer_evidence"), records[row["hunk"]]["diff"])
    return {r["hunk"]: r for r in reviews}


def _with_mechanical(reviews, inputs):
    locked = {}
    for edge in inputs["mechanical_prerequisites"]:
        locked.setdefault(edge["consumer"], []).append(edge)
    return {h: {**review, "requires": locked.get(h, []) + list(review["requires"])}
            for h, review in reviews.items()}


def review_dependencies(rows, hunks, summaries, ask, retries=1):
    inputs = review_inputs(rows, hunks, summaries)
    if len(hunks) < 2 or not any(r.get("port") is True for r in rows):
        reviews = {r["hunk"]: {"hunk": r["hunk"], "requires": [],
                               "reason": "No selected behavioral dependency to review"} for r in rows}
        return _with_mechanical(reviews, inputs)
    prompt = REVIEW_PROMPT + "\n" + json.dumps(inputs, ensure_ascii=False)
    feedback = ""
    for attempt in range(retries + 1):
        answer = ask(prompt + feedback)
        try:
            return _with_mechanical(validate_review(answer, inputs), inputs)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            if attempt == retries:
                raise ValueError("TRIAGE_DEPENDENCY_INVALID: %s" % exc) from exc
            feedback = "\n上次回答未通过检查：%s。重新输出完整 reviews，且不要重复机械边。\n上次回答：%s" % (
                exc, json.dumps(answer, ensure_ascii=False))


def attach_reviews(summaries, hunks, rows):
    """Bind persisted reviews to these exact inputs before grouping uses them."""
    if not any("dependency_review" in r for r in rows):
        return  # Legacy triage files still use the grouping-stage review.
    decisions = {r["hunk"]: r for r in rows}
    for summary, hunk in zip(summaries, hunks):
        row = decisions[summary["hunk"]]
        review = row.get("dependency_review")
        if not isinstance(review, dict) or row.get("dependency_fingerprint") != fingerprint(summary, hunk):
            raise ValueError("triage dependency review does not match this patch/revision")
        for edge in review["requires"]:
            if row.get("port") is True and (edge["provider"] not in decisions
                                            or decisions[edge["provider"]].get("port") is not True):
                raise ValueError("selected hunk is missing its reviewed prerequisite")
        summary["dependency_review"] = review


def reviewed_prerequisites(summaries):
    return [{**edge, "consumer": r["hunk"], "kind": edge.get("kind", "reviewed_prerequisite")}
            for r in summaries for edge in r.get("dependency_review", {}).get("requires", [])]
