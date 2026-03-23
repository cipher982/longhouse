"""Skills Platform for Zerg.

A skills platform enabling workspace-scoped tools with markdown-based manifests.
Skills are defined via SKILL.md files with YAML frontmatter, similar to Clawdbot.

Usage:
    from zerg.skills import SkillLoader, SkillRegistry

    # Load skills from a workspace
    loader = SkillLoader()
    skills = loader.load_workspace_skills(Path("/path/to/workspace"))

    # Use a skill registry for a workspace
    registry = SkillRegistry()
    registry.load_for_workspace(Path("/path/to/workspace"))

    # Get formatted prompt for system prompt injection
    prompt = registry.format_skills_prompt()
"""

from zerg.skills.loader import SkillLoader
from zerg.skills.models import Skill
from zerg.skills.models import SkillEntry
from zerg.skills.models import SkillManifest
from zerg.skills.models import SkillRequirements
from zerg.skills.registry import SkillRegistry

__all__ = [
    "Skill",
    "SkillEntry",
    "SkillLoader",
    "SkillManifest",
    "SkillRegistry",
    "SkillRequirements",
]
