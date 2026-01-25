"""Skill loader service.

Loads skills from filesystem directories and packages.
Supports multiple skill sources with precedence:
  package < bundled < user < workspace

Skills from higher-precedence sources override lower ones by name.
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Dict
from typing import List
from typing import Optional
from typing import Set

from zerg.skills.models import Skill
from zerg.skills.models import SkillEntry
from zerg.skills.models import SkillManifest
from zerg.skills.models import SkillSource
from zerg.skills.parser import parse_skill_file
from zerg.skills.parser import validate_manifest

logger = logging.getLogger(__name__)

# Default bundled skills directory (relative to this module)
BUNDLED_SKILLS_DIR = Path(__file__).parent / "bundled"

# User skills directory
USER_SKILLS_DIR = Path.home() / ".zerg" / "skills"


def _check_binary_exists(binary: str) -> bool:
    """Check if a binary exists in PATH."""
    return shutil.which(binary) is not None


def _check_env_exists(env_var: str) -> bool:
    """Check if an environment variable is set."""
    return env_var in os.environ


class SkillLoader:
    """Loads skills from directories and computes eligibility.

    Example:
        loader = SkillLoader()

        # Load all skills for a workspace
        entries = loader.load_workspace_skills("/path/to/workspace")

        # Load only bundled skills
        bundled = loader.load_bundled_skills()
    """

    def __init__(
        self,
        bundled_dir: Optional[Path] = None,
        user_dir: Optional[Path] = None,
        extra_dirs: Optional[List[Path]] = None,
    ):
        """Initialize the skill loader.

        Args:
            bundled_dir: Directory for bundled skills (default: zerg/skills/bundled)
            user_dir: Directory for user-managed skills (default: ~/.zerg/skills)
            extra_dirs: Additional directories to scan for skills
        """
        self.bundled_dir = bundled_dir or BUNDLED_SKILLS_DIR
        self.user_dir = user_dir or USER_SKILLS_DIR
        self.extra_dirs = extra_dirs or []

    def load_from_directory(
        self,
        directory: Path,
        source: SkillSource,
    ) -> List[Skill]:
        """Load all skills from a directory.

        Each subdirectory containing a SKILL.md file is treated as a skill.

        Args:
            directory: Directory to scan
            source: Source type for loaded skills

        Returns:
            List of loaded skills
        """
        skills = []
        directory = Path(directory)

        if not directory.exists() or not directory.is_dir():
            logger.debug(f"Skills directory does not exist: {directory}")
            return skills

        for skill_dir in directory.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                skill = self._load_skill(skill_file, source)
                if skill:
                    skills.append(skill)
            except Exception as e:
                logger.warning(f"Failed to load skill from {skill_dir}: {e}")

        return skills

    def _load_skill(
        self,
        skill_file: Path,
        source: SkillSource,
    ) -> Optional[Skill]:
        """Load a single skill from a SKILL.md file.

        Args:
            skill_file: Path to SKILL.md
            source: Source type for the skill

        Returns:
            Loaded Skill or None if invalid
        """
        try:
            frontmatter, content = parse_skill_file(skill_file)
        except Exception as e:
            logger.warning(f"Failed to parse {skill_file}: {e}")
            return None

        # Validate manifest
        error = validate_manifest(frontmatter)
        if error:
            logger.warning(f"Invalid skill manifest {skill_file}: {error}")
            return None

        manifest = SkillManifest.from_frontmatter(frontmatter)

        return Skill(
            manifest=manifest,
            content=content,
            base_dir=skill_file.parent,
            file_path=skill_file,
            source=source,
        )

    def load_bundled_skills(self) -> List[Skill]:
        """Load bundled skills shipped with Zerg."""
        return self.load_from_directory(self.bundled_dir, SkillSource.BUNDLED)

    def load_user_skills(self) -> List[Skill]:
        """Load user-managed skills from ~/.zerg/skills."""
        return self.load_from_directory(self.user_dir, SkillSource.USER)

    def load_workspace_skills(self, workspace_path: Path) -> List[Skill]:
        """Load skills from a workspace's skills/ directory."""
        skills_dir = Path(workspace_path) / "skills"
        return self.load_from_directory(skills_dir, SkillSource.WORKSPACE)

    def load_all_skills(
        self,
        workspace_path: Optional[Path] = None,
        include_bundled: bool = True,
        include_user: bool = True,
    ) -> Dict[str, Skill]:
        """Load all skills with precedence merging.

        Skills from higher-precedence sources override lower ones by name.
        Precedence (low to high): bundled < user < workspace

        Args:
            workspace_path: Optional workspace to load skills from
            include_bundled: Include bundled skills
            include_user: Include user-managed skills

        Returns:
            Dict mapping skill name to Skill (after merging)
        """
        merged: Dict[str, Skill] = {}

        # Load in precedence order (lowest first)
        if include_bundled:
            for skill in self.load_bundled_skills():
                merged[skill.name] = skill

        # Load from extra directories
        for extra_dir in self.extra_dirs:
            for skill in self.load_from_directory(extra_dir, SkillSource.PACKAGE):
                merged[skill.name] = skill

        if include_user:
            for skill in self.load_user_skills():
                merged[skill.name] = skill

        if workspace_path:
            for skill in self.load_workspace_skills(workspace_path):
                merged[skill.name] = skill

        return merged

    def compute_eligibility(
        self,
        skill: Skill,
        available_config: Optional[Set[str]] = None,
    ) -> SkillEntry:
        """Compute eligibility for a skill based on requirements.

        Args:
            skill: Skill to check
            available_config: Set of available config keys

        Returns:
            SkillEntry with eligibility information
        """
        missing_bins = []
        missing_env = []
        missing_config = []

        reqs = skill.manifest.requires

        # Check required binaries
        for binary in reqs.bins:
            if not _check_binary_exists(binary):
                missing_bins.append(binary)

        # Check any_bins (at least one must exist)
        if reqs.any_bins:
            has_any = any(_check_binary_exists(b) for b in reqs.any_bins)
            if not has_any:
                missing_bins.extend(reqs.any_bins)

        # Check environment variables
        for env_var in reqs.env:
            if not _check_env_exists(env_var):
                missing_env.append(env_var)

        # Check config keys
        if available_config is not None:
            for config_key in reqs.config:
                if config_key not in available_config:
                    missing_config.append(config_key)

        # Eligible if no missing requirements (or always=True)
        eligible = skill.manifest.always or (not missing_bins and not missing_env and not missing_config)

        return SkillEntry(
            skill=skill,
            eligible=eligible,
            missing_bins=missing_bins,
            missing_env=missing_env,
            missing_config=missing_config,
        )

    def load_skill_entries(
        self,
        workspace_path: Optional[Path] = None,
        available_config: Optional[Set[str]] = None,
        filter_eligible: bool = False,
    ) -> List[SkillEntry]:
        """Load skills with eligibility information.

        Args:
            workspace_path: Optional workspace path
            available_config: Available config keys for checking requirements
            filter_eligible: If True, only return eligible skills

        Returns:
            List of SkillEntry with eligibility computed
        """
        skills = self.load_all_skills(workspace_path)
        entries = [self.compute_eligibility(skill, available_config) for skill in skills.values()]

        if filter_eligible:
            entries = [e for e in entries if e.eligible]

        return sorted(entries, key=lambda e: e.name)
