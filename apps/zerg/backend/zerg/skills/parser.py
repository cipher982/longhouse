"""SKILL.md parser with YAML frontmatter support.

Parses markdown files with YAML frontmatter in the format:
---
name: skill-name
description: Skill description
---

# Skill Content
"""

import logging
import re
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Optional
from typing import Tuple

import yaml

logger = logging.getLogger(__name__)

# Frontmatter regex: matches --- at start, YAML content, ---
FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?",
    re.DOTALL,
)


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content.

    Args:
        content: Raw markdown content with optional frontmatter

    Returns:
        Tuple of (frontmatter_dict, remaining_content)
        If no frontmatter, returns ({}, content)
    """
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}, content

    yaml_content = match.group(1)
    remaining = content[match.end() :]

    try:
        frontmatter = yaml.safe_load(yaml_content) or {}
        if not isinstance(frontmatter, dict):
            logger.warning(f"Frontmatter is not a dict: {type(frontmatter)}")
            return {}, content
        return frontmatter, remaining
    except yaml.YAMLError as e:
        logger.warning(f"Failed to parse YAML frontmatter: {e}")
        return {}, content


def parse_skill_file(file_path: Path) -> Tuple[Dict[str, Any], str]:
    """Parse a SKILL.md file.

    Args:
        file_path: Path to SKILL.md file

    Returns:
        Tuple of (frontmatter_dict, content)

    Raises:
        FileNotFoundError: If file doesn't exist
        PermissionError: If file can't be read
    """
    content = file_path.read_text(encoding="utf-8")
    frontmatter, remaining = parse_frontmatter(content)

    # If name not in frontmatter, use directory name
    if not frontmatter.get("name"):
        frontmatter["name"] = file_path.parent.name

    return frontmatter, remaining


def validate_manifest(frontmatter: Dict[str, Any]) -> Optional[str]:
    """Validate skill manifest frontmatter.

    Args:
        frontmatter: Parsed frontmatter dict

    Returns:
        Error message if invalid, None if valid
    """
    if not frontmatter.get("name"):
        return "Skill manifest missing required 'name' field"

    name = frontmatter["name"]
    if not isinstance(name, str):
        return f"Skill name must be a string, got {type(name).__name__}"

    # Validate name format (alphanumeric, hyphens, underscores)
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        return f"Invalid skill name '{name}': must contain only alphanumeric, hyphens, underscores"

    return None
