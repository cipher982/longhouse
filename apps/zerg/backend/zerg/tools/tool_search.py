"""Semantic search over tool catalog using embeddings.

This module provides semantic search for finding relevant tools based on
natural language queries. It uses OpenAI's text-embedding-3-small model
and caches embeddings to disk for performance.

Usage:
    # Get or build the search index
    index = await get_tool_search_index()

    # Search for tools
    results = await index.search("send a message to slack", top_k=5)
    for entry in results:
        print(f"{entry.name}: {entry.summary}")
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from openai import AsyncOpenAI

if TYPE_CHECKING:
    from zerg.types.tools import Tool as BaseTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool catalog entry (moved from catalog.py)
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
    "memory_": "memory",
    "knowledge_": "knowledge",
    "web_": "web",
    "http_": "web",
    "spawn_commis": "oikos",
    "spawn_workspace_commis": "oikos",
    "list_commiss": "oikos",
    "read_commis": "oikos",
    "get_commis_evidence": "oikos",
    "get_tool_output": "oikos",
    "grep_commiss": "oikos",
    "get_commis": "oikos",
    "contact_user": "oikos",
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
    """Extract a compact summary from a tool description."""
    if not description:
        return ""
    desc = " ".join(description.split())
    sentences = re.split(r"(?<=[.!?])\s+", desc)
    if sentences:
        first = sentences[0]
        if len(first) <= max_length:
            return first
    if len(desc) <= max_length:
        return desc
    truncated = desc[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > max_length * 0.6:
        truncated = truncated[:last_space]
    return truncated.rstrip(".,") + "..."


def _extract_param_hints(tool: BaseTool) -> str:
    """Extract parameter hints from tool schema."""
    schema = getattr(tool, "args_schema", None)
    if not schema:
        return "()"
    try:
        fields = schema.model_fields
    except AttributeError:
        return "()"
    params = []
    for name, field_info in fields.items():
        if name.startswith("_"):
            continue
        is_required = field_info.is_required()
        param_str = name if is_required else f"{name}?"
        params.append(param_str)
    if not params:
        return "()"
    if len(params) > 5:
        params = params[:5] + ["..."]
    return f"({', '.join(params)})"


@dataclass(frozen=True)
class ToolCatalogEntry:
    """Compact representation of a tool for catalog."""

    name: str
    summary: str
    category: str
    param_hints: str

    def format_for_prompt(self) -> str:
        """Format entry for system prompt injection."""
        return f"- **{self.name}**{self.param_hints}: {self.summary}"

    def format_compact(self) -> str:
        """Format entry as compact one-liner."""
        return f"{self.name}{self.param_hints} - {self.summary}"


@lru_cache(maxsize=1)
def build_catalog() -> tuple[ToolCatalogEntry, ...]:
    """Build compact catalog from full tool registry.

    Cached and only rebuilt when the module is reloaded.
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


def format_catalog_for_prompt(
    catalog: tuple[ToolCatalogEntry, ...] | None = None,
    *,
    exclude_core: bool = False,
    max_tools: int | None = None,
) -> str:
    """Format catalog as markdown for system prompt injection."""
    if catalog is None:
        catalog = build_catalog()

    from zerg.tools.lazy_binder import CORE_TOOLS

    entries = list(catalog)
    if exclude_core:
        entries = [e for e in entries if e.name not in CORE_TOOLS]

    if max_tools and len(entries) > max_tools:
        entries = entries[:max_tools]

    by_category: dict[str, list[ToolCatalogEntry]] = defaultdict(list)
    for entry in entries:
        by_category[entry.category].append(entry)

    lines = []
    category_order = [
        "oikos",
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
        cat_display = category.replace("_", " ").title()
        lines.append(f"\n### {cat_display}")
        for entry in sorted(cat_entries, key=lambda e: e.name):
            lines.append(entry.format_for_prompt())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context for per-fiche tool filtering
# ---------------------------------------------------------------------------

_search_allowed_tools: ContextVar[list[str] | None] = ContextVar("search_allowed_tools", default=None)
_search_max_results: ContextVar[int] = ContextVar("search_max_results", default=20)


def set_search_context(
    allowed_tools: list[str] | None = None,
    max_results: int = 20,
) -> None:
    """Set the search context for the current fiche."""
    _search_allowed_tools.set(allowed_tools)
    _search_max_results.set(max_results)


def clear_search_context() -> None:
    """Clear the search context after oikos completes."""
    _search_allowed_tools.set(None)
    _search_max_results.set(20)


def _is_tool_allowed(name: str) -> bool:
    """Check if a tool is allowed by the current context's allowlist."""
    allowed = _search_allowed_tools.get()
    if allowed is None or len(allowed) == 0:
        return True

    for pattern in allowed:
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
        elif pattern == name:
            return True

    return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536

EMBEDDING_CACHE_DIR = Path(__file__).parent.parent.parent / "data"
EMBEDDING_CACHE_FILE = EMBEDDING_CACHE_DIR / "tool_embeddings.npz"

_search_index: ToolSearchIndex | None = None


# ---------------------------------------------------------------------------
# Search Index
# ---------------------------------------------------------------------------


class ToolSearchIndex:
    """Semantic search over tool catalog using embeddings."""

    def __init__(self, catalog: tuple[ToolCatalogEntry, ...]):
        self.catalog = catalog
        self.embeddings: np.ndarray | None = None
        self._client: AsyncOpenAI | None = None
        self._name_to_idx: dict[str, int] = {e.name: i for i, e in enumerate(catalog)}

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI()
        return self._client

    async def build_index(self) -> None:
        """Generate embeddings for all tools."""
        logger.info(f"Building tool search index for {len(self.catalog)} tools")

        texts = []
        for entry in self.catalog:
            text = f"{entry.name}: {entry.summary} (category: {entry.category})"
            texts.append(text)

        try:
            response = await self.client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts,
            )
            self.embeddings = np.array([r.embedding for r in response.data], dtype=np.float32)
            logger.info(f"Generated embeddings with shape {self.embeddings.shape}")
        except Exception as e:
            logger.error(f"Failed to generate embeddings: {e}")
            raise

    async def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        min_score: float = 0.0,
    ) -> list[tuple[ToolCatalogEntry, float]]:
        """Find tools matching query by semantic similarity."""
        if self.embeddings is None:
            raise RuntimeError("Index not built. Call build_index() first.")

        try:
            response = await self.client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=[query],
            )
            query_vec = np.array(response.data[0].embedding, dtype=np.float32)
        except Exception as e:
            logger.error(f"Failed to embed query: {e}")
            raise

        similarities = self.embeddings @ query_vec

        if top_k >= len(similarities):
            top_indices = np.argsort(similarities)[::-1]
        else:
            top_indices = np.argpartition(similarities, -top_k)[-top_k:]
            top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

        results = []
        for idx in top_indices:
            score = float(similarities[idx])
            if score < min_score:
                continue
            results.append((self.catalog[idx], score))

        return results

    async def search_names(self, query: str, top_k: int = 5) -> list[str]:
        """Search and return just tool names."""
        results = await self.search(query, top_k=top_k)
        return [entry.name for entry, _score in results]

    def get_entry_by_name(self, name: str) -> ToolCatalogEntry | None:
        """Get catalog entry by tool name."""
        idx = self._name_to_idx.get(name)
        if idx is not None:
            return self.catalog[idx]
        return None


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def _get_catalog_hash(catalog: tuple[ToolCatalogEntry, ...]) -> str:
    """Generate a hash of the catalog for cache validation."""
    import hashlib

    content = "|".join(f"{e.name}:{e.summary}" for e in catalog)
    return hashlib.md5(content.encode()).hexdigest()[:16]


