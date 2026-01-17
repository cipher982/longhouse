"""Auto summarizer for supervisor runs -> Memory Files."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
from datetime import datetime
from datetime import timezone
from typing import Any

from openai import AsyncOpenAI

from zerg.config import get_settings
from zerg.crud import memory_crud
from zerg.database import get_session_factory
from zerg.services import memory_embeddings

SUMMARY_SYSTEM_PROMPT = (
    "You are a summarizer that writes durable memory for a personal AI assistant. "
    "Produce concise JSON only. Keep it short and factual.\n\n"
    "Return JSON with keys:\n"
    "- title: 3-8 words, Title Case\n"
    "- topic: short topic phrase\n"
    "- outcome: one-sentence outcome\n"
    "- summary_bullets: 3-6 short bullets\n"
    "- tags: 3-6 lowercase tags (no spaces)\n"
)


def _safe_parse_json(text: str | None) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _extract_output_text(response: Any) -> str | None:
    # SDK object or dict
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    data = response
    if hasattr(response, "model_dump"):
        data = response.model_dump()
    if isinstance(data, dict):
        if isinstance(data.get("output_text"), str):
            return data["output_text"].strip()
        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        return text.strip()
    return None


def _slugify(text: str, max_length: int = 40) -> str:
    value = (text or "").lower().strip()
    value = re.sub(r"[^a-z0-9\\s-]", "", value)
    value = re.sub(r"[\\s_]+", "-", value).strip("-")
    if not value:
        return "summary"
    return value[:max_length]


def _default_title(task: str | None, result_text: str | None) -> str:
    base = (task or "").strip() or (result_text or "").strip()
    if not base:
        return "Run Summary"
    return " ".join(base.split())[:60]


async def _generate_summary(task: str, result_text: str) -> dict[str, Any] | None:
    settings = get_settings()
    if settings.testing or settings.llm_disabled or not settings.openai_api_key:
        return None

    model = os.getenv("JARVIS_MEMORY_SUMMARY_MODEL", "gpt-5-mini")
    reasoning_effort = os.getenv("JARVIS_MEMORY_SUMMARY_REASONING_EFFORT", "none")
    base_url = os.getenv("OPENAI_BASE_URL")

    client_kwargs = {"api_key": settings.openai_api_key}
    if base_url:
        client_kwargs["base_url"] = base_url

    client = AsyncOpenAI(**client_kwargs)

    user_prompt = f"Task:\\n{task}\\n\\nResult:\\n{result_text}\\n"

    response = await client.responses.create(
        model=model,
        reasoning={"effort": reasoning_effort},
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": SUMMARY_SYSTEM_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
    )

    output_text = _extract_output_text(response)
    return _safe_parse_json(output_text)


def _build_markdown(
    *,
    title: str,
    topic: str,
    outcome: str,
    summary_bullets: list[str],
    tags: list[str],
    run_id: int,
    thread_id: int,
    trace_id: str | None,
    created_at: datetime,
) -> str:
    date_str = created_at.strftime("%Y-%m-%d")
    lines = [
        f"# Episode: {title}",
        f"Date: {date_str}",
        f"Topic: {topic}",
        f"Outcome: {outcome}",
        f"Refs: thread_id={thread_id}, run_id={run_id}" + (f", trace_id={trace_id}" if trace_id else ""),
        f"Tags: {tags}",
        "",
        "Summary:",
    ]
    for bullet in summary_bullets:
        lines.append(f"- {bullet}")
    return "\n".join(lines)


async def persist_run_summary(
    *,
    owner_id: int,
    thread_id: int,
    run_id: int,
    task: str,
    result_text: str,
    trace_id: str | None = None,
) -> None:
    """Persist an episodic memory file for a completed run."""
    try:
        summary_data = await _generate_summary(task, result_text)
    except Exception:
        summary_data = None

    created_at = datetime.now(timezone.utc)
    date_str = created_at.strftime("%Y-%m-%d")

    if summary_data:
        title = summary_data.get("title") or _default_title(task, result_text)
        topic = summary_data.get("topic") or title
        outcome = summary_data.get("outcome") or ""
        summary_bullets = summary_data.get("summary_bullets") or []
        tags = summary_data.get("tags") or []
    else:
        title = _default_title(task, result_text)
        topic = title
        outcome = _truncate(result_text)
        summary_bullets = [_truncate(result_text)]
        tags = []

    # Normalize bullets + tags
    summary_bullets = [str(b).strip() for b in summary_bullets if str(b).strip()]
    if not summary_bullets:
        summary_bullets = [_truncate(result_text)]

    tags = [str(t).strip().lower().replace(" ", "-") for t in tags if str(t).strip()]

    content = _build_markdown(
        title=title,
        topic=topic,
        outcome=outcome or _truncate(result_text),
        summary_bullets=summary_bullets[:6],
        tags=tags,
        run_id=run_id,
        thread_id=thread_id,
        trace_id=trace_id,
        created_at=created_at,
    )

    slug = _slugify(title)
    path = f"episodes/{date_str}/{run_id}-{slug}.md"

    session_factory = get_session_factory()
    db = session_factory()
    try:
        row = memory_crud.upsert_memory_file(
            db,
            owner_id=owner_id,
            path=path,
            title=title,
            content=content,
            tags=tags,
            metadata={
                "run_id": run_id,
                "thread_id": thread_id,
                "trace_id": trace_id,
            },
        )

        # Best-effort embedding
        memory_embeddings.maybe_upsert_embedding(
            db,
            owner_id=owner_id,
            memory_file_id=row.id,
            content=row.content,
        )
    finally:
        db.close()


def schedule_run_summary(**kwargs: Any) -> None:
    """Schedule summary persistence without blocking caller."""
    settings = get_settings()
    if settings.testing or settings.llm_disabled:
        return

    asyncio.create_task(
        persist_run_summary(**kwargs),
        context=contextvars.Context(),
    )


def _truncate(text: str | None, max_chars: int = 220) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars].rstrip() + "…"
