"""Tests for skill-to-tool conversion."""

from pathlib import Path

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from pydantic import Field

from zerg.skills.integration import SkillIntegration
from zerg.skills.integration import create_skill_tool


class SearchInput(BaseModel):
    query: str = Field(description="Search query")
    limit: int = Field(default=10, description="Max results")


def mock_search(query: str, limit: int = 10) -> str:
    """Search for something."""
    return f"Results for {query} (limit {limit})"


@pytest.fixture
def search_tool() -> StructuredTool:
    return StructuredTool.from_function(
        func=mock_search,
        name="web_search",
        description="Search the web",
        args_schema=SearchInput,
    )


@pytest.fixture
def skill_workspace(tmp_path: Path) -> Path:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Skill with tool dispatch
    search_dir = skills_dir / "quick-search"
    search_dir.mkdir()
    (search_dir / "SKILL.md").write_text(
        """---
name: quick-search
description: Quick web search
tool_dispatch: web_search
---

# Quick Search
Dispatches to web_search tool.
"""
    )
    return tmp_path


class MockToolRegistry:
    def __init__(self, tools: list[StructuredTool]):
        self.tools = {t.name: t for t in tools}

    def get_tool(self, name: str) -> StructuredTool:
        return self.tools.get(name)


def test_create_skill_tool_preserves_schema(search_tool: StructuredTool, skill_workspace: Path):
    """Verify that create_skill_tool copies args_schema from the target tool."""
    integration = SkillIntegration(workspace_path=skill_workspace)
    integration.load()

    skill = integration.get_skill("quick-search")
    registry = MockToolRegistry([search_tool])

    skill_tool = create_skill_tool(skill, registry)

    assert skill_tool is not None
    assert skill_tool.name == "skill_quick-search"

    # This is the failing assertion
    assert skill_tool.args_schema is not None, "Skill tool should have an args_schema"
    assert skill_tool.args_schema == SearchInput

    # Verify it's actually callable with correct params
    result = skill_tool.invoke({"query": "hello", "limit": 5})
    assert "Results for hello (limit 5)" in result
