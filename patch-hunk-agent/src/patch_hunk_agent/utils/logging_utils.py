"""Logging setup and run records (timings, tokens, errors, grouping results)."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any


__all__ = ["get_logger", "setup_logging", "RunRecorder", "utc_now_iso"]

LOGGER_NAME = "patch_hunk_agent"
_CONFIGURED = False


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def setup_logging(level: str | int = "INFO", log_file: str | Path | None = None) -> logging.Logger:
    """Configure the package logger once; safe to call repeatedly."""
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)
    resolved = logging.getLevelName(level) if isinstance(level, str) else level
    if isinstance(resolved, str):  # unknown level name
        resolved = logging.INFO
    logger.setLevel(resolved)

    if not _CONFIGURED:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        _CONFIGURED = True

    if log_file:
        target = Path(log_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        absolute = str(target.resolve())
        existing = {
            getattr(h, "baseFilename", None) for h in logger.handlers if isinstance(h, logging.FileHandler)
        }
        if absolute not in existing:
            file_handler = logging.FileHandler(target, encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
            )
            logger.addHandler(file_handler)
    return logger


@dataclass
class RunRecorder:
    """Records structured run events and writes them as JSON lines.

    Secrets must never reach this object: ``redact`` scrubs obvious key material.
    """

    path: str | Path | None = None
    enabled: bool = True
    events: list[dict[str, Any]] = field(default_factory=list)
    _start: float = field(default_factory=time.perf_counter)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)

    @property
    def elapsed_s(self) -> float:
        return round(time.perf_counter() - self._start, 6)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        if not self.enabled:
            return {}
        entry = {
            "event": event,
            "timestamp": utc_now_iso(),
            "elapsed_s": self.elapsed_s,
        }
        entry.update({k: redact(v) for k, v in fields.items()})
        self.events.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def summary(self) -> dict[str, Any]:
        return {
            "event_count": len(self.events),
            "elapsed_s": self.elapsed_s,
            "events_by_type": _count_events(self.events),
        }


def _count_events(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        name = str(event.get("event", "unknown"))
        counts[name] = counts.get(name, 0) + 1
    return counts


_SECRET_KEYS = ("api_key", "apikey", "authorization", "token_secret", "secret")


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Best-effort scrubbing of credential material from recorded payloads."""
    if _depth > 6:
        return "<max-depth>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in _SECRET_KEYS) and not str(key).lower().endswith(
                "_tokens"
            ):
                out[key] = "<redacted>"
            else:
                out[key] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        for marker in ("sk-", "Bearer "):
            if marker in value:
                return "<redacted>"
        if len(value) > 4000:
            return value[:4000] + "...<truncated>"
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return value


def _env_log_level(default: str = "INFO") -> str:
    return os.environ.get("PATCH_HUNK_AGENT_LOG_LEVEL", default)
