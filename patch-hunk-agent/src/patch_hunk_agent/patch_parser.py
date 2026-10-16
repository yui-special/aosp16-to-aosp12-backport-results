"""Unified diff parser producing a stable, complete hunk inventory.

Parsing rules that matter for this project:

* Hunk bodies are delimited by the counts in the ``@@`` header, not by
  lookahead.  A removed line that begins with ``--`` renders as ``--- ...`` and
  is textually identical to a file header, so count-driven parsing is the only
  trustworthy option.
* A malformed hunk header is a hard error.  A count mismatch is reported as a
  warning on the affected hunk and parsing resynchronises instead of silently
  dropping the hunk.
* Hunk identifiers (``H1`` ... ``Hn``) are assigned in document order and never
  change afterwards, so grouping decisions stay traceable to the original text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import PatchParseError

__all__ = ["FileChange", "Hunk", "PatchDocument", "PatchParser", "parse_patch"]

HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)$"
)

_FILE_HEADER_PREFIXES = (
    "diff --git ",
    "diff --combined ",
    "diff --cc ",
    "Index: ",
)
_METADATA_PREFIXES = (
    "index ",
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
    "GIT binary patch",
    "\\ No newline at end of file",
)


def _unquote_path(text: str) -> str:
    """Handle git's quoted path form (``"a/x y.c"``) and trailing timestamps."""
    value = text.strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = value[1:-1]
        value = value.replace('\\"', '"').replace("\\\\", "\\")
        for escape, char in (("\\t", "\t"), ("\\n", "\n"), ("\\r", "\r")):
            value = value.replace(escape, char)
        return value
    if "\t" in value:
        value = value.split("\t", 1)[0]
    return value.strip()


def _strip_ab_prefix(path: str) -> str:
    for prefix in ("a/", "b/", "./"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _split_git_paths(rest: str) -> tuple[str, str]:
    """Split the ``a/x b/y`` tail of a ``diff --git`` line."""
    text = rest.strip()
    if text.startswith('"'):
        end = 1
        while end < len(text):
            if text[end] == "\\":
                end += 2
                continue
            if text[end] == '"':
                break
            end += 1
        left = _unquote_path(text[: end + 1])
        right = _unquote_path(text[end + 1 :])
        return left, right
    if " " in text:
        left, right = text.split(" ", 1)
        return _unquote_path(left), _unquote_path(right)
    return _unquote_path(text), _unquote_path(text)


@dataclass
class FileChange:
    """A single file touched by the patch."""

    old_path: str
    new_path: str
    file_path: str
    target_file_path: str
    change_type: str = "modify"
    is_binary: bool = False
    header_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hunk_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "file_path": self.file_path,
            "target_file_path": self.target_file_path,
            "change_type": self.change_type,
            "is_binary": self.is_binary,
            "hunk_ids": list(self.hunk_ids),
            "warnings": list(self.warnings),
        }


