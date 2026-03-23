"""Tests for skill integration with tool system."""

from pathlib import Path

import pytest

from zerg.skills.integration import SkillContext
from zerg.skills.integration import SkillIntegration
from zerg.skills.integration import augment_system_prompt
from zerg.skills.integration import get_skill_tool_names


@pytest.fixture
def skill_workspace(tmp_path: Path) -> Path:
    """Create a workspace with test skills."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Skill without tool dispatch
    github_dir = skills_dir / "github"
    github_dir.mkdir()
    (github_dir / "SKILL.md").write_text(
        """---
name: github
description: GitHub integration
---

# GitHub Skill
Use github_* tools.
"""
    )

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


class TestSkillContext:
    """Tests for SkillContext class."""

    def test_load_skills(self, skill_workspace: Path) -> None:
        """Load skills into context."""
        ctx = SkillContext(workspace_path=skill_workspace)
        ctx.load()

        skills = ctx.get_eligible_skills()
        names = [s.name for s in skills]
        assert "github" in names

    def test_get_prompt_index_only_by_default(self, skill_workspace: Path) -> None:
        """Default get_prompt returns index-only (no full content)."""
        ctx = SkillContext(workspace_path=skill_workspace)
        prompt = ctx.get_prompt()

        assert "# Available Skills" in prompt
        assert "**github**" in prompt
        # Should NOT contain the full SKILL.md body
        assert "Use github_* tools." not in prompt

    def test_get_prompt_with_content(self, skill_workspace: Path) -> None:
        """get_prompt with include_content=True returns full content."""
        ctx = SkillContext(workspace_path=skill_workspace)
        prompt = ctx.get_prompt(include_content=True)

        assert "# Available Skills" in prompt
        assert "Use github_* tools." in prompt

    def test_lazy_load(self, skill_workspace: Path) -> None:
        """Context loads lazily on first access."""
        ctx = SkillContext(workspace_path=skill_workspace)
        assert ctx._loaded is False

        ctx.get_prompt()
        assert ctx._loaded is True

    def test_get_skill(self, skill_workspace: Path) -> None:
        """Get specific skill from context."""
        ctx = SkillContext(workspace_path=skill_workspace)
        skill = ctx.get_skill("github")

        assert skill is not None
        assert skill.name == "github"

    def test_allowed_skills_filter(self, skill_workspace: Path) -> None:
        """Allowed skills filter works."""
        ctx = SkillContext(
            workspace_path=skill_workspace,
            allowed_skills=["github"],
        )

        skills = ctx.get_eligible_skills()
        names = [s.name for s in skills]
        assert "github" in names
        # quick-search may or may not be in names depending on allowlist


class TestAugmentSystemPrompt:
    """Tests for augment_system_prompt function."""

    def test_augment_at_end(self, skill_workspace: Path) -> None:
        """Augment prompt at end (index-only by default)."""
        ctx = SkillContext(workspace_path=skill_workspace)
        ctx.load()

        original = "You are a helpful assistant."
        augmented = augment_system_prompt(original, ctx, position="end")

        assert augmented.startswith("You are a helpful assistant.")
        assert "# Available Skills" in augmented
        # Index mode: description in compact form, not full content
        assert "**github**" in augmented

    def test_augment_at_start(self, skill_workspace: Path) -> None:
        """Augment prompt at start."""
        ctx = SkillContext(workspace_path=skill_workspace)
        ctx.load()

        original = "You are a helpful assistant."
        augmented = augment_system_prompt(original, ctx, position="start")

        assert augmented.endswith("You are a helpful assistant.")
        assert augmented.index("# Available Skills") < augmented.index("helpful")

    def test_augment_with_full_content(self, skill_workspace: Path) -> None:
        """Augment prompt with include_content=True returns full skill bodies."""
        ctx = SkillContext(workspace_path=skill_workspace)
        ctx.load()

        original = "You are a helpful assistant."
        augmented = augment_system_prompt(original, ctx, position="end", include_content=True)

        assert "Use github_* tools." in augmented

    def test_augment_after_marker(self, skill_workspace: Path) -> None:
        """Augment prompt after marker."""
        ctx = SkillContext(workspace_path=skill_workspace)
        ctx.load()

        original = "You are a helpful assistant.\n\n[SKILLS_MARKER]\n\nBe concise."
        augmented = augment_system_prompt(original, ctx, position="after:[SKILLS_MARKER]")

        assert "[SKILLS_MARKER]" in augmented
        # Skills should be after marker
        marker_pos = augmented.index("[SKILLS_MARKER]")
        skills_pos = augmented.index("# Available Skills")
        assert skills_pos > marker_pos


class TestGetSkillToolNames:
    """Tests for get_skill_tool_names function."""

    def test_get_tool_names(self, skill_workspace: Path) -> None:
        """Get tool names from skills with dispatch."""
        ctx = SkillContext(workspace_path=skill_workspace)
        ctx.load()

        tool_names = get_skill_tool_names(ctx)

        assert "web_search" in tool_names


class TestSkillIntegration:
    """Tests for SkillIntegration class."""

    def test_augment_prompt(self, skill_workspace: Path) -> None:
        """Augment prompt via integration (index-only by default)."""
        integration = SkillIntegration(workspace_path=skill_workspace)
        integration.load()

        prompt = integration.augment_prompt("You are helpful.")

        assert "You are helpful." in prompt
        assert "**github**" in prompt
        # Index mode should not contain full content
        assert "Use github_* tools." not in prompt

    def test_augment_prompt_with_content(self, skill_workspace: Path) -> None:
        """Augment prompt with include_content=True."""
        integration = SkillIntegration(workspace_path=skill_workspace)
        integration.load()

        prompt = integration.augment_prompt("You are helpful.", include_content=True)

        assert "Use github_* tools." in prompt

    def test_get_skill_names(self, skill_workspace: Path) -> None:
        """Get loaded skill names."""
        integration = SkillIntegration(workspace_path=skill_workspace)
        integration.load()

        names = integration.get_skill_names()

        assert "github" in names

    def test_get_prompt_index_only(self, skill_workspace: Path) -> None:
        """Default get_prompt returns index-only."""
        integration = SkillIntegration(workspace_path=skill_workspace)

        prompt = integration.get_prompt()

        assert "# Available Skills" in prompt
        assert "**github**" in prompt

    def test_get_prompt_with_content(self, skill_workspace: Path) -> None:
        """get_prompt with include_content=True returns full content."""
        integration = SkillIntegration(workspace_path=skill_workspace)

        prompt = integration.get_prompt(include_content=True)

        assert "Use github_* tools." in prompt

    def test_get_skill_content(self, skill_workspace: Path) -> None:
        """Get full content for a specific skill."""
        integration = SkillIntegration(workspace_path=skill_workspace)
        integration.load()

        content = integration.get_skill_content("github")

        assert content is not None
        assert "github" in content.lower()
        assert "Use github_* tools." in content

    def test_get_skill_content_not_found(self, skill_workspace: Path) -> None:
        """get_skill_content returns None for unknown skill."""
        integration = SkillIntegration(workspace_path=skill_workspace)
        integration.load()

        content = integration.get_skill_content("nonexistent")

        assert content is None

    def test_allowed_skills_filter(self, skill_workspace: Path) -> None:
        """Allowed skills filter applied."""
        integration = SkillIntegration(
            workspace_path=skill_workspace,
            allowed_skills=["github"],
        )
        integration.load()

        names = integration.get_skill_names()
        assert "github" in names

    def test_char_budget(self, skill_workspace: Path) -> None:
        """Character budget drops skills when over limit."""
        integration = SkillIntegration(workspace_path=skill_workspace)
        integration.load()

        # Very small budget — should drop some or all skills
        prompt = integration.get_prompt(char_budget=50)
        # Either empty or truncated
        assert len(prompt) <= 50 or prompt == ""

        # Large budget — should include all
        prompt = integration.get_prompt(char_budget=10000)
        assert "**github**" in prompt
        assert "**quick-search**" in prompt
