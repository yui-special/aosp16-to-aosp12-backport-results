"""Dependencies over the complete patch, before relevance filtering.

Syntax proves a small subset. Semantic edges require quotes from both changed
hunks and are carried into grouping, rather than independently guessed again.
These edges express prerequisites, not instructions to merge whole API families.
"""
import hashlib
import json

from tools.source_structure import structure_relations


REVIEW_PROMPT = """核对整份补丁的直接依赖。输入包含所有原始 hunk，包括初步判为 skip 的 hunk。
initial_port 只是相关性初判，不是依赖证据。摘要用于解释意图，以 diff 和 source_structure 为准。
对每个 hunk 判断：实施该修改时，哪些其他 hunk 的改动必须已经存在？

规则：
- requires 是单向先后依赖，不表示必须合组。只共享名称、所在方法或修复目标不能建立依赖。
- 调用依赖要核对接收者、作用域、签名和实参；不允许按常见方法的裸名称连接整个补丁。
- 导入、声明、初始化或输出修改本身可能不修复漏洞，但若被保留的逻辑/测试需要，必须列为前提。
  无关导入、日志和测试设施不因与修复同文件就保留。
- 测试逐条查看完整断言和预期值，指出真正实现该可观察行为的修改。
  声明/赋值不自动等于输出该值；不同接收者、字段或输出文本不能互相替代。
  断言输出文本时，核对生产代码实际输出的精确文本以及被测对象；不能只找相似字段名。
- 移除功能开关的条件判断可以是修复；删除不再被引用的开关声明通常是独立清理。
  顺序是先移除引用，再删除声明，不能倒置依赖，让修复强制依赖清理。
- unsupported/partial/missing 表示缺少语法证据，不表示无依赖；继续核对 diff，不能猜出类型解析结果。
- 不存在跨 hunk 前提时 requires=[] 并说明原因。不要为了让所有修改相关而补边。
- 每条依赖引用 provider 和 consumer 各自 diff 中的真实代码，去掉 diff 行首 +/-/空格标记。
  quote 必须是完整连续代码行，并至少包含该侧一条修改行；不要引用摘要、hunk 标题或省略号。
  通常使用 after；删除引用等场景可使用 before。reason 说明为什么两段代码构成直接前提。

只输出 JSON，reviews 必须覆盖每个输入 hunk 恰好一次：
{"reviews":[{"hunk":0,"reason":"该 hunk 的前提判断", "requires":[
  {"provider":1,"reason":"具体依赖关系",
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


def review_inputs(rows, hunks, summaries):
    by_hunk = {h.get("idx", i): h for i, h in enumerate(hunks)}
    decisions = {r["hunk"]: r.get("initial_decision", r).get("port") for r in rows}
    records = []
    for i, summary in enumerate(summaries):
        h = summary.get("hunk", i)
        source = summary.get("source_structure", {})
        records.append({"hunk": h, "file": by_hunk[h].get("file", summary.get("file")),
                        "initial_port": decisions[h], "summary": summary.get("summary", ""),
                        "semantic_contract": summary.get("semantic_contract", {}),
                        "diff": by_hunk[h]["text"], "source_structure": {
                            side: {k: source.get(side, {}).get(k) for k in
                                   ("status", "reason", "scopes", "calls", "declarations", "writes",
                                    "imports", "import_uses", "statements", "context", "context_truncated")}
                            for side in ("before", "after")}})
    return {"hunks": records, "syntax_prerequisites": syntax_prerequisites(summaries)}


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
            seen.add(provider)
            if not isinstance(edge.get("reason"), str) or not edge["reason"].strip():
                raise ValueError("prerequisite requires a concrete reason")
            _check_quote(edge.get("provider_evidence"), records[provider]["diff"])
            _check_quote(edge.get("consumer_evidence"), records[row["hunk"]]["diff"])
    return {r["hunk"]: r for r in reviews}


def review_dependencies(rows, hunks, summaries, ask, retries=1):
    inputs = review_inputs(rows, hunks, summaries)
    if len(hunks) < 2 or not any(r.get("port") is True for r in rows):
        return {r["hunk"]: {"hunk": r["hunk"], "requires": [],
                            "reason": "No selected cross-hunk dependency to review"} for r in rows}
    prompt = REVIEW_PROMPT + "\n" + json.dumps(inputs, ensure_ascii=False)
    feedback = ""
    for attempt in range(retries + 1):
        answer = ask(prompt + feedback)
        try:
            return validate_review(answer, inputs)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            if attempt == retries:
                raise ValueError("TRIAGE_DEPENDENCY_INVALID: %s" % exc) from exc
            feedback = "\n上次回答未通过检查：%s。重新输出完整 reviews。\n上次回答：%s" % (
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
    return [{**edge, "consumer": r["hunk"], "kind": "reviewed_prerequisite"}
            for r in summaries for edge in r.get("dependency_review", {}).get("requires", [])]
