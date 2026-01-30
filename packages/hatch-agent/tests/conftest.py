"""Test configuration and fixtures."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from hatch.context import ExecutionContext
from hatch.context import clear_context_cache


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear context cache before each test."""
    clear_context_cache()
    yield
    clear_context_cache()


@pytest.fixture
def laptop_context() -> ExecutionContext:
    """Context for laptop environment (not in container, writable home)."""
    return ExecutionContext(in_container=False, home_writable=True)


@pytest.fixture
def container_readonly_context() -> ExecutionContext:
    """Context for container with read-only home."""
    return ExecutionContext(in_container=True, home_writable=False)


@pytest.fixture
def container_writable_context() -> ExecutionContext:
    """Context for container with writable home."""
    return ExecutionContext(in_container=True, home_writable=True)


@pytest.fixture
def mock_zai_key():
    """Mock ZAI_API_KEY environment variable."""
    with mock.patch.dict(os.environ, {"ZAI_API_KEY": "test-zai-key"}):
        yield "test-zai-key"


@pytest.fixture
def mock_openai_key():
    """Mock OPENAI_API_KEY environment variable."""
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}):
        yield "test-openai-key"


@pytest.fixture
def clean_env():
    """Clean environment without API keys."""
    keys_to_remove = [
        "ZAI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
    ]
    original = {k: os.environ.get(k) for k in keys_to_remove if k in os.environ}
    for k in keys_to_remove:
        os.environ.pop(k, None)
    yield
    # Restore
    for k, v in original.items():
        if v is not None:
            os.environ[k] = v


def has_zai_credentials() -> bool:
    """Check if ZAI credentials are available."""
    return bool(os.environ.get("ZAI_API_KEY"))


def has_openai_credentials() -> bool:
    """Check if OpenAI credentials are available."""
    return bool(os.environ.get("OPENAI_API_KEY"))


def has_bedrock_credentials() -> bool:
    """Check if Bedrock credentials are likely available (AWS profile configured)."""
    # Just check if aws command exists and profile is set
    import shutil

    return shutil.which("aws") is not None


def has_claude_cli() -> bool:
    """Check if claude CLI is installed."""
    import shutil

    return shutil.which("claude") is not None


def has_codex_cli() -> bool:
    """Check if codex CLI is installed."""
    import shutil

    return shutil.which("codex") is not None


def has_gemini_cli() -> bool:
    """Check if gemini CLI is installed."""
    import shutil

    return shutil.which("gemini") is not None
