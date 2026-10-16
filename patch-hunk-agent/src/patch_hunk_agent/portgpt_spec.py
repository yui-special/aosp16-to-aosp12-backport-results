"""Bridge to PortGPT's hunk-group specification.

PortGPT (the ``*-modified`` tree) reads a group spec JSON and turns each group
into one *merged hunk* that is ported in a single agent session:

    {
      "groups": [
        {"id": 1, "name": "...", "hunks": [0, 1, 6],
         "concern": "...", "why_together": "..."}
      ],
      "dependencies": [{"from_hunk": 3, "to_hunk": 6, "why": "..."}],
      "notes": "...",
      "_model": "...", "_patch": "..."
    }

The ``hunks`` numbers are **0-based indices** produced by PortGPT's own
``tools.utils.split_patch_indexed``.  This module therefore never guesses the
numbering: it runs PortGPT's splitter (imported, or extracted from
``tools/utils.py`` when PortGPT's third-party dependencies are unavailable) and
checks every hunk against this agent's inventory before building a spec.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import logging
import re
import sys
import warnings
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterable

from .schemas import hunk_sort_key
from .utils.json_utils import save_json
from .utils.logging_utils import get_logger

LOGGER = get_logger("portgpt.spec")

__all__ = [
    "PortGPTBridgeError",
    "SplitterInfo",
    "load_portgpt_splitter",
    "split_patch_with_portgpt",
    "check_alignment",
    "load_group_spec",
    "check_spec_coverage",
    "grouping_document_from_spec",
    "build_group_spec",
    "build_merged_hunks",
    "dump_merged_hunks",
    "write_group_spec",
    "DEFAULT_SPEC_FILENAME",
]

DEFAULT_SPEC_FILENAME = "hunk_groups.json"


class PortGPTBridgeError(Exception):
    """Raised when the PortGPT tree cannot provide a usable splitter."""


class SplitterInfo:
    """Where the hunk splitter came from - recorded so runs stay auditable."""

    def __init__(self, source: str, origin: str, splitter: Callable) -> None:
        self.source = source  # "import" | "extracted"
        self.origin = origin  # file path the code was loaded from
        self.splitter = splitter

    def to_dict(self) -> dict:
        return {"source": self.source, "origin": self.origin}


def load_portgpt_splitter(portgpt_root: str | Path) -> SplitterInfo:
    """Return PortGPT's ``split_patch_indexed`` with provenance information.

    Tries a normal import first (``<root>/tools/utils.py``); if PortGPT's own
    dependencies (Levenshtein, langchain, ...) are missing, the single function is
    extracted from the same file and executed - still PortGPT's code, minus the
    unrelated imports.
    """
    root = Path(portgpt_root)
    utils_path = root / "tools" / "utils.py"
    if not utils_path.is_file():
        raise PortGPTBridgeError(f"not a PortGPT tree (missing tools/utils.py): {root}")

    importer = _import_splitter(root, utils_path)
    if importer is not None:
        return SplitterInfo("import", str(utils_path), importer)
    return SplitterInfo("extracted", str(utils_path), _extract_splitter(utils_path))


def _import_splitter(root: Path, utils_path: Path) -> Callable | None:
    module_name = "portgpt_tools_utils_for_alignment"
    with _quiet_syntax_warnings():
        try:
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            spec = importlib.util.spec_from_file_location(module_name, utils_path)
            if spec is None or spec.loader is None:  # pragma: no cover - defensive
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - any import problem falls back to extraction
            sys.modules.pop(module_name, None)
            return None
    splitter = getattr(module, "split_patch_indexed", None)
    return splitter if callable(splitter) else None


def _extract_splitter(utils_path: Path) -> Callable:
    """Execute only ``split_patch_indexed`` from PortGPT's utils module."""
    source = utils_path.read_text(encoding="utf-8")
    with _quiet_syntax_warnings():  # ast.parse compiles and may warn on sloppy escapes
        tree = ast.parse(source, filename=str(utils_path))
    wanted = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    target = [node for node in wanted if node.name == "split_patch_indexed"]
    if not target:
        raise PortGPTBridgeError(f"split_patch_indexed not found in {utils_path}")
    module = ast.Module(body=target, type_ignores=[])
    namespace: dict[str, Any] = {
        "__name__": "portgpt_utils_extract",
        "logger": logging.getLogger("portgpt.splitter"),
        "re": re,
        "json": json,
        "os": __import__("os"),
    }
    with _quiet_syntax_warnings():
        code = compile(module, str(utils_path), "exec")
    exec(code, namespace)  # noqa: S102 - trusted local file
    splitter = namespace["split_patch_indexed"]
    if not callable(splitter):  # pragma: no cover - defensive
        raise PortGPTBridgeError(f"could not load split_patch_indexed from {utils_path}")
    return splitter


