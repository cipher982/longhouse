"""Tests for skill loader."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from zerg.skills.loader import SkillLoader
from zerg.skills.models import SkillSource


@pytest.fixture
def skill_workspace(tmp_path: Path) -> Path:
    """Create a workspace with test skills."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Create a valid skill
    valid_skill = skills_dir / "valid-skill"
    valid_skill.mkdir()
    (valid_skill / "SKILL.md").write_text(
        """---
name: valid-skill
description: A valid test skill
---

# Valid Skill Content
"""
    )

    # Create skill with requirements
    req_skill = skills_dir / "req-skill"
    req_skill.mkdir()
    (req_skill / "SKILL.md").write_text(
        """---
name: req-skill
description: Skill with requirements
requires:
  bins:
    - nonexistent-binary
  env:
    - NONEXISTENT_ENV_VAR
---

# Content
"""
    )

    # Create skill without SKILL.md (should be ignored)
    no_skill = skills_dir / "no-skill"
    no_skill.mkdir()
    (no_skill / "README.md").write_text("Not a skill")

    return tmp_path


class TestSkillLoader:
    """Tests for SkillLoader class."""

    def test_load_from_directory(self, skill_workspace: Path) -> None:
        """Load skills from a directory."""
        loader = SkillLoader()
        skills = loader.load_from_directory(skill_workspace / "skills", SkillSource.WORKSPACE)

        assert len(skills) == 2
        names = [s.name for s in skills]
        assert "valid-skill" in names
        assert "req-skill" in names

    def test_load_from_nonexistent_directory(self) -> None:
        """Return empty list for nonexistent directory."""
        loader = SkillLoader()
        skills = loader.load_from_directory(Path("/nonexistent/path"), SkillSource.WORKSPACE)
        assert skills == []

    def test_load_workspace_skills(self, skill_workspace: Path) -> None:
        """Load skills from workspace/skills directory."""
        loader = SkillLoader()
        skills = loader.load_workspace_skills(skill_workspace)

        assert len(skills) == 2
        for skill in skills:
            assert skill.source == SkillSource.WORKSPACE

    def test_load_bundled_skills(self) -> None:
        """Load bundled skills shipped with Zerg."""
        loader = SkillLoader()
        skills = loader.load_bundled_skills()

        # Should have the bundled skills we created
        names = [s.name for s in skills]
        assert "github" in names
        assert "web-search" in names
        assert "slack" in names

        for skill in skills:
            assert skill.source == SkillSource.BUNDLED

    def test_load_all_skills_merging(self, skill_workspace: Path) -> None:
        """Skills merge with correct precedence."""
        # Create conflicting bundled skill
        loader = SkillLoader(bundled_dir=skill_workspace / "bundled")
        bundled_dir = skill_workspace / "bundled" / "valid-skill"
        bundled_dir.mkdir(parents=True)
        (bundled_dir / "SKILL.md").write_text(
            """---
name: valid-skill
description: Bundled version
---
# Bundled content
"""
        )

        skills = loader.load_all_skills(workspace_path=skill_workspace)

        # Workspace version should override bundled
        assert "valid-skill" in skills
        assert skills["valid-skill"].source == SkillSource.WORKSPACE
        assert skills["valid-skill"].description == "A valid test skill"

    def test_compute_eligibility_all_met(self, skill_workspace: Path) -> None:
        """Skill is eligible when all requirements are met."""
        loader = SkillLoader()
        skills = loader.load_workspace_skills(skill_workspace)
        valid_skill = next(s for s in skills if s.name == "valid-skill")

        entry = loader.compute_eligibility(valid_skill)

        assert entry.eligible is True
        assert entry.missing_bins == []
        assert entry.missing_env == []

    def test_compute_eligibility_missing_requirements(self, skill_workspace: Path) -> None:
        """Skill is ineligible when requirements missing."""
        loader = SkillLoader()
        skills = loader.load_workspace_skills(skill_workspace)
        req_skill = next(s for s in skills if s.name == "req-skill")

        entry = loader.compute_eligibility(req_skill)

        assert entry.eligible is False
        assert "nonexistent-binary" in entry.missing_bins
        assert "NONEXISTENT_ENV_VAR" in entry.missing_env

    def test_load_skill_entries(self, skill_workspace: Path) -> None:
        """Load skill entries with eligibility."""
        loader = SkillLoader()
        entries = loader.load_skill_entries(workspace_path=skill_workspace)

        assert len(entries) >= 2  # May include bundled
        eligible_count = sum(1 for e in entries if e.eligible)
        assert eligible_count >= 1  # valid-skill should be eligible

    def test_load_skill_entries_filter_eligible(self, skill_workspace: Path) -> None:
        """Filter to only eligible skills."""
        loader = SkillLoader()
        entries = loader.load_skill_entries(workspace_path=skill_workspace, filter_eligible=True)

        for entry in entries:
            assert entry.eligible is True


class TestSkillLoaderEnvironment:
    """Tests for environment-based eligibility."""

    def test_env_requirement_met(self, tmp_path: Path) -> None:
        """Skill eligible when env var is set."""
        skills_dir = tmp_path / "skills" / "env-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            """---
name: env-skill
requires:
  env:
    - TEST_ENV_VAR
---
"""
        )

        loader = SkillLoader()
        with patch.dict(os.environ, {"TEST_ENV_VAR": "value"}):
            entries = loader.load_skill_entries(workspace_path=tmp_path)

        env_entry = next((e for e in entries if e.name == "env-skill"), None)
        assert env_entry is not None
        assert env_entry.eligible is True

    def test_env_requirement_not_met(self, tmp_path: Path) -> None:
        """Skill ineligible when env var is not set."""
        skills_dir = tmp_path / "skills" / "env-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            """---
name: env-skill
requires:
  env:
    - DEFINITELY_NOT_SET_12345
---
"""
        )

        loader = SkillLoader()
        entries = loader.load_skill_entries(workspace_path=tmp_path)

        env_entry = next((e for e in entries if e.name == "env-skill"), None)
        assert env_entry is not None
        assert env_entry.eligible is False
        assert "DEFINITELY_NOT_SET_12345" in env_entry.missing_env
