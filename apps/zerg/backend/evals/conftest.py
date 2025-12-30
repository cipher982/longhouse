"""Pytest configuration for eval tests.

This module provides fixtures and configuration for running eval tests
in hermetic mode (stubbed LLM, no real SSH).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from sqlalchemy.orm import Session

# Import fixtures from the main conftest.py
# Add parent dir to path so we can import from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import test fixtures (db_session, test_user, etc.)
# This makes them available to eval tests
pytest_plugins = ["tests.conftest"]


# ---------------------------------------------------------------------------
# Pydantic models for YAML dataset validation
# ---------------------------------------------------------------------------


class EvalAssertion(BaseModel):
    """Single assertion within a test case."""

    type: str
    value: str | int | None = None
    max: int | None = None
    min: int | None = None
    count: int | None = None
    case_insensitive: bool = False


class EvalCase(BaseModel):
    """Single eval test case."""

    id: str
    category: str
    description: str | None = None
    input: str | None = None
    timeout: int = 120
    assert_: list[EvalAssertion] = []
    tags: list[str] = []

    class Config:
        """Pydantic config."""

        fields = {"assert_": "assert"}


class EvalDataset(BaseModel):
    """Complete eval dataset from YAML."""

    version: str
    description: str | None = None
    cases: list[EvalCase]


# ---------------------------------------------------------------------------
# Pytest configuration hooks
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    """Add custom CLI options for evals."""
    parser.addoption(
        "--variant",
        action="store",
        default="baseline",
        help="Variant to run (baseline, improved, etc.)",
    )


def pytest_collection_modifyitems(config, items):
    """Filter tests by variant if specified."""
    variant = config.getoption("--variant")
    if variant:
        # For Phase 1, we just run all tests with the specified variant
        # Variant filtering will be implemented in Phase 2
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def load_eval_datasets():
    """Load all YAML datasets from evals/datasets/ directory.

    This is a plain function (not a fixture) so it can be called during
    pytest test generation phase.
    """
    datasets_dir = Path(__file__).parent / "datasets"
    datasets = {}

    for yaml_file in datasets_dir.glob("*.yml"):
        with open(yaml_file) as f:
            data = yaml.safe_load(f)
            dataset = EvalDataset(**data)
            datasets[yaml_file.stem] = dataset

    return datasets


@pytest.fixture(scope="session")
def eval_datasets():
    """Provide loaded datasets as a session fixture."""
    return load_eval_datasets()


@pytest.fixture
def eval_runner(db_session, test_user):
    """Create an EvalRunner instance for testing."""
    from evals.runner import EvalRunner
    from zerg.services.supervisor_service import SupervisorService

    supervisor_service = SupervisorService(db_session)
    return EvalRunner(supervisor_service, test_user.id)


@pytest.fixture(autouse=True)
def hermetic_mode():
    """Ensure hermetic mode is enabled for all eval tests."""
    # Set environment variable to signal hermetic mode
    os.environ["EVAL_MODE"] = "hermetic"
    yield
    # Cleanup
    os.environ.pop("EVAL_MODE", None)


# ---------------------------------------------------------------------------
# Hermetic stubs (already provided by tests/conftest.py)
# ---------------------------------------------------------------------------
# The existing conftest.py already stubs:
# - OpenAI/LangChain (via _StubChatOpenAI)
# - Database (per-test session via db_session fixture)
# - Auth (AUTH_DISABLED=1 via TESTING=1)
#
# No additional stubs needed for Phase 1 - we're calling SupervisorService
# directly which uses the existing test infrastructure.
