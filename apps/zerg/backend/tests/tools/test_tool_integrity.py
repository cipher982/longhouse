"""Tool integrity contract tests.

Ensures all tools in the registry have proper schema definitions,
descriptions, and are JSON-serializable. These tests act as a gatekeeper
to catch schema-related bugs before E2E tests.
"""

import json

import pytest
from pydantic import BaseModel

from zerg.tools.builtin import BUILTIN_TOOLS


class TestToolSchemaIntegrity:
    """Verify all builtin tools have proper args_schema definitions."""

    def test_all_tools_have_args_schema(self):
        """Every tool should have an args_schema (not None)."""
        tools_without_schema = []

        for tool in BUILTIN_TOOLS:
            if tool.args_schema is None:
                tools_without_schema.append(tool.name)

        assert not tools_without_schema, (
            f"Tools missing args_schema: {tools_without_schema}. "
            "All tools must have explicit args_schema for proper LLM function calling."
        )

    def test_args_schema_is_pydantic_model(self):
        """args_schema should be a Pydantic BaseModel subclass."""
        non_pydantic_schemas = []

        for tool in BUILTIN_TOOLS:
            if tool.args_schema is not None:
                # Check if it's a Pydantic model class
                if not (isinstance(tool.args_schema, type) and issubclass(tool.args_schema, BaseModel)):
                    non_pydantic_schemas.append((tool.name, type(tool.args_schema).__name__))

        assert not non_pydantic_schemas, (
            f"Tools with non-Pydantic args_schema: {non_pydantic_schemas}. "
            "args_schema must be a Pydantic BaseModel subclass."
        )

    def test_args_schema_is_json_serializable(self):
        """Schema should be JSON-serializable for LLM API calls."""
        non_serializable = []

        for tool in BUILTIN_TOOLS:
            if tool.args_schema is not None:
                try:
                    schema = tool.args_schema.model_json_schema()
                    # Verify it's actually serializable
                    json.dumps(schema)
                except Exception as e:
                    non_serializable.append((tool.name, str(e)))

        assert not non_serializable, (
            f"Tools with non-serializable schema: {non_serializable}. "
            "args_schema must produce JSON-serializable schema."
        )


class TestToolDescriptionIntegrity:
    """Verify all tools have meaningful descriptions."""

    MIN_DESCRIPTION_LENGTH = 10

    def test_all_tools_have_description(self):
        """Every tool should have a description."""
        tools_without_description = []

        for tool in BUILTIN_TOOLS:
            if not tool.description:
                tools_without_description.append(tool.name)

        assert not tools_without_description, (
            f"Tools missing description: {tools_without_description}. "
            "All tools must have descriptions for LLM understanding."
        )

    def test_descriptions_meet_minimum_length(self):
        """Descriptions should be meaningful (not just a word or two)."""
        short_descriptions = []

        for tool in BUILTIN_TOOLS:
            if tool.description and len(tool.description) < self.MIN_DESCRIPTION_LENGTH:
                short_descriptions.append((tool.name, len(tool.description), tool.description))

        assert not short_descriptions, (
            f"Tools with too-short descriptions: {short_descriptions}. "
            f"Descriptions should be at least {self.MIN_DESCRIPTION_LENGTH} characters."
        )


class TestToolFunctionIntegrity:
    """Verify tool functions are properly defined."""

    def test_all_tools_have_func_or_coroutine(self):
        """Every tool should have a callable func or coroutine."""
        tools_without_callable = []

        for tool in BUILTIN_TOOLS:
            has_func = callable(getattr(tool, "func", None))
            has_coroutine = callable(getattr(tool, "coroutine", None))

            if not has_func and not has_coroutine:
                tools_without_callable.append(tool.name)

        assert not tools_without_callable, (
            f"Tools missing callable func/coroutine: {tools_without_callable}. "
            "All tools must have either a callable func or coroutine."
        )

    def test_tool_names_are_valid_identifiers(self):
        """Tool names should be valid Python identifiers (snake_case)."""
        invalid_names = []

        for tool in BUILTIN_TOOLS:
            # Check it's a valid identifier
            if not tool.name.replace("_", "").isalnum():
                invalid_names.append(tool.name)
            # Check it doesn't start with a digit
            if tool.name[0].isdigit():
                invalid_names.append(tool.name)

        assert not invalid_names, (
            f"Tools with invalid names: {invalid_names}. "
            "Tool names should be valid Python identifiers."
        )


class TestToolUniqueNames:
    """Verify no duplicate tool names exist."""

    def test_no_duplicate_tool_names(self):
        """All tool names should be unique."""
        seen = {}
        duplicates = []

        for tool in BUILTIN_TOOLS:
            if tool.name in seen:
                duplicates.append(tool.name)
            seen[tool.name] = True

        assert not duplicates, f"Duplicate tool names found: {duplicates}"
