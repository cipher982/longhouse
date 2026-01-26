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
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from openai import AsyncOpenAI

from .catalog import ToolCatalogEntry
from .catalog import build_catalog

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context for per-fiche tool filtering
# ---------------------------------------------------------------------------

# Allowlist for current fiche context (None = all allowed)
_search_allowed_tools: ContextVar[list[str] | None] = ContextVar("search_allowed_tools", default=None)

# Max results cap for current context (used to align with rebind cap)
_search_max_results: ContextVar[int] = ContextVar("search_max_results", default=20)


def set_search_context(
    allowed_tools: list[str] | None = None,
    max_results: int = 20,
) -> None:
    """Set the search context for the current fiche.

    Call this before running concierge to configure search_tools behavior.

    Args:
        allowed_tools: Optional allowlist (supports wildcards like "github_*").
                      If None, all tools are allowed.
        max_results: Maximum results to return from search (should match MAX_TOOLS_FROM_SEARCH).
    """
    _search_allowed_tools.set(allowed_tools)
    _search_max_results.set(max_results)


def clear_search_context() -> None:
    """Clear the search context after concierge completes."""
    _search_allowed_tools.set(None)
    _search_max_results.set(20)


def _is_tool_allowed(name: str) -> bool:
    """Check if a tool is allowed by the current context's allowlist."""
    allowed = _search_allowed_tools.get()
    if allowed is None or len(allowed) == 0:
        return True  # No allowlist = all allowed

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

# Embedding model - text-embedding-3-small is fast and cheap
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536  # Default for text-embedding-3-small

# Cache file location (relative to backend root)
# Using data/ directory which is gitignored
EMBEDDING_CACHE_DIR = Path(__file__).parent.parent.parent / "data"
EMBEDDING_CACHE_FILE = EMBEDDING_CACHE_DIR / "tool_embeddings.npz"

# Singleton instance
_search_index: ToolSearchIndex | None = None


# ---------------------------------------------------------------------------
# Search Index
# ---------------------------------------------------------------------------


class ToolSearchIndex:
    """Semantic search over tool catalog using embeddings."""

    def __init__(self, catalog: tuple[ToolCatalogEntry, ...]):
        """Initialize search index with catalog.

        Args:
            catalog: Tool catalog entries to index.
        """
        self.catalog = catalog
        self.embeddings: np.ndarray | None = None
        self._client: AsyncOpenAI | None = None
        self._name_to_idx: dict[str, int] = {e.name: i for i, e in enumerate(catalog)}

    @property
    def client(self) -> AsyncOpenAI:
        """Lazy-initialize OpenAI client."""
        if self._client is None:
            self._client = AsyncOpenAI()
        return self._client

    async def build_index(self) -> None:
        """Generate embeddings for all tools.

        This is called once when the index is first created or when the
        cache is invalid. Embeddings are cached to disk.
        """
        logger.info(f"Building tool search index for {len(self.catalog)} tools")

        # Build text representations for embedding
        texts = []
        for entry in self.catalog:
            # Combine name, summary, and category for richer embedding
            text = f"{entry.name}: {entry.summary} (category: {entry.category})"
            texts.append(text)

        # Batch embed all texts
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
        """Find tools matching query by semantic similarity.

        Args:
            query: Natural language query describing desired functionality.
            top_k: Maximum number of results to return.
            min_score: Minimum similarity score (0-1) to include.

        Returns:
            List of (entry, score) tuples sorted by descending similarity.
        """
        if self.embeddings is None:
            raise RuntimeError("Index not built. Call build_index() first.")

        # Embed the query
        try:
            response = await self.client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=[query],
            )
            query_vec = np.array(response.data[0].embedding, dtype=np.float32)
        except Exception as e:
            logger.error(f"Failed to embed query: {e}")
            raise

        # Compute cosine similarity
        # Note: OpenAI embeddings are already normalized, so dot product = cosine
        similarities = self.embeddings @ query_vec

        # Get top-k indices
        if top_k >= len(similarities):
            top_indices = np.argsort(similarities)[::-1]
        else:
            # Use argpartition for efficiency with large catalogs
            top_indices = np.argpartition(similarities, -top_k)[-top_k:]
            top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

        # Build results
        results = []
        for idx in top_indices:
            score = float(similarities[idx])
            if score < min_score:
                continue
            results.append((self.catalog[idx], score))

        return results

    async def search_names(self, query: str, top_k: int = 5) -> list[str]:
        """Search and return just tool names.

        Convenience method for when you just need the names.
        """
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
    """Generate a hash of the catalog for cache validation.

    The hash changes if tools are added, removed, or descriptions change.
    """
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
    """Load embeddings from disk cache if valid.

    Returns None if cache is missing, stale, or corrupted.
    """
    if not EMBEDDING_CACHE_FILE.exists():
        logger.debug("Embeddings cache not found")
        return None

    try:
        data = np.load(EMBEDDING_CACHE_FILE, allow_pickle=False)

        # Validate cache
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
    """Get or build the tool search index.

    This is the main entry point for tool search. The index is cached
    in memory and embeddings are cached to disk.

    Args:
        force_rebuild: If True, rebuild index even if cache is valid.

    Returns:
        ToolSearchIndex ready for queries.
    """
    global _search_index

    # Return cached instance if valid
    if _search_index is not None and not force_rebuild:
        return _search_index

    # Build catalog
    catalog = build_catalog()
    catalog_hash = _get_catalog_hash(catalog)
    tool_names = [e.name for e in catalog]

    # Create index
    index = ToolSearchIndex(catalog)

    # Try to load from cache
    if not force_rebuild:
        cached_embeddings = _load_embeddings_cache(catalog_hash, tool_names)
        if cached_embeddings is not None:
            index.embeddings = cached_embeddings
            _search_index = index
            return index

    # Build fresh embeddings
    await index.build_index()

    # Save to cache
    if index.embeddings is not None:
        _save_embeddings_cache(index.embeddings, catalog_hash, tool_names)

    _search_index = index
    return index


def clear_search_index_cache() -> None:
    """Clear the in-memory search index cache."""
    global _search_index
    _search_index = None


def delete_embeddings_cache() -> bool:
    """Delete the disk embeddings cache.

    Returns:
        True if cache was deleted, False if it didn't exist.
    """
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
    """Search tools by description - designed for fiche use.

    This function is wrapped as a tool that fiches can call to discover
    available tools. Results are filtered by the current fiche's allowlist
    (set via set_search_context) and capped by the context's max_results.

    Args:
        query: What you want to do (e.g., "send a message to slack")
        max_results: Maximum number of results to return (may be further
                    capped by context's max_results setting).

    Returns:
        Dictionary with:
        - tools: List of {name, summary, category, relevance} matches
        - total_available: Total tools in registry
        - query: The search query
    """
    index = await get_tool_search_index()

    # Get context-based cap (aligned with MAX_TOOLS_FROM_SEARCH in concierge)
    context_cap = _search_max_results.get()
    effective_max = min(max_results, context_cap)

    # Search with extra headroom for filtering
    # Request more results than needed since some may be filtered out
    search_count = min(effective_max * 2, 20)  # Cap at 20 for perf
    results = await index.search(query, top_k=search_count)

    # Filter by allowlist and cap results
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