@contextlib.contextmanager
def _quiet_syntax_warnings():
    """PortGPT's source emits SyntaxWarnings on compile; keep our output clean."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        yield


def split_patch_with_portgpt(
    portgpt_root: str | Path, patch_text: str, *, flag_commit: bool = True
) -> tuple[SplitterInfo, str, list[dict]]:
    """Split ``patch_text`` exactly the way PortGPT will, returning its hunks."""
    info = load_portgpt_splitter(portgpt_root)
    message, hunks = info.splitter(patch_text, flag_commit)
    return info, message, list(hunks or [])


def check_alignment(hunks: Iterable[dict], portgpt_hunks: Iterable[dict]) -> dict:
    """Compare this agent's hunk inventory with PortGPT's numbering.

    Returns ``{"aligned": bool, "problems": [...], "index_of": {hunk_id: idx}}``.
    Any difference in count, order, file path, ``@@`` header or body line count is
    reported - an index that points at another hunk would silently corrupt the
    backport, so it must never pass unnoticed.
    """
    mine = list(hunks)
    theirs = list(portgpt_hunks)
    problems: list[dict] = []
    index_of: dict[str, int] = {}

    if len(mine) != len(theirs):
        problems.append(
            {
                "field": "hunk_count",
                "mine": len(mine),
                "portgpt": len(theirs),
                "detail": "different number of hunks; index mapping is not trustworthy",
            }
        )

    for position, theirs_hunk in enumerate(theirs):
        if position >= len(mine):
            break
        my_hunk = mine[position]
        index_of[str(my_hunk.get("hunk_id"))] = position
        their_file = str(theirs_hunk.get("file") or "")
        their_header = _header_line(theirs_hunk.get("text") or "")
        their_body = _body_lines(theirs_hunk.get("text") or "")
        if their_file != str(my_hunk.get("file_path")):
            problems.append(
                {
                    "field": "file_path",
                    "hunk_id": my_hunk.get("hunk_id"),
                    "index": position,
                    "mine": my_hunk.get("file_path"),
                    "portgpt": their_file,
                }
            )
        if their_header != str(my_hunk.get("header")):
            problems.append(
                {
                    "field": "header",
                    "hunk_id": my_hunk.get("hunk_id"),
                    "index": position,
                    "mine": my_hunk.get("header"),
                    "portgpt": their_header,
                }
            )
        my_body = len(my_hunk.get("line_map") or [])
        if their_body != my_body:
            problems.append(
                {
                    "field": "body_line_count",
                    "hunk_id": my_hunk.get("hunk_id"),
                    "index": position,
                    "mine": my_body,
                    "portgpt": their_body,
                    "detail": (
                        "PortGPT stops a hunk body at the first line that does not start "
                        "with ' ', '+' or '-'; check for blank lines inside the hunk"
                    ),
                }
            )
    if len(mine) > len(theirs):
        for my_hunk in mine[len(theirs) :]:
            problems.append(
                {
                    "field": "extra_hunk",
                    "hunk_id": my_hunk.get("hunk_id"),
                    "detail": "this hunk has no counterpart in PortGPT's numbering",
                }
            )
    return {"aligned": not problems, "problems": problems, "index_of": index_of}


def _header_line(text: str) -> str:
    for line in text.split("\n"):
        if line.startswith("@@"):
            return line
    return ""


def _body_lines(text: str) -> int:
    lines = text.split("\n")
    try:
        header_at = next(index for index, line in enumerate(lines) if line.startswith("@@"))
    except StopIteration:
        return 0
    return len(lines[header_at + 1 :])


def load_group_spec(path: str | Path) -> dict:
    """Read a PortGPT group spec (as written by hand, by another tool, or here)."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        raise PortGPTBridgeError(f"{path} is not a PortGPT group spec (missing 'groups')")
    return payload


