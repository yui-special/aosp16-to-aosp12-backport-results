"""Configuration loading: YAML files, environment expansion, no secrets in code."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context_provider import ContextConfig
from .dependency_analyzer import AnalyzerConfig
from .dependency_validator import ValidatorConfig
from .grouping_engine import GroupingConfig
from .partition_validator import PartitionValidatorConfig
from .utils.logging_utils import get_logger

__all__ = [
    "HunkAgentConfig",
    "EvidenceConfig",
    "LoggingConfig",
    "OutputConfig",
    "load_yaml",
    "load_config",
    "load_env_file",
    "implicit_provider_from_env",
    "apply_reasoning_effort",
    "expand_env",
    "deep_merge",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_MODELS_PATH",
    "ENV_FILE_NAME",
]

LOGGER = get_logger("config")

DEFAULT_CONFIG_PATH = "configs/default.yaml"
DEFAULT_MODELS_PATH = "configs/models.yaml"
ENV_FILE_NAME = ".env"
REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-?(?P<default>[^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` recursively."""
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            name = match.group("name")
            default = match.group("default")
            if name in os.environ:
                return os.environ[name]
            return default if default is not None else ""

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


def load_env_file(
    path: str | Path | None = None, *, override: bool = False, filename: str = ENV_FILE_NAME
) -> dict:
    """Load ``KEY=VALUE`` pairs from a ``.env`` file into ``os.environ``.

    Looked up in the current directory first, then at the repository root.  Real
    environment variables win unless ``override`` is set, so a shell export always
    beats a stale file.  Secrets stay out of the YAML configuration this way.
    """
    candidates = [Path(path)] if path else [Path.cwd() / filename, REPO_ROOT / filename]
    loaded: dict[str, str] = {}
    for candidate in candidates:
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[len("export ") :].strip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or not key:
                continue
            text = value.strip()
            if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
                text = text[1:-1]
            if override or key not in os.environ:
                os.environ[key] = text
            loaded[key] = text
        break
    if loaded:
        LOGGER.debug("loaded %d variable(s) from %s", len(loaded), candidates[0])
    return loaded


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into ``base`` (returns a new dict)."""
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def implicit_provider_from_env(provider_name: str, analyzer: dict) -> dict | None:
    """Build an OpenAI-compatible provider from the environment.

    Lets ``PATCH_HUNK_AGENT_PROVIDER`` name something that is not in
    ``configs/models.yaml`` (for example a vendor-specific alias) as long as the
    endpoint is supplied through the environment.  Returns ``None`` when there is
    nothing to build from, so the caller can report the misconfiguration.
    """
    base_url = os.environ.get("PATCH_HUNK_AGENT_BASE_URL", "").strip()
    model = os.environ.get("PATCH_HUNK_AGENT_MODEL", "").strip()
    api_key = os.environ.get("PATCH_HUNK_AGENT_API_KEY", "").strip()
    if not (base_url or model or api_key):
        return None
    return {
        "kind": "openai_compatible",
        "name": provider_name,
        "base_url": base_url or "https://api.openai.com/v1",
        "model": model or str(analyzer.get("model") or provider_name),
        "api_key": api_key,
        "api_key_env": "OPENAI_API_KEY",
    }


REASONING_EFFORT_ENV = "PATCH_HUNK_AGENT_REASONING_EFFORT"


def apply_reasoning_effort(provider_config: dict) -> dict:
    """Apply ``PATCH_HUNK_AGENT_REASONING_EFFORT`` to an OpenAI-compatible provider.

    Reasoning models can spend the whole output budget on hidden reasoning and
    return an empty answer (observed with ``deepseek-flash``: 35k characters of
    ``reasoning_content`` and zero ``content``, ``finish_reason=length``).  Setting
    this to ``none`` (or ``low``/``medium``/``high`` where supported) makes the
    request ask for a direct answer.  A YAML ``extra_body.reasoning_effort`` on the
    provider still wins, so this is only a default.
    """
    effort = os.environ.get(REASONING_EFFORT_ENV, "").strip()
    if not effort:
        return provider_config
    if provider_config.get("kind") not in (
        "openai",
        "openai_compatible",
        "openai-compatible",
    ):
        return provider_config
    extra_body = dict(provider_config.get("extra_body") or {})
    extra_body.setdefault("reasoning_effort", effort)
    provider_config["extra_body"] = extra_body
    return provider_config


def _load_yaml_minimal(text: str) -> dict:
    """Restricted YAML reader used when PyYAML is unavailable.

    Supports nested mappings, lists of scalars, comments, quoted scalars and
    inline flow scalars - exactly what the shipped configuration files use.
    """
    entries: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        entries.append((indent, raw_line.strip()))
    if not entries:
        return {}
    value, _ = _parse_block(entries, 0, entries[0][0])
    return value if isinstance(value, dict) else {"__value__": value}


def _parse_block(entries: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if entries[index][1].startswith("- "):
        return _parse_sequence(entries, index, indent)
    return _parse_mapping(entries, index, indent)


def _parse_sequence(entries: list[tuple[int, str]], index: int, indent: int) -> tuple[list, int]:
    items: list[Any] = []
    while index < len(entries) and entries[index][0] == indent and entries[index][1].startswith("- "):
        content = entries[index][1][2:].strip()
        if content:
            items.append(_parse_scalar(content))
            index += 1
            continue
        if index + 1 < len(entries) and entries[index + 1][0] > indent:
            nested, index = _parse_block(entries, index + 1, entries[index + 1][0])
            items.append(nested)
            continue
        items.append(None)
        index += 1
    return items, index


def _parse_mapping(entries: list[tuple[int, str]], index: int, indent: int) -> tuple[dict, int]:
    mapping: dict[str, Any] = {}
    while index < len(entries) and entries[index][0] == indent:
        content = entries[index][1]
        if content.startswith("- "):
            break
        if ":" not in content:
            raise ValueError(f"unsupported YAML line (no key): {content!r}")
        key, _, rest = content.partition(":")
        key = key.strip().strip('"').strip("'")
        rest = rest.strip()
        if rest:
            mapping[key] = _parse_scalar(rest)
            index += 1
            continue
        if index + 1 < len(entries) and entries[index + 1][0] > indent:
            value, index = _parse_block(entries, index + 1, entries[index + 1][0])
            mapping[key] = value
            continue
        mapping[key] = None
        index += 1
    return mapping, index


def _parse_scalar(text: str) -> Any:
    value = text.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_flow(inner)]
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        out: dict[str, Any] = {}
        if not inner:
            return out
        for part in _split_flow(inner):
            if ":" not in part:
                continue
            key, _, raw = part.partition(":")
            out[key.strip().strip('"').strip("'")] = _parse_scalar(raw)
        return out
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    # YAML 1.1 null tokens only; ``none`` is a plain string (used for
    # reasoning_effort), so it must not be turned into None.
    if lowered in ("null", "~"):
        return None
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _split_flow(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    quote = ""
    for char in text:
        if quote:
            current += char
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            current += char
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def load_yaml(path: str | Path) -> dict:
    """Load a YAML file with PyYAML when available, else the built-in subset."""
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError:
        LOGGER.debug("PyYAML not installed; using the built-in YAML subset reader")
        return _load_yaml_minimal(text) or {}
    data = yaml.safe_load(text)
    return data or {}


@dataclass
class EvidenceConfig:
    """Knobs for phase A symbol extraction."""

    mode: str = "heuristic"
    include_context_references: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> EvidenceConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class LoggingConfig:
    level: str = "INFO"
    record_run: bool = True
    log_file: str | None = None
    record_file: str | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> LoggingConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class OutputConfig:
    directory: str = "results"

    @classmethod
    def from_dict(cls, data: dict | None) -> OutputConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class HunkAgentConfig:
    """Fully resolved configuration for one run."""

    context: ContextConfig = field(default_factory=ContextConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    analyzer: AnalyzerConfig = field(default_factory=AnalyzerConfig)
    grouping: GroupingConfig = field(default_factory=GroupingConfig)
    validator: ValidatorConfig = field(default_factory=ValidatorConfig)
    partition_validator: PartitionValidatorConfig = field(default_factory=PartitionValidatorConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    provider: str = "mock-heuristic"
    provider_config: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    analysis_version: str = "1.0"
    config_path: str | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, raw: dict | None, *, config_path: str | None = None, models: dict | None = None
    ) -> HunkAgentConfig:
        data = dict(raw or {})
        section = data.get("patch_hunk_agent", data)
        analyzer = dict(section.get("analyzer", {}))
        provider = str(analyzer.pop("provider", "mock-heuristic"))
        analyzer.pop("api_key", None)
        analyzer.pop("base_url", None)
        env_provider = os.environ.get("PATCH_HUNK_AGENT_PROVIDER")
        if env_provider:
            provider = env_provider

        models_data = dict(models or {})
        providers = dict(models_data.get("providers", {}))
        provider_config = dict(providers.get(provider, {}))
        if not provider_config:
            # A provider name that is not in models.yaml is fine when the
            # environment supplies an OpenAI-compatible endpoint; otherwise the
            # run would silently degrade to an empty provider, so fail loudly.
            implicit = implicit_provider_from_env(provider, analyzer)
            if implicit is None:
                raise ValueError(
                    f"unknown provider {provider!r}: not found in {DEFAULT_MODELS_PATH} "
                    f"(known: {', '.join(sorted(providers)) or 'none'}) and no "
                    "PATCH_HUNK_AGENT_BASE_URL was provided"
                )
            provider_config = implicit
        apply_reasoning_effort(provider_config)
        if provider_config.get("kind") in ("openai", "openai_compatible", "openai-compatible"):
            provider_config["base_url"] = (
                os.environ.get("PATCH_HUNK_AGENT_BASE_URL")
                or provider_config.get("base_url")
                or "https://api.openai.com/v1"
            )
            provider_config["model"] = (
                os.environ.get("PATCH_HUNK_AGENT_MODEL")
                or analyzer.get("model")
                or provider_config.get("model")
                or "gpt-4o-mini"
            )
            api_key = os.environ.get("PATCH_HUNK_AGENT_API_KEY")
            if api_key:
                provider_config["api_key"] = api_key
        provider_config.setdefault("model", analyzer.get("model") or provider_config.get("model", provider))

        logging_config = LoggingConfig.from_dict(section.get("logging"))
        env_level = os.environ.get("PATCH_HUNK_AGENT_LOG_LEVEL")
        if env_level:
            logging_config.level = env_level

        return cls(
            context=ContextConfig.from_dict(section.get("context")),
            evidence=EvidenceConfig.from_dict(section.get("evidence")),
            analyzer=AnalyzerConfig.from_dict(analyzer),
            grouping=GroupingConfig.from_dict(section.get("grouping")),
            validator=ValidatorConfig.from_dict(section.get("validator")),
            partition_validator=PartitionValidatorConfig.from_dict(
                section.get("partition_validator")
            ),
            logging=logging_config,
            output=OutputConfig.from_dict(section.get("output")),
            provider=provider,
            provider_config=provider_config,
            models=models_data,
            analysis_version=str(section.get("analysis_version", "1.0")),
            config_path=config_path,
            raw=data,
        )

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        models_path: str | Path | None = None,
        overrides: dict | None = None,
    ) -> HunkAgentConfig:
        # ``.env`` first: the provider/model/key overrides are read from the
        # environment right below, and secrets never belong in the YAML files.
        load_env_file()
        resolved_path = path or os.environ.get("PATCH_HUNK_AGENT_CONFIG") or DEFAULT_CONFIG_PATH
        raw: dict = {}
        candidate = Path(resolved_path)
        if not candidate.is_file() and not candidate.is_absolute():
            # Fall back to the repository root so the agent works from any cwd
            # (for example when it is invoked from the PortGPT checkout).
            candidate = REPO_ROOT / resolved_path
        if candidate.is_file():
            raw = load_yaml(candidate)
        else:
            LOGGER.debug("config file %s not found; using built-in defaults", candidate)

        models_file = Path(models_path) if models_path else Path(DEFAULT_MODELS_PATH)
        if not models_file.is_file() and not models_file.is_absolute():
            models_file = REPO_ROOT / DEFAULT_MODELS_PATH
        models = load_yaml(models_file) if models_file.is_file() else {}
        merged = deep_merge(expand_env(raw), overrides or {})
        return cls.from_dict(merged, config_path=str(candidate) if candidate.is_file() else None, models=expand_env(models))

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "provider_config": dict(self.provider_config),
            "context": vars(self.context),
            "evidence": vars(self.evidence),
            "analyzer": vars(self.analyzer),
            "grouping": vars(self.grouping),
            "validator": vars(self.validator),
            "partition_validator": vars(self.partition_validator),
            "logging": vars(self.logging),
            "output": vars(self.output),
        }


def load_config(
    path: str | Path | None = None, *, overrides: dict | None = None
) -> HunkAgentConfig:
    return HunkAgentConfig.load(path, overrides=overrides)
