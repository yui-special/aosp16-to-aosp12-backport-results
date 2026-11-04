"""Optional config-declaration/capability relations for grouping.

This module is intentionally independent of triage and the grouping planner. It
returns empty results when a patch has no high-confidence config declaration and
generated accessor pair.
"""
import os
import re


def _enabled():
    return os.environ.get("ENABLE_CONFIG_ANCHORS", "1") != "0"


def canonical_name(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _added_text(hunk):
    return "\n".join(
        line[1:] for line in hunk["text"].splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _relation_text(hunk, summary):
    contract = summary.get("semantic_contract") or {}
    parts = [_added_text(hunk)]
    for key in (
        "inputs", "callees", "symbol_relations", "prerequisite_semantics",
        "state_read", "obligations",
    ):
        value = contract.get(key) or []
        if isinstance(value, str):
            value = [value]
        parts.extend(str(item) for item in value)
    return "\n".join(parts)


def extract_relations(summaries, hunks):
    """Return only exact config-name to generated-accessor relations."""
    if not _enabled():
        return []
    by_id = {hunk["idx"]: hunk for hunk in hunks}
    providers = {}
    for summary in summaries:
        hunk = by_id.get(summary["hunk"])
        if not hunk or not str(hunk.get("file", "")).endswith(".aconfig"):
            continue
        added = _added_text(hunk)
        names = re.findall(
            r'\bname\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"', added
        )
        if not names:
            continue
        package_match = re.search(r'\bpackage\s*:\s*"([^"]+)"', hunk["text"])
        namespace_match = re.search(r'\bnamespace\s*:\s*"([^"]+)"', hunk["text"])
        fixed_read_only = bool(re.search(
            r"\bis_fixed_read_only\s*:\s*true\b", hunk["text"]
        ))
        for name in names:
            capability = "config:" + canonical_name(name)
            providers.setdefault(capability, []).append({
                "hunk": summary["hunk"],
                "name": name,
                "package": package_match.group(1) if package_match else "",
                "namespace": namespace_match.group(1) if namespace_match else "",
                "fixed_read_only": fixed_read_only,
                "file": hunk["file"],
            })

    relations = []
    seen = set()
    for summary in summaries:
        hunk = by_id.get(summary["hunk"])
        if not hunk:
            continue
        accessors = re.findall(
            r"\bFlags\.([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            _relation_text(hunk, summary),
        )
        for accessor in sorted(set(accessors)):
            capability = "config:" + canonical_name(accessor)
            candidates = {
                item["hunk"]: item for item in providers.get(capability, [])
                if item["hunk"] != summary["hunk"]
            }
            # Ambiguous declarations must not create a guessed edge.
            if len(candidates) != 1:
                continue
            provider = next(iter(candidates.values()))
            key = (capability, provider["hunk"], summary["hunk"])
            if key in seen:
                continue
            seen.add(key)
            relations.append({
                "id": capability,
                "provider": provider["hunk"],
                "consumer": summary["hunk"],
                "declaration": provider["name"],
                "accessor": accessor,
                "package": provider["package"],
                "namespace": provider["namespace"],
                "fixed_read_only": provider["fixed_read_only"],
                "resolution": (
                    "INLINE_TRUE" if provider["fixed_read_only"] else "UNRESOLVED"
                ),
                "consumer_file": hunk["file"],
                "evidence": "config declaration provides generated Flags accessor",
            })
    return relations


def annotate_summaries(summaries, hunks):
    """Attach relation metadata without changing the summary schema by default."""
    relations = extract_relations(summaries, hunks)
    provides, requires = {}, {}
    for relation in relations:
        provides.setdefault(relation["provider"], []).append(relation)
        requires.setdefault(relation["consumer"], []).append(relation)
    for summary in summaries:
        idx = summary["hunk"]
        if idx in provides:
            summary["provides_config_capabilities"] = provides[idx]
        if idx in requires:
            summary["requires_config_capabilities"] = requires[idx]
    return relations


def promote_triage_consumers(rows, summaries):
    """Close config relations in either direction."""
    if not _enabled():
        return rows
    by_hunk = {row["hunk"]: row for row in rows}

    def promote(row, category, state, capability):
        if not row or row.get("port") is True:
            return
        row["port"] = True
        row["category"] = category
        row["needs_semantic_mapping"] = True
        row["review_required"] = False
        row["closure_symbols"] = []
        row["triage_state"] = state
        suffix = "关联 config capability %s" % capability
        if suffix not in (row.get("reason") or ""):
            row["reason"] = ((row.get("reason") or "") + " | " + suffix).strip(" |")

    for summary in summaries:
        for relation in summary.get("requires_config_capabilities") or []:
            provider = by_hunk.get(relation["provider"])
            consumer = by_hunk.get(relation["consumer"])
            if not provider or not consumer:
                continue
            if consumer.get("port") is True:
                promote(
                    provider,
                    "metadata",
                    "capability_provider_required",
                    relation["id"],
                )
            if provider.get("port") is True:
                promote(
                    consumer,
                    "fix",
                    "capability_consumer_required",
                    relation["id"],
                )
            if (
                provider.get("port") is not True
                and consumer.get("port") is not True
                and (
                    provider.get("review_required") is True
                    or consumer.get("review_required") is True
                )
            ):
                # A high-confidence config declaration/accessor pair must not
                # disappear merely because both endpoints were initially
                # skipped. Symbol closure already marked one endpoint as
                # review-worthy; promote the whole capability pair to review.
                promote(
                    provider,
                    "metadata",
                    "capability_provider_required",
                    relation["id"],
                )
                promote(
                    consumer,
                    "fix",
                    "capability_consumer_required",
                    relation["id"],
                )
    return rows


def group_bindings(summaries):
    """Turn high-confidence config relations into bounded grouping constraints."""
    if not _enabled():
        return []
    max_hunks = max(1, int(os.environ.get("CONFIG_ANCHOR_MAX_HUNKS", "6")))
    max_consumers = max(1, int(os.environ.get("CONFIG_ANCHOR_MAX_CONSUMERS", "4")))
    by_hunk = {summary["hunk"]: summary for summary in summaries}
    available_ids = set(by_hunk)
    by_provider = {}
    for summary in summaries:
        for relation in summary.get("provides_config_capabilities") or []:
            key = (relation["id"], relation["provider"])
            by_provider.setdefault(key, {
                "id": relation["id"],
                "provider": relation["provider"],
                "consumers": set(),
                "evidence": relation["evidence"],
            })["consumers"].add(relation["consumer"])
    bindings = []
    for item in by_provider.values():
        consumers = sorted(item["consumers"] & available_ids)
        if item["provider"] not in available_ids or not consumers:
            continue
        can_bind = (
            len(consumers) <= max_consumers
            and 1 + len(consumers) <= max_hunks
        )
        for consumer in consumers:
            consumer_file = str((by_hunk.get(consumer) or {}).get("file") or "")
            test_consumer = bool(re.search(
                r"(?:^|/)(?:test|tests)/|\bTest(?:s)?\.|_test\.", consumer_file
            ))
            bindings.append({
                "id": item["id"],
                "provider": item["provider"],
                "consumer": consumer,
                "binding": can_bind and not test_consumer,
                "reason": item["evidence"],
            })
    return bindings
