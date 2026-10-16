"""LLM client interface plus shared prompt/parse plumbing."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Iterable

from ..utils.json_utils import loads_lenient
from ..utils.logging_utils import get_logger, redact

__all__ = ["LLMResponse", "LLMClient", "load_prompt", "DEFAULT_SYSTEM_PROMPT_FILE"]

LOGGER = get_logger("llm")

DEFAULT_SYSTEM_PROMPT_FILE = "dependency_analysis.txt"
_FALLBACK_SYSTEM_PROMPT = (
    "You are a Patch Hunk Dependency Analysis Agent. Analyse cross-hunk "
    "dependencies and return JSON only."
)


def prompt_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "prompts"


def load_prompt(name: str, prompt_dir_path: str | Path | None = None) -> str:
    """Read a prompt file, falling back to a minimal built-in prompt."""
    base = Path(prompt_dir_path) if prompt_dir_path else prompt_dir()
    candidate = Path(name)
    if not candidate.is_absolute():
        candidate = base / name
    try:
        return candidate.read_text(encoding="utf-8").strip()
    except OSError as exc:
        LOGGER.warning("could not read prompt %s (%s); using built-in prompt", candidate, exc)
        return _FALLBACK_SYSTEM_PROMPT


@dataclass
class LLMResponse:
    text: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0
    error: str | None = None
    raw: Any | None = None
    attempts: int = 1
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


class LLMClient(ABC):
    """Single entry point for every model call in this package."""

    name = "llm"

    def __init__(
        self,
        *,
        model: str = "",
        system_prompt: str | None = None,
        prompt_path: str | None = None,
        recorder: Any | None = None,
    ) -> None:
        self.model = model
        self.recorder = recorder
        self._system_prompt = (
            system_prompt
            if system_prompt is not None
            else load_prompt(prompt_path or DEFAULT_SYSTEM_PROMPT_FILE)
        )
        self._usage_total: dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
            "failed_calls": 0,
            "estimated": False,
        }

    # -- to be implemented by providers ----------------------------------- #

    @abstractmethod
    def complete(
        self,
        messages: list[dict],
        *,
        json_output: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Run one chat completion."""

    # -- shared behaviour -------------------------------------------------- #

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def build_messages(self, payload: dict, extra_messages: Iterable[dict] | None = None) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": self._system_prompt}]
        messages.append(
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        )
        messages.extend(extra_messages or [])
        return messages

    def analyze_dependencies(
        self,
        payload: dict,
        *,
        extra_messages: Iterable[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Call the model and return a structured call record.

        The returned mapping contains ``parsed`` (JSON payload or ``None``),
        ``raw_text``, ``usage`` and ``error``.  Malformed JSON is reported, never
        repaired silently into a claim.
        """
        messages = self.build_messages(payload, extra_messages)
        started = time.perf_counter()
        response = self.complete(
            messages, json_output=True, temperature=temperature, max_tokens=max_tokens
        )
        latency = round(time.perf_counter() - started, 6)
        self._accumulate(response)

        record: dict[str, Any] = {
            "status": "ok",
            "parsed": None,
            "raw_text": response.text,
            "model": response.model or self.model,
            "usage": response.usage,
            "latency_s": latency,
            "error": response.error,
            "repairs": [],
            "attempts": response.attempts,
            "notes": list(response.notes),
            "messages": messages,
        }
        if response.error:
            record["status"] = "error"
            if self.recorder:
                self.recorder.record(
                    "llm_call_failed", model=record["model"], error=response.error, latency_s=latency
                )
            return record

        try:
            parsed, repairs = loads_lenient(response.text)
        except json.JSONDecodeError as exc:
            record["status"] = "parse_error"
            record["error"] = f"invalid JSON from model: {exc}"
            if self.recorder:
                self.recorder.record(
                    "llm_parse_error",
                    model=record["model"],
                    error=record["error"],
                    raw_text=response.text[:2000],
                )
            return record

        if isinstance(parsed, list):
            parsed = {"dependencies": parsed, "unresolved_questions": []}
        if not isinstance(parsed, dict):
            record["status"] = "parse_error"
            record["error"] = f"model returned {type(parsed).__name__}, expected a JSON object"
            return record

        if isinstance(parsed.get("dependencies"), dict):  # tolerate single-object form
            parsed["dependencies"] = [parsed["dependencies"]]
        record["parsed"] = parsed
        record["repairs"] = repairs
        if self.recorder:
            self.recorder.record(
                "llm_call",
                model=record["model"],
                usage=response.usage,
                latency_s=latency,
                repair_count=len(repairs),
                dependency_count=len(parsed.get("dependencies") or []),
                notes=list(response.notes),
            )
        return record

    def _accumulate(self, response: LLMResponse) -> None:
        usage = response.usage or {}
        self._usage_total["calls"] += 1
        if response.error:
            self._usage_total["failed_calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                self._usage_total[key] += int(value)
        if usage.get("estimated"):
            self._usage_total["estimated"] = True

    def total_usage(self) -> dict:
        return dict(self._usage_total)

    def reset_usage(self) -> None:
        for key in self._usage_total:
            self._usage_total[key] = 0 if key != "estimated" else False

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "usage": self.total_usage()}

    @staticmethod
    def log_safe(payload: Any) -> Any:
        return redact(payload)