def check_spec_coverage(spec: dict, hunk_count: int) -> dict:
    """Verify a spec is a complete, duplicate-free partition of ``hunk_count`` hunks."""
    problems: list[dict] = []
    assignment: dict[int, Any] = {}
    for group in spec.get("groups") or []:
        if not isinstance(group, dict):
            problems.append({"reason": "group is not an object", "group": group})
            continue
        members = group.get("hunks")
        if not isinstance(members, list) or not members:
            problems.append({"reason": "group has no hunks", "group_id": group.get("id")})
            continue
        for member in members:
            if not isinstance(member, int) or isinstance(member, bool):
                problems.append(
                    {
                        "reason": "hunk index is not an integer",
                        "group_id": group.get("id"),
                        "index": member,
                    }
                )
                continue
            if member < 0 or member >= hunk_count:
                problems.append(
                    {
                        "reason": "hunk index out of range",
                        "group_id": group.get("id"),
                        "index": member,
                        "hunk_count": hunk_count,
                    }
                )
                continue
            if member in assignment:
                problems.append(
                    {
                        "reason": "hunk assigned to more than one group",
                        "index": member,
                        "groups": [assignment[member], group.get("id")],
                    }
                )
                continue
            assignment[member] = group.get("id")
    uncovered = [index for index in range(hunk_count) if index not in assignment]
    return {
        "valid": not problems and not uncovered,
        "problems": problems,
        "uncovered_hunks": uncovered,
        "assigned_count": len(assignment),
        "hunk_count": hunk_count,
    }


def grouping_document_from_spec(
    spec: dict,
    hunks: list[dict],
    *,
    patch_id: str | None = None,
    notes: str | None = None,
) -> dict:
    """Import a PortGPT spec back into this agent's grouping document format.

    PortGPT's 0-based indices are mapped onto this agent's ``H<n>`` ids using the
    hunk order (run :func:`check_alignment` first when the inventory could differ).
    This makes an externally produced spec reviewable and re-validatable with this
    agent's validators and evaluator.
    """
    ids_by_index = {position: str(hunk.get("hunk_id")) for position, hunk in enumerate(hunks)}
    groups: list[dict] = []
    for position, group in enumerate(spec.get("groups") or [], start=1):
        hunk_ids = sorted(
            (ids_by_index[index] for index in group.get("hunks") or [] if index in ids_by_index),
            key=hunk_sort_key,
        )
        reason = " ".join(
            part
            for part in (
                str(group.get("concern") or "").strip(),
                str(group.get("why_together") or "").strip(),
            )
            if part
        )
        groups.append(
            {
                "group_id": f"G{position}",
                "hunk_ids": hunk_ids,
                "group_reason": reason,
                "dependency_ids": [],
                "name": group.get("name") or "",
                "source_group_id": group.get("id", position),
                # Original wording is preserved so a re-export stays faithful.
                "source_name": group.get("name") or "",
                "source_concern": group.get("concern") or "",
                "source_why": group.get("why_together") or "",
            }
        )
    return {
        "patch_id": patch_id or spec.get("_patch") or "patch_001",
        "status": "complete",
        "groups": groups,
        # Kept verbatim so that re-exporting this grouping does not lose the
        # original wording (and never invents a category or strength for it).
        "source_dependencies": [
            {
                "from_hunk": ids_by_index.get(record.get("from_hunk")),
                "to_hunk": ids_by_index.get(record.get("to_hunk")),
                "why": str(record.get("why") or ""),
                "category": record.get("category"),
                "strength": record.get("strength"),
                "origin": "portgpt_spec",
            }
            for record in spec.get("dependencies") or []
            if ids_by_index.get(record.get("from_hunk")) and ids_by_index.get(record.get("to_hunk"))
        ],
        "cross_group_dependencies": [],
        "unresolved_questions": [],
        "validation": {
            "all_hunks_assigned_once": False,
            "strong_dependencies_within_groups": False,
            "unresolved_dependencies_reported": False,
        },
        "metadata": {
            "source": "portgpt_group_spec",
            "source_model": spec.get("_model"),
            "notes": notes or spec.get("notes"),
        },
    }


