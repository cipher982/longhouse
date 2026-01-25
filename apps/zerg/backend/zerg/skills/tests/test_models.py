"""Tests for skill data models."""

from pathlib import Path

from zerg.skills.models import Skill
from zerg.skills.models import SkillEntry
from zerg.skills.models import SkillManifest
from zerg.skills.models import SkillRequirements
from zerg.skills.models import SkillSource


class TestSkillRequirements:
    """Tests for SkillRequirements model."""

    def test_from_empty_dict(self) -> None:
        """Create from empty dict."""
        reqs = SkillRequirements.from_dict({})
        assert reqs.bins == ()
        assert reqs.any_bins == ()
        assert reqs.env == ()
        assert reqs.config == ()

    def test_from_none(self) -> None:
        """Create from None."""
        reqs = SkillRequirements.from_dict(None)
        assert reqs.bins == ()

    def test_from_full_dict(self) -> None:
        """Create from dict with all fields."""
        data = {
            "bins": ["git", "gh"],
            "anyBins": ["docker", "podman"],
            "env": ["GITHUB_TOKEN"],
            "config": ["github.token"],
        }
        reqs = SkillRequirements.from_dict(data)

        assert reqs.bins == ("git", "gh")
        assert reqs.any_bins == ("docker", "podman")
        assert reqs.env == ("GITHUB_TOKEN",)
        assert reqs.config == ("github.token",)

    def test_snake_case_any_bins(self) -> None:
        """Support snake_case any_bins."""
        data = {"any_bins": ["docker", "podman"]}
        reqs = SkillRequirements.from_dict(data)
        assert reqs.any_bins == ("docker", "podman")


class TestSkillManifest:
    """Tests for SkillManifest model."""

    def test_from_basic_frontmatter(self) -> None:
        """Create from basic frontmatter."""
        frontmatter = {
            "name": "test-skill",
            "description": "A test skill",
        }
        manifest = SkillManifest.from_frontmatter(frontmatter)

        assert manifest.name == "test-skill"
        assert manifest.description == "A test skill"
        assert manifest.emoji == ""
        assert manifest.user_invocable is True

    def test_from_clawdbot_style_frontmatter(self) -> None:
        """Parse clawdbot-style nested metadata."""
        frontmatter = {
            "name": "github",
            "description": "GitHub skill",
            "metadata": {
                "clawdbot": {
                    "emoji": "🐙",
                    "primaryEnv": "GITHUB_TOKEN",
                    "requires": {
                        "bins": ["gh"],
                        "env": ["GITHUB_TOKEN"],
                    },
                }
            },
        }
        manifest = SkillManifest.from_frontmatter(frontmatter)

        assert manifest.emoji == "🐙"
        assert manifest.primary_env == "GITHUB_TOKEN"
        assert manifest.requires.bins == ("gh",)
        assert manifest.requires.env == ("GITHUB_TOKEN",)

    def test_from_frontmatter_with_tool_dispatch(self) -> None:
        """Parse tool dispatch configuration."""
        frontmatter = {
            "name": "quick-search",
            "command-tool": "web_search",
        }
        manifest = SkillManifest.from_frontmatter(frontmatter)
        assert manifest.tool_dispatch == "web_search"

    def test_raw_frontmatter_preserved(self) -> None:
        """Raw frontmatter dict is preserved."""
        frontmatter = {
            "name": "test",
            "custom_field": "custom_value",
        }
        manifest = SkillManifest.from_frontmatter(frontmatter)
        assert manifest.raw["custom_field"] == "custom_value"


class TestSkill:
    """Tests for Skill model."""

    def test_skill_properties(self) -> None:
        """Skill properties delegate to manifest."""
        manifest = SkillManifest.from_frontmatter(
            {
                "name": "test-skill",
                "description": "Test description",
            }
        )
        skill = Skill(
            manifest=manifest,
            content="# Content",
            base_dir=Path("/tmp/skills/test-skill"),
            file_path=Path("/tmp/skills/test-skill/SKILL.md"),
        )

        assert skill.name == "test-skill"
        assert skill.description == "Test description"

    def test_format_for_prompt(self) -> None:
        """Format skill for system prompt."""
        manifest = SkillManifest.from_frontmatter(
            {
                "name": "test-skill",
                "description": "Test description",
                "metadata": {"clawdbot": {"emoji": "🧪"}},
            }
        )
        skill = Skill(
            manifest=manifest,
            content="Use this skill for testing.",
            base_dir=Path("/tmp/skills/test-skill"),
            file_path=Path("/tmp/skills/test-skill/SKILL.md"),
        )

        prompt = skill.format_for_prompt()

        assert "🧪 test-skill" in prompt
        assert "Test description" in prompt
        assert "Use this skill for testing." in prompt


class TestSkillEntry:
    """Tests for SkillEntry model."""

    def test_to_dict(self) -> None:
        """Convert entry to dict for API."""
        manifest = SkillManifest.from_frontmatter(
            {
                "name": "test-skill",
                "description": "Test",
            }
        )
        skill = Skill(
            manifest=manifest,
            content="",
            base_dir=Path("/tmp"),
            file_path=Path("/tmp/SKILL.md"),
            source=SkillSource.BUNDLED,
        )
        entry = SkillEntry(
            skill=skill,
            eligible=False,
            missing_bins=["git"],
            missing_env=["TOKEN"],
        )

        data = entry.to_dict()

        assert data["name"] == "test-skill"
        assert data["source"] == "bundled"
        assert data["eligible"] is False
        assert "git" in data["missing_requirements"]["bins"]
        assert "TOKEN" in data["missing_requirements"]["env"]
