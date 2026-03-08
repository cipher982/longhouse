"""Pytest configuration for eval tests.

This module provides fixtures and configuration for running eval tests
with REAL LLM calls (OpenAI API). Evals test actual AI quality.

NOTE: Evals cost money. Not run in CI. Use for manual quality testing.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from zerg import database as database_module
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import User
from zerg.models.enums import UserRole


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
    rubric: str | None = None
    min_score: float | None = None
    commis_id: int | None = None  # For commis-specific assertions
    path: str | None = None  # For artifact assertions
    tool: str | None = None  # For commis_tool_called


class Message(BaseModel):
    """Single message in a conversation."""

    role: str  # 'user' | 'assistant' | 'system'
    content: str


class EvalCase(BaseModel):
    """Single eval test case.

    Either 'input' (single-turn) or 'messages' (multi-turn) must be provided.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    category: str
    description: str | None = None
    input: str | None = None  # Single-turn: just the task
    messages: list[Message] | None = None  # Multi-turn: full conversation
    timeout: int = 120
    assert_: list[EvalAssertion] = Field(default_factory=list, alias="assert")
    tags: list[str] = Field(default_factory=list)


class VariantConfig(BaseModel):
    """Variant configuration for A/B testing."""

    model: str | None = None
    temperature: float = 0.0
    reasoning_effort: str = "none"
    prompt_version: int | None = None
    overrides: dict = Field(default_factory=dict)


class EvalDataset(BaseModel):
    """Complete eval dataset from YAML."""

    version: str
    description: str | None = None
    variants: dict[str, VariantConfig] = Field(default_factory=dict)
    cases: list[EvalCase]


@pytest.fixture
def db_session(tmp_path):
    """Create an isolated SQLite database for eval runs."""
    db_path = tmp_path / "evals.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    SessionLocal = make_sessionmaker(engine)

    previous_engine = database_module.default_engine
    previous_factory = database_module.default_session_factory
    database_module.default_engine = engine
    database_module.default_session_factory = SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        database_module.default_engine = previous_engine
        database_module.default_session_factory = previous_factory
        engine.dispose()


@pytest.fixture
def test_user(db_session):
    """Provide the canonical eval user expected by auto-seed helpers."""
    user = User(email="dev@local", role=UserRole.ADMIN.value)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Pytest configuration hooks
# ---------------------------------------------------------------------------


STATIC_EVAL_MARKERS = {
    "critical": "Critical test that must pass for deployment",
    "fast": "Fast test (< 5s execution time)",
    "slow": "Slow test (> 30s execution time)",
    "optional": "Optional test (informational, no block)",
    "quick": "Quick sanity check test",
    "conversational": "Conversational test category",
    "infrastructure": "Infrastructure test category",
    "multi_step": "Multi-step test category",
    "tool_usage": "Tool usage test category",
    "edge_case": "Edge case test category",
    "performance": "Performance test category",
    "commis": "Commis delegation test",
    "multi_turn": "Multi-turn conversation test",
    "llm_graded": "Test uses LLM-as-judge for evaluation",
    "live_only": "Test requires live mode (real OpenAI API)",
}


def pytest_configure(config):
    """Register static markers and dataset tags used by evals."""
    registered_tags = set(STATIC_EVAL_MARKERS)
    for marker, description in STATIC_EVAL_MARKERS.items():
        config.addinivalue_line("markers", f"{marker}: {description}")

    for dataset in load_eval_datasets().values():
        for case in dataset.cases:
            for tag in case.tags:
                if tag in registered_tags:
                    continue
                config.addinivalue_line("markers", f"{tag}: Eval dataset tag")
                registered_tags.add(tag)


def pytest_addoption(parser):
    """Add custom CLI options for evals."""
    parser.addoption(
        "--variant",
        action="store",
        default="baseline",
        help="Variant to run (baseline, improved, etc.)",
    )
    # Note: Tag filtering uses pytest's built-in -m flag
    # Example: pytest -m critical
    # Example: pytest -m "fast and not slow"


