"""Offline providers: scripted responses and a deterministic heuristic stand-in.

``HeuristicMockClient`` exists so the whole pipeline can run (and regress) with
no network.  It is a rule engine over machine-extracted evidence, **not** a
language model, and it must never be presented as evidence about model quality.
"""

from __future__ import annotations

import json
from typing import Any
from collections.abc import Callable, Iterable

from .base import LLMClient, LLMResponse

__all__ = ["MockLLMClient", "HeuristicMockClient", "heuristic_responder"]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockLLMClient(LLMClient):
    """Returns scripted responses (or calls ``responder``) without any network."""

    name = "mock"

    def __init__(
        self,
        *,
        responses: Iterable[Any] | None = None,
        responder: Callable[[dict], Any] | None = None,
        model: str = "mock-llm",
        system_prompt: str | None = None,
        recorder: Any | None = None,
    ) -> None:
        super().__init__(model=model, system_prompt=system_prompt, recorder=recorder)
        self._responses = list(responses or [])
        self._responder = responder
        self.calls: list[dict] = []

    def complete(
        self,
        messages: list[dict],
        *,
        json_output: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "json_output": json_output})
        if self._responses:
            response = self._responses.pop(0)
        elif self._responder is not None:
            payload = _payload_from_messages(messages)
            response = self._responder(payload)
        else:
            return LLMResponse(
                model=self.model,
                error="mock client has no scripted response left and no responder",
            )
        if isinstance(response, (dict, list)):
            text = json.dumps(response, ensure_ascii=False)
        else:
            text = str(response)
        prompt_tokens = _estimate_tokens(json.dumps(messages, ensure_ascii=False))
        completion_tokens = _estimate_tokens(text)
        return LLMResponse(
            text=text,
            model=self.model,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "estimated": True,
            },
        )


class HeuristicMockClient(MockLLMClient):
    """Deterministic stand-in that answers from machine evidence only."""

    name = "mock-heuristic"

    def __init__(self, *, model: str = "mock-heuristic", system_prompt: str | None = None, **_: Any) -> None:
        super().__init__(responder=heuristic_responder, model=model, system_prompt=system_prompt)


def _payload_from_messages(messages: list[dict]) -> dict:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        try:
            payload = json.loads(message.get("content") or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


_CATEGORY_BY_SIGNAL = {
    "signature_change": "INTERFACE_SIGNATURE",
    "definition_use": "DEFINITION_USE",
    "removed_definition_still_referenced": "DEFINITION_USE",
    "shared_write": "SHARED_STATE",
    "write_read": "SHARED_STATE",
}

_RISK_BY_CATEGORY = {
    "INTERFACE_SIGNATURE": (
        "Backporting only one side leaves the caller and the definition with "
        "mismatched parameter lists, which breaks compilation or silently changes behaviour."
    ),
    "DEFINITION_USE": (
        "The reference may remain unresolved if the definition is not ported with it."
    ),
    "SHARED_STATE": (
        "Initialisation and use of the same state would diverge, leaving the state "
        "in an inconsistent form."
    ),
    "CONTEXT_DEPENDENCY": "The change may depend on structural context introduced by the other hunk.",
}


def heuristic_responder(payload: dict) -> dict:
    """Turn machine candidates into dependency records (offline, deterministic)."""
    dependencies: list[dict] = []
    questions: list[dict] = []
    for pair in payload.get("candidate_pairs") or []:
        hint = str(pair.get("machine_hint") or "NONE")
        if hint in ("NONE", "WEAK_HINT"):
            continue
        signals = pair.get("signals") or []
        if not signals:
            continue
        primary = _primary_signal(signals)
        category = _CATEGORY_BY_SIGNAL.get(primary["signal"], "CONTEXT_DEPENDENCY")
        status = primary.get("target_symbol_status") or "unknown"
        strength = "STRONG" if hint == "STRONG_HINT" else "CONDITIONAL"
        needs_more = status == "unknown"
        signal_evidence = [
            item
            for item in (pair.get("machine_evidence") or [])
            if item.get("signal") == primary["signal"]
        ]
        evidence_source = signal_evidence or list(pair.get("machine_evidence") or [])
        evidence = [
            {
                "file_path": item.get("file_path", ""),
                "symbol": item.get("symbol", ""),
                "description": item.get("description", ""),
                **({"line": item["line"]} if item.get("line") else {}),
            }
            for item in evidence_source[:3]
        ]
        if not evidence:
            evidence = [
                {
                    "file_path": "",
                    "symbol": primary["symbol"],
                    "description": primary["description"],
                }
            ]
        dependencies.append(
            {
                "source_hunk": primary["direction"].split("->")[0],
                "target_hunk": primary["direction"].split("->")[1],
                "category": category,
                "strength": strength,
                "evidence": evidence,
                "separation_risk": _RISK_BY_CATEGORY.get(
                    category, _RISK_BY_CATEGORY["CONTEXT_DEPENDENCY"]
                ),
                "target_version_status": status,
                "reasoning_summary": (
                    f"Machine signal {primary['signal']} on symbol {primary['symbol']}; "
                    f"target version state reported as {status}."
                ),
                "needs_more_evidence": needs_more,
                "origin": "heuristic_mock",
            }
        )
        if needs_more:
            questions.append(
                {
                    "question": (
                        f"Is {primary['symbol']} already available in the target version in the "
                        "form this patch expects?"
                    ),
                    "hunk_ids": list(pair.get("pair") or []),
                    "reason": "target version state is unknown",
                    "needs_more_evidence": True,
                    "suggested_query": {"symbol": primary["symbol"], "side": "target"},
                }
            )
    return {
        "status": "partial" if questions else "complete",
        "dependencies": dependencies,
        "unresolved_questions": questions,
    }


def _primary_signal(signals: list[dict]) -> dict:
    priority = ("signature_change", "definition_use", "removed_definition_still_referenced", "shared_write", "write_read")
    for name in priority:
        for signal in signals:
            if signal.get("signal") == name:
                return signal
    return signals[0]
