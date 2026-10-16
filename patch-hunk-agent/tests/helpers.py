"""Helpers shared by the test modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FIXTURES, SOURCE_REVISION, TARGET_REVISION, TREES


def fixture_path(name: str) -> Path:
    return FIXTURES / f"{name}.diff"


def parse_fixture(name: str, *, patch_id: str | None = None):
    from patch_hunk_agent.patch_parser import PatchParser

    return PatchParser().parse_file(fixture_path(name), patch_id=patch_id or name)


def fixture_hunks(name: str, *, patch_id: str | None = None) -> list[dict]:
    from patch_hunk_agent.evidence_extractor import enrich_hunks

    document = parse_fixture(name, patch_id=patch_id)
    return enrich_hunks(document.hunk_dicts())


def fixture_contexts(hunks: list[dict], *, with_access: bool = True, config: dict | None = None):
    from patch_hunk_agent.context_provider import (
        CodeContextProvider,
        LocalTreeAccess,
    )

    if with_access:
        provider = CodeContextProvider(
            source_access=LocalTreeAccess({SOURCE_REVISION: str(TREES / "source")}),
            target_access=LocalTreeAccess({TARGET_REVISION: str(TREES / "target")}),
            config=config,
        )
        return provider.get_contexts(hunks, SOURCE_REVISION, TARGET_REVISION)
    return CodeContextProvider(config=config).get_contexts(hunks, None, None)


def dependency(
    source: str,
    target: str,
    *,
    category: str = "DEFINITION_USE",
    strength: str = "STRONG",
    dependency_id: str = "D1",
    target_version_status: str = "absent",
    evidence: list[dict] | None = None,
    separation_risk: str = "the call would stay unresolved",
    needs_more_evidence: bool = False,
    reasoning_summary: str = "test dependency",
) -> dict:
    return {
        "dependency_id": dependency_id,
        "source_hunk": source,
        "target_hunk": target,
        "category": category,
        "strength": strength,
        "evidence": evidence
        if evidence is not None
        else [{"file_path": "src/example.c", "symbol": "helper", "line": 12, "description": "d"}],
        "separation_risk": separation_risk,
        "target_version_status": target_version_status,
        "reasoning_summary": reasoning_summary,
        "needs_more_evidence": needs_more_evidence,
    }


def as_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