def pytest_collection_modifyitems(config, items):
    """Apply pytest markers based on YAML tags.

    All evals now use REAL LLM calls (EVAL_MODE=live).
    """
    # Load datasets to access tags
    datasets = load_eval_datasets()

    for item in items:
        # Apply markers based on YAML tags
        # Test ID format: "test_eval_case[dataset::case_id]"
        if "test_eval_case[" in item.nodeid:
            # Extract dataset and case_id from test node
            # Format: test_eval_case[live::greeting_quality]
            test_param = item.nodeid.split("[")[1].rstrip("]")
            if "::" in test_param:
                dataset_name, case_id = test_param.split("::", 1)
                dataset = datasets.get(dataset_name)
                if dataset:
                    # Find the case and apply its tags as markers
                    for case in dataset.cases:
                        if case.id == case_id:
                            for tag in case.tags:
                                # Apply marker (e.g., @pytest.mark.critical)
                                item.add_marker(getattr(pytest.mark, tag))
                            break


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
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
def eval_runner(db_session, test_user, request, eval_case):
    """Create an EvalRunner instance for testing.

    If a variant is specified via --variant flag, it will be applied.
    """
    from evals.runner import EvalRunner
    from zerg.services.auto_seed import _seed_server_knowledge
    from zerg.services.auto_seed import _seed_user_context
    from zerg.services.oikos_service import OikosService

    # Evals should reflect a real "dev@local" environment. In tests we create the
    # dev user deterministically, but user context (servers/integrations) is not
    # present unless it's seeded from scripts/user_context.local.json.
    #
    # Live evals in particular assume servers exist; without this, the model can
    # correctly answer "(No servers configured)" which fails the dataset rubric.
    _seed_user_context()
    _seed_server_knowledge()
    db_session.expire_all()

    oikos_service = OikosService(db_session)
    runner = EvalRunner(oikos_service, test_user.id)

    # Apply variant overrides (if the dataset defines variants)
    dataset_name, _case = eval_case
    variant_name = request.config.getoption("--variant", "baseline")

    datasets = load_eval_datasets()
    dataset = datasets.get(dataset_name)
    if dataset and dataset.variants:
        runner = runner.with_variant(variant_name, {k: v.model_dump() for k, v in dataset.variants.items()})

    return runner


@pytest.fixture(autouse=True)
def live_mode():
    """Configure evals for live mode (REAL LLM calls).

    All evals use real OpenAI API - no stubbing.
    Restores real OpenAI module if tests/conftest.py stubbed it.
    """
    import sys

    # Ensure EVAL_MODE=live is set
    os.environ["EVAL_MODE"] = "live"

    # Remove any OpenAI stub from test conftest
    # This ensures real API calls for both oikos and grader
    _openai_stub_backup = sys.modules.get("openai")
    if "openai" in sys.modules:
        del sys.modules["openai"]

    yield

    # Restore stub after test (for test isolation)
    if _openai_stub_backup is not None:
        sys.modules["openai"] = _openai_stub_backup


# ---------------------------------------------------------------------------
# Result merging (pytest hooks)
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session, exitstatus):
    """Merge per-commis temp files after all tests complete.

    This runs in both master and commis processes, so we need to check
    if we're the master before merging.
    """
    import os

    # Only merge on master node (not on xdist commis)
    if os.environ.get("PYTEST_XDIST_COMMIS"):
        return

    # Only merge if we have temp files (tests actually ran)
    from evals.results_store import cleanup_temp_results
    from evals.results_store import get_temp_results_dir
    from evals.results_store import merge_results

    temp_dir = get_temp_results_dir()
    temp_files = list(temp_dir.glob("*.jsonl"))

    if not temp_files:
        return

    # Get variant name from CLI
    variant = session.config.getoption("--variant", "baseline")

    # Merge results
    try:
        result_file = merge_results(variant=variant)
        print(f"\n✅ Results saved to: {result_file}")

        # Cleanup temp files
        cleanup_temp_results()
    except Exception as e:
        print(f"\n⚠️  Failed to merge results: {e}")
