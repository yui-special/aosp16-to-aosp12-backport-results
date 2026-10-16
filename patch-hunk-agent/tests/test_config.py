"""Configuration: env expansion, provider resolution, no secrets in the repo."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from patch_hunk_agent.config import (
    HunkAgentConfig,
    _load_yaml_minimal,
    deep_merge,
    expand_env,
    load_yaml,
)


@pytest.mark.parametrize("name", ["default.yaml", "models.yaml"])
def test_builtin_yaml_reader_matches_pyyaml(project_root: Path, name: str):
    path = project_root / "configs" / name
    text = path.read_text(encoding="utf-8")
    assert _load_yaml_minimal(text) == load_yaml(path)


def test_env_expansion_with_and_without_defaults(monkeypatch):
    monkeypatch.setenv("PATCH_HUNK_AGENT_TEST_VALUE", "explicit")
    assert expand_env("${PATCH_HUNK_AGENT_TEST_VALUE}") == "explicit"
    assert expand_env("${PATCH_HUNK_AGENT_TEST_MISSING:-fallback}") == "fallback"
    assert expand_env("${PATCH_HUNK_AGENT_TEST_MISSING}") == ""
    assert expand_env({"a": ["${PATCH_HUNK_AGENT_TEST_VALUE}"]}) == {"a": ["explicit"]}


def test_deep_merge_is_recursive():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    override = {"a": {"c": 9}}
    assert deep_merge(base, override) == {"a": {"b": 1, "c": 9}, "d": 3}
    assert base == {"a": {"b": 1, "c": 2}, "d": 3}


def test_defaults_apply_when_the_file_is_missing(tmp_path):
    config = HunkAgentConfig.load(tmp_path / "does-not-exist.yaml")
    assert config.provider == "mock-heuristic"
    assert config.analyzer.max_format_retries == 2
    assert config.grouping.promoted_categories == ("DEFINITION_USE", "INTERFACE_SIGNATURE")
    assert config.context.max_lines_per_block == 120
    assert config.config_path is None


def test_repo_config_loads_with_the_documented_values(project_root):
    config = HunkAgentConfig.load(project_root / "configs" / "default.yaml")
    assert config.provider == "mock-heuristic"
    assert config.evidence.mode == "heuristic"
    assert config.analyzer.max_pairs_per_call == 8
    assert config.validator.cross_check_context is True
    assert config.output.directory == "results"
    assert config.config_path is not None


def test_overrides_win_over_the_file(project_root):
    config = HunkAgentConfig.load(
        project_root / "configs" / "default.yaml",
        overrides={"patch_hunk_agent": {"analyzer": {"max_pairs_per_call": 3}}},
    )
    assert config.analyzer.max_pairs_per_call == 3


def test_env_overrides_are_applied(monkeypatch, project_root):
    monkeypatch.setenv("PATCH_HUNK_AGENT_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PATCH_HUNK_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("PATCH_HUNK_AGENT_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("PATCH_HUNK_AGENT_MODEL", "some-model")
    monkeypatch.setenv("PATCH_HUNK_AGENT_API_KEY", "unit-test-key")
    config = HunkAgentConfig.load(project_root / "configs" / "default.yaml")
    assert config.provider == "openai"
    assert config.provider_config["base_url"] == "https://example.invalid/v1"
    assert config.provider_config["model"] == "some-model"
    assert config.provider_config["api_key"] == "unit-test-key"


def test_api_key_is_never_taken_from_the_config_file(project_root, monkeypatch):
    monkeypatch.delenv("PATCH_HUNK_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = HunkAgentConfig.load(project_root / "configs" / "default.yaml")
    serialised = json.dumps(config.as_dict())
    assert "api_key" not in json.dumps(config.provider_config) or "api_key" in json.dumps(
        {"api_key_env": "OPENAI_API_KEY"}
    )
    assert "sk-" not in serialised
    assert config.provider_config.get("api_key_env") != "PATCH_HUNK_AGENT_API_KEY"


def test_shipped_configs_contain_no_secrets(project_root):
    for path in sorted((project_root / "configs").glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        assert "sk-" not in text
        assert "Bearer " not in text
    env_example = project_root / ".env.example"
    if env_example.is_file():  # developers may have copied (and renamed) it
        text = env_example.read_text(encoding="utf-8")
        assert "PATCH_HUNK_AGENT_API_KEY=" in text
        assert "sk-" not in text


def test_unknown_provider_kind_is_rejected():
    from patch_hunk_agent.llm import build_llm_client

    with pytest.raises(ValueError):
        build_llm_client("nope", provider_config={"kind": "not-a-provider"})


def test_unknown_provider_name_uses_the_endpoint_from_env(
    monkeypatch, project_root, clean_environ
):
    """A vendor-style alias plus env endpoint config must still work."""
    for name in ("PATCH_HUNK_AGENT_BASE_URL", "PATCH_HUNK_AGENT_MODEL", "PATCH_HUNK_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATCH_HUNK_AGENT_PROVIDER", "deepseek")
    monkeypatch.setenv("PATCH_HUNK_AGENT_BASE_URL", "https://relay.example.invalid/v1")
    monkeypatch.setenv("PATCH_HUNK_AGENT_MODEL", "deepseek-chat")
    monkeypatch.setenv("PATCH_HUNK_AGENT_API_KEY", "unit-key")

    config = HunkAgentConfig.load(project_root / "configs" / "default.yaml")

    assert config.provider == "deepseek"
    assert config.provider_config["kind"] == "openai_compatible"
    assert config.provider_config["base_url"] == "https://relay.example.invalid/v1"
    assert config.provider_config["model"] == "deepseek-chat"
    assert config.provider_config["api_key"] == "unit-key"


def test_unknown_provider_name_without_an_endpoint_fails_loudly(
    monkeypatch, project_root, clean_environ
):
    for name in ("PATCH_HUNK_AGENT_BASE_URL", "PATCH_HUNK_AGENT_MODEL", "PATCH_HUNK_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATCH_HUNK_AGENT_PROVIDER", "deepseek")

    with pytest.raises(ValueError) as excinfo:
        HunkAgentConfig.load(project_root / "configs" / "default.yaml")

    assert "unknown provider 'deepseek'" in str(excinfo.value)
    assert "mock-heuristic" in str(excinfo.value)  # the message lists the known ones


def test_reasoning_effort_applies_to_implicit_providers(monkeypatch, project_root, clean_environ):
    for name in ("PATCH_HUNK_AGENT_BASE_URL", "PATCH_HUNK_AGENT_MODEL", "PATCH_HUNK_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATCH_HUNK_AGENT_PROVIDER", "deepseek")
    monkeypatch.setenv("PATCH_HUNK_AGENT_BASE_URL", "https://relay.example.invalid/v1")
    monkeypatch.setenv("PATCH_HUNK_AGENT_API_KEY", "unit-key")
    monkeypatch.setenv("PATCH_HUNK_AGENT_REASONING_EFFORT", "none")

    config = HunkAgentConfig.load(project_root / "configs" / "default.yaml")

    assert config.provider_config["extra_body"] == {"reasoning_effort": "none"}


def test_reasoning_effort_reaches_provider_settings(monkeypatch, project_root, clean_environ):
    from patch_hunk_agent.llm.provider import ProviderSettings

    monkeypatch.setenv("PATCH_HUNK_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("PATCH_HUNK_AGENT_BASE_URL", "https://relay.example.invalid/v1")
    monkeypatch.delenv("PATCH_HUNK_AGENT_REASONING_EFFORT", raising=False)

    config = HunkAgentConfig.load(project_root / "configs" / "default.yaml")
    settings = ProviderSettings.from_dict(config.provider_config)

    # configs/models.yaml ships the switch for reasoning models
    assert settings.extra_body == {"reasoning_effort": "none"}
    assert settings.endpoint() == "https://relay.example.invalid/v1/chat/completions"


def test_env_file_is_loaded_and_real_env_wins(tmp_path, monkeypatch, real_env_file_loader, clean_environ):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "export PATCH_HUNK_AGENT_UNIT_MODE=from-file\n"
        'PATCH_HUNK_AGENT_UNIT_QUOTED="quoted value"\n'
        "PATCH_HUNK_AGENT_UNIT_EXISTING=file-value\n"
        "NOT_A_PAIR\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PATCH_HUNK_AGENT_UNIT_EXISTING", "env-value")
    monkeypatch.delenv("PATCH_HUNK_AGENT_UNIT_MODE", raising=False)
    monkeypatch.delenv("PATCH_HUNK_AGENT_UNIT_QUOTED", raising=False)

    loaded = real_env_file_loader(env_file)

    assert loaded["PATCH_HUNK_AGENT_UNIT_MODE"] == "from-file"
    assert loaded["PATCH_HUNK_AGENT_UNIT_QUOTED"] == "quoted value"
    assert os.environ["PATCH_HUNK_AGENT_UNIT_MODE"] == "from-file"
    assert os.environ["PATCH_HUNK_AGENT_UNIT_EXISTING"] == "env-value"
    assert "NOT_A_PAIR" not in loaded


def test_missing_env_file_is_not_an_error(tmp_path, real_env_file_loader):
    assert real_env_file_loader(tmp_path / "does-not-exist.env") == {}


def test_dotenv_supplies_provider_model_and_key(
    tmp_path, monkeypatch, project_root, real_env_file_loader, clean_environ
):
    (tmp_path / ".env").write_text(
        "PATCH_HUNK_AGENT_PROVIDER=openai\n"
        "PATCH_HUNK_AGENT_BASE_URL=https://relay.example.invalid/v1\n"
        "PATCH_HUNK_AGENT_MODEL=unit-model\n"
        "PATCH_HUNK_AGENT_API_KEY=unit-key\n",
        encoding="utf-8",
    )
    for name in (
        "PATCH_HUNK_AGENT_PROVIDER",
        "PATCH_HUNK_AGENT_BASE_URL",
        "PATCH_HUNK_AGENT_MODEL",
        "PATCH_HUNK_AGENT_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("patch_hunk_agent.config.load_env_file", real_env_file_loader)

    config = HunkAgentConfig.load(project_root / "configs" / "default.yaml")

    assert config.provider == "openai"
    assert config.provider_config["base_url"] == "https://relay.example.invalid/v1"
    assert config.provider_config["model"] == "unit-model"
    assert config.provider_config["api_key"] == "unit-key"


def test_provider_dispatch():
    from patch_hunk_agent.llm import (
        HeuristicMockClient,
        MockLLMClient,
        OpenAICompatibleClient,
        build_llm_client,
    )

    assert isinstance(build_llm_client("mock"), MockLLMClient)
    assert isinstance(build_llm_client("mock-heuristic"), HeuristicMockClient)
    client = build_llm_client(
        "openai", provider_config={"kind": "openai_compatible", "base_url": "https://x.invalid/v1"}
    )
    assert isinstance(client, OpenAICompatibleClient)
    assert client.settings.endpoint() == "https://x.invalid/v1/chat/completions"
