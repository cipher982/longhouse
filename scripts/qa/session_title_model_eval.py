#!/usr/bin/env python3
"""Benchmark candidate OpenRouter models for first-message session titles.

Run from repo root with the server environment:
    cd server && uv run python ../scripts/qa/session_title_model_eval.py --rounds 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from openai import AsyncOpenAI  # noqa: E402

from zerg.models_config import ModelProvider  # noqa: E402
from zerg.models_config import build_openai_compatible_client_kwargs  # noqa: E402
from zerg.services.session_processing.summarize import safe_parse_json  # noqa: E402
from zerg.services.session_title import sanitize_title  # noqa: E402
from zerg.services.title_generator import INITIAL_SESSION_TITLE_SYSTEM_PROMPT  # noqa: E402
from zerg.services.title_generator import _build_initial_session_title_prompt  # noqa: E402


@dataclass(frozen=True)
class Candidate:
    name: str
    model: str
    extra_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class Sample:
    label: str
    message: str
    expected_any: tuple[str, ...]
    banned_any: tuple[str, ...] = ()


DEFAULT_CANDIDATES = [
    Candidate("gemini-3.1-flash-lite", "google/gemini-3.1-flash-lite"),
    Candidate("gemini-3.1-flash-lite-nitro", "google/gemini-3.1-flash-lite:nitro"),
    Candidate("gemini-3-flash-preview", "google/gemini-3-flash-preview"),
    Candidate("gemini-2.5-flash-lite", "google/gemini-2.5-flash-lite"),
    Candidate("qwen3.5-flash", "qwen/qwen3.5-flash-02-23"),
    Candidate("qwen3.6-flash", "qwen/qwen3.6-flash"),
    Candidate("llama-4-scout-nitro", "meta-llama/llama-4-scout:nitro"),
    Candidate("llama-3.1-8b-nitro", "meta-llama/llama-3.1-8b-instruct:nitro"),
    Candidate("mistral-small-3-nitro", "mistralai/mistral-small-24b-instruct-2501:nitro"),
    Candidate(
        "deepseek-v4-flash-throughput",
        "deepseek/deepseek-v4-flash",
        {"provider": {"sort": "throughput"}},
    ),
]


SAMPLES = [
    Sample(
        "menu row clickability",
        "there is no indication the rows are clickable, workshop modern practices for macos and swift "
        "and general UX, how to let users know if you see this row and click it will open",
        ("row", "click", "menu"),
        ("done", "shipped"),
    ),
    Sample(
        "laggy highlight",
        "moving my mouse up and down starts visibly lagging with the highlight against my cursor; "
        "also there is dead space between rows where nothing is highlighted. make it a sharp boundary",
        ("highlight", "lag", "row", "space", "boundary"),
    ),
    Sample(
        "title system",
        "What is a summary title or anchor title? I have no idea why we have a ladder. "
        "Why are we not just computing the title?",
        ("title", "anchor", "summary", "naming"),
        ("ladder",),
    ),
    Sample(
        "fast ai title",
        "use openrouter deepseek v4 flash to generate a short title from the initial user message, "
        "persist it into DB and use it in iOS and web",
        ("title", "generation", "ios", "web", "openrouter"),
    ),
    Sample(
        "pasted shipped recap",
        "Done and shipped. Pushed ebae901f5 to origin/main: Open menu bar sessions from row titles. "
        "What changed: Managed session rows now use timeline_title, then summary_title, then first user message.",
        ("menu", "row", "title", "session"),
        ("done", "shipped", "pushed"),
    ),
    Sample(
        "image rendering",
        "```text\n[Image #1]\nPHASE1 tool result image fix: inline data urls are not rendering in iOS session detail, "
        "figure out whether ingest strips them or the client drops the attachment payload, then patch and test.\n```",
        ("image", "render", "ios", "tool", "data"),
        ("image #1", "phase1"),
    ),
    Sample(
        "backup plan",
        "Can you review the backup recovery master plan and turn it into a small launch checklist? "
        "Keep it practical, no giant enterprise process.",
        ("backup", "recovery", "launch", "checklist"),
        ("enterprise",),
    ),
]


def _parse_title(raw: str) -> str | None:
    parsed = safe_parse_json(raw)
    if isinstance(parsed, dict) and isinstance(parsed.get("title"), str):
        return sanitize_title(parsed["title"], max_words=6)
    return sanitize_title(raw.strip().strip('"'), max_words=6)


def _quality_score(title: str | None, sample: Sample) -> tuple[int, list[str]]:
    if not title:
        return 0, ["empty"]
    lowered = title.lower()
    notes: list[str] = []
    score = 0
    matched = [term for term in sample.expected_any if term in lowered]
    if matched:
        score += min(3, len(matched))
    else:
        notes.append("missed expected terms")
    banned = [term for term in sample.banned_any if term in lowered]
    if banned:
        score -= 2
        notes.append("banned: " + ",".join(banned))
    words = title.split()
    if 2 <= len(words) <= 6:
        score += 1
    else:
        notes.append(f"word_count={len(words)}")
    if len(title) <= 48:
        score += 1
    else:
        notes.append(f"long={len(title)}")
    if title.endswith((".", "!", "?")):
        score -= 1
        notes.append("trailing punctuation")
    return score, notes


async def _call_candidate(
    client: AsyncOpenAI,
    candidate: Candidate,
    sample: Sample,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    user_prompt = _build_initial_session_title_prompt(
        sample.message,
        metadata={"project": "longhouse", "provider": "codex", "git_branch": "main"},
    )
    messages = [
        {"role": "system", "content": INITIAL_SESSION_TITLE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    kwargs: dict[str, Any] = {"model": candidate.model, "messages": messages}
    if candidate.extra_body:
        kwargs["extra_body"] = candidate.extra_body

    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=timeout_seconds)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        raw = response.choices[0].message.content if response.choices else ""
        title = _parse_title(raw or "")
        score, notes = _quality_score(title, sample)
        usage = getattr(response, "usage", None)
        return {
            "candidate": candidate.name,
            "model": candidate.model,
            "sample": sample.label,
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "title": title,
            "raw": raw,
            "score": score,
            "notes": notes,
            "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
            "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
            "routed_model": getattr(response, "model", None),
        }
    except Exception as exc:
        return {
            "candidate": candidate.name,
            "model": candidate.model,
            "sample": sample.label,
            "ok": False,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "score": 0,
        }


def _summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for candidate in sorted({row["candidate"] for row in rows}):
        candidate_rows = [row for row in rows if row["candidate"] == candidate]
        ok_rows = [row for row in candidate_rows if row["ok"]]
        latencies = [row["elapsed_ms"] for row in ok_rows]
        scores = [row["score"] for row in ok_rows]
        summaries.append(
            {
                "candidate": candidate,
                "model": candidate_rows[0]["model"],
                "ok": len(ok_rows),
                "errors": len(candidate_rows) - len(ok_rows),
                "median_ms": int(statistics.median(latencies)) if latencies else None,
                "p90_ms": (
                    int(statistics.quantiles(latencies, n=10)[8])
                    if len(latencies) >= 2
                    else (latencies[0] if latencies else None)
                ),
                "min_ms": min(latencies) if latencies else None,
                "max_ms": max(latencies) if latencies else None,
                "mean_score": round(statistics.mean(scores), 2) if scores else 0,
                "titles": [row.get("title") for row in ok_rows],
            }
        )
    return sorted(summaries, key=lambda row: (-(row["mean_score"]), row["median_ms"] or 999999))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--models", nargs="*", help="Optional model slugs to evaluate instead of defaults.")
    args = parser.parse_args()

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY is required", file=sys.stderr)
        return 2

    candidates = [Candidate(model, model) for model in args.models] if args.models else DEFAULT_CANDIDATES
    client = AsyncOpenAI(**build_openai_compatible_client_kwargs(provider=ModelProvider.OPENROUTER, api_key=api_key))
    rows: list[dict[str, Any]] = []
    try:
        for _round in range(max(1, args.rounds)):
            for candidate in candidates:
                for sample in SAMPLES:
                    row = await _call_candidate(client, candidate, sample, timeout_seconds=args.timeout)
                    row["round"] = _round + 1
                    rows.append(row)
                    if not args.json:
                        status = "OK" if row["ok"] else "ERR"
                        title = row.get("title") or row.get("error")
                        print(f"{status:3} {row['elapsed_ms']:5}ms {candidate.name:30} {sample.label:24} {title}")
    finally:
        await client.close()

    payload = {"summary": _summarize(rows), "rows": rows}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("\nSummary")
        for row in payload["summary"]:
            print(
                f"{row['candidate']:30} score={row['mean_score']:<4} "
                f"median={row['median_ms']}ms p90={row['p90_ms']}ms "
                f"ok={row['ok']} errors={row['errors']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
