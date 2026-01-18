"""Tool catalog for lazy loading and semantic search.

This module provides a compact representation of all tools for:
1. System prompt injection (tool awareness without full schemas)
2. Semantic search via embeddings
3. Lazy loading based on search results

The catalog is built once at startup from the tool registry.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core tools - always loaded with full schemas
# ---------------------------------------------------------------------------

CORE_TOOLS: frozenset[str] = frozenset(
    [
        # Worker coordination
        "spawn_worker",
        "list_workers",
        "read_worker_result",
        "get_worker_evidence",
        "get_tool_output",
        "grep_workers",
        "get_worker_metadata",
        # User interaction
        "contact_user",
        # Tool discovery (for lazy loading)
        "search_tools",
        "list_tools",
        # Common utilities
        "web_search",
        "http_request",
    ]
)


# ---------------------------------------------------------------------------
# Tool categories (inferred from name prefix)
# ---------------------------------------------------------------------------

CATEGORY_PREFIXES = {
    "github_": "github",
    "jira_": "jira",
    "linear_": "linear",
    "notion_": "notion",
    "slack_": "messaging",
    "discord_": "messaging",
    "send_email": "messaging",
    "send_sms": "messaging",
    "send_imessage": "messaging",
    "list_imessage": "messaging",
    "ssh_": "infrastructure",
    "runner_": "infrastructure",
    "container_": "infrastructure",
    "task_": "tasks",
    "agent_memory_": "memory",
    "memory_": "memory",
    "knowledge_": "knowledge",
    "web_": "web",
    "http_": "web",
    "spawn_worker": "supervisor",
    "list_workers": "supervisor",
    "read_worker": "supervisor",
    "get_worker_evidence": "supervisor",
    "get_tool_output": "supervisor",
    "grep_workers": "supervisor",
    "get_worker": "supervisor",
    "contact_user": "supervisor",
    "get_current_": "personal",
    "get_whoop_": "personal",
    "search_notes": "personal",
    "datetime_": "utility",
    "generate_uuid": "utility",
    "math_": "utility",
    "refresh_connector": "utility",
    "search_tools": "tool_discovery",
    "list_tools": "tool_discovery",
}


def _infer_category(tool_name: str) -> str:
    """Infer tool category from name prefix."""
    for prefix, category in CATEGORY_PREFIXES.items():
        if tool_name.startswith(prefix):
            return category
    return "other"


def _extract_summary(description: str, max_length: int = 120) -> str:
    """Extract a compact summary from a tool description.

    Takes the first sentence or truncates at max_length.
    """
    if not description:
        return ""

    # Clean up whitespace
    desc = " ".join(description.split())

    # Try to get first sentence
    sentences = re.split(r"(?<=[.!?])\s+", desc)
    if sentences:
        first = sentences[0]
        if len(first) <= max_length:
            return first

    # Truncate at max_length
    if len(desc) <= max_length:
        return desc

    # Find last word boundary before max_length
    truncated = desc[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > max_length * 0.6:  # Don't truncate too aggressively
        truncated = truncated[:last_space]

    return truncated.rstrip(".,") + "..."


def _extract_param_hints(tool: BaseTool) -> str:
    """Extract parameter hints from tool schema.

    Returns a compact string like: (owner, repo, state?, limit?)
    """
    schema = getattr(tool, "args_schema", None)
    if not schema:
        return "()"

    # Get schema fields
    try:
        fields = schema.model_fields
    except AttributeError:
        return "()"

    params = []
    for name, field_info in fields.items():
        # Skip internal fields
        if name.startswith("_"):
            continue

        # Check if required
        is_required = field_info.is_required()
        param_str = name if is_required else f"{name}?"
        params.append(param_str)

    if not params:
        return "()"

    # Limit to first 5 params for brevity
    if len(params) > 5:
        params = params[:5] + ["..."]

    return f"({', '.join(params)})"


# ---------------------------------------------------------------------------
# Catalog entry dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCatalogEntry:
    """Compact representation of a tool for catalog."""

    name: str
    summary: str  # 1-2 sentences, ~120 chars max
    category: str  # github, messaging, personal, etc.
    param_hints: str  # (owner, repo, state?, limit?)

    def format_for_prompt(self) -> str:
        """Format entry for system prompt injection."""
        return f"- **{self.name}**{self.param_hints}: {self.summary}"

    def format_compact(self) -> str:
        """Format entry as compact one-liner."""
        return f"{self.name}{self.param_hints} - {self.summary}"


# ---------------------------------------------------------------------------
# Catalog building
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def build_catalog() -> tuple[ToolCatalogEntry, ...]:
    """Build compact catalog from full tool registry.

    This is cached and only rebuilt when the module is reloaded.
    Call clear_catalog_cache() to force rebuild.

    Returns:
        Tuple of ToolCatalogEntry sorted by name.
    """
    from zerg.tools import get_registry

    registry = get_registry()
    entries = []

    for tool in registry.all_tools():
        entry = ToolCatalogEntry(
            name=tool.name,
            summary=_extract_summary(tool.description),
            category=_infer_category(tool.name),
            param_hints=_extract_param_hints(tool),
        )
        entries.append(entry)

    return tuple(sorted(entries, key=lambda e: e.name))


def clear_catalog_cache() -> None:
    """Clear the catalog cache to force rebuild."""
    build_catalog.cache_clear()


def get_catalog_by_category() -> dict[str, list[ToolCatalogEntry]]:
    """Get catalog entries grouped by category."""
    catalog = build_catalog()
    by_category: dict[str, list[ToolCatalogEntry]] = defaultdict(list)

    for entry in catalog:
        by_category[entry.category].append(entry)

    return dict(by_category)


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


def format_catalog_for_prompt(
    catalog: tuple[ToolCatalogEntry, ...] | None = None,
    *,
    exclude_core: bool = False,
    max_tools: int | None = None,
) -> str:
    """Format catalog as markdown for system prompt injection.

    Args:
        catalog: Catalog entries to format. Uses build_catalog() if None.
        exclude_core: If True, exclude core tools (they're already bound).
        max_tools: Maximum number of tools to include.

    Returns:
        Markdown-formatted tool catalog organized by category.
    """
    if catalog is None:
        catalog = build_catalog()

    # Filter entries
    entries = list(catalog)
    if exclude_core:
        entries = [e for e in entries if e.name not in CORE_TOOLS]

    if max_tools and len(entries) > max_tools:
        entries = entries[:max_tools]

    # Group by category
    by_category: dict[str, list[ToolCatalogEntry]] = defaultdict(list)
    for entry in entries:
        by_category[entry.category].append(entry)

    # Build markdown
    lines = []

    # Category display order
    category_order = [
        "supervisor",
        "web",
        "messaging",
        "github",
        "jira",
        "linear",
        "notion",
        "tasks",
        "memory",
        "knowledge",
        "personal",
        "infrastructure",
        "utility",
        "other",
    ]

    for category in category_order:
        if category not in by_category:
            continue

        cat_entries = by_category[category]
        if not cat_entries:
            continue

        # Format category name
        cat_display = category.replace("_", " ").title()
        lines.append(f"\n### {cat_display}")

        for entry in sorted(cat_entries, key=lambda e: e.name):
            lines.append(entry.format_for_prompt())

    return "\n".join(lines)


def format_catalog_compact(
    catalog: tuple[ToolCatalogEntry, ...] | None = None,
    *,
    exclude_core: bool = False,
) -> str:
    """Format catalog as a compact list (no categories).

    Args:
        catalog: Catalog entries to format. Uses build_catalog() if None.
        exclude_core: If True, exclude core tools.

    Returns:
        Compact one-tool-per-line format.
    """
    if catalog is None:
        catalog = build_catalog()

    entries = list(catalog)
    if exclude_core:
        entries = [e for e in entries if e.name not in CORE_TOOLS]

    return "\n".join(e.format_compact() for e in sorted(entries, key=lambda e: e.name))


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def get_core_tool_names() -> frozenset[str]:
    """Get the set of core tool names that are always loaded."""
    return CORE_TOOLS


def is_core_tool(tool_name: str) -> bool:
    """Check if a tool is a core tool."""
    return tool_name in CORE_TOOLS


def get_catalog_stats() -> dict:
    """Get statistics about the tool catalog."""
    catalog = build_catalog()
    by_category = get_catalog_by_category()

    return {
        "total_tools": len(catalog),
        "core_tools": len(CORE_TOOLS),
        "lazy_tools": len(catalog) - len([e for e in catalog if e.name in CORE_TOOLS]),
        "categories": {cat: len(entries) for cat, entries in by_category.items()},
        "approx_prompt_tokens": len(format_catalog_for_prompt()) // 4,  # Rough estimate
    }
