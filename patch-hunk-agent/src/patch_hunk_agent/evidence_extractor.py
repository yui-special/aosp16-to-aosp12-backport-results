"""Deterministic, conservative evidence extraction (phases A and B).

This module never decides that a dependency exists.  It produces *candidate*
symbols, definitions, references and pairwise signals that are handed to the
LLM analyzer together with the code context.  Anything produced here is a
hypothesis with a locatable code position attached.
"""

from __future__ import annotations

import re
from typing import Any
from collections.abc import Iterable

from .schemas import canonical_pair, hunk_sort_key

__all__ = [
    "RESERVED_WORDS",
    "strip_strings_and_comments",
    "classify_definition",
    "parameter_text",
    "function_scope",
    "extract_symbols",
    "enrich_hunks",
    "signature_changes",
    "touched_functions",
    "extract",
    "summarize_hunk",
    "merge_symbol_evidence",
]

RESERVED_WORDS = {
    # C / C++
    "auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else",
    "enum", "extern", "float", "for", "goto", "if", "inline", "int", "long", "register",
    "restrict", "return", "short", "signed", "sizeof", "static", "struct", "switch", "typedef",
    "union", "unsigned", "void", "volatile", "while", "class", "namespace", "template",
    "typename", "public", "private", "protected", "new", "delete", "this", "nullptr", "bool",
    "true", "false", "try", "catch", "throw", "using", "operator", "virtual", "explicit",
    "friend", "mutable", "constexpr", "noexcept", "static_assert", "assert", # Java / C# / Python / JS keywords that show up in multi-language repos
    "def", "lambda", "pass", "raise", "except", "finally", "import", "from", "as", "with",
    "yield", "global", "nonlocal", "await", "async", "in", "is", "not", "and", "or", "None",
    "True", "False", "print", "elif", "function", "var", "let", "undefined", "typeof",
    "instanceof", "extends", "implements", "interface", "package", "throws", "synchronized",
    "final", "abstract", "super", "null", "String", "Integer", "Object", "override",
}

_DEFINITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("macro", re.compile(r"^\s*#\s*define\s+(?P<name>[A-Za-z_]\w*)")),
    ("type", re.compile(r"^\s*(?:typedef\s+)?(?:struct|union|enum|class)\s+(?P<name>[A-Za-z_]\w*)\b")),
    ("typedef", re.compile(r"^\s*typedef\b[^;{]*?\b(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;")),
    ("python_def", re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\(")),
    ("function", re.compile(
        r"^\s*(?:[A-Za-z_]\w*[\s\*&]+)+?(?P<name>[A-Za-z_]\w*)\s*\([^;{)]*\)\s*(?:const\s*)?(?:\{|$)"
    )),
    ("variable", re.compile(
        r"^\s*(?:(?:static|const|extern|volatile|register|unsigned|signed|struct\s+\w+|enum\s+\w+|"
        r"[A-Za-z_]\w*(?:\s*<[^>]*>)?)[\s\*]+)+(?P<name>[A-Za-z_]\w*)\s*(?:=|;|\[|,|\))"
    )),
    ("field", re.compile(r"^\s*[A-Za-z_][\w\s\*&:<>,\[\]]*\s(?P<name>[A-Za-z_]\w*)\s*;\s*$")),
)

_CALL_RE = re.compile(r"\b(?P<name>[A-Za-z_]\w*)\s*\(")
_ASSIGN_RE = re.compile(r"(?:^|[^\w.>])(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:=|\+=|-=|\*=|/=|\|=|&=|\^=)(?!=)")
_IDENT_RE = re.compile(r"\b(?P<name>[A-Za-z_]\w*)\b")

# A line starting with one of these is a statement, never a declaration.
_STATEMENT_KEYWORDS = {
    "return", "if", "else", "while", "for", "do", "switch", "case", "default", "goto", "break",
    "continue", "sizeof", "assert", "throw", "raise", "yield", "await", "pass", "del",
    "try", "except", "finally", "with", "import", "from", "print", "log", "LOG", "DEBUG",
}


def strip_strings_and_comments(line: str, in_block_comment: bool = False) -> tuple[str, bool]:
    """Remove comments and string bodies so regexes only see real code.

    Preprocessor directives are preserved verbatim: macro names matter here.
    """
    out: list[str] = []
    i = 0
    length = len(line)
    while i < length:
        if in_block_comment:
            end = line.find("*/", i)
            if end == -1:
                return "".join(out), True
            i = end + 2
            in_block_comment = False
            continue
        char = line[i]
        if char == "/" and i + 1 < length and line[i + 1] == "*":
            in_block_comment = True
            i += 2
            continue
        if char == "/" and i + 1 < length and line[i + 1] == "/":
            break
        if char == "#":
            out.append(line[i:])
            break
        if char in "\"'":
            quote = char
            i += 1
            while i < length:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == quote:
                    i += 1
                    break
                i += 1
            out.append('""')
            continue
        out.append(char)
        i += 1
    return "".join(out), in_block_comment


def classify_definition(code_line: str) -> tuple[str, str] | None:
    """Return ``(kind, name)`` when a code line looks like a definition."""
    first = re.match(r"\s*([A-Za-z_]\w*)", code_line)
    if first and first.group(1) in _STATEMENT_KEYWORDS:
        return None
    for kind, pattern in _DEFINITION_PATTERNS:
        match = pattern.match(code_line)
        if not match:
            continue
        name = match.group("name")
        if name in RESERVED_WORDS:
            continue
        return kind, name
    return None


def parameter_text(code_line: str) -> str | None:
    """Return the parenthesised parameter list of a declaration line."""
    start = code_line.find("(")
    if start == -1:
        return None
    depth = 0
    for index in range(start, len(code_line)):
        char = code_line[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return code_line[start : index + 1]
    return code_line[start:]


_FUNCTION_HEADER_RE = re.compile(
    r"^\s*(?:[A-Za-z_]\w*[\s\*&]+)+?(?P<name>[A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?$"
)


def function_scope(
    lines: list[str], index: int, *, scan_limit: int = 400
) -> dict[str, Any] | None:
    """Best-effort enclosing function lookup for line ``index`` (0-based).

    Uses brace depth plus a function-header shape check.  Returns
    ``{"name", "start_line", "end_line", "confidence"}`` or ``None``.
    """
    if not lines or index < 0 or index >= len(lines):
        return None
    cleaned: list[str] = []
    in_block = False
    for line in lines:
        code, in_block = strip_strings_and_comments(line, in_block)
        cleaned.append(code)

    low = max(0, index - scan_limit)
    header_index: int | None = None
    name: str | None = None
    for probe in range(index, low - 1, -1):
        match = _FUNCTION_HEADER_RE.match(cleaned[probe])
        if not match:
            continue
        candidate = match.group("name")
        if candidate in RESERVED_WORDS:
            continue
        # The body must open shortly after the header to be a definition.
        nxt = probe + 1
        while nxt < len(lines) and not cleaned[nxt].strip():
            nxt += 1
        if nxt >= len(lines) or not cleaned[nxt].lstrip().startswith("{"):
            continue
        header_index, name = probe, candidate
        break
    if header_index is None or name is None:
        return None

    depth = 0
    opened = False
    end_index = len(lines) - 1
    for probe in range(header_index, min(len(lines), header_index + scan_limit)):
        for char in cleaned[probe]:
            if char == "{":
                depth += 1
                opened = True
            elif char == "}":
                depth -= 1
        if opened and depth <= 0:
            end_index = probe
            break
    return {
        "name": name,
        "start_line": header_index + 1,
        "end_line": end_index + 1,
        "confidence": "high" if header_index >= max(0, index - 30) else "medium",
    }


def _iter_body(hunk: dict) -> Iterable[dict]:
    line_map = hunk.get("line_map")
    if line_map:
        yield from line_map
        return
    new_line = hunk.get("new_start", 1)
    old_line = hunk.get("old_start", 1)
    for text in hunk.get("context_lines") or []:
        yield {"kind": "context", "old_line": old_line, "new_line": new_line, "text": text}
        old_line += 1
        new_line += 1
    for text in hunk.get("removed_lines") or []:
        yield {"kind": "del", "old_line": old_line, "new_line": None, "text": text}
        old_line += 1
    for text in hunk.get("added_lines") or []:
        yield {"kind": "add", "old_line": None, "new_line": new_line, "text": text}
        new_line += 1


def _entry(
    name: str,
    role: str,
    *,
    kind: str | None = None,
    use_kind: str | None = None,
    lines: dict | None = None,
    text: str = "",
    confidence: str = "medium",
    before: str | None = None,
    after: str | None = None,
) -> dict:
    entry = {
        "name": name,
        "role": role,
        "kind": kind,
        "use_kind": use_kind,
        "old_line": (lines or {}).get("old_line"),
        "new_line": (lines or {}).get("new_line"),
        "text": text,
        "confidence": confidence,
    }
    entry["line"] = entry["new_line"] if entry["new_line"] is not None else entry["old_line"]
    if before is not None:
        entry["before"] = before
    if after is not None:
        entry["after"] = after
    return entry


def extract_symbols(hunk: dict) -> list[dict]:
    """Symbols added, removed or referenced by a single hunk."""
    symbols: list[dict] = []
    in_block = False
    for position in _iter_body(hunk):
        text = position.get("text") or ""
        code, in_block = strip_strings_and_comments(text, in_block)
        lines = {"old_line": position.get("old_line"), "new_line": position.get("new_line")}
        kind = position.get("kind")
        if kind == "no_newline":
            continue
        definition = classify_definition(code) if kind in ("add", "del") else None
        declared = definition[1] if definition else None
        if kind == "add" and definition:
            symbols.append(
                _entry(
                    declared,
                    "definition",
                    kind=definition[0],
                    lines=lines,
                    text=text,
                    confidence="high",
                    after=code.strip(),
                )
            )
        elif kind == "del" and definition:
            symbols.append(
                _entry(
                    declared,
                    "deletion",
                    kind=definition[0],
                    lines=lines,
                    text=text,
                    confidence="high",
                    before=code.strip(),
                )
            )
        if kind == "del":
            continue

        calls = [match.group("name") for match in _CALL_RE.finditer(code)]
        writes = [match.group("name") for match in _ASSIGN_RE.finditer(code)]
        if code.lstrip().startswith("#"):
            # Preprocessor directive: names in the directive are not code references.
            calls = []
            writes = []
        for name in calls:
            if name in RESERVED_WORDS or name == declared:
                continue
            symbols.append(
                _entry(
                    name,
                    "reference",
                    use_kind="call",
                    lines=lines,
                    text=text,
                    confidence="high" if kind == "add" else "medium",
                )
            )
        for name in writes:
            if name in RESERVED_WORDS or name == declared:
                continue
            symbols.append(
                _entry(
                    name,
                    "reference",
                    use_kind="write",
                    lines=lines,
                    text=text,
                    confidence="high",
                )
            )
        covered = set(calls) | set(writes)
        if declared:
            covered.add(declared)
        if code.lstrip().startswith("#"):
            continue
        for match in _IDENT_RE.finditer(code):
            name = match.group("name")
            if name in RESERVED_WORDS or name in covered:
                continue
            symbols.append(
                _entry(
                    name,
                    "reference",
                    use_kind="read",
                    lines=lines,
                    text=text,
                    confidence="low",
                )
            )
    return _dedupe_symbols(symbols)


def _symbol_key(entry: dict) -> tuple:
    return (
        entry.get("name"),
        entry.get("role"),
        entry.get("use_kind"),
        entry.get("line"),
    )


def _dedupe_symbols(entries: list[dict]) -> list[dict]:
    best: dict[tuple, dict] = {}
    order: list[tuple] = []
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    for entry in entries:
        key = _symbol_key(entry)
        if key not in best:
            best[key] = entry
            order.append(key)
            continue
        current = best[key]
        if confidence_rank.get(entry.get("confidence", "low"), 0) > confidence_rank.get(
            current.get("confidence", "low"), 0
        ):
            best[key] = entry
    return [best[key] for key in order]


def signature_changes(hunk: dict) -> list[dict]:
    """Detect a changed declaration/definition signature inside one hunk."""
    changes: list[dict] = []
    incoming: dict[str, dict] = {}
    for entry in hunk.get("symbols") or extract_symbols(hunk):
        if entry.get("role") == "definition" and entry.get("kind") in ("function", "python_def"):
            incoming[entry["name"]] = entry
    for entry in hunk.get("symbols") or extract_symbols(hunk):
        if entry.get("role") != "deletion" or entry.get("kind") not in ("function", "python_def"):
            continue
        name = entry["name"]
        added = incoming.get(name)
        if not added:
            continue
        before_params = parameter_text(entry.get("text", ""))
        after_params = parameter_text(added.get("text", ""))
        if before_params is None or after_params is None or before_params == after_params:
            continue
        changes.append(
            {
                "name": name,
                "kind": "function",
                "before": entry.get("text", "").strip(),
                "after": added.get("text", "").strip(),
                "before_line": entry.get("line"),
                "after_line": added.get("line"),
                "before_params": before_params,
                "after_params": after_params,
            }
        )
    return changes


def touched_functions(hunk: dict, context: dict | None = None) -> list[str]:
    """Function names this hunk lives inside (from the section header or scope)."""
    names: list[str] = []
    section = (hunk.get("section") or "").strip()
    if section:
        match = re.search(r"([A-Za-z_]\w*)\s*\(", section)
        candidate = match.group(1) if match else ""
        if not candidate:
            bare = re.search(r"([A-Za-z_]\w*)", section)
            candidate = bare.group(1) if bare else ""
        if candidate and candidate not in RESERVED_WORDS:
            names.append(candidate)
    if context:
        for side in ("source_context", "target_context"):
            scope = (context.get(side) or {}).get("scope_function")
            if scope and scope not in names:
                names.append(scope)
    return names


def summarize_hunk(hunk: dict, context: dict | None = None) -> dict:
    """Compact, prompt-friendly view of a hunk."""
    symbols = hunk.get("symbols") or extract_symbols(hunk)
    grouped: dict[str, dict[str, list[dict]]] = {}
    for entry in symbols:
        role = entry.get("role") or "reference"
        grouped.setdefault(role, {}).setdefault(entry["name"], []).append(
            {
                "kind": entry.get("kind"),
                "use_kind": entry.get("use_kind"),
                "line": entry.get("line"),
                "confidence": entry.get("confidence"),
            }
        )
    summary = {
        "hunk_id": hunk.get("hunk_id"),
        "file_path": hunk.get("file_path"),
        "target_file_path": hunk.get("target_file_path"),
        "header": hunk.get("header"),
        "section": hunk.get("section"),
        "added_lines": hunk.get("added_lines") or [],
        "removed_lines": hunk.get("removed_lines") or [],
        "context_excerpt": (hunk.get("context_lines") or [])[:12],
        "symbols": grouped,
        "signature_changes": signature_changes({**hunk, "symbols": symbols}),
        "touched_functions": touched_functions(hunk, context),
    }
    if context:
        summary["retrieval_status"] = context.get("retrieval_status")
        summary["target_symbol_status"] = context.get("target_symbol_status", {})
        summary["source_context_available"] = bool((context.get("source_context") or {}).get("available"))
        summary["target_context_available"] = bool((context.get("target_context") or {}).get("available"))
    return summary


def merge_symbol_evidence(hunk: dict, extra_evidence: list[dict]) -> list[dict]:
    """Attach retrieved symbol evidence to a hunk without duplicating it."""
    existing = hunk.get("evidence") or []
    keys = {(item.get("symbol"), item.get("file_path"), item.get("line")) for item in existing}
    merged = list(existing)
    for item in extra_evidence:
        key = (item.get("symbol"), item.get("file_path"), item.get("line"))
        if key in keys:
            continue
        keys.add(key)
        merged.append(item)
    return merged


def enrich_hunks(hunks: Iterable[dict]) -> list[dict]:
    """Return copies of ``hunks`` with deterministic ``symbols`` attached."""
    enriched: list[dict] = []
    for hunk in hunks:
        copy = dict(hunk)
        copy["symbols"] = hunk.get("symbols") or extract_symbols(hunk)
        enriched.append(copy)
    return enriched


def _index(hunk: dict) -> dict:
    definitions: dict[str, dict] = {}
    deletions: dict[str, dict] = {}
    references: dict[str, list[dict]] = {}
    writes: dict[str, list[dict]] = {}
    for entry in hunk.get("symbols") or extract_symbols(hunk):
        name = entry.get("name")
        if not name:
            continue
        role = entry.get("role")
        if role == "definition":
            definitions.setdefault(name, entry)
        elif role == "deletion":
            deletions.setdefault(name, entry)
        elif role == "reference":
            references.setdefault(name, []).append(entry)
            if entry.get("use_kind") == "write":
                writes.setdefault(name, []).append(entry)
    return {
        "definitions": definitions,
        "deletions": deletions,
        "references": references,
        "writes": writes,
        "signatures": {change["name"]: change for change in signature_changes(hunk)},
    }


def _evidence_item(
    file_path: str, symbol: str, description: str, line: int | None, **extra: Any
) -> dict:
    item: dict[str, Any] = {
        "file_path": file_path,
        "symbol": symbol,
        "description": description,
        "source": "machine_extraction",
    }
    if line is not None:
        item["line"] = line
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


def _pair_signals(
    first: dict,
    first_index: dict,
    second: dict,
    second_index: dict,
    contexts: dict[str, dict],
) -> list[dict]:
    signals: list[dict] = []
    first_id, second_id = first["hunk_id"], second["hunk_id"]

    def status(symbol: str, hunk_id: str | None = None) -> str | None:
        """Target-version state as seen by one side of the pair.

        The relevant side depends on the signal: for a definition/use relation
        the consumer decides whether the prerequisite is still needed, while for
        a signature change the hunk that rewrites the signature decides.
        """
        order = [hunk_id] if hunk_id else []
        order += [first_id, second_id]
        for candidate in order:
            record = contexts.get(candidate) or {}
            value = (record.get("target_symbol_status") or {}).get(symbol)
            if value:
                return str(value)
        return None

    def add(
        signal: str,
        symbol: str,
        definition_hunk: dict,
        definition_entry: dict,
        reference_hunk: dict,
        reference_entry: dict,
        description: str,
        status_hunk_id: str | None = None,
        **extra: Any,
    ) -> None:
        signals.append(
            {
                "signal": signal,
                "direction": f"{definition_hunk['hunk_id']}->{reference_hunk['hunk_id']}",
                "symbol": symbol,
                "definition": {
                    "hunk_id": definition_hunk["hunk_id"],
                    "kind": definition_entry.get("kind"),
                    "line": definition_entry.get("line"),
                    "text": definition_entry.get("text"),
                },
                "reference": {
                    "hunk_id": reference_hunk["hunk_id"],
                    "use_kind": reference_entry.get("use_kind"),
                    "line": reference_entry.get("line"),
                    "text": reference_entry.get("text"),
                },
                "target_symbol_status": status(symbol, status_hunk_id),
                "description": description,
                **extra,
            }
        )

    for symbol, definition_entry in sorted(first_index["definitions"].items()):
        for reference_entry in sorted(
            second_index["references"].get(symbol, []),
            key=lambda e: (e.get("line") or 0, e.get("use_kind") or ""),
        ):
            add(
                "definition_use",
                symbol,
                first,
                definition_entry,
                second,
                reference_entry,
                f"{first_id} defines {symbol} and {second_id} references it "
                f"({reference_entry.get('use_kind')}).",
                status_hunk_id=second_id,
            )
    for symbol, definition_entry in sorted(second_index["definitions"].items()):
        for reference_entry in sorted(
            first_index["references"].get(symbol, []),
            key=lambda e: (e.get("line") or 0, e.get("use_kind") or ""),
        ):
            add(
                "definition_use",
                symbol,
                second,
                definition_entry,
                first,
                reference_entry,
                f"{second_id} defines {symbol} and {first_id} references it "
                f"({reference_entry.get('use_kind')}).",
                status_hunk_id=first_id,
            )

    for symbol, change in sorted(first_index["signatures"].items()):
        for reference_entry in sorted(
            second_index["references"].get(symbol, []), key=lambda e: (e.get("line") or 0)
        ):
            add(
                "signature_change",
                symbol,
                first,
                {
                    "kind": "function",
                    "line": change.get("after_line"),
                    "text": change.get("after"),
                },
                second,
                reference_entry,
                f"{first_id} changes the signature of {symbol} "
                f"({change.get('before_params')} -> {change.get('after_params')}) and "
                f"{second_id} uses it.",
                status_hunk_id=first_id,
                before_params=change.get("before_params"),
                after_params=change.get("after_params"),
            )
    for symbol, change in sorted(second_index["signatures"].items()):
        for reference_entry in sorted(
            first_index["references"].get(symbol, []), key=lambda e: (e.get("line") or 0)
        ):
            add(
                "signature_change",
                symbol,
                second,
                {
                    "kind": "function",
                    "line": change.get("after_line"),
                    "text": change.get("after"),
                },
                first,
                reference_entry,
                f"{second_id} changes the signature of {symbol} "
                f"({change.get('before_params')} -> {change.get('after_params')}) and "
                f"{first_id} uses it.",
                status_hunk_id=second_id,
                before_params=change.get("before_params"),
                after_params=change.get("after_params"),
            )

    for symbol, deletion_entry in sorted(first_index["deletions"].items()):
        for reference_entry in sorted(
            second_index["references"].get(symbol, []), key=lambda e: (e.get("line") or 0)
        ):
            add(
                "removed_definition_still_referenced",
                symbol,
                first,
                deletion_entry,
                second,
                reference_entry,
                f"{first_id} removes the declaration of {symbol} while {second_id} still "
                "references it.",
                status_hunk_id=second_id,
            )
    for symbol, deletion_entry in sorted(second_index["deletions"].items()):
        for reference_entry in sorted(
            first_index["references"].get(symbol, []), key=lambda e: (e.get("line") or 0)
        ):
            add(
                "removed_definition_still_referenced",
                symbol,
                second,
                deletion_entry,
                first,
                reference_entry,
                f"{second_id} removes the declaration of {symbol} while {first_id} still "
                "references it.",
                status_hunk_id=first_id,
            )

    for symbol in sorted(set(first_index["writes"]) & set(second_index["writes"])):
        add(
            "shared_write",
            symbol,
            first,
            first_index["writes"][symbol][0],
            second,
            second_index["writes"][symbol][0],
            f"Both {first_id} and {second_id} write {symbol}.",
            status_hunk_id=first_id,
        )
    for symbol in sorted(set(first_index["writes"]) & set(second_index["references"])):
        if symbol in second_index["writes"]:
            continue
        add(
            "write_read",
            symbol,
            first,
            first_index["writes"][symbol][0],
            second,
            second_index["references"][symbol][0],
            f"{first_id} writes {symbol} and {second_id} reads it.",
            status_hunk_id=second_id,
        )
    for symbol in sorted(set(second_index["writes"]) & set(first_index["references"])):
        if symbol in first_index["writes"]:
            continue
        add(
            "write_read",
            symbol,
            second,
            second_index["writes"][symbol][0],
            first,
            first_index["references"][symbol][0],
            f"{second_id} writes {symbol} and {first_id} reads it.",
            status_hunk_id=first_id,
        )

    return signals


def _normalise_context(context: Any) -> dict[str, dict]:
    if not context:
        return {}
    if isinstance(context, dict):
        if "hunk_id" in context:
            return {str(context.get("hunk_id")): context}
        return {str(key): value for key, value in context.items() if isinstance(value, dict)}
    records: dict[str, dict] = {}
    for item in context:
        if isinstance(item, dict) and item.get("hunk_id"):
            records[str(item["hunk_id"])] = item
    return records


def _machine_hint(signals: list[dict]) -> str:
    if not signals:
        return "NONE"
    names = {signal["signal"] for signal in signals}
    for coupling in ("signature_change", "definition_use"):
        related = [signal for signal in signals if signal["signal"] == coupling]
        if not related:
            continue
        if all(signal.get("target_symbol_status") == "absent" for signal in related):
            return "STRONG_HINT"
        return "CONDITIONAL_HINT"
    if names & {"shared_write", "write_read", "removed_definition_still_referenced"}:
        return "CONDITIONAL_HINT"
    return "WEAK_HINT"


def extract(hunks: Iterable[dict], context: Any = None) -> list[dict]:
    """Build candidate hunk pairs with machine signals and locatable evidence.

    ``context`` accepts a context record, a list of records, or a mapping keyed
    by hunk id.  The output is deliberately only a *candidate* list: the LLM
    analyzer decides whether a dependency actually exists.
    """
    contexts = _normalise_context(context)
    prepared = [
        hunk if hunk.get("symbols") else {**hunk, "symbols": extract_symbols(hunk)} for hunk in hunks
    ]
    indexes = {hunk["hunk_id"]: _index(hunk) for hunk in prepared}
    candidates: list[dict] = []

    for position, first in enumerate(prepared):
        for second in prepared[position + 1 :]:
            signals = _pair_signals(
                first,
                indexes[first["hunk_id"]],
                second,
                indexes[second["hunk_id"]],
                contexts,
            )
            shared_symbols = sorted({signal["symbol"] for signal in signals})
            evidence = [
                _evidence_item(
                    first.get("file_path", ""),
                    signal["symbol"],
                    signal["description"],
                    (signal.get("definition") or {}).get("line"),
                    hunk_id=(signal.get("definition") or {}).get("hunk_id"),
                    related_hunk=(signal.get("reference") or {}).get("hunk_id"),
                    reference_line=(signal.get("reference") or {}).get("line"),
                    signal=signal["signal"],
                )
                for signal in signals
            ]
            same_file = first.get("file_path") == second.get("file_path")
            adjacency: int | None = None
            if same_file:
                adjacency = abs(int(first.get("old_start") or 0) - int(second.get("old_start") or 0))
            weak_notes: list[str] = []
            shared_functions = sorted(
                set(touched_functions(first, contexts.get(first["hunk_id"])))
                & set(touched_functions(second, contexts.get(second["hunk_id"])))
            )
            if shared_functions:
                weak_notes.append(
                    "both hunks are inside the same function: " + ", ".join(shared_functions)
                )
            if same_file:
                weak_notes.append(f"both hunks touch {first.get('file_path')}")
            if adjacency is not None and adjacency <= 5:
                weak_notes.append(f"hunks start {adjacency} line(s) apart")

            candidates.append(
                {
                    "pair": list(canonical_pair(first["hunk_id"], second["hunk_id"])),
                    "hunk_ids": list(canonical_pair(first["hunk_id"], second["hunk_id"])),
                    "signals": signals,
                    "shared_symbols": shared_symbols,
                    "signal_names": sorted({signal["signal"] for signal in signals}),
                    "machine_hint": _machine_hint(signals),
                    "machine_evidence": evidence,
                    "proximity_notes": weak_notes,
                    "same_file": same_file,
                }
            )
    candidates.sort(key=lambda item: (hunk_sort_key(item["pair"][0]), hunk_sort_key(item["pair"][1])))
    return candidates
