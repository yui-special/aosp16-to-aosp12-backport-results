"""Shared test fixtures.  No test touches the network or any real repository."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = ROOT / "tests" / "fixtures"
TREES = FIXTURES / "trees"

SOURCE_REVISION = "SRC"
TARGET_REVISION = "TGT"

from patch_hunk_agent.config import load_env_file as REAL_LOAD_ENV_FILE  # noqa: E402


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Tests must not depend on the developer's own .env file."""
    monkeypatch.setattr("patch_hunk_agent.config.load_env_file", lambda *args, **kwargs: {})


@pytest.fixture
def real_env_file_loader():
    """The real .env loader, for the tests that exercise it."""
    return REAL_LOAD_ENV_FILE


@pytest.fixture
def clean_environ():
    """Restore os.environ after a test that loads a .env file."""
    import os

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def source_tree() -> Path:
    return TREES / "source"


@pytest.fixture(scope="session")
def target_tree() -> Path:
    return TREES / "target"


@pytest.fixture(scope="session")
def source_access(source_tree: Path):
    from patch_hunk_agent.context_provider import LocalTreeAccess

    return LocalTreeAccess({SOURCE_REVISION: str(source_tree)}, name="test-source")


@pytest.fixture(scope="session")
def target_access(target_tree: Path):
    from patch_hunk_agent.context_provider import LocalTreeAccess

    return LocalTreeAccess({TARGET_REVISION: str(target_tree)}, name="test-target")


@pytest.fixture
def patched_pipeline(source_access, target_access):
    """A pipeline wired to the offline heuristic provider and the fixture trees."""
    return build_test_pipeline(
        source_dir=TREES / "source", target_dir=TREES / "target"
    )


@pytest.fixture
def contextless_pipeline():
    """A pipeline with no repository access at all (missing-context scenario)."""
    return build_test_pipeline(source_dir=None, target_dir=None)


def build_test_pipeline(*, source_dir, target_dir, llm=None):
    """Build a pipeline with the offline heuristic provider and explicit trees."""
    from patch_hunk_agent.config import HunkAgentConfig
    from patch_hunk_agent.context_provider import (
        CodeContextProvider,
        LocalTreeAccess,
    )
    from patch_hunk_agent.llm import HeuristicMockClient
    from patch_hunk_agent.pipeline import HunkGroupingPipeline

    config = HunkAgentConfig.load(str(ROOT / "configs" / "default.yaml"))
    # The documented Test A-E scenarios describe the deterministic dependency rules
    # (STRONG => same group), so pin them to the components algorithm.  Global
    # (model-proposed) partitioning is covered in tests/test_global_grouper.py.
    config.grouping.mode = "components"
    source_access = (
        LocalTreeAccess({SOURCE_REVISION: str(source_dir)}) if source_dir else None
    )
    target_access = (
        LocalTreeAccess({TARGET_REVISION: str(target_dir)}) if target_dir else None
    )
    provider = CodeContextProvider(
        source_access=source_access, target_access=target_access, config=config.context
    )
    return HunkGroupingPipeline(
        config, llm=llm if llm is not None else HeuristicMockClient(), context_provider=provider
    )
