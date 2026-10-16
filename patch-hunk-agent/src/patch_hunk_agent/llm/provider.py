"""OpenAI-compatible chat-completions provider (stdlib HTTP only)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..utils.logging_utils import get_logger
from .base import LLMClient, LLMResponse

__all__ = ["ProviderSettings", "OpenAICompatibleClient", "DEFAULT_API_KEY_ENV"]

LOGGER = get_logger("llm.provider")

DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class ProviderSettings:
    """Everything needed to call a model, all of it configurable."""

    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    api_key_env: str = DEFAULT_API_KEY_ENV
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_s: float = 60.0
    max_retries: int = 3
    retry_backoff_s: float = 1.5
    json_response_format: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    path: str = "/chat/completions"

    @classmethod
    def from_dict(cls, data: dict | None) -> ProviderSettings:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        return cls(**payload)

    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""

    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + (self.path if self.path.startswith("/") else "/" + self.path)

    def redacted(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": "<set>" if self.resolved_api_key() else "<missing>",
            "api_key_env": self.api_key_env,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "json_response_format": self.json_response_format,
        }


class OpenAICompatibleClient(LLMClient):
    """Client for any endpoint exposing ``POST {base_url}/chat/completions``."""

    name = "openai-compatible"

    def __init__(self, settings: ProviderSettings | dict | None = None, **kwargs: Any) -> None:
        if isinstance(settings, dict):
            settings = ProviderSettings.from_dict(settings)
        settings = settings or ProviderSettings()
        super().__init__(
            model=kwargs.pop("model", settings.model),
            system_prompt=kwargs.pop("system_prompt", None),
            prompt_path=kwargs.pop("prompt_path", None),
            recorder=kwargs.pop("recorder", None),
        )
        self.settings = settings
        if not self.settings.resolved_api_key():
            LOGGER.warning(
                "no API key found (checked %s); requests will fail with 401",
                self.settings.api_key_env,
            )

    def complete(
        self,
        messages: list[dict],
        *,
        json_output: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": self.settings.max_tokens if max_tokens is None else max_tokens,
        }
        payload.update(self.settings.extra_body)
        use_json_format = bool(json_output and self.settings.json_response_format)
        if use_json_format:
            payload["response_format"] = {"type": "json_object"}

        notes: list[str] = []
        last_error = ""
        attempts = 0
        started = time.perf_counter()
        for attempt in range(1, max(1, self.settings.max_retries) + 1):
            attempts = attempt
            try:
                status, body = self._post(payload)
            except _RetryableError as exc:
                last_error = str(exc)
                if attempt < self.settings.max_retries:
                    time.sleep(self.settings.retry_backoff_s * attempt)
                    continue
                break
            except _FatalError as exc:
                last_error = str(exc)
                if "response_format" in str(exc) and use_json_format:
                    payload.pop("response_format", None)
                    use_json_format = False
                    notes.append("response_format rejected by endpoint; retried without it")
                    continue
                break
            else:
                return self._parse_body(body, status, started, attempt, notes)
        return LLMResponse(
            model=self.model,
            error=last_error or "request failed",
            latency_s=round(time.perf_counter() - started, 6),
            attempts=max(1, attempts),
            notes=notes,
        )

    # -- HTTP ------------------------------------------------------------- #

    def _post(self, payload: dict) -> tuple[int, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.settings.resolved_api_key()}",
        }
        headers.update(self.settings.extra_headers)
        request = urllib.request.Request(
            self.settings.endpoint(), data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_s) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, _safe_json(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            message = f"HTTP {exc.code}: {detail}"
            if exc.code in _RETRY_STATUS:
                raise _RetryableError(message) from exc
            raise _FatalError(message) from exc
        except urllib.error.URLError as exc:
            raise _RetryableError(f"network error: {exc.reason}") from exc
        except TimeoutError as exc:  # pragma: no cover - socket timeouts vary
            raise _RetryableError(f"timeout after {self.settings.timeout_s}s") from exc

    def _parse_body(
        self, body: Any, status: int, started: float, attempt: int, notes: list[str]
    ) -> LLMResponse:
        latency = round(time.perf_counter() - started, 6)
        if not isinstance(body, dict):
            return LLMResponse(
                model=self.model,
                error=f"unexpected response body (status {status})",
                latency_s=latency,
                attempts=attempt,
                notes=notes,
            )
        choices = body.get("choices") or []
        if not choices:
            error = body.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            return LLMResponse(
                model=str(body.get("model") or self.model),
                error=f"no choices in response: {detail or 'unknown error'}",
                latency_s=latency,
                attempts=attempt,
                notes=notes,
                raw=body,
            )
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # some gateways return content parts
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        usage = body.get("usage") or {}
        return LLMResponse(
            text=str(content or ""),
            model=str(body.get("model") or self.model),
            usage={
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "estimated": False,
            },
            latency_s=latency,
            attempts=attempt,
            notes=notes,
            raw={"finish_reason": choices[0].get("finish_reason")},
        )


class _RetryableError(Exception):
    pass


class _FatalError(Exception):
    pass


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
