"""Tests for backend configurations."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from hatch.backends import (
    Backend,
    BackendConfig,
    configure_bedrock,
    configure_codex,
    configure_gemini,
    configure_zai,
    get_config,
)
from hatch.context import ExecutionContext


class TestBackendEnum:
    """Tests for Backend enum."""

    def test_backend_values(self):
        """Backend enum has expected values."""
        assert Backend.ZAI.value == "zai"
        assert Backend.BEDROCK.value == "bedrock"
        assert Backend.CODEX.value == "codex"
        assert Backend.GEMINI.value == "gemini"

    def test_backend_from_string(self):
        """Backend can be created from string."""
        assert Backend("zai") == Backend.ZAI
        assert Backend("bedrock") == Backend.BEDROCK
        assert Backend("codex") == Backend.CODEX
        assert Backend("gemini") == Backend.GEMINI

    def test_invalid_backend(self):
        """Invalid backend string raises ValueError."""
        with pytest.raises(ValueError):
            Backend("invalid")

    def test_backend_is_string(self):
        """Backend values are strings."""
        for backend in Backend:
            assert isinstance(backend.value, str)
            assert backend == backend.value


class TestBackendConfig:
    """Tests for BackendConfig dataclass."""

    def test_build_env_merges_correctly(self):
        """build_env merges with os.environ correctly."""
        config = BackendConfig(
            cmd=["test"],
            env={"NEW_VAR": "new_value"},
        )
        env = config.build_env()

        # New var should be present
        assert env["NEW_VAR"] == "new_value"
        # PATH should still be there from os.environ
        assert "PATH" in env

    def test_build_env_unsets_vars(self):
        """build_env removes vars in env_unset."""
        with mock.patch.dict(os.environ, {"REMOVE_ME": "original"}):
            config = BackendConfig(
                cmd=["test"],
                env={},
                env_unset=["REMOVE_ME"],
            )
            env = config.build_env()
            assert "REMOVE_ME" not in env

    def test_build_env_overrides_existing(self):
        """build_env allows overriding existing env vars."""
        with mock.patch.dict(os.environ, {"OVERRIDE_ME": "original"}):
            config = BackendConfig(
                cmd=["test"],
                env={"OVERRIDE_ME": "new_value"},
            )
            env = config.build_env()
            assert env["OVERRIDE_ME"] == "new_value"

    def test_stdin_data_default_none(self):
        """stdin_data defaults to None."""
        config = BackendConfig(cmd=["test"], env={})
        assert config.stdin_data is None


class TestConfigureZai:
    """Tests for z.ai backend configuration."""

    def test_requires_api_key(self, clean_env, laptop_context):
        """Raises ValueError when no API key available."""
        with pytest.raises(ValueError, match="ZAI_API_KEY not set"):
            configure_zai("test prompt", laptop_context)

    def test_uses_env_api_key(self, laptop_context):
        """Uses ZAI_API_KEY from environment."""
        with mock.patch.dict(os.environ, {"ZAI_API_KEY": "env-key"}):
            config = configure_zai("test prompt", laptop_context)
            assert config.env["ANTHROPIC_AUTH_TOKEN"] == "env-key"

    def test_api_key_argument_overrides_env(self, laptop_context):
        """api_key argument overrides environment variable."""
        with mock.patch.dict(os.environ, {"ZAI_API_KEY": "env-key"}):
            config = configure_zai("test prompt", laptop_context, api_key="arg-key")
            assert config.env["ANTHROPIC_AUTH_TOKEN"] == "arg-key"

    def test_command_structure(self, mock_zai_key, laptop_context):
        """Command has correct structure."""
        config = configure_zai("test prompt", laptop_context)
        assert config.cmd == [
            "claude",
            "--print",
            "-",
            "--output-format",
            "text",
            "--dangerously-skip-permissions",
        ]

    def test_command_with_stream_json(self, mock_zai_key, laptop_context):
        """Command includes stream-json and partial messages when requested."""
        config = configure_zai(
            "test prompt",
            laptop_context,
            output_format="stream-json",
            include_partial_messages=True,
        )
        assert config.cmd == [
            "claude",
            "--print",
            "-",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--include-partial-messages",
        ]

    def test_env_vars_set_correctly(self, mock_zai_key, laptop_context):
        """Environment variables set correctly."""
        config = configure_zai("test prompt", laptop_context)
        assert config.env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
        assert config.env["ANTHROPIC_AUTH_TOKEN"] == mock_zai_key
        assert config.env["ANTHROPIC_MODEL"] == "glm-5"

    def test_unsets_bedrock_vars(self, mock_zai_key, laptop_context):
        """Unsets CLAUDE_CODE_USE_BEDROCK and ANTHROPIC_API_KEY."""
        config = configure_zai("test prompt", laptop_context)
        assert "CLAUDE_CODE_USE_BEDROCK" in config.env_unset
        assert "ANTHROPIC_API_KEY" in config.env_unset

    def test_prompt_via_stdin(self, mock_zai_key, laptop_context):
        """Prompt passed via stdin_data."""
        config = configure_zai("my test prompt", laptop_context)
        assert config.stdin_data == b"my test prompt"

    def test_custom_model(self, mock_zai_key, laptop_context):
        """Custom model can be specified."""
        config = configure_zai("test", laptop_context, model="custom-model")
        assert config.env["ANTHROPIC_MODEL"] == "custom-model"

    def test_custom_base_url(self, mock_zai_key, laptop_context):
        """Custom base URL can be specified."""
        config = configure_zai("test", laptop_context, base_url="https://custom.api")
        assert config.env["ANTHROPIC_BASE_URL"] == "https://custom.api"

    def test_container_readonly_sets_home(self, mock_zai_key, container_readonly_context):
        """Sets HOME=/tmp in container with read-only home."""
        config = configure_zai("test", container_readonly_context)
        assert config.env["HOME"] == "/tmp"

    def test_container_writable_no_home_override(
        self, mock_zai_key, container_writable_context
    ):
        """Does not override HOME in container with writable home."""
        config = configure_zai("test", container_writable_context)
        assert "HOME" not in config.env


class TestConfigureBedrock:
    """Tests for Bedrock backend configuration."""

    def test_command_structure(self, laptop_context):
        """Command has correct structure."""
        config = configure_bedrock("test prompt", laptop_context)
        assert config.cmd == [
            "claude",
            "--print",
            "-",
            "--output-format",
            "text",
            "--dangerously-skip-permissions",
        ]

    def test_command_with_stream_json(self, laptop_context):
        """Command includes stream-json and partial messages when requested."""
        config = configure_bedrock(
            "test prompt",
            laptop_context,
            output_format="stream-json",
            include_partial_messages=True,
        )
        assert config.cmd == [
            "claude",
            "--print",
            "-",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--include-partial-messages",
        ]

    def test_env_vars_set_correctly(self, laptop_context):
        """Environment variables set correctly."""
        config = configure_bedrock("test prompt", laptop_context)
        assert config.env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert config.env["AWS_PROFILE"] == "zh-qa-engineer"
        assert config.env["AWS_REGION"] == "us-east-1"
        assert "claude-sonnet" in config.env["ANTHROPIC_MODEL"]

    def test_custom_aws_profile(self, laptop_context):
        """Custom AWS profile can be specified."""
        config = configure_bedrock("test", laptop_context, aws_profile="custom-profile")
        assert config.env["AWS_PROFILE"] == "custom-profile"

    def test_custom_aws_region(self, laptop_context):
        """Custom AWS region can be specified."""
        config = configure_bedrock("test", laptop_context, aws_region="eu-west-1")
        assert config.env["AWS_REGION"] == "eu-west-1"

    def test_custom_model(self, laptop_context):
        """Custom model can be specified."""
        config = configure_bedrock("test", laptop_context, model="anthropic.claude-v2")
        assert config.env["ANTHROPIC_MODEL"] == "anthropic.claude-v2"

    def test_prompt_via_stdin(self, laptop_context):
        """Prompt passed via stdin_data."""
        config = configure_bedrock("my bedrock prompt", laptop_context)
        assert config.stdin_data == b"my bedrock prompt"

    def test_no_env_unset(self, laptop_context):
        """No environment variables need to be unset."""
        config = configure_bedrock("test", laptop_context)
        assert config.env_unset == []

    def test_container_readonly_sets_home(self, container_readonly_context):
        """Sets HOME=/tmp in container with read-only home."""
        config = configure_bedrock("test", container_readonly_context)
        assert config.env["HOME"] == "/tmp"


class TestConfigureCodex:
    """Tests for Codex backend configuration."""

    def test_requires_api_key(self, clean_env, laptop_context):
        """Raises ValueError when no API key available."""
        with pytest.raises(ValueError, match="OPENAI_API_KEY not set"):
            configure_codex("test prompt", laptop_context)

    def test_uses_env_api_key(self, laptop_context):
        """Uses OPENAI_API_KEY from environment."""
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "env-key"}):
            config = configure_codex("test prompt", laptop_context)
            assert config.env["OPENAI_API_KEY"] == "env-key"

    def test_api_key_argument_overrides_env(self, laptop_context):
        """api_key argument overrides environment variable."""
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "env-key"}):
            config = configure_codex("test", laptop_context, api_key="arg-key")
            assert config.env["OPENAI_API_KEY"] == "arg-key"

    def test_command_structure_full_auto(self, mock_openai_key, laptop_context):
        """Command has correct structure with full-auto."""
        config = configure_codex("test prompt", laptop_context)
        assert config.cmd == ["codex", "exec", "-", "--full-auto"]

    def test_command_structure_no_full_auto(self, mock_openai_key, laptop_context):
        """Command without full-auto flag."""
        config = configure_codex("test prompt", laptop_context, full_auto=False)
        assert config.cmd == ["codex", "exec", "-"]

    def test_custom_model(self, mock_openai_key, laptop_context):
        """Custom model adds -m flag."""
        config = configure_codex("test", laptop_context, model="gpt-5")
        assert "-m" in config.cmd
        assert "gpt-5" in config.cmd

    def test_reasoning_effort(self, mock_openai_key, laptop_context):
        """Reasoning effort adds -c flag."""
        config = configure_codex("test", laptop_context, reasoning_effort="high")
        assert "-c" in config.cmd
        assert "model_reasoning_effort=high" in config.cmd

    def test_no_reasoning_effort_by_default(self, mock_openai_key, laptop_context):
        """No reasoning effort flag when not specified."""
        config = configure_codex("test", laptop_context)
        assert "-c" not in config.cmd

    def test_prompt_via_stdin(self, mock_openai_key, laptop_context):
        """Prompt passed via stdin_data."""
        config = configure_codex("my codex prompt", laptop_context)
        assert config.stdin_data == b"my codex prompt"

    def test_container_readonly_sets_home(
        self, mock_openai_key, container_readonly_context
    ):
        """Sets HOME in container with read-only home."""
        config = configure_codex("test", container_readonly_context)
        assert config.env["HOME"] == "/tmp"


class TestConfigureGemini:
    """Tests for Gemini backend configuration."""

    def test_command_structure(self, laptop_context):
        """Command has correct structure."""
        config = configure_gemini("test prompt", laptop_context)
        assert config.cmd == ["gemini", "--model", "gemini-3-flash-preview", "--yolo", "-p", "-"]

    def test_no_api_key_required(self, clean_env, laptop_context):
        """Does not require API key (uses OAuth)."""
        # Should not raise
        config = configure_gemini("test prompt", laptop_context)
        assert config.cmd is not None

    def test_prompt_via_stdin(self, laptop_context):
        """Prompt passed via stdin_data."""
        config = configure_gemini("my gemini prompt", laptop_context)
        assert config.stdin_data == b"my gemini prompt"

    def test_minimal_env(self, laptop_context):
        """Minimal environment modifications on laptop."""
        config = configure_gemini("test", laptop_context)
        # Should not add unnecessary env vars
        assert "OPENAI_API_KEY" not in config.env
        assert "ZAI_API_KEY" not in config.env

    def test_container_readonly_sets_home(self, container_readonly_context):
        """Sets HOME in container with read-only home."""
        config = configure_gemini("test", container_readonly_context)
        assert config.env["HOME"] == "/tmp"


class TestGetConfig:
    """Tests for the get_config dispatcher."""

    def test_dispatches_to_zai(self, mock_zai_key, laptop_context):
        """Dispatches to configure_zai for ZAI backend."""
        config = get_config(Backend.ZAI, "test", laptop_context)
        assert "claude" in config.cmd
        assert "ANTHROPIC_AUTH_TOKEN" in config.env

    def test_dispatches_to_bedrock(self, laptop_context):
        """Dispatches to configure_bedrock for BEDROCK backend."""
        config = get_config(Backend.BEDROCK, "test", laptop_context)
        assert "claude" in config.cmd
        assert config.env.get("CLAUDE_CODE_USE_BEDROCK") == "1"

    def test_dispatches_to_codex(self, mock_openai_key, laptop_context):
        """Dispatches to configure_codex for CODEX backend."""
        config = get_config(Backend.CODEX, "test", laptop_context)
        assert "codex" in config.cmd

    def test_dispatches_to_gemini(self, laptop_context):
        """Dispatches to configure_gemini for GEMINI backend."""
        config = get_config(Backend.GEMINI, "test", laptop_context)
        assert "gemini" in config.cmd

    def test_passes_kwargs(self, mock_zai_key, laptop_context):
        """Passes kwargs to configure function."""
        config = get_config(Backend.ZAI, "test", laptop_context, model="custom")
        assert config.env["ANTHROPIC_MODEL"] == "custom"


class TestUnicodePrompts:
    """Tests for handling unicode in prompts."""

    def test_zai_unicode_prompt(self, mock_zai_key, laptop_context):
        """ZAI handles unicode prompts."""
        prompt = "Fix the bug in \u65e5\u672c\u8a9e code"
        config = configure_zai(prompt, laptop_context)
        assert config.stdin_data == prompt.encode("utf-8")

    def test_codex_unicode_prompt(self, mock_openai_key, laptop_context):
        """Codex handles unicode prompts."""
        prompt = "Analyze \U0001F680 emoji usage"  # Rocket emoji
        config = configure_codex(prompt, laptop_context)
        assert config.stdin_data == prompt.encode("utf-8")

    def test_gemini_unicode_prompt(self, laptop_context):
        """Gemini handles unicode prompts."""
        prompt = "Explain \u03c0 calculation"
        config = configure_gemini(prompt, laptop_context)
        assert config.stdin_data == prompt.encode("utf-8")


class TestLargePrompts:
    """Tests for handling large prompts."""

    def test_large_prompt_via_stdin(self, mock_zai_key, laptop_context):
        """Large prompts go via stdin to avoid ARG_MAX."""
        # 100KB prompt
        large_prompt = "x" * 100_000
        config = configure_zai(large_prompt, laptop_context)
        assert len(config.stdin_data) == 100_000
        # Command should use stdin, not have prompt in args
        assert large_prompt not in " ".join(config.cmd)
