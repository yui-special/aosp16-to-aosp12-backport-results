"""JSON helpers: deterministic dump/load plus lenient extraction of LLM output.

LLM responses are not trustworthy input.  Everything that goes through
``loads_lenient`` is treated as text to be repaired and parsed, never as fact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

__all__ = [
    "dumps",
    "load_json",
    "save_json",
    "extract_json_block",
    "repair_json_text",
    "loads_lenient",
]


def dumps(obj: Any, *, indent: int = 2, sort_keys: bool = False) -> str:
    """Serialise ``obj`` as UTF-8 friendly JSON text."""
    return json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=sort_keys)


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: str | Path, obj: Any, *, indent: int = 2) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(obj, indent=indent) + "\n", encoding="utf-8")
    return target


_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_CODE_FENCE_LINE_RE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*$")
_SMART_QUOTES = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"}
_PY_LITERALS = (
    (re.compile(r"\bTrue\b"), "true"),
    (re.compile(r"\bFalse\b"), "false"),
    (re.compile(r"\bNone\b"), "null"),
)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    # A fence that is not closed, or text before/after a fence.
    lines = [line for line in stripped.splitlines() if not _CODE_FENCE_LINE_RE.match(line)]
    return "\n".join(lines).strip()


def _strip_json_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments while honouring string literals."""
    out: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    quote = ""
    while i < length:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < length:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                in_string = False
            i += 1
            continue
        if ch in "\"'":
            in_string = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < length and text[i + 1] == "/":
            while i < length and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < length and text[i + 1] == "*":
            i += 2
            while i + 1 < length and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def repair_json_text(text: str) -> tuple[str, list[str]]:
    """Apply conservative repairs.  Returns ``(repaired_text, repairs)``."""
    repairs: list[str] = []
    repaired = text

    def replace_all(pattern: re.Pattern[str], repl: str) -> None:
        nonlocal repaired
        new = pattern.sub(repl, repaired)
        if new != repaired:
            repaired = new

    fenced = _strip_fences(repaired)
    if fenced != repaired.strip():
        repaired = fenced
        repairs.append("stripped_markdown_fence")

    for bad, good in _SMART_QUOTES.items():
        if bad in repaired:
            repaired = repaired.replace(bad, good)
            if "normalized_smart_quotes" not in repairs:
                repairs.append("normalized_smart_quotes")

    without_comments = _strip_json_comments(repaired)
    if without_comments != repaired:
        repaired = without_comments
        repairs.append("stripped_json_comments")

    for pattern, repl in _PY_LITERALS:
        before = repaired
        replace_all(pattern, repl)
        if repaired != before and "normalized_python_literals" not in repairs:
            repairs.append("normalized_python_literals")

    before = repaired
    replace_all(_TRAILING_COMMA_RE, r"\1")
    if repaired != before:
        repairs.append("removed_trailing_commas")

    return repaired.strip(), repairs


def _iter_candidate_spans(text: str):
    """Yield balanced ``{...}``/``[...]`` substrings, ordered by start offset."""
    pairs = {"}": "{", "]": "["}
    for start, opener in enumerate(text):
        if opener not in "{[":
            continue
        stack: list[str] = []
        in_string = False
        quote = ""
        i = start
        while i < len(text):
            ch = text[i]
            if in_string:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    in_string = False
                i += 1
                continue
            if ch in "\"'":
                in_string = True
                quote = ch
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if not stack or stack[-1] != pairs[ch]:
                    break
                stack.pop()
                if not stack:
                    yield text[start : i + 1]
                    break
            i += 1


def extract_json_block(text: str) -> str | None:
    """Return the first balanced JSON object/array found in ``text``."""
    if not text:
        return None
    for candidate in _iter_candidate_spans(text):
        try:
            json.loads(candidate)
        except Exception:  # noqa: BLE001 - any failure just means "not valid JSON"
            continue
        return candidate
    return None


def loads_lenient(text: str) -> tuple[Any, list[str]]:
    """Parse LLM JSON output, repairing fences/comments/trailing commas.

    Raises :class:`json.JSONDecodeError` when nothing parseable can be found.
    """
    if isinstance(text, (dict, list)):
        return text, []

    repairs: list[str] = []

    def attempt(payload: str) -> Any:
        try:
            return json.loads(payload, strict=False)
        except json.JSONDecodeError:
            return _MISS

    direct = attempt(text)
    if direct is not _MISS:
        return direct, repairs

    repaired, applied = repair_json_text(text)
    repairs.extend(applied)
    parsed = attempt(repaired)
    if parsed is not _MISS:
        return parsed, repairs

    if "'" in repaired and '"' not in repaired:
        # Last-resort: a response that used Python-style single quotes throughout.
        parsed = attempt(repaired.replace("'", '"'))
        if parsed is not _MISS:
            repairs.append("converted_single_quotes")
            return parsed, repairs

    block = extract_json_block(repaired)
    if block is not None:
        parsed = attempt(block)
        if parsed is not _MISS:
            repairs.append("extracted_json_block")
            return parsed, repairs

    # Last resort: try to grab a block from the *original* text, then repair it.
    block = extract_json_block(text)
    if block is not None:
        repaired_block, applied2 = repair_json_text(block)
        parsed = attempt(repaired_block)
        if parsed is not _MISS:
            repairs.append("extracted_json_block")
            repairs.extend(applied2)
            return parsed, repairs

    raise json.JSONDecodeError("no parseable JSON found in response", text[:400], 0)


class _Missing:
    __slots__ = ()


_MISS = _Missing()
