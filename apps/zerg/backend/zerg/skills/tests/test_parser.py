"""Tests for SKILL.md parser."""

from pathlib import Path

from zerg.skills.parser import parse_frontmatter
from zerg.skills.parser import parse_skill_file
from zerg.skills.parser import validate_manifest


class TestParseFrontmatter:
    """Tests for parse_frontmatter function."""

    def test_parse_basic_frontmatter(self) -> None:
        """Parse basic YAML frontmatter."""
        content = """---
name: test-skill
description: A test skill
---

# Content here
"""
        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter["name"] == "test-skill"
        assert frontmatter["description"] == "A test skill"
        assert "# Content here" in remaining

    def test_parse_no_frontmatter(self) -> None:
        """Return empty dict when no frontmatter."""
        content = "# Just markdown content"
        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter == {}
        assert remaining == content

    def test_parse_complex_frontmatter(self) -> None:
        """Parse frontmatter with nested structures."""
        content = """---
name: complex-skill
description: Complex skill
requires:
  bins:
    - git
    - gh
  env:
    - GITHUB_TOKEN
---

# Content
"""
        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter["name"] == "complex-skill"
        assert frontmatter["requires"]["bins"] == ["git", "gh"]
        assert frontmatter["requires"]["env"] == ["GITHUB_TOKEN"]

    def test_parse_invalid_yaml(self) -> None:
        """Return empty dict on invalid YAML."""
        content = """---
name: [invalid yaml
---

# Content
"""
        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter == {}

    def test_parse_empty_frontmatter(self) -> None:
        """Handle empty frontmatter block."""
        content = """---
---

# Content
"""
        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter == {}


class TestParseSkillFile:
    """Tests for parse_skill_file function."""

    def test_parse_skill_file(self, tmp_path: Path) -> None:
        """Parse SKILL.md file from disk."""
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            """---
name: test-skill
description: Test description
---

# Test Content
"""
        )

        frontmatter, content = parse_skill_file(skill_file)

        assert frontmatter["name"] == "test-skill"
        assert "# Test Content" in content

    def test_parse_skill_file_uses_dir_name(self, tmp_path: Path) -> None:
        """Use directory name when name not in frontmatter."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            """---
description: No name field
---

# Content
"""
        )

        frontmatter, _ = parse_skill_file(skill_file)

        assert frontmatter["name"] == "my-skill"


class TestValidateManifest:
    """Tests for validate_manifest function."""

    def test_valid_manifest(self) -> None:
        """Valid manifest returns None."""
        frontmatter = {"name": "valid-skill", "description": "A skill"}
        assert validate_manifest(frontmatter) is None

    def test_missing_name(self) -> None:
        """Missing name returns error."""
        frontmatter = {"description": "No name"}
        error = validate_manifest(frontmatter)
        assert error is not None
        assert "name" in error.lower()

    def test_invalid_name_type(self) -> None:
        """Non-string name returns error."""
        frontmatter = {"name": 123}
        error = validate_manifest(frontmatter)
        assert error is not None
        assert "string" in error.lower()

    def test_invalid_name_format(self) -> None:
        """Invalid name format returns error."""
        frontmatter = {"name": "invalid name with spaces"}
        error = validate_manifest(frontmatter)
        assert error is not None
        assert "alphanumeric" in error.lower()

    def test_valid_name_formats(self) -> None:
        """Various valid name formats."""
        valid_names = ["skill", "my-skill", "my_skill", "skill123", "MySkill"]
        for name in valid_names:
            frontmatter = {"name": name}
            assert validate_manifest(frontmatter) is None, f"'{name}' should be valid"
