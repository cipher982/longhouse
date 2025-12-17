"""Knowledge base tools for searching user-specific knowledge."""

import logging
from typing import List

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class KnowledgeSearchInput(BaseModel):
    """Input schema for knowledge_search tool."""

    query: str = Field(description="Search terms (keywords or phrases)")
    limit: int = Field(default=5, description="Maximum number of results to return", ge=1, le=20)


def knowledge_search(query: str, limit: int = 5) -> List[dict]:
    """Search user's knowledge base for relevant content.

    This tool searches across all knowledge sources (URLs, documents, repos) that
    the user has configured. Use this to look up infrastructure details, server
    information, project-specific facts, or any other user-specific context.

    Args:
        query: Search terms (keywords or phrases). Be specific to get relevant results.
        limit: Maximum number of results to return (1-20, default 5)

    Returns:
        List of matching snippets with source information. Each result contains:
        - source: Name of the knowledge source
        - path: Document path or URL
        - title: Document title (if available)
        - snippets: List of relevant text excerpts containing your query
        - score: Relevance score (higher is better)

    Example:
        >>> knowledge_search("prod-web server ip address")
        [
            {
                "source": "Infrastructure Docs",
                "path": "https://docs.example.com/servers.md",
                "title": "Server Overview",
                "snippets": ["prod-web (192.0.2.10) - Production web server"],
                "score": 0.95
            }
        ]
    """
    from zerg.context import get_worker_context
    from zerg.crud import knowledge_crud
    from zerg.database import db_session

    # Get current user from worker context (V1.1: fixed context resolution)
    ctx = get_worker_context()
    if ctx is None or ctx.owner_id is None:
        return [{
            "error": "No user context available. knowledge_search requires authenticated worker context."
        }]

    owner_id = ctx.owner_id

    # Search documents
    with db_session() as db:
        results = knowledge_crud.search_knowledge_documents(
            db,
            owner_id=owner_id,
            query=query,
            limit=limit,
        )

    if not results:
        return [{
            "message": f"No results found for '{query}' in your knowledge base."
        }]

    # Format results
    formatted_results = []
    for doc, source in results:
        # Extract snippets containing the query
        snippets = extract_snippets(doc.content_text, query, max_snippets=3)

        formatted_results.append({
            "source": source.name,
            "source_id": source.id,
            "document_id": doc.id,
            "path": doc.path,
            "title": doc.title,
            "snippets": snippets,
            "score": 1.0,  # Phase 0: no relevance scoring
        })

    return formatted_results


def extract_snippets(text: str, query: str, max_snippets: int = 3, context_chars: int = 150) -> List[str]:
    """Extract text snippets containing the query string.

    Args:
        text: Full document text
        query: Search query
        max_snippets: Maximum number of snippets to return
        context_chars: Number of characters of context on each side

    Returns:
        List of text excerpts containing the query
    """
    snippets = []
    query_lower = query.lower()
    text_lower = text.lower()

    # Find all occurrences of query
    start = 0
    while len(snippets) < max_snippets:
        pos = text_lower.find(query_lower, start)
        if pos == -1:
            break

        # Extract context around the match
        snippet_start = max(0, pos - context_chars)
        snippet_end = min(len(text), pos + len(query) + context_chars)

        snippet = text[snippet_start:snippet_end].strip()

        # Add ellipsis if not at start/end
        if snippet_start > 0:
            snippet = "..." + snippet
        if snippet_end < len(text):
            snippet = snippet + "..."

        snippets.append(snippet)
        start = pos + 1

    # If no exact matches, return first N lines containing any word from query
    if not snippets:
        words = query_lower.split()
        lines = text.split("\n")
        for line in lines:
            if any(word in line.lower() for word in words):
                snippets.append(line.strip())
                if len(snippets) >= max_snippets:
                    break

    # Fallback: return first N characters if still no snippets
    if not snippets:
        snippets.append(text[:context_chars * 2] + "...")

    return snippets


# Create LangChain tool
knowledge_search_tool = StructuredTool.from_function(
    func=knowledge_search,
    name="knowledge_search",
    description=(
        "Search the user's knowledge base for relevant content. "
        "Use this to look up infrastructure details, server information, "
        "project-specific facts, or any other user-specific context. "
        "When you encounter unfamiliar terms (server names, project names, etc.), "
        "search the knowledge base first before asking the user."
    ),
    args_schema=KnowledgeSearchInput,
)

# Export tools list for registry
TOOLS = [knowledge_search_tool]