def _save_embeddings_cache(
    embeddings: np.ndarray,
    catalog_hash: str,
    tool_names: list[str],
) -> None:
    """Save embeddings to disk cache."""
    try:
        EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            EMBEDDING_CACHE_FILE,
            embeddings=embeddings,
            catalog_hash=catalog_hash,
            tool_names=tool_names,
        )
        logger.info(f"Saved embeddings cache to {EMBEDDING_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to save embeddings cache: {e}")


def _load_embeddings_cache(
    catalog_hash: str,
    expected_names: list[str],
) -> np.ndarray | None:
    """Load embeddings from disk cache if valid."""
    if not EMBEDDING_CACHE_FILE.exists():
        logger.debug("Embeddings cache not found")
        return None

    try:
        data = np.load(EMBEDDING_CACHE_FILE, allow_pickle=False)
        cached_hash = str(data.get("catalog_hash", ""))
        if cached_hash != catalog_hash:
            logger.info(f"Embeddings cache stale (hash mismatch: {cached_hash} != {catalog_hash})")
            return None
        cached_names = list(data.get("tool_names", []))
        if cached_names != expected_names:
            logger.info("Embeddings cache stale (tool names changed)")
            return None
        embeddings = data["embeddings"]
        logger.info(f"Loaded embeddings cache with shape {embeddings.shape}")
        return embeddings
    except Exception as e:
        logger.warning(f"Failed to load embeddings cache: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_tool_search_index(*, force_rebuild: bool = False) -> ToolSearchIndex:
    """Get or build the tool search index."""
    global _search_index

    if _search_index is not None and not force_rebuild:
        return _search_index

    catalog = build_catalog()
    catalog_hash = _get_catalog_hash(catalog)
    tool_names = [e.name for e in catalog]

    index = ToolSearchIndex(catalog)

    if not force_rebuild:
        cached_embeddings = _load_embeddings_cache(catalog_hash, tool_names)
        if cached_embeddings is not None:
            index.embeddings = cached_embeddings
            _search_index = index
            return index

    await index.build_index()

    if index.embeddings is not None:
        _save_embeddings_cache(index.embeddings, catalog_hash, tool_names)

    _search_index = index
    return index


def clear_search_index_cache() -> None:
    """Clear the in-memory search index cache."""
    global _search_index
    _search_index = None


def delete_embeddings_cache() -> bool:
    """Delete the disk embeddings cache."""
    if EMBEDDING_CACHE_FILE.exists():
        EMBEDDING_CACHE_FILE.unlink()
        logger.info(f"Deleted embeddings cache at {EMBEDDING_CACHE_FILE}")
        return True
    return False


# ---------------------------------------------------------------------------
# Search tools meta-tool (for fiches)
# ---------------------------------------------------------------------------


async def search_tools_for_fiche(
    query: str,
    max_results: int = 5,
) -> dict:
    """Search tools by description - designed for fiche use."""
    index = await get_tool_search_index()

    context_cap = _search_max_results.get()
    effective_max = min(max_results, context_cap)

    search_count = min(effective_max * 2, 20)
    results = await index.search(query, top_k=search_count)

    tools = []
    for entry, score in results:
        if not _is_tool_allowed(entry.name):
            logger.debug(f"Filtered out tool '{entry.name}' - not in allowlist")
            continue

        tools.append(
            {
                "name": entry.name,
                "summary": entry.summary,
                "category": entry.category,
                "params": entry.param_hints,
                "relevance": round(score, 3),
            }
        )

        if len(tools) >= effective_max:
            break

    return {
        "tools": tools,
        "total_available": len(index.catalog),
        "query": query,
    }
