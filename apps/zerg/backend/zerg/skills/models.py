"""Skill data models for the Skills Platform.

Defines the core data structures for skills:
- SkillManifest: Parsed SKILL.md frontmatter
- SkillRequirements: Environment/binary requirements
- Skill: A loaded skill with content and metadata
- SkillEntry: A skill with computed eligibility status
"""

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional


class SkillSource(str, Enum):
    """Where a skill was loaded from."""

    BUNDLED = "bundled"  # Shipped with Zerg
    WORKSPACE = "workspace"  # From a specific workspace
    USER = "user"  # User-managed skills directory
    PACKAGE = "package"  # npm/pypi package


@dataclass(frozen=True)
class SkillRequirements:
    """Requirements for a skill to be eligible.

    Attributes:
        bins: Required binary executables (all must be present)
        any_bins: Binary executables where at least one must be present
        env: Required environment variables
        config: Required config keys in skill config
    """

    bins: tuple[str, ...] = field(default_factory=tuple)
    any_bins: tuple[str, ...] = field(default_factory=tuple)
    env: tuple[str, ...] = field(default_factory=tuple)
    config: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SkillRequirements":
        """Create from dictionary (parsed YAML)."""
        if not data:
            return cls()
        return cls(
            bins=tuple(data.get("bins") or []),
            any_bins=tuple(data.get("anyBins") or data.get("any_bins") or []),
            env=tuple(data.get("env") or []),
            config=tuple(data.get("config") or []),
        )


@dataclass(frozen=True)
class SkillManifest:
    """Parsed SKILL.md frontmatter metadata.

    Attributes:
        name: Unique skill identifier
        description: Human-readable description
        emoji: Optional emoji for UI display
        homepage: Optional URL to documentation
        primary_env: Primary environment variable (for UI hints)
        requires: Skill requirements
        user_invocable: Whether users can invoke via slash command
        model_invocable: Whether the model can invoke this skill
        always: Always include in prompt (even if requirements not met)
        os: List of supported operating systems
        tool_dispatch: If set, skill dispatches to a specific tool
        raw: Raw frontmatter dict for extension
    """

    name: str
    description: str = ""
    emoji: str = ""
    homepage: str = ""
    primary_env: str = ""
    requires: SkillRequirements = field(default_factory=SkillRequirements)
    user_invocable: bool = True
    model_invocable: bool = True
    always: bool = False
    os: tuple[str, ...] = field(default_factory=tuple)
    tool_dispatch: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_frontmatter(cls, frontmatter: Dict[str, Any]) -> "SkillManifest":
        """Create manifest from parsed YAML frontmatter."""
        # Handle clawdbot-style nested metadata
        metadata = frontmatter.get("metadata", {})
        if isinstance(metadata, str):
            # Sometimes metadata is serialized JSON in YAML
            import json

            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}

        clawdbot = metadata.get("clawdbot", {}) if isinstance(metadata, dict) else {}

        requires_data = clawdbot.get("requires") or frontmatter.get("requires")

        return cls(
            name=frontmatter.get("name", ""),
            description=frontmatter.get("description", ""),
            emoji=clawdbot.get("emoji") or frontmatter.get("emoji", ""),
            homepage=frontmatter.get("homepage", ""),
            primary_env=clawdbot.get("primaryEnv") or frontmatter.get("primary_env", ""),
            requires=SkillRequirements.from_dict(requires_data),
            user_invocable=frontmatter.get("user_invocable", True),
            model_invocable=frontmatter.get("model_invocable", True),
            always=clawdbot.get("always") or frontmatter.get("always", False),
            os=tuple(clawdbot.get("os") or frontmatter.get("os") or []),
            tool_dispatch=frontmatter.get("command-tool") or frontmatter.get("tool_dispatch"),
            raw=dict(frontmatter),
        )


@dataclass
class Skill:
    """A loaded skill with content and metadata.

    Attributes:
        manifest: Parsed manifest from SKILL.md frontmatter
        content: The markdown content (after frontmatter)
        base_dir: Directory containing the skill
        file_path: Path to SKILL.md
        source: Where the skill was loaded from
        loaded_at: When the skill was loaded
    """

    manifest: SkillManifest
    content: str
    base_dir: Path
    file_path: Path
    source: SkillSource = SkillSource.WORKSPACE
    loaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def name(self) -> str:
        """Skill name from manifest."""
        return self.manifest.name

    @property
    def description(self) -> str:
        """Skill description from manifest."""
        return self.manifest.description

    def format_for_prompt(self) -> str:
        """Format skill for inclusion in system prompt.

        Returns markdown block with skill name, description, and content.
        """
        lines = []

        # Header with name and emoji
        if self.manifest.emoji:
            lines.append(f"## {self.manifest.emoji} {self.manifest.name}")
        else:
            lines.append(f"## {self.manifest.name}")

        # Description if present
        if self.manifest.description:
            lines.append("")
            lines.append(f"*{self.manifest.description}*")

        # Content
        if self.content.strip():
            lines.append("")
            lines.append(self.content.strip())

        return "\n".join(lines)


@dataclass
class SkillEntry:
    """A skill with computed eligibility information.

    Used for skill management UI and filtering.

    Attributes:
        skill: The loaded skill
        eligible: Whether the skill meets all requirements
        missing_bins: Binary executables that are missing
        missing_env: Environment variables that are missing
        missing_config: Config keys that are missing
    """

    skill: Skill
    eligible: bool = True
    missing_bins: List[str] = field(default_factory=list)
    missing_env: List[str] = field(default_factory=list)
    missing_config: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Skill name."""
        return self.skill.name

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "name": self.skill.name,
            "description": self.skill.description,
            "emoji": self.skill.manifest.emoji,
            "source": self.skill.source.value,
            "eligible": self.eligible,
            "user_invocable": self.skill.manifest.user_invocable,
            "model_invocable": self.skill.manifest.model_invocable,
            "missing_requirements": {
                "bins": self.missing_bins,
                "env": self.missing_env,
                "config": self.missing_config,
            },
        }
