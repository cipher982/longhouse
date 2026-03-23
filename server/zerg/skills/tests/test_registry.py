"""Tests for skill registry."""

from pathlib import Path

import pytest

from zerg.skills.models import Skill
from zerg.skills.models import SkillManifest
from zerg.skills.models import SkillSource
from zerg.skills.registry import SkillRegistry
from zerg.skills.registry import get_skill_registry
from zerg.skills.registry import reset_skill_registry


@pytest.fixture
def sample_skills() -> list[Skill]:
    """Create sample skills for testing."""
    skills = []
    for name, desc, eligible in [
        ("github", "GitHub integration", True),
        ("slack", "Slack messaging", True),
        ("jira", "Jira tickets", False),
    ]:
        manifest = SkillManifest.from_frontmatter(
            {
                "name": name,
                "description": desc,
            }
        )
        skill = Skill(
            manifest=manifest,
            content=f"# {name} content",
            base_dir=Path(f"/tmp/skills/{name}"),
            file_path=Path(f"/tmp/skills/{name}/SKILL.md"),
            source=SkillSource.BUNDLED,
        )
        skills.append(skill)
    return skills


@pytest.fixture
def skill_workspace(tmp_path: Path) -> Path:
    """Create a workspace with test skills."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    for name in ["github", "slack"]:
        skill_dir = skills_dir / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {name.title()} skill
---

# {name.title()} Content
"""
        )

    return tmp_path


class TestSkillRegistry:
    """Tests for SkillRegistry class."""

    def test_load_for_workspace(self, skill_workspace: Path) -> None:
        """Load skills from workspace."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        assert registry.get_skill("github") is not None
        assert registry.get_skill("slack") is not None

    def test_get_all_skills(self, skill_workspace: Path) -> None:
        """Get all loaded skills."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        skills = registry.get_all_skills()
        assert len(skills) >= 2  # workspace + bundled
        names = [s.name for s in skills]
        assert "github" in names

    def test_get_skill_names(self, skill_workspace: Path) -> None:
        """Get set of skill names."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        names = registry.get_skill_names()
        assert "github" in names
        assert "slack" in names

    def test_filter_by_allowlist_exact(self, skill_workspace: Path) -> None:
        """Filter by exact skill name."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        skills = registry.filter_by_allowlist(["github"])
        names = [s.name for s in skills]
        assert "github" in names
        assert "slack" not in names

    def test_filter_by_allowlist_wildcard(self, skill_workspace: Path) -> None:
        """Filter by wildcard pattern."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        # Should match github, github-actions, etc.
        skills = registry.filter_by_allowlist(["git*"])
        names = [s.name for s in skills]
        assert "github" in names

    def test_filter_by_allowlist_none(self, skill_workspace: Path) -> None:
        """None allowlist returns all eligible."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        skills = registry.filter_by_allowlist(None)
        assert len(skills) >= 2

    def test_format_skills_prompt(self, skill_workspace: Path) -> None:
        """Format skills for system prompt."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        prompt = registry.format_skills_prompt()

        assert "# Available Skills" in prompt
        assert "github" in prompt.lower()

    def test_get_snapshot(self, skill_workspace: Path) -> None:
        """Get immutable skill snapshot."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        snapshot = registry.get_snapshot()

        assert snapshot.version > 0
        assert len(snapshot.skills) >= 2
        assert snapshot.prompt != ""

    def test_get_user_invocable_commands(self, skill_workspace: Path) -> None:
        """Get user-invocable commands."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        commands = registry.get_user_invocable_commands()

        assert len(commands) >= 2
        assert all("name" in c for c in commands)
        assert all("description" in c for c in commands)

    def test_reload(self, skill_workspace: Path) -> None:
        """Reload skills from workspace."""
        registry = SkillRegistry()
        registry.load_for_workspace(skill_workspace)

        v1 = registry._version

        registry.reload()

        assert registry._version > v1


class TestGlobalRegistry:
    """Tests for global registry functions."""

    def test_get_skill_registry_singleton(self) -> None:
        """Global registry is singleton."""
        reset_skill_registry()

        reg1 = get_skill_registry()
        reg2 = get_skill_registry()

        assert reg1 is reg2

    def test_reset_skill_registry(self) -> None:
        """Reset creates new registry."""
        reg1 = get_skill_registry()
        reset_skill_registry()
        reg2 = get_skill_registry()

        assert reg1 is not reg2
