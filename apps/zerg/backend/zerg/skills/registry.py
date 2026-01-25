"""Skill registry for runtime skill management.

The SkillRegistry manages loaded skills and integrates with the tool system.
It provides:
- Skill lookup by name
- Skill filtering by eligibility
- Prompt generation for system prompts
- Integration with tool allowlists
"""

import logging
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Dict
from typing import FrozenSet
from typing import List
from typing import Optional
from typing import Set

from zerg.skills.loader import SkillLoader
from zerg.skills.models import Skill
from zerg.skills.models import SkillEntry

logger = logging.getLogger(__name__)


@dataclass
class SkillSnapshot:
    """Immutable snapshot of skills for a run.

    Attributes:
        prompt: Pre-formatted prompt text for system prompt
        skills: List of skill summaries
        version: Snapshot version for cache invalidation
    """

    prompt: str
    skills: List[Dict[str, str]]  # [{name, description, emoji}]
    version: int = 0


@dataclass
class SkillRegistry:
    """Runtime registry for loaded skills.

    Thread-safe skill management with support for:
    - Multiple skill sources (bundled, user, workspace)
    - Eligibility filtering
    - Prompt generation

    Example:
        registry = SkillRegistry()
        registry.load_for_workspace("/path/to/workspace")

        # Get prompt for system prompt
        prompt = registry.format_skills_prompt()

        # Get eligible skills only
        eligible = registry.get_eligible_skills()
    """

    _skills: Dict[str, Skill] = field(default_factory=dict)
    _entries: Dict[str, SkillEntry] = field(default_factory=dict)
    _loader: SkillLoader = field(default_factory=SkillLoader)
    _workspace_path: Optional[Path] = None
    _version: int = 0

    def load_for_workspace(
        self,
        workspace_path: Optional[Path] = None,
        available_config: Optional[Set[str]] = None,
    ) -> None:
        """Load skills for a workspace.

        Args:
            workspace_path: Workspace to load skills from
            available_config: Available config keys for eligibility
        """
        self._workspace_path = Path(workspace_path) if workspace_path else None

        entries = self._loader.load_skill_entries(
            workspace_path=self._workspace_path,
            available_config=available_config,
        )

        self._skills = {e.skill.name: e.skill for e in entries}
        self._entries = {e.name: e for e in entries}
        self._version += 1

        logger.info(f"Loaded {len(self._skills)} skills for workspace " f"{self._workspace_path or 'default'}")

    def reload(self, available_config: Optional[Set[str]] = None) -> None:
        """Reload skills from current workspace."""
        self.load_for_workspace(self._workspace_path, available_config)

    def get_skill(self, name: str) -> Optional[Skill]:
        """Get a skill by name."""
        return self._skills.get(name)

    def get_entry(self, name: str) -> Optional[SkillEntry]:
        """Get a skill entry with eligibility info."""
        return self._entries.get(name)

    def get_all_skills(self) -> List[Skill]:
        """Get all loaded skills."""
        return list(self._skills.values())

    def get_all_entries(self) -> List[SkillEntry]:
        """Get all skill entries."""
        return list(self._entries.values())

    def get_eligible_skills(self) -> List[Skill]:
        """Get only eligible skills."""
        return [e.skill for e in self._entries.values() if e.eligible]

    def get_eligible_entries(self) -> List[SkillEntry]:
        """Get only eligible skill entries."""
        return [e for e in self._entries.values() if e.eligible]

    def get_skill_names(self) -> FrozenSet[str]:
        """Get set of all skill names."""
        return frozenset(self._skills.keys())

    def filter_by_allowlist(
        self,
        allowed: Optional[List[str]] = None,
    ) -> List[Skill]:
        """Filter skills by allowlist.

        Supports wildcards: "github*" matches "github", "github-issues", etc.

        Args:
            allowed: List of allowed skill names/patterns. None = all allowed.

        Returns:
            List of matching skills
        """
        if not allowed:
            return self.get_eligible_skills()

        result = []
        for skill in self.get_eligible_skills():
            for pattern in allowed:
                if pattern.endswith("*"):
                    prefix = pattern[:-1]
                    if skill.name.startswith(prefix):
                        result.append(skill)
                        break
                elif pattern == skill.name:
                    result.append(skill)
                    break

        return result

    def format_skills_prompt(
        self,
        skills: Optional[List[Skill]] = None,
        max_skills: Optional[int] = None,
    ) -> str:
        """Format skills for system prompt injection.

        Args:
            skills: Skills to format (default: eligible skills)
            max_skills: Maximum number of skills to include

        Returns:
            Markdown-formatted skills section
        """
        if skills is None:
            skills = self.get_eligible_skills()

        # Filter to model-invocable skills
        skills = [s for s in skills if s.manifest.model_invocable]

        if max_skills and len(skills) > max_skills:
            skills = skills[:max_skills]

        if not skills:
            return ""

        lines = ["# Available Skills", ""]

        for skill in sorted(skills, key=lambda s: s.name):
            lines.append(skill.format_for_prompt())
            lines.append("")

        return "\n".join(lines)

    def get_snapshot(
        self,
        allowed: Optional[List[str]] = None,
    ) -> SkillSnapshot:
        """Get an immutable snapshot of skills.

        Args:
            allowed: Optional allowlist for filtering

        Returns:
            SkillSnapshot with prompt and metadata
        """
        skills = self.filter_by_allowlist(allowed)
        prompt = self.format_skills_prompt(skills)

        skill_summaries = [
            {
                "name": s.name,
                "description": s.description,
                "emoji": s.manifest.emoji,
            }
            for s in skills
        ]

        return SkillSnapshot(
            prompt=prompt,
            skills=skill_summaries,
            version=self._version,
        )

    def get_user_invocable_commands(self) -> List[Dict[str, str]]:
        """Get skills that users can invoke via slash commands.

        Returns:
            List of {name, description} for user-invocable skills
        """
        commands = []
        for entry in self._entries.values():
            if entry.eligible and entry.skill.manifest.user_invocable:
                commands.append(
                    {
                        "name": entry.skill.name,
                        "description": entry.skill.description[:100],  # Discord limit
                        "emoji": entry.skill.manifest.emoji,
                    }
                )
        return sorted(commands, key=lambda c: c["name"])


# Global registry instance for convenience
_global_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """Get the global skill registry instance."""
    global _global_registry
    if _global_registry is None:
        _global_registry = SkillRegistry()
    return _global_registry


def reset_skill_registry() -> None:
    """Reset the global skill registry (for testing)."""
    global _global_registry
    _global_registry = None
