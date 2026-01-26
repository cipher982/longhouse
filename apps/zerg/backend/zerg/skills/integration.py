"""Integration between Skills Platform and Zerg's tool system.

Provides:
- Skill-to-tool conversion for tool dispatch
- System prompt augmentation with skills
- Skill execution context management
"""

import logging
from pathlib import Path
from typing import Any
from typing import List
from typing import Optional
from typing import Set

from langchain_core.tools import StructuredTool

from zerg.skills.models import Skill
from zerg.skills.models import SkillEntry
from zerg.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class SkillContext:
    """Context for skill execution within an course.

    Manages skill loading and prompt generation for a specific run,
    supporting workspace-scoped skills.

    Example:
        ctx = SkillContext(workspace_path="/path/to/workspace")
        ctx.load()

        # Get prompt for system prompt
        skill_prompt = ctx.get_prompt()

        # Get skill-based tools
        tools = ctx.get_skill_tools()
    """

    def __init__(
        self,
        workspace_path: Optional[Path] = None,
        allowed_skills: Optional[List[str]] = None,
        available_config: Optional[Set[str]] = None,
    ):
        """Initialize skill context.

        Args:
            workspace_path: Workspace to load skills from
            allowed_skills: Allowlist of skill names/patterns
            available_config: Available config keys for eligibility
        """
        self.workspace_path = Path(workspace_path) if workspace_path else None
        self.allowed_skills = allowed_skills
        self.available_config = available_config or set()
        self._registry = SkillRegistry()
        self._loaded = False

    def load(self) -> None:
        """Load skills into context."""
        self._registry.load_for_workspace(
            workspace_path=self.workspace_path,
            available_config=self.available_config,
        )
        self._loaded = True

    def ensure_loaded(self) -> None:
        """Ensure skills are loaded."""
        if not self._loaded:
            self.load()

    def get_prompt(self) -> str:
        """Get skills prompt for system prompt injection."""
        self.ensure_loaded()
        skills = self._registry.filter_by_allowlist(self.allowed_skills)
        return self._registry.format_skills_prompt(skills)

    def get_eligible_skills(self) -> List[Skill]:
        """Get eligible skills."""
        self.ensure_loaded()
        return self._registry.filter_by_allowlist(self.allowed_skills)

    def get_skill_entries(self) -> List[SkillEntry]:
        """Get all skill entries with eligibility info."""
        self.ensure_loaded()
        return self._registry.get_all_entries()

    def get_skill(self, name: str) -> Optional[Skill]:
        """Get a specific skill by name."""
        self.ensure_loaded()
        return self._registry.get_skill(name)


def create_skill_tool(
    skill: Skill,
    tool_registry: Optional[Any] = None,
) -> Optional[StructuredTool]:
    """Create a LangChain tool from a skill with tool dispatch.

    If the skill has tool_dispatch set, creates a wrapper tool that
    invokes the target tool with the skill's context.

    Args:
        skill: Skill with tool_dispatch configured
        tool_registry: Tool registry for looking up target tool

    Returns:
        StructuredTool wrapping the dispatched tool, or None
    """
    if not skill.manifest.tool_dispatch:
        return None

    target_tool_name = skill.manifest.tool_dispatch

    # Look up target tool
    if tool_registry:
        target_tool = tool_registry.get(target_tool_name)
        if not target_tool:
            logger.warning(f"Skill {skill.name} dispatches to unknown tool: {target_tool_name}")
            return None

        # Create wrapper that adds skill context
        def skill_tool_wrapper(**kwargs: Any) -> Any:
            # Could add skill-specific context here
            return target_tool.func(**kwargs)

        return StructuredTool.from_function(
            func=skill_tool_wrapper,
            name=f"skill_{skill.name}",
            description=f"[Skill: {skill.name}] {skill.description}",
        )

    return None


def augment_system_prompt(
    system_prompt: str,
    skill_context: SkillContext,
    position: str = "end",
) -> str:
    """Augment system prompt with skills.

    Args:
        system_prompt: Original system prompt
        skill_context: Skill context with loaded skills
        position: Where to insert skills ("start", "end", or "after:marker")

    Returns:
        Augmented system prompt
    """
    skill_prompt = skill_context.get_prompt()
    if not skill_prompt:
        return system_prompt

    if position == "start":
        return f"{skill_prompt}\n\n{system_prompt}"
    elif position == "end":
        return f"{system_prompt}\n\n{skill_prompt}"
    elif position.startswith("after:"):
        marker = position[6:]
        if marker in system_prompt:
            return system_prompt.replace(marker, f"{marker}\n\n{skill_prompt}")
        # Marker not found, append to end
        return f"{system_prompt}\n\n{skill_prompt}"
    else:
        return f"{system_prompt}\n\n{skill_prompt}"


def get_skill_tool_names(skill_context: SkillContext) -> List[str]:
    """Get tool names referenced by skills.

    Returns list of tool names that skills dispatch to.
    """
    skills = skill_context.get_eligible_skills()
    tool_names = []

    for skill in skills:
        if skill.manifest.tool_dispatch:
            tool_names.append(skill.manifest.tool_dispatch)

    return tool_names


class SkillIntegration:
    """High-level skill integration for courses.

    Provides a simple interface for integrating skills into fiche execution.

    Example:
        integration = SkillIntegration(
            workspace_path="/path/to/workspace",
            allowed_skills=["github*", "slack*"],
        )

        # Augment system prompt
        system_prompt = integration.augment_prompt(original_prompt)

        # Get additional tools from skills
        skill_tools = integration.get_tools(tool_registry)
    """

    def __init__(
        self,
        workspace_path: Optional[Path] = None,
        allowed_skills: Optional[List[str]] = None,
        available_config: Optional[Set[str]] = None,
    ):
        """Initialize skill integration.

        Args:
            workspace_path: Workspace to load skills from
            allowed_skills: Allowlist of skill names/patterns
            available_config: Available config keys for eligibility
        """
        self._context = SkillContext(
            workspace_path=workspace_path,
            allowed_skills=allowed_skills,
            available_config=available_config,
        )

    def load(self) -> None:
        """Load skills."""
        self._context.load()

    def augment_prompt(
        self,
        system_prompt: str,
        position: str = "end",
    ) -> str:
        """Augment system prompt with skills.

        Args:
            system_prompt: Original system prompt
            position: Where to insert skills

        Returns:
            Augmented system prompt
        """
        return augment_system_prompt(system_prompt, self._context, position)

    def get_tools(
        self,
        tool_registry: Optional[Any] = None,
    ) -> List[StructuredTool]:
        """Get tools from skills with tool dispatch.

        Args:
            tool_registry: Tool registry for looking up target tools

        Returns:
            List of skill-wrapped tools
        """
        self._context.ensure_loaded()
        tools = []

        for skill in self._context.get_eligible_skills():
            if skill.manifest.tool_dispatch:
                tool = create_skill_tool(skill, tool_registry)
                if tool:
                    tools.append(tool)

        return tools

    def get_prompt(self) -> str:
        """Get skills prompt."""
        return self._context.get_prompt()

    def get_skill_names(self) -> List[str]:
        """Get names of loaded skills."""
        self._context.ensure_loaded()
        return [s.name for s in self._context.get_eligible_skills()]

    def get_skill(self, name: str) -> Optional[Skill]:
        """Get a skill by name."""
        return self._context.get_skill(name)
