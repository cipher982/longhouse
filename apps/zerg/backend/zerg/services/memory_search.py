"""Memory file search service (embeddings-first with keyword fallback)."""

from __future__ import annotations

from typing import List

from sqlalchemy.orm import Session

from zerg.crud import memory_crud
from zerg.models.models import MemoryFile
from zerg.services import memory_embeddings


def _extract_snippets(text: str, query: str, max_snippets: int = 3, context_chars: int = 150) -> List[str]:
    """Extract snippets containing the query from a text block."""
    snippets: List[str] = []
    query_lower = query.lower()
    text_lower = text.lower()

    start = 0
    while len(snippets) < max_snippets:
        pos = text_lower.find(query_lower, start)
        if pos == -1:
            break

        snippet_start = max(0, pos - context_chars)
        snippet_end = min(len(text), pos + len(query) + context_chars)
        snippet = text[snippet_start:snippet_end].strip()

        if snippet_start > 0:
            snippet = "..." + snippet
        if snippet_end < len(text):
            snippet = snippet + "..."

        snippets.append(snippet)
        start = pos + 1

    if not snippets:
        words = query_lower.split()
        lines = text.split("\n")
        for line in lines:
            if any(word in line.lower() for word in words):
                snippets.append(line.strip())
                if len(snippets) >= max_snippets:
                    break

    if not snippets:
        snippets.append(text[: context_chars * 2] + "...")

    return snippets


def _format_result(file: MemoryFile, *, score: float, query: str) -> dict:
    return {
        "path": file.path,
        "title": file.title,
        "tags": file.tags or [],
        "score": score,
        "snippets": _extract_snippets(file.content, query),
    }


def search_memory_files(
    db: Session,
    *,
    owner_id: int,
    query: str,
    tags: List[str] | None = None,
    limit: int = 5,
    use_embeddings: bool = True,
    query_embedding=None,
) -> List[dict]:
    """Search memory files using embeddings-first, with keyword fallback."""
    if use_embeddings:
        if query_embedding is None:
            query_embedding = memory_embeddings.embed_query(query)

        hits = memory_embeddings.search_memory_embeddings(
            db,
            owner_id=owner_id,
            query_embedding=query_embedding,
            limit=limit,
        )

        if hits:
            ids = [memory_id for memory_id, _score in hits]
            files = memory_crud.get_memory_files_by_ids(db, owner_id=owner_id, ids=ids)
            file_map = {f.id: f for f in files}
            results: List[dict] = []
            for memory_id, score in hits:
                file = file_map.get(memory_id)
                if not file:
                    continue
                results.append(_format_result(file, score=score, query=query))
            return results

    # Fallback: keyword search
    files = memory_crud.search_memory_files_keyword(
        db,
        owner_id=owner_id,
        query=query,
        tags=tags,
        limit=limit,
    )
    return [_format_result(file, score=1.0, query=query) for file in files]