_LANGUAGE_TEMPLATES = {
    "zh": {
        "single_name": "{file} 单 hunk 改动",
        "multi_name": "{file} 等 {count} 个文件：{symbols}",
        "concern_single": "本组只含 {hunk_id}（{file}，{section}）。",
        "concern_multi": "本组包含 {hunks}，涉及 {files}；由 {categories} 依赖绑定：{summary}。",
        "why": "{detail}若分开移植，{risk}",
        "why_no_risk": "{detail}",
        "note": "该分组由 Patch Hunk Dependency Grouping Agent 生成（依赖为 LLM 提出并经结构校验，非已验证的移植结果）。",
    },
    "en": {
        "single_name": "{file} single-hunk change",
        "multi_name": "{file} and {count} files: {symbols}",
        "concern_single": "This group contains only {hunk_id} ({file}, {section}).",
        "concern_multi": (
            "This group contains {hunks} across {files}; bound by {categories} "
            "dependencies: {summary}."
        ),
        "why": "{detail} If ported separately: {risk}",
        "why_no_risk": "{detail}",
        "note": (
            "Generated by the Patch Hunk Dependency Grouping Agent; dependencies are "
            "LLM-proposed and structurally validated, not verified backport results."
        ),
    },
}


def build_group_spec(
    grouping_document: dict,
    hunks: list[dict],
    *,
    index_of: dict[str, int] | None = None,
    dependencies: Iterable[dict] | None = None,
    patch_id: str | None = None,
    model: str | None = None,
    language: str = "zh",
    notes: str | None = None,
    extra_groups: list[dict] | None = None,
) -> dict:
    """Build the group spec PortGPT consumes from this agent's grouping document.

    ``index_of`` maps ``H<n>`` to PortGPT's 0-based index; pass the result of
    :func:`check_alignment`.  Without it the natural ``H<n> -> n-1`` mapping is
    used, which is only safe after a successful alignment check.

    Groups carrying ``source_name``/``source_concern``/``source_why`` (i.e. taken
    from an imported spec or a reviewed grouping document) keep that wording; only
    groups without it get text generated from the dependency records.
    """
    templates = _LANGUAGE_TEMPLATES.get(language) or _LANGUAGE_TEMPLATES["zh"]
    hunk_map = {str(hunk.get("hunk_id")): hunk for hunk in hunks}
    default_index = {str(hunk.get("hunk_id")): position for position, hunk in enumerate(hunks)}
    mapping = dict(default_index)
    mapping.update(index_of or {})

    if dependencies is None:
        # Groupings imported from an external spec carry their own wording; keep it.
        dependency_list = list(grouping_document.get("source_dependencies") or [])
    else:
        dependency_list = list(dependencies)
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for record in dependency_list:
        pair = _record_pair(record)
        if pair is None:
            continue
        by_pair.setdefault(pair, []).append(record)

    groups: list[dict] = []
    for position, group in enumerate(grouping_document.get("groups") or [], start=1):
        hunk_ids = sorted(
            (str(hunk_id) for hunk_id in group.get("hunk_ids") or []), key=hunk_sort_key
        )
        # Ascending order matches PortGPT's own normalisation of group members.
        members = sorted(mapping[hunk_id] for hunk_id in hunk_ids if hunk_id in mapping)
        missing = [hunk_id for hunk_id in hunk_ids if hunk_id not in mapping]
        related: list[dict] = []
        for left in hunk_ids:
            for right in hunk_ids:
                if hunk_sort_key(left) >= hunk_sort_key(right):
                    continue
                key = tuple(sorted((left, right), key=hunk_sort_key))
                related.extend(by_pair.get(key, []))  # type: ignore[arg-type]
        groups.append(
            {
                "id": position,
                "name": str(group.get("source_name") or "").strip()
                or _group_name(hunk_ids, hunk_map, related, templates),
                "hunks": members,
                "concern": str(group.get("source_concern") or "").strip()
                or _group_concern(hunk_ids, hunk_map, related, templates),
                "why_together": str(group.get("source_why") or "").strip()
                or _group_why(related, templates),
                "group_id": group.get("group_id"),
                "hunk_ids": hunk_ids,
                "missing_hunks": missing,
            }
        )

    spec: dict[str, Any] = {
        "groups": groups,
        "dependencies": [
            {
                "from_hunk": _endpoint_index(record, mapping, "source"),
                "to_hunk": _endpoint_index(record, mapping, "target"),
                "category": record.get("category"),
                "strength": record.get("strength"),
                "why": _record_text(record),
                "symbols": _symbols_of(record),
            }
            for record in dependency_list
        ],
        "notes": notes or templates["note"],
        "_model": model,
        "_patch": patch_id,
        "_source": "patch_hunk_agent.grouping_document",
    }
    if grouping_document.get("patch_id"):
        spec["_patch"] = spec["_patch"] or grouping_document["patch_id"]
    if extra_groups:
        spec["groups"].extend(extra_groups)
    return spec


