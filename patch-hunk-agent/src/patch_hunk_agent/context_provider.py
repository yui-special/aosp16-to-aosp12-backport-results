"""Source and target code context retrieval (section 4.3 / 12.2).

Access is read-only by construction: ``GitRepoAccess`` only issues read-only
git subcommands and ``LocalTreeAccess`` only reads files inside a declared root.
Missing context is always reported as missing - a failed lookup is never
treated as evidence that a symbol does not exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from collections.abc import Iterable

from .evidence_extractor import (
    classify_definition,
    function_scope,
    parameter_text,
    signature_changes,
)
from .utils import git_utils
from .utils.logging_utils import get_logger

__all__ = [
    "ContextConfig",
    "SourceAccess",
    "NullAccess",
    "LocalTreeAccess",
    "GitRepoAccess",
    "CodeContextProvider",
]

LOGGER = get_logger("context")


@dataclass
class ContextConfig:
    """Knobs from ``configs/default.yaml`` (``context`` section)."""

    enabled: bool = True
    context_radius: int = 40
    max_lines_per_block: int = 120
    max_total_chars: int = 24000
    max_symbol_queries: int = 12
    max_search_results: int = 20
    include_enclosing_function: bool = True
    allow_search: bool = True
    search_extensions: tuple[str, ...] = (
        ".c", ".h", ".cc", ".cpp", ".hpp", ".java", ".py", ".js", ".ts", ".go", ".rs", ".kt",
    )

    @classmethod
    def from_dict(cls, data: dict | None) -> ContextConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        if "search_extensions" in payload and isinstance(payload["search_extensions"], list):
            payload["search_extensions"] = tuple(payload["search_extensions"])
        return cls(**payload)


class SourceAccess(Protocol):  # pragma: no cover - structural typing only
    name: str

    def available(self) -> bool: ...

    def supports_revision(self, revision: str | None) -> bool: ...

    def read_file(self, revision: str | None, path: str) -> str | None: ...

    def symbol_hits(
        self, revision: str | None, symbol: str, *, max_results: int = 20
    ) -> tuple[list[dict], str | None]: ...


class NullAccess:
    """No repository access: every lookup reports 'not retrieved'."""

    name = "none"

    def available(self) -> bool:
        return False

    def supports_revision(self, revision: str | None) -> bool:  # noqa: ARG002
        return False

    def read_file(self, revision: str | None, path: str) -> str | None:  # noqa: ARG002
        return None

    def symbol_hits(
        self, revision: str | None, symbol: str, *, max_results: int = 20
    ) -> tuple[list[dict], str | None]:  # noqa: ARG002
        return [], "no repository access configured"


class LocalTreeAccess:
    """Read files from extracted source trees mapped to revision identifiers."""

    def __init__(
        self,
        trees: dict[str, str | Path] | None = None,
        *,
        default_dir: str | Path | None = None,
        name: str = "local-tree",
        extensions: Iterable[str] = ContextConfig.search_extensions,
    ) -> None:
        self.name = name
        self._trees = {str(key): Path(value) for key, value in (trees or {}).items()}
        self._default = Path(default_dir) if default_dir else None
        self._extensions = tuple(extensions)
        self._cache: dict[tuple[str, str, int], tuple[list[dict], str | None]] = {}

    def available(self) -> bool:
        return bool(self._trees or self._default)

    def supports_revision(self, revision: str | None) -> bool:
        return self._root(revision) is not None

    def revisions(self) -> list[str]:
        return sorted(self._trees)

    def _root(self, revision: str | None) -> Path | None:
        if revision is not None:
            key = str(revision)
            if key in self._trees:
                return self._trees[key]
            # An explicit default applies to any revision; otherwise a revision
            # that was never mapped must report "not found" instead of guessing.
            return self._default
        if self._default is not None:
            return self._default
        if len(self._trees) == 1:
            return next(iter(self._trees.values()))
        return None

    def _resolve(self, root: Path, path: str) -> Path | None:
        candidate = (root / path.lstrip("/")).resolve()
        try:
            if not candidate.is_relative_to(root.resolve()):
                return None
        except (OSError, ValueError):  # pragma: no cover - defensive
            return None
        return candidate

    def read_file(self, revision: str | None, path: str) -> str | None:
        root = self._root(revision)
        if root is None:
            return None
        candidate = self._resolve(root, path)
        if candidate is None or not candidate.is_file():
            return None
        try:
            return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - IO failure
            LOGGER.warning("failed to read %s: %s", candidate, exc)
            return None

    def _iter_files(self, root: Path, path: str | None) -> Iterable[Path]:
        if path:
            candidate = self._resolve(root, path)
            if candidate is None:
                return []
            if candidate.is_file():
                return [candidate]
            if candidate.is_dir():
                return sorted(p for p in candidate.rglob("*") if p.is_file())
            return []
        return sorted(p for p in root.rglob("*") if p.is_file())

    def symbol_hits(
        self, revision: str | None, symbol: str, *, max_results: int = 20
    ) -> tuple[list[dict], str | None]:
        root = self._root(revision)
        if root is None:
            return [], f"no tree configured for revision {revision!r}"
        cache_key = (str(revision), symbol, max_results)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
        hits: list[dict] = []
        for file_path in self._iter_files(root, None):
            if file_path.suffix not in self._extensions:
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if not pattern.search(line):
                    continue
                hits.append(
                    {
                        "file_path": file_path.relative_to(root).as_posix(),
                        "line": line_no,
                        "text": line,
                    }
                )
                if len(hits) >= max_results:
                    break
            if len(hits) >= max_results:
                break
        result = (hits, None)
        self._cache[cache_key] = result
        return result


class GitRepoAccess:
    """Read-only access to a git repository via the git CLI."""

    def __init__(self, repo_path: str | Path, *, name: str = "git") -> None:
        self.name = name
        self.repo_path = Path(repo_path)
        self._cache: dict[tuple[str, str, int], tuple[list[dict], str | None]] = {}

    def available(self) -> bool:
        return git_utils.git_available() and git_utils.is_git_repo(self.repo_path)

    def supports_revision(self, revision: str | None) -> bool:
        return bool(revision) and self.available()

    def read_file(self, revision: str | None, path: str) -> str | None:
        if not self.supports_revision(revision):
            return None
        return git_utils.git_show(self.repo_path, str(revision), path)

    def symbol_hits(
        self, revision: str | None, symbol: str, *, max_results: int = 20
    ) -> tuple[list[dict], str | None]:
        if not self.supports_revision(revision):
            return [], "git repository not available"
        cache_key = (str(revision), symbol, max_results)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result = git_utils.git_grep(
            self.repo_path,
            str(revision),
            r"\b" + re.escape(symbol) + r"\b",
            max_results=max_results,
        )
        self._cache[cache_key] = result
        return result


class CodeContextProvider:
    """Builds the per-hunk context record used by the dependency analyzer."""

    def __init__(
        self,
        *,
        source_access: SourceAccess | None = None,
        target_access: SourceAccess | None = None,
        config: ContextConfig | dict | None = None,
        recorder: Any | None = None,
    ) -> None:
        self.source_access = source_access or NullAccess()
        self.target_access = target_access or NullAccess()
        self.config = (
            config if isinstance(config, ContextConfig) else ContextConfig.from_dict(config or {})
        )
        self.recorder = recorder

    # -- hunk context ----------------------------------------------------- #

    def get_contexts(
        self,
        hunks: Iterable[dict],
        source_revision: str | None = None,
        target_revision: str | None = None,
    ) -> list[dict]:
        return [self.get_context(hunk, source_revision, target_revision) for hunk in hunks]

    def get_context(
        self,
        hunk: dict,
        source_revision: str | None = None,
        target_revision: str | None = None,
    ) -> dict:
        hunk_id = str(hunk.get("hunk_id"))
        if not self.config.enabled:
            return {
                "hunk_id": hunk_id,
                "source_context": {"available": False, "reason": "context retrieval disabled"},
                "target_context": {"available": False, "reason": "context retrieval disabled"},
                "symbol_evidence": [],
                "target_symbol_status": {},
                "retrieval_status": "unavailable",
                "queries": [],
                "errors": [],
            }

        symbols = hunk.get("symbols") or []
        changes = {change["name"]: change for change in signature_changes({**hunk, "symbols": symbols})}
        source_context = self._build_code_block(hunk, "source", source_revision)
        target_context = self._build_code_block(hunk, "target", target_revision)

        (
            symbol_evidence,
            target_status,
            target_definition_text,
            queries,
            errors,
            truncated,
        ) = self._collect_symbol_evidence(hunk, source_revision, target_revision, symbols, changes)

        status = self._retrieval_status(source_context, target_context, queries, errors)
        record = {
            "hunk_id": hunk_id,
            "file_path": hunk.get("file_path"),
            "target_file_path": hunk.get("target_file_path"),
            "source_revision": source_revision,
            "target_revision": target_revision,
            "source_context": source_context,
            "target_context": target_context,
            "symbol_evidence": symbol_evidence,
            "target_symbol_status": target_status,
            "target_definition_text": target_definition_text,
            "retrieval_status": status,
            "queries": queries,
            "errors": errors,
            "evidence_truncated": truncated,
        }
        if self.recorder is not None:
            self.recorder.record(
                "context_retrieved",
                hunk_id=hunk_id,
                retrieval_status=status,
                query_count=len(queries),
                error_count=len(errors),
            )
        return record

    # -- internals -------------------------------------------------------- #

    def _diff_snippet(self, hunk: dict, side: str) -> str:
        context = list(hunk.get("context_lines") or [])
        if side == "source":
            extra = [f"- {line}" for line in hunk.get("removed_lines") or []]
        else:
            extra = [f"+ {line}" for line in hunk.get("added_lines") or []]
        return "\n".join(context + extra)

    def _access_for(self, side: str) -> SourceAccess:
        return self.source_access if side == "source" else self.target_access

    def _revision_for(self, side: str, source_revision, target_revision) -> str | None:
        return source_revision if side == "source" else target_revision

    def _anchor_index(self, hunk: dict, side: str, lines: list[str]) -> int | None:
        """Line index (0-based) the retrieved block should be centred on."""
        name = _section_function_name(hunk)
        if name and self.config.include_enclosing_function:
            pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(")
            for index, line in enumerate(lines):
                if pattern.search(line):
                    scope = function_scope(lines, index)
                    if scope:
                        return index
        start = int(hunk.get("old_start") or 1)
        if side == "target" and hunk.get("new_start"):
            start = int(hunk["new_start"])
        return max(0, min(len(lines) - 1, start - 1))

    def _build_code_block(self, hunk: dict, side: str, revision: str | None) -> dict:
        snippet = self._diff_snippet(hunk, side)
        access = self._access_for(side)
        path = hunk.get("file_path") if side == "source" else hunk.get("target_file_path")
        block: dict[str, Any] = {
            "available": bool(snippet.strip()),
            "code": snippet,
            "origin": "diff_hunk",
            "file_path": path,
            "line_start": None,
            "line_end": None,
            "scope_function": _section_function_name(hunk),
            "truncated": False,
        }
        if not access.available():
            block["reason"] = "no repository access configured; hunk text only"
            return block
        if not access.supports_revision(revision):
            block["reason"] = f"no source tree or repository is mapped to revision {revision!r}"
            return block
        if not path:
            block["reason"] = "hunk has no file path"
            return block
        try:
            text = access.read_file(revision, str(path))
        except Exception as exc:  # noqa: BLE001 - access implementations are pluggable
            LOGGER.warning("context read failed for %s@%s: %s", path, revision, exc)
            block["reason"] = f"read failed: {exc}"
            return block
        if text is None:
            block["reason"] = f"file not found at revision {revision!r}"
            return block

        lines = text.splitlines()
        if not lines:
            block["reason"] = "file is empty at the requested revision"
            return block
        anchor = self._anchor_index(hunk, side, lines)
        start = max(0, int(anchor) - self.config.context_radius)
        end = min(len(lines), int(anchor) + self.config.context_radius + 1)
        scope = function_scope(lines, int(anchor)) if self.config.include_enclosing_function else None
        if scope:
            start = max(0, min(start, scope["start_line"] - 1))
            end = min(len(lines), max(end, scope["end_line"]))
        if end - start > self.config.max_lines_per_block:
            half = self.config.max_lines_per_block // 2
            start = max(0, int(anchor) - half)
            end = min(len(lines), start + self.config.max_lines_per_block)
            block["truncated"] = True
        block.update(
            {
                "available": True,
                "code": "\n".join(lines[start:end]),
                "origin": "repository",
                "line_start": start + 1,
                "line_end": end,
                "scope_function": (scope or {}).get("name") or block["scope_function"],
            }
        )
        return block

    def _candidate_symbol_order(self, symbols: list[dict], changes: dict[str, dict]) -> list[str]:
        order: list[str] = []

        def push(name: str | None) -> None:
            if name and name not in order:
                order.append(name)

        for entry in symbols:
            if entry.get("role") == "definition" and entry.get("confidence") == "high":
                push(entry.get("name"))
        for name in sorted(changes):
            push(name)
        for entry in symbols:
            if entry.get("role") == "deletion":
                push(entry.get("name"))
        for entry in symbols:
            if entry.get("role") == "reference" and entry.get("use_kind") == "call":
                push(entry.get("name"))
        for entry in symbols:
            if entry.get("role") == "reference" and entry.get("use_kind") == "write":
                push(entry.get("name"))
        for entry in symbols:
            push(entry.get("name"))
        return order[: self.config.max_symbol_queries]

    def _collect_symbol_evidence(
        self,
        hunk: dict,
        source_revision: str | None,
        target_revision: str | None,
        symbols: list[dict],
        changes: dict[str, dict],
    ) -> tuple[list[dict], dict[str, str], dict[str, str], list[dict], list[str], bool]:
        evidence: list[dict] = []
        target_status: dict[str, str] = {}
        target_definition_text: dict[str, str] = {}
        queries: list[dict] = []
        errors: list[str] = []
        truncated = False
        budget = self.config.max_total_chars

        for symbol in self._candidate_symbol_order(symbols, changes):
            target_definitions: list[dict] = []
            target_references = 0
            target_query_ok = False
            for side in ("source", "target"):
                access = self._access_for(side)
                revision = self._revision_for(side, source_revision, target_revision)
                if not access.available():
                    queries.append(
                        {
                            "kind": "symbol_search",
                            "side": side,
                            "symbol": symbol,
                            "revision": revision,
                            "status": "skipped",
                            "detail": "no repository access configured for this side",
                        }
                    )
                    continue
                if not access.supports_revision(revision):
                    queries.append(
                        {
                            "kind": "symbol_search",
                            "side": side,
                            "symbol": symbol,
                            "revision": revision,
                            "status": "skipped",
                            "detail": f"no source tree or repository is mapped to revision {revision!r}",
                        }
                    )
                    continue
                if not self.config.allow_search:
                    queries.append(
                        {
                            "kind": "symbol_search",
                            "side": side,
                            "symbol": symbol,
                            "revision": revision,
                            "status": "skipped",
                            "detail": "search disabled by configuration",
                        }
                    )
                    continue
                hits, error = access.symbol_hits(
                    revision, symbol, max_results=self.config.max_search_results
                )
                if error:
                    errors.append(f"{side} search for {symbol!r} failed: {error}")
                    queries.append(
                        {
                            "kind": "symbol_search",
                            "side": side,
                            "symbol": symbol,
                            "revision": revision,
                            "status": "error",
                            "detail": error,
                        }
                    )
                    continue
                target_query_ok = target_query_ok or side == "target"
                queries.append(
                    {
                        "kind": "symbol_search",
                        "side": side,
                        "symbol": symbol,
                        "revision": revision,
                        "status": "ok",
                        "matches": len(hits),
                    }
                )
                for hit in hits:
                    classification = classify_definition(hit.get("text", ""))
                    is_definition = bool(classification and classification[1] == symbol)
                    if side == "target":
                        if is_definition:
                            target_definitions.append(hit)
                        else:
                            target_references += 1
                    if budget <= 0:
                        truncated = True
                        continue
                    text = hit.get("text", "")
                    budget -= len(text)
                    evidence.append(
                        {
                            "symbol": symbol,
                            "revision": revision,
                            "side": side,
                            "kind": "definition" if is_definition else "reference",
                            "definition_kind": classification[0] if is_definition else None,
                            "file_path": hit.get("file_path"),
                            "line": hit.get("line"),
                            "text": text,
                            "source": "repository",
                        }
                    )
            target_status[symbol] = self._target_status_for(
                symbol, target_definitions, target_references, target_query_ok, changes
            )
            if target_definitions:
                target_definition_text[symbol] = target_definitions[0].get("text", "").strip()

        unknown = sorted(symbol for symbol, value in target_status.items() if value == "unknown")
        if unknown:
            errors.append(
                f"target version state could not be determined for {len(unknown)} symbol(s), "
                f"e.g. {unknown[0]!r}"
            )
        return evidence, target_status, target_definition_text, queries, errors, truncated

    @staticmethod
    def _target_status_for(
        symbol: str,
        definitions: list[dict],
        reference_count: int,
        query_ok: bool,
        changes: dict[str, dict],
    ) -> str:
        if not query_ok:
            return "unknown"
        if not definitions:
            return "partially_present" if reference_count else "absent"
        change = changes.get(symbol)
        if change:
            after = change.get("after_params")
            for hit in definitions:
                if parameter_text(hit.get("text", "")) == after:
                    return "present"
            return "absent"
        return "present"

    def _retrieval_status(
        self,
        source_context: dict,
        target_context: dict,
        queries: list[dict],
        errors: list[str],
    ) -> str:
        source_ok = source_context.get("origin") == "repository"
        target_ok = target_context.get("origin") == "repository"
        if not source_ok and not target_ok:
            return "unavailable"
        ran = [query for query in queries if query.get("status") in ("ok", "error")]
        failed = [query for query in queries if query.get("status") == "error"]
        if not ran or len(failed) == len(ran):
            return "failed" if ran else "partial"
        if failed or errors or not (source_ok and target_ok):
            return "partial"
        return "complete"

    # -- on-demand evidence (phase D) ------------------------------------- #

    def additional_evidence(self, request: dict) -> dict:
        """Targeted follow-up query: ``{"symbol", "side", "kind"}``.

        Every query must name a symbol or a file; open-ended repository scans are
        rejected so the analyzer cannot turn into a crawler.
        """
        symbol = str(request.get("symbol") or "").strip()
        side = str(request.get("side") or "target")
        path = str(request.get("file_path") or "").strip()
        if not symbol and not path:
            return {"status": "rejected", "detail": "request must name a symbol or a file_path"}
        access = self._access_for("source" if side == "source" else "target")
        revision = request.get("revision")
        result: dict[str, Any] = {
            "request": request,
            "side": side,
            "symbol": symbol or None,
            "file_path": path or None,
        }
        if not access.available():
            result.update({"status": "unavailable", "detail": "no repository access configured"})
            return result
        if not access.supports_revision(revision):
            result.update(
                {
                    "status": "unavailable",
                    "detail": f"no source tree or repository is mapped to revision {revision!r}",
                }
            )
            return result
        if path and not symbol:
            text = access.read_file(revision, path)
            if text is None:
                result.update({"status": "not_found", "detail": "file not found"})
                return result
            lines = text.splitlines()[: self.config.max_lines_per_block]
            result.update({"status": "ok", "code": "\n".join(lines), "truncated": len(text.splitlines()) > len(lines)})
            return result
        hits, error = access.symbol_hits(
            revision, symbol, max_results=self.config.max_search_results
        )
        if error:
            result.update({"status": "failed", "detail": error})
            return result
        enriched: list[dict] = []
        for hit in hits:
            classification = classify_definition(hit.get("text", ""))
            enriched.append(
                {
                    **hit,
                    "kind": "definition"
                    if classification and classification[1] == symbol
                    else "reference",
                    "side": side,
                    "revision": revision,
                }
            )
        result.update({"status": "ok", "matches": enriched})
        return result


def _section_function_name(hunk: dict) -> str | None:
    section = (hunk.get("section") or "").strip()
    if not section:
        return None
    match = re.search(r"([A-Za-z_]\w*)\s*\(", section)
    if match:
        return match.group(1)
    bare = re.search(r"([A-Za-z_]\w*)", section)
    return bare.group(1) if bare else None