@dataclass
class Hunk:
    """One hunk with a stable identifier and the raw text it came from."""

    hunk_id: str
    file_path: str
    target_file_path: str
    old_path: str
    new_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    section: str
    diff_text: str
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    context_lines: list[str] = field(default_factory=list)
    line_map: list[dict] = field(default_factory=list)
    file_change_type: str = "modify"
    parse_warnings: list[str] = field(default_factory=list)
    symbols: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)

    @property
    def old_end(self) -> int:
        return self.old_start + max(self.old_count - 1, 0)

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count - 1, 0)

    def to_dict(self) -> dict:
        """Serialise using the schema documented in section 4.2."""
        return {
            "hunk_id": self.hunk_id,
            "file_path": self.file_path,
            "target_file_path": self.target_file_path,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "old_end": self.old_end,
            "new_end": self.new_end,
            "header": self.header,
            "section": self.section,
            "diff_text": self.diff_text,
            "added_lines": list(self.added_lines),
            "removed_lines": list(self.removed_lines),
            "context_lines": list(self.context_lines),
            "line_map": list(self.line_map),
            "file_change_type": self.file_change_type,
            "parse_warnings": list(self.parse_warnings),
            "symbols": list(self.symbols),
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Hunk:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        payload.setdefault("target_file_path", payload.get("file_path", ""))
        payload.setdefault("old_path", payload.get("file_path", ""))
        payload.setdefault("new_path", payload.get("target_file_path", ""))
        return cls(**payload)  # type: ignore[arg-type]


@dataclass
class PatchDocument:
    """A parsed patch: file changes, hunk inventory and parser warnings."""

    patch_id: str
    raw_text: str
    files: list[FileChange] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def hunk_dicts(self) -> list[dict]:
        return [hunk.to_dict() for hunk in self.hunks]

    def hunk_ids(self) -> list[str]:
        return [hunk.hunk_id for hunk in self.hunks]

    def hunk_by_id(self, hunk_id: str) -> Hunk | None:
        for hunk in self.hunks:
            if hunk.hunk_id == hunk_id:
                return hunk
        return None

    def files_touched(self) -> list[str]:
        seen: list[str] = []
        for hunk in self.hunks:
            if hunk.file_path not in seen:
                seen.append(hunk.file_path)
        return seen

    def summary(self) -> dict:
        return {
            "patch_id": self.patch_id,
            "file_count": len(self.files),
            "hunk_count": len(self.hunks),
            "hunk_ids": self.hunk_ids(),
            "files": [
                {
                    "file_path": change.file_path,
                    "change_type": change.change_type,
                    "is_binary": change.is_binary,
                    "hunk_count": len(change.hunk_ids),
                }
                for change in self.files
            ],
            "warnings": list(self.warnings),
        }


class PatchParser:
    """Parse unified diffs into :class:`PatchDocument` objects.

    Malformed hunk *headers* always raise, because a hunk that cannot be decoded
    would otherwise be silently dropped.  Count mismatches are always downgraded
    to warnings so a partially corrupt patch still yields an explicit inventory;
    set ``require_complete_counts`` to reject such patches instead.
    """

    def __init__(
        self,
        *,
        max_hunks: int | None = None,
        require_complete_counts: bool = False,
    ) -> None:
        self.max_hunks = max_hunks
        self.require_complete_counts = require_complete_counts

    # -- public API ------------------------------------------------------- #

    def parse(self, patch_text: str) -> list[dict]:
        """Parse ``patch_text`` and return the hunk inventory as dicts."""
        return self.parse_document(patch_text).hunk_dicts()

    def parse_file(self, path: str | Path, *, patch_id: str | None = None) -> PatchDocument:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return self.parse_document(text, patch_id=patch_id or Path(path).stem)

    def parse_document(self, patch_text: str, *, patch_id: str | None = None) -> PatchDocument:
        if not isinstance(patch_text, str):
            raise PatchParseError(f"patch_text must be a string, got {type(patch_text).__name__}")
        if not patch_text.strip():
            raise PatchParseError("patch is empty")

        lines = _split_lines(patch_text)
        document = PatchDocument(patch_id=patch_id or "patch_001", raw_text=patch_text)

        file_change: FileChange | None = None
        pending_git_paths: tuple[str, str] | None = None
        hunk_state: dict | None = None
        skipped_lines = 0

        for index, line in enumerate(lines):
            line_no = index + 1

            if hunk_state is not None and line.startswith("\\ No newline"):
                hunk_state["raw"].append(line)
                hunk_state["line_map"].append({"kind": "no_newline", "text": line})
                continue

            if hunk_state is not None and not _hunk_counts_satisfied(hunk_state):
                if _is_hunk_body_line(line):
                    _consume_hunk_line(hunk_state, line)
                    continue
                if line == "":
                    _consume_hunk_line(hunk_state, " ")
                    continue
                # A header-looking line before the declared counts are met.
                if not _looks_like_header(line):
                    _consume_hunk_line(hunk_state, " " + line)
                    hunk_state["parse_warnings"].append(
                        f"line {line_no} did not start with ' ', '+' or '-' and was treated as context"
                    )
                    continue
                _finalise_hunk(document, hunk_state, file_change, reason="hunk_count_mismatch")
                document.warnings.append(
                    f"hunk {hunk_state['hunk_id']} ended early at line {line_no}: "
                    "declared line counts were not satisfied"
                )
                hunk_state = None
                # fall through and handle this line as a header

            if line.startswith("@@"):
                if hunk_state is not None and _hunk_counts_satisfied(hunk_state):
                    _finalise_hunk(document, hunk_state, file_change)
                    hunk_state = None
                if file_change is None:
                    raise PatchParseError(
                        f"line {line_no}: hunk header found before any file header "
                        f"({line!r})"
                    )
                hunk_state = _start_hunk(line, line_no, file_change, len(document.hunks) + 1)
                if self.max_hunks is not None and len(document.hunks) + 1 > self.max_hunks:
                    raise PatchParseError(
                        f"patch contains more than max_hunks={self.max_hunks} hunks; "
                        "refusing to truncate the inventory"
                    )
                continue

            if line.startswith(_FILE_HEADER_PREFIXES):
                if hunk_state is not None:
                    _finalise_hunk(document, hunk_state, file_change)
                    hunk_state = None
                if line.startswith("diff --git "):
                    left, right = _split_git_paths(line[len("diff --git ") :])
                    pending_git_paths = (left, right)
                    file_change = _new_file_change(left, right, document)
                elif line.startswith("Index: "):
                    path = _unquote_path(line[len("Index: ") :])
                    pending_git_paths = (path, path)
                    file_change = _new_file_change(path, path, document)
                else:  # pragma: no cover - reserved for combined diffs
                    document.warnings.append(
                        f"line {line_no}: combined diff format is not supported ({line!r})"
                    )
                continue

            if line.startswith("--- "):
                if hunk_state is not None:
                    _finalise_hunk(document, hunk_state, file_change)
                    hunk_state = None
                old_path = _unquote_path(line[4:])
                if file_change is None:
                    file_change = _new_file_change(old_path, old_path, document)
                file_change.old_path = old_path
                _refresh_paths(file_change)
                continue

            if line.startswith("+++ "):
                if hunk_state is not None:
                    _finalise_hunk(document, hunk_state, file_change)
                    hunk_state = None
                new_path = _unquote_path(line[4:])
                if file_change is None:
                    file_change = _new_file_change(new_path, new_path, document)
                file_change.new_path = new_path
                _refresh_paths(file_change)
                continue

            if file_change is not None and line.startswith(_METADATA_PREFIXES):
                _apply_metadata(file_change, line)
                continue

            if line.startswith("commit ") or line.startswith("From ") or line.startswith("Subject:"):
                skipped_lines += 1
                continue

            skipped_lines += 1

        if hunk_state is not None:
            if _hunk_counts_satisfied(hunk_state):
                _finalise_hunk(document, hunk_state, file_change)
            else:
                _finalise_hunk(document, hunk_state, file_change, reason="hunk_incomplete")
                document.warnings.append(
                    f"hunk {hunk_state['hunk_id']} is incomplete at end of patch"
                )

        if skipped_lines:
            document.warnings.append(f"{skipped_lines} non-diff line(s) were ignored")
        if not document.hunks:
            raise PatchParseError(
                "no hunks found: the input is not a unified diff with '@@' hunk headers"
            )
        if self.require_complete_counts:
            broken = [
                hunk.hunk_id
                for hunk in document.hunks
                if any("do not match" in w or "ended before" in w for w in hunk.parse_warnings)
            ]
            if broken:
                raise PatchParseError(
                    "declared hunk line counts do not match the hunk bodies for: "
                    + ", ".join(broken)
                )
        if pending_git_paths is not None and file_change is None:  # pragma: no cover
            document.warnings.append("git file header without content")
        return document


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _new_file_change(old_path: str, new_path: str, document: PatchDocument) -> FileChange:
    change = FileChange(
        old_path=old_path,
        new_path=new_path,
        file_path=_strip_ab_prefix(old_path) if old_path != "/dev/null" else _strip_ab_prefix(new_path),
        target_file_path=_strip_ab_prefix(new_path) if new_path != "/dev/null" else _strip_ab_prefix(old_path),
    )
    document.files.append(change)
    return change


def _refresh_paths(change: FileChange) -> None:
    if change.old_path == "/dev/null":
        change.change_type = "add"
    elif change.new_path == "/dev/null":
        change.change_type = "delete"
    if (
        change.old_path != "/dev/null"
        and change.new_path != "/dev/null"
        and change.change_type not in ("rename", "copy")
    ):
        change.change_type = "modify"
    change.file_path = (
        _strip_ab_prefix(change.old_path)
        if change.old_path != "/dev/null"
        else _strip_ab_prefix(change.new_path)
    )
    change.target_file_path = (
        _strip_ab_prefix(change.new_path)
        if change.new_path != "/dev/null"
        else _strip_ab_prefix(change.old_path)
    )


def _apply_metadata(change: FileChange, line: str) -> None:
    if line.startswith("rename from "):
        change.change_type = "rename"
        change.old_path = _unquote_path(line[len("rename from ") :])
    elif line.startswith("rename to "):
        change.change_type = "rename"
        change.new_path = _unquote_path(line[len("rename to ") :])
    elif line.startswith("copy from "):
        change.change_type = "copy"
        change.old_path = _unquote_path(line[len("copy from ") :])
    elif line.startswith("copy to "):
        change.change_type = "copy"
        change.new_path = _unquote_path(line[len("copy to ") :])
    elif line.startswith("new file mode "):
        change.change_type = "add"
    elif line.startswith("deleted file mode "):
        change.change_type = "delete"
    elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
        change.is_binary = True
        change.warnings.append("binary file: no textual hunks available")
    if change.change_type in ("rename", "copy"):
        _refresh_paths(change)


def _looks_like_header(line: str) -> bool:
    return bool(
        line.startswith("@@")
        or line.startswith(_FILE_HEADER_PREFIXES)
        or line.startswith("--- ")
        or line.startswith("+++ ")
    )


def _is_hunk_body_line(line: str) -> bool:
    return line.startswith((" ", "+", "-"))


def _hunk_counts_satisfied(state: dict) -> bool:
    return state["old_used"] >= state["old_count"] and state["new_used"] >= state["new_count"]


def _start_hunk(header_line: str, line_no: int, change: FileChange, ordinal: int) -> dict:
    match = HUNK_HEADER_RE.match(header_line)
    if not match:
        raise PatchParseError(
            f"line {line_no}: malformed hunk header {header_line!r} "
            "(expected '@@ -old_start[,old_count] +new_start[,new_count] @@')"
        )
    old_count = int(match.group("old_count")) if match.group("old_count") is not None else 1
    new_count = int(match.group("new_count")) if match.group("new_count") is not None else 1
    return {
        "hunk_id": f"H{ordinal}",
        "header": header_line.rstrip(),
        "section": (match.group("section") or "").strip(),
        "old_start": int(match.group("old_start")),
        "old_count": old_count,
        "new_start": int(match.group("new_start")),
        "new_count": new_count,
        "raw": [header_line],
        "added_lines": [],
        "removed_lines": [],
        "context_lines": [],
        "line_map": [],
        "parse_warnings": [],
        "old_used": 0,
        "new_used": 0,
        "old_line": int(match.group("old_start")),
        "new_line": int(match.group("new_start")),
    }


def _consume_hunk_line(state: dict, line: str) -> None:
    marker = line[:1]
    text = line[1:] if marker in (" ", "+", "-") else line
    if not line.startswith((" ", "+", "-")):
        marker = " "
        text = line
    state["raw"].append(line)
    if marker == " ":
        state["context_lines"].append(text)
        state["line_map"].append(
            {
                "kind": "context",
                "old_line": state["old_line"],
                "new_line": state["new_line"],
                "text": text,
            }
        )
        state["old_used"] += 1
        state["new_used"] += 1
        state["old_line"] += 1
        state["new_line"] += 1
    elif marker == "+":
        state["added_lines"].append(text)
        state["line_map"].append(
            {"kind": "add", "old_line": None, "new_line": state["new_line"], "text": text}
        )
        state["new_used"] += 1
        state["new_line"] += 1
    else:
        state["removed_lines"].append(text)
        state["line_map"].append(
            {"kind": "del", "old_line": state["old_line"], "new_line": None, "text": text}
        )
        state["old_used"] += 1
        state["old_line"] += 1


def _finalise_hunk(
    document: PatchDocument,
    state: dict,
    change: FileChange | None,
    *,
    reason: str | None = None,
) -> Hunk:
    warnings = list(state["parse_warnings"])
    if reason == "hunk_count_mismatch":
        warnings.append(
            f"declared counts (-{state['old_count']} +{state['new_count']}) do not match "
            f"observed lines (-{state['old_used']} +{state['new_used']})"
        )
    elif reason == "hunk_incomplete":
        warnings.append(
            f"hunk text ended before counts were satisfied (observed -{state['old_used']} "
            f"+{state['new_used']}, declared -{state['old_count']} +{state['new_count']})"
        )
    if change is None:  # pragma: no cover - guarded by the caller
        raise PatchParseError(f"hunk {state['hunk_id']} has no owning file header")

    hunk = Hunk(
        hunk_id=state["hunk_id"],
        file_path=change.file_path,
        target_file_path=change.target_file_path,
        old_path=change.old_path,
        new_path=change.new_path,
        old_start=state["old_start"],
        old_count=state["old_count"],
        new_start=state["new_start"],
        new_count=state["new_count"],
        header=state["header"],
        section=state["section"],
        diff_text="\n".join(state["raw"]),
        added_lines=list(state["added_lines"]),
        removed_lines=list(state["removed_lines"]),
        context_lines=list(state["context_lines"]),
        line_map=list(state["line_map"]),
        file_change_type=change.change_type,
        parse_warnings=warnings,
    )
    document.hunks.append(hunk)
    change.hunk_ids.append(hunk.hunk_id)
    return hunk


def parse_patch(patch_text: str, *, patch_id: str | None = None) -> PatchDocument:
    """Convenience wrapper used by the CLI and tests."""
    return PatchParser().parse_document(patch_text, patch_id=patch_id)