def write_group_spec(spec: dict, path: str | Path) -> Path:
    return save_json(path, spec)


def build_merged_hunks(
    spec: dict,
    hunks: list[dict],
    *,
    portgpt_hunks: list[dict] | None = None,
    index_of: dict[str, int] | None = None,
    order: list[str] | None = None,
    coordination: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Turn a group spec into PortGPT's ``merged hunk`` records.

    Each record is one porting unit: the member hunks concatenated into a single
    patch text in ascending index order - the same shape PortGPT's own
    ``group_patch`` returns (``gid``/``name``/``hunks``/``concern``/``why``/
    ``files``/``text``).

    When ``portgpt_hunks`` (the output of PortGPT's splitter) is given, its hunk
    text is used verbatim so the produced patch files are byte-identical to the
    ones PortGPT would build itself.  Otherwise the text is rebuilt from this
    agent's own hunk inventory.
    """
    mapping = {str(hunk.get("hunk_id")): position for position, hunk in enumerate(hunks)}
    mapping.update(index_of or {})
    by_id = {str(hunk.get("hunk_id")): hunk for hunk in hunks}
    authoritative = {
        int(hunk["idx"]): str(hunk.get("text") or "") for hunk in portgpt_hunks or []
    }

    merged: list[dict] = []
    # PortGPT's group_patch() emits groups ordered by their smallest member index,
    # and dump_merged_hunks() numbers files in that order - match it exactly.
    ordered_groups = sorted(
        spec.get("groups") or [],
        key=lambda group: min(
            [int(index) for index in group.get("hunks") or [] if isinstance(index, int)]
            or [1 << 30]
        ),
    )
    for group in ordered_groups:
        members = sorted(
            int(index) for index in (group.get("hunks") or []) if isinstance(index, int)
        )
        texts: list[str] = []
        files: list[str] = []
        for index in members:
            hunk = by_id.get(_hunk_id_for_index(index, mapping))
            if index in authoritative:
                texts.append(authoritative[index])
            elif hunk is not None:
                texts.append(merged_hunk_text(hunk))
            if hunk is not None:
                # PortGPT lists one entry per member hunk (duplicates included);
                # keep that exactly so the two producers stay comparable.
                files.append(str(hunk.get("file_path") or ""))
        merged.append(
            {
                "gid": group.get("id"),
                "name": group.get("name") or "",
                "hunks": members,
                "concern": group.get("concern") or "",
                "why": group.get("why_together") or "",
                "files": files,
                "text": "\n".join(text for text in texts if text) + ("\n" if texts else ""),
                "group_id": str(group.get("group_id") or ""),
                "coordination": [
                    str(item)
                    for item in (coordination or {}).get(str(group.get("group_id") or ""), [])
                ],
            }
        )
    if order:
        position = {str(gid): index for index, gid in enumerate(order)}
        merged.sort(
            key=lambda item: position.get(item.get("group_id") or "", len(position) + item["gid"])
        )
    for item in merged:
        # ``why`` is the field PortGPT's describe_group() renders into the porting
        # prompt, so the coordination directives ride along with it.
        if item.get("coordination"):
            item["why"] = (str(item.get("why") or "").strip() + " " + " ".join(item["coordination"])).strip()
    return merged


def merged_hunk_text(hunk: dict, *, portgpt_text: str | None = None) -> str:
    """Rebuild one hunk's patch text (``--- a/`` + ``+++ b/`` + ``@@`` + body)."""
    if portgpt_text:
        return portgpt_text
    old_path = str(hunk.get("old_path") or f"a/{hunk.get('file_path')}")
    new_path = str(hunk.get("new_path") or f"b/{hunk.get('target_file_path')}")
    lines = [f"--- {old_path}", f"+++ {new_path}", str(hunk.get("header") or "")]
    for entry in hunk.get("line_map") or []:
        kind = entry.get("kind")
        text = entry.get("text") or ""
        if kind == "add":
            lines.append("+" + text)
        elif kind == "del":
            lines.append("-" + text)
        elif kind == "no_newline":
            lines.append(text)
        else:
            lines.append(" " + text)
    return "\n".join(lines)


def dump_merged_hunks(merged: list[dict], out_dir: str | Path) -> tuple[Path, Path]:
    """Write PortGPT-compatible merged hunk files.

    The layout matches PortGPT's own ``dump_merged_hunks`` so the result can be
    fed back through its ``merged_hunks_dir`` input (``load_merged_hunk_files``):
    ``<out>/original/merged-hunk-NN.patch`` + ``original/index.json`` +
    ``<out>/index.md``, plus an empty ``<out>/ported/`` for the results.
    """
    root = Path(out_dir)
    original = root / "original"
    ported = root / "ported"
    original.mkdir(parents=True, exist_ok=True)
    ported.mkdir(parents=True, exist_ok=True)

    # Remove files from an earlier run: PortGPT reads every *.patch in this
    # directory as a session input, so a leftover `merged-hunk-NN.patch` from a
    # previous grouping would silently become a phantom task.
    stale = sorted(original.glob("merged-hunk-*.patch"))
    for path in stale:
        path.unlink()
    if stale:
        LOGGER.info("removed %d stale merged hunk file(s) from %s", len(stale), original)

    head = [
        "# merged hunk 列表",
        "",
        "每个 merged hunk = 一组互相依赖的原始 hunk，作为一个整体、在一个独立 session 里移植。",
        "",
        "| # | group id | 原始 hunk 编号 | 文件数 | 组名 |",
        "|---|---|---|---|---|",
    ]
    body: list[str] = []
    index: list[dict] = []
    for position, group in enumerate(merged):
        filename = f"merged-hunk-{position:02d}.patch"
        (original / filename).write_text(
            str(group.get("text") or ""), encoding="utf-8", newline="\n"
        )
        entry = {
            "file": filename,
            "gid": group.get("gid"),
            "name": group.get("name") or "",
            "hunks": group.get("hunks") or [],
            "concern": group.get("concern") or "",
            "why": group.get("why") or "",
            "files": group.get("files") or [],
        }
        if group.get("coordination"):
            # Added only when there is something to coordinate, so a plain
            # grouping stays byte-identical to PortGPT's own dump.
            entry["coordination"] = group["coordination"]
        index.append(entry)
        head.append(
            f"| {position} | `{group.get('gid')}` | "
            f"{', '.join(str(member) for member in group.get('hunks') or [])} | "
            f"{len(group.get('files') or [])} | {group.get('name') or ''} |"
        )
        body.append("")
        body.append(
            f"## merged hunk {position}（group {group.get('gid')}）：{group.get('name') or ''}"
        )
        body.append("")
        body.append(
            "- 原始 hunk 编号：{}".format(", ".join(str(member) for member in group.get("hunks") or []))
        )
        body.append("- 涉及文件：")
        for path in group.get("files") or []:
            body.append(f"  - `{path}`")
        if group.get("concern"):
            body.append(f"- 关注点：{group['concern']}")
        if group.get("why"):
            body.append(f"- 为什么必须一起改：{group['why']}")
        body.append(f"- 文件：`original/{filename}`")

    (root / "index.md").write_text("\n".join(head + body) + "\n", encoding="utf-8", newline="\n")
    # Written the same way PortGPT writes it (indent=2, no trailing newline) so the
    # two producers stay byte-comparable.
    (original / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return original, ported


def _hunk_id_for_index(index: int, mapping: dict[str, int]) -> str:
    for hunk_id, position in mapping.items():
        if position == index:
            return hunk_id
    return f"H{index + 1}"


def _endpoint_index(record: dict, mapping: dict[str, int], side: str) -> int | None:
    """0-based PortGPT index of one endpoint, accepting both record shapes."""
    if side == "source" and record.get("source_hunk") is not None:
        return mapping.get(str(record.get("source_hunk")))
    if side == "target" and record.get("target_hunk") is not None:
        return mapping.get(str(record.get("target_hunk")))
    key = "from_hunk" if side == "source" else "to_hunk"
    value = record.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text in mapping:  # a hunk id such as "H4"
        return mapping[text]
    digits = text.lstrip("Hh")
    if digits.isdigit():  # an index expressed as a string
        return int(digits)
    return None


def _record_pair(record: dict) -> tuple[str, str] | None:
    """Unordered ``(H<i>, H<j>)`` key for a dependency record of either shape."""
    left = record.get("source_hunk") or record.get("from_hunk")
    right = record.get("target_hunk") or record.get("to_hunk")
    if left is None or right is None:
        return None
    left = str(left) if str(left).startswith("H") else f"H{int(left) + 1}"
    right = str(right) if str(right).startswith("H") else f"H{int(right) + 1}"
    return tuple(sorted((left, right), key=hunk_sort_key))  # type: ignore[return-value]


def _record_text(record: dict) -> str:
    for key in ("reasoning_summary", "why", "separation_risk"):
        text = str(record.get(key) or "").strip()
        if text:
            return text
    return ""


def _group_name(hunk_ids: list[str], hunk_map: dict[str, dict], related: list[dict], templates: dict) -> str:
    files = _files_of(hunk_ids, hunk_map)
    first = _basename(files[0]) if files else "unknown"
    if len(hunk_ids) == 1:
        hunk = hunk_map.get(hunk_ids[0]) or {}
        section = str(hunk.get("section") or "").strip()
        if section:
            return f"{first} :: {section}"
        return templates["single_name"].format(file=first)
    label = _related_name(related) or "、".join(_related_symbols(related, limit=2)) or "co-changing hunks"
    others = len(files) - 1
    if others > 0:
        return f"{first} +{others} files :: {label}"
    return f"{first} :: {label}"


def _group_concern(hunk_ids: list[str], hunk_map: dict[str, dict], related: list[dict], templates: dict) -> str:
    files = _files_of(hunk_ids, hunk_map)
    if len(hunk_ids) == 1:
        hunk = hunk_map.get(hunk_ids[0]) or {}
        return templates["concern_single"].format(
            hunk_id=hunk_ids[0],
            file=hunk.get("file_path") or "",
            section=hunk.get("section") or "-",
        )
    categories = sorted({str(record.get("category")) for record in related})
    summary = "；".join(
        filter(
            None,
            [str(record.get("reasoning_summary") or "").strip().rstrip(".") for record in related[:2]],
        )
    )
    return templates["concern_multi"].format(
        hunks="、".join(hunk_ids),
        files="、".join(_basename(path) for path in files),
        categories="/".join(categories) or "co-change",
        summary=summary or "—",
    )


def _group_why(related: list[dict], templates: dict) -> str:
    if not related:
        return ""
    parts: list[str] = []
    for record in sorted(related, key=lambda item: str(item.get("dependency_id") or "")):
        symbols = _symbols_of(record)
        label = _record_label(record)
        text = _record_text(record).rstrip(".")
        detail = f"{label}{text}" + (f"，涉及 {', '.join(symbols)}" if symbols else "")
        risk = str(record.get("separation_risk") or "").strip().rstrip(".")
        parts.append(
            templates["why"].format(detail=detail + "。", risk=risk + "。")
            if risk
            else templates["why_no_risk"].format(detail=detail + "。")
        )
    return " ".join(parts)


def _record_label(record: dict) -> str:
    """``D1 (DEFINITION_USE STRONG): `` when known, else just a neutral prefix."""
    dependency_id = str(record.get("dependency_id") or "").strip()
    fields = [
        str(field)
        for field in (record.get("category"), record.get("strength"))
        if str(field or "").strip()
    ]
    if dependency_id and fields:
        return f"{dependency_id}（{' '.join(fields)}）："
    if dependency_id:
        return f"{dependency_id}："
    if fields:
        return f"（{' '.join(fields)}）："
    return ""


def _files_of(hunk_ids: list[str], hunk_map: dict[str, dict]) -> list[str]:
    files: list[str] = []
    for hunk_id in hunk_ids:
        path = str((hunk_map.get(hunk_id) or {}).get("file_path") or "")
        if path and path not in files:
            files.append(path)
    return files


def _symbols_of(record: dict) -> list[str]:
    symbols: list[str] = []
    for item in record.get("evidence") or []:
        symbol = str(item.get("symbol") or "").strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _related_symbols(related: list[dict], *, limit: int = 3) -> list[str]:
    symbols: list[str] = []
    for record in related:
        for symbol in _symbols_of(record):
            if symbol not in symbols:
                symbols.append(symbol)
    return symbols[:limit]


def _related_name(related: list[dict]) -> str:
    for record in related:
        text = _record_text(record).rstrip(".")
        if text:
            return text if len(text) <= 80 else text[:77] + "..."
    return ""


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else "unknown"
