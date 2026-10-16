"""HTTP provider behaviour with a stubbed transport (no network access)."""

from __future__ import annotations

import json
import urllib.error
from typing import Any


from patch_hunk_agent.llm.provider import OpenAICompatibleClient, ProviderSettings
from patch_hunk_agent.utils.logging_utils import redact


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _completion(content: str, *, model: str = "server-model") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _client(**overrides: Any) -> OpenAICompatibleClient:
    defaults: dict[str, Any] = {
        "base_url": "https://api.example.invalid/v1",
        "api_key": "unit-test-key",
        "model": "unit-model",
        "max_retries": 3,
        "retry_backoff_s": 0.0,
    }
    defaults.update(overrides)
    settings = ProviderSettings(**defaults)
    return OpenAICompatibleClient(settings)


def test_successful_completion(monkeypatch):
    captured: list[dict] = []

    def fake_urlopen(request, timeout=None):
        captured.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(_completion('{"dependencies": []}'))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    response = _client().complete([{"role": "user", "content": "hi"}])
    assert response.ok
    assert response.text == '{"dependencies": []}'
    assert response.model == "server-model"
    assert response.usage["total_tokens"] == 15
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert captured[0]["model"] == "unit-model"


def test_retryable_failures_are_retried(monkeypatch):
    calls: list[int] = []

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                request.full_url, 429, "too many requests", {}, None
            )
        return FakeResponse(_completion("{}"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    response = _client().complete([{"role": "user", "content": "hi"}])
    assert response.ok
    assert response.attempts == 3
    assert len(calls) == 3


def test_fatal_error_is_not_retried(monkeypatch):
    calls: list[int] = []

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    response = _client().complete([{"role": "user", "content": "hi"}])
    assert not response.ok
    assert "401" in (response.error or "")
    assert response.attempts == 1
    assert len(calls) == 1


def test_response_format_rejection_falls_back(monkeypatch):
    payloads: list[dict] = []

    def fake_urlopen(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        payloads.append(payload)
        if "response_format" in payload:
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "bad request",
                {},
                io_body(b'{"error": {"message": "unsupported parameter: response_format"}}'),
            )
        return FakeResponse(_completion("{}"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    response = _client().complete([{"role": "user", "content": "hi"}])
    assert response.ok
    assert len(payloads) == 2
    assert "response_format" not in payloads[1]
    assert any("response_format rejected" in note for note in response.notes)


def test_network_errors_are_reported_after_the_retry_budget(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("dns failure")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = _client(max_retries=2)
    response = client.complete([{"role": "user", "content": "hi"}])
    assert not response.ok
    assert "network error" in (response.error or "")
    assert response.attempts == 2


def test_missing_choices_is_an_error(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: FakeResponse({"model": "m"})
    )
    response = _client().complete([{"role": "user", "content": "hi"}])
    assert not response.ok
    assert "no choices" in (response.error or "")


def test_analyze_dependencies_records_usage_and_errors(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: FakeResponse(_completion('{"dependencies": []}')),
    )
    client = _client()
    record = client.analyze_dependencies({"task": "x"})
    assert record["status"] == "ok"
    assert record["parsed"] == {"dependencies": []}
    assert client.total_usage()["calls"] == 1
    assert client.total_usage()["total_tokens"] == 15
    assert client.describe()["model"] == "unit-model"


def test_settings_redact_the_key():
    settings = ProviderSettings(api_key="unit-test-key")
    assert settings.redacted()["api_key"] == "<set>"
    assert "unit-test-key" not in json.dumps(settings.redacted())


def test_redact_scrubs_credentials():
    payload = {"api_key": "sk-live-123", "Authorization": "Bearer abc", "text": "Bearer xyz"}
    scrubbed = json.dumps(redact(payload))
    assert "sk-live-123" not in scrubbed
    assert "abc" not in scrubbed
    assert "xyz" not in scrubbed


def test_client_without_a_key_warns_but_does_not_raise(caplog):
    client = OpenAICompatibleClient(ProviderSettings(api_key="", api_key_env="MISSING_ENV_VAR"))
    assert client.settings.resolved_api_key() == ""
    assert client.model == "gpt-4o-mini"


def test_unsupported_content_shape_is_handled(monkeypatch):
    payload = {
        "model": "server-model",
        "choices": [
            {"message": {"content": [{"type": "text", "text": '{"a": 1}'}]}, "finish_reason": "stop"}
        ],
    }
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: FakeResponse(payload)
    )
    response = _client().complete([{"role": "user", "content": "hi"}])
    assert response.text == '{"a": 1}'


def io_body(payload: bytes):
    import io

    return io.BytesIO(payload)
