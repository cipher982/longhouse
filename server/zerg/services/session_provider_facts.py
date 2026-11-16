"""Project provider-authored facts onto the session surfaces clients render.

A provider writes turn durations, recaps and titles into its own transcript on
lines the render surface never shows. The engine parses them into typed facts
and ships them beside the raw bytes; catalogd keeps one row per source line.
This module turns those rows into the decorations the API serves: the
`turn_end` footer on the event a turn finished on, and the session's
`last_turn`.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from zerg.catalogd.client import CatalogClient

logger = logging.getLogger(__name__)

_LIST_TIMEOUT_SECONDS = 4.25
TURN_DURATION = "turn.duration"
TURN_END = "turn.end"
SESSION_RECAP = "session.recap"
SESSION_TITLE = "session.title"
TURN_USAGE = "turn.usage"
SESSION_COST = "session.cost"
SESSION_PERMISSION_MODE = "session.permission_mode"
SESSION_WORKTREE = "session.worktree"
DELEGATION_WAITING = "delegation.waiting"
DELEGATION_SCHEDULED = "delegation.scheduled"


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            numeric = float(raw)
        except ValueError:
            return None
        return numeric if math.isfinite(numeric) else None
    return None


def _parse_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def session_provider_facts(catalogd: CatalogClient, session_id: UUID | str) -> list[dict[str, Any]]:
    """Recent provider facts for a session, newest first, payload decoded."""
    try:
        result = await catalogd.call(
            "session.provider_facts.list.v2",
            {"session_id": str(session_id)},
            timeout_seconds=_LIST_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - a page without provider facts still renders
        logger.warning("Failed to list provider facts for session %s", session_id, exc_info=True)
        return []
    return provider_facts_from_rows(result.get("facts") if isinstance(result, dict) else None)


def provider_facts_from_rows(facts: object) -> list[dict[str, Any]]:
    """Catalog fact rows (from the list RPC or the session read) as the API projects them."""
    if not isinstance(facts, list):
        return []
    projected: list[dict[str, Any]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        payload = fact.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        at = _parse_at(fact.get("at"))
        if at is None or not isinstance(payload, dict):
            continue
        projected.append(
            {
                "kind": str(fact.get("kind") or ""),
                "at": at,
                "source_position": fact.get("source_position"),
                "payload": payload,
            }
        )
    return projected


def _event_time(event: dict[str, object]) -> datetime | None:
    return _parse_at(event.get("timestamp"))


def turn_ends_by_event(
    facts: list[dict[str, Any]], events: list[dict[str, object]], *, page_is_tail: bool = True
) -> dict[str, dict[str, Any]]:
    """Anchor each turn-ending fact (`turn.duration` / `turn.end`) to its turn's final reply.

    A turn is the run of non-user events after the last user event before the
    fact; the anchor is the newest of those at or before the fact's time.
    Claude writes `turn_duration` and `stop_hook_summary` (`turn.end`) after
    the turn's final assistant message and Codex's `task_complete` behaves the
    same, so the rule is provider-neutral.
    """
    ordered = sorted(
        (
            (time, str(event["event_id"]), event.get("role"))
            for event in events
            if (time := _event_time(event)) is not None and event.get("event_id")
        ),
        key=lambda item: item[0],
    )
    decorations: dict[str, dict[str, Any]] = {}
    relevant = sorted(
        (f for f in facts if f.get("kind") in {TURN_DURATION, TURN_END}),
        key=lambda f: (f["at"], 0 if f.get("kind") == TURN_DURATION else 1, int(f.get("source_position") or 0)),
    )
    for fact in relevant:
        before = [item for item in ordered if item[0] <= fact["at"]]
        if not before or (len(before) == len(ordered) and not page_is_tail):
            continue
        anchor = None
        for _time, event_id, role in reversed(before):
            if role == "user":
                break
            if role != "system":
                anchor = event_id
                break
        if anchor is None:
            continue
        payload = fact["payload"]
        kind = fact.get("kind")
        if kind == TURN_DURATION:
            duration_ms = payload.get("duration_ms")
            if not isinstance(duration_ms, int):
                continue
            message_count = payload.get("message_count")
            existing = decorations.get(anchor) or {}
            decorations[anchor] = {
                "duration_ms": duration_ms,
                "ended_at": fact["at"].isoformat(),
                "message_count": message_count if isinstance(message_count, int) else existing.get("message_count"),
                "outcome": existing["outcome"] if existing.get("outcome") in {"aborted", "interrupted"} else _turn_outcome(payload),
                **({"stop_hook_continued": existing["stop_hook_continued"]} if "stop_hook_continued" in existing else {}),
                **({"stop_hook_summary": existing["stop_hook_summary"]} if "stop_hook_summary" in existing else {}),
                **({"source": existing["source"]} if "source" in existing else {}),
            }
        else:
            existing = dict(decorations.get(anchor) or {})
            duration_val = payload.get("duration_ms") if isinstance(payload.get("duration_ms"), int) else existing.get("duration_ms", 0)
            msg_count = payload.get("message_count") if isinstance(payload.get("message_count"), int) else existing.get("message_count")
            entry: dict[str, Any] = {
                "duration_ms": int(duration_val),
                "ended_at": existing.get("ended_at") or fact["at"].isoformat(),
                "message_count": msg_count,
                "outcome": _turn_outcome(payload),
            }
            if isinstance(payload.get("stop_hook_continued"), bool):
                entry["stop_hook_continued"] = payload["stop_hook_continued"]
            elif "stop_hook_continued" in existing:
                entry["stop_hook_continued"] = existing["stop_hook_continued"]
            summary = payload.get("stop_hook_summary")
            if isinstance(summary, str) and summary.strip():
                entry["stop_hook_summary"] = summary.strip()
            elif "stop_hook_summary" in existing:
                entry["stop_hook_summary"] = existing["stop_hook_summary"]
            source = payload.get("source")
            if isinstance(source, str) and source.strip():
                entry["source"] = source.strip()
            elif "source" in existing:
                entry["source"] = existing["source"]
            decorations[anchor] = entry
    return decorations


def _turn_outcome(payload: Mapping[str, Any]) -> str:
    """Codex and Claude close completed, aborted, or interrupted turns."""
    raw = str(payload.get("outcome") or "").strip().lower()
    if raw in {"aborted", "interrupted"}:
        return raw
    return "completed"


def last_turn(facts: list[dict[str, Any]], decorations: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The newest finished turn, with its anchor event when it is on the page."""
    latest_duration = None
    previous_duration_at = None
    latest_end = None
    for fact in facts:
        kind = fact.get("kind")
        if kind == TURN_DURATION and isinstance(fact["payload"].get("duration_ms"), int):
            if latest_duration is None or fact["at"] > latest_duration["at"]:
                if latest_duration is not None:
                    previous_duration_at = latest_duration["at"]
                latest_duration = fact
            elif previous_duration_at is None or fact["at"] > previous_duration_at:
                previous_duration_at = fact["at"]
        elif kind == TURN_END:
            if latest_end is None or (fact["at"], int(fact.get("source_position") or 0)) > (
                latest_end["at"],
                int(latest_end.get("source_position") or 0),
            ):
                latest_end = fact
    if latest_duration is None and latest_end is None:
        return None
    if latest_duration is not None and (latest_end is None or latest_duration["at"] >= latest_end["at"]):
        ended_at = latest_duration["at"].isoformat()
        event_id = next((eid for eid, turn in decorations.items() if turn.get("ended_at") == ended_at), None)
        anchored = decorations.get(event_id, {}) if event_id is not None else {}
        outcome = anchored.get("outcome")
        if outcome is None:
            # No rows to anchor on (a header-only workspace, or the turn is
            # off the page): an end fact newer than the previous turn's
            # duration closed this same turn, and it carries the outcome.
            outcome = _turn_outcome(latest_duration["payload"])
            if latest_end is not None and (previous_duration_at is None or latest_end["at"] > previous_duration_at):
                end_outcome = _turn_outcome(latest_end["payload"])
                if end_outcome != "completed":
                    outcome = end_outcome
        return {
            "duration_ms": latest_duration["payload"]["duration_ms"],
            "ended_at": ended_at,
            "event_id": event_id,
            "outcome": outcome,
        }
    assert latest_end is not None
    ended_at = latest_end["at"].isoformat()
    event_id = next((eid for eid, turn in decorations.items() if turn.get("ended_at") == ended_at), None)
    payload = latest_end["payload"]
    return {
        "duration_ms": int(payload["duration_ms"]) if isinstance(payload.get("duration_ms"), int) else 0,
        "ended_at": ended_at,
        "event_id": event_id,
        "outcome": _turn_outcome(payload),
    }


def recap(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The provider's newest away recap; never synthesised."""
    latest = None
    for fact in facts:
        if fact.get("kind") != SESSION_RECAP:
            continue
        text = fact["payload"].get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if latest is None or fact["at"] > latest["at"]:
            latest = fact
    if latest is None:
        return None
    return {"text": latest["payload"]["text"], "at": latest["at"].isoformat()}


def usage_latest(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest turn-ending usage: what the next prompt starts from."""
    latest = None
    for fact in facts:
        if fact.get("kind") != TURN_USAGE or type(fact["payload"].get("output_tokens")) is not int:
            continue
        if latest is None or fact["at"] > latest["at"]:
            latest = fact
    if latest is None:
        return None
    payload = latest["payload"]
    # The engine names the provider's own context accounting; facts written
    # before it did carry only Claude's input classes, whose sum is the same number.
    if type(payload.get("context_tokens")) is int:
        context_tokens = int(payload["context_tokens"])
    else:
        context_tokens = sum(
            int(payload[key])
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            if type(payload.get(key)) is int
        )
    model = payload.get("model") if isinstance(payload.get("model"), str) else None
    effort = payload.get("effort") if isinstance(payload.get("effort"), str) else None
    context_window = int(payload["context_window"]) if type(payload.get("context_window")) is int else None
    context_window_source = "native" if context_window is not None else None
    return {
        "model": model,
        "effort": effort,
        "context_tokens": context_tokens,
        "context_window": context_window,
        "context_window_source": context_window_source,
        "output_tokens": int(payload["output_tokens"]),
        "thinking_tokens": int(payload["thinking_tokens"]) if type(payload.get("thinking_tokens")) is int else None,
        "at": latest["at"].isoformat(),
        "label": usage_label(model, effort, context_tokens, context_window),
    }


def short_model_name(raw: str | None) -> str | None:
    """'claude-fable-5-1' → 'fable 5.1', 'deepseek/deepseek-v4.1-flash' → 'deepseek v4.1 flash'.

    Vendor and router paths are chrome: the provider glyph already says whose
    session it is. A dash between two digits is a version separator.
    """
    name = (raw or "").strip().lower().rsplit("/", 1)[-1]
    name = name.removeprefix("claude-")
    if not name:
        return None
    out = []
    for index, char in enumerate(name):
        if char != "-":
            out.append(char)
            continue
        previous = name[index - 1] if index > 0 else " "
        following = name[index + 1] if index + 1 < len(name) else " "
        out.append("." if previous.isdigit() and following.isdigit() else " ")
    return "".join(out)


def compact_tokens(tokens: int) -> str:
    """501447 → '501k', 900 → '900', 1_260_000 → '1.3M', 999_500 → '1.0M'."""
    thousands = int(tokens / 1_000 + 0.5)
    if thousands >= 1_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{thousands}k"
    return str(tokens)


def usage_label(model: str | None, effort: str | None, context_tokens: int, context_window: int | None) -> str:
    """The one model/context line every client shows: 'opus 5 · high · 501k ctx'.

    With a provider-reported window it reads '25k/258k ctx'. Clients render it
    verbatim so web and iOS cannot drift.
    """
    parts = [part for part in (short_model_name(model), (effort or "").strip()) if part]
    context = compact_tokens(context_tokens)
    if context_window:
        context = f"{context}/{compact_tokens(context_window)}"
    parts.append(f"{context} ctx")
    return " · ".join(parts)


def provider_titles(facts: list[dict[str, Any]]) -> list[str]:
    """Provider titles oldest first, so the first one is the one to freeze."""
    titled = [
        (int(fact.get("source_position") or 0), fact["payload"]["title"])
        for fact in facts
        if fact.get("kind") == SESSION_TITLE and isinstance(fact["payload"].get("title"), str) and fact["payload"]["title"].strip()
    ]
    titled.sort(key=lambda item: item[0])
    return [title for _, title in titled]


def _newest_fact(facts: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    latest = None
    for fact in facts:
        if fact.get("kind") != kind:
            continue
        if latest is None or (
            (fact["at"], int(fact.get("source_position") or 0)) > (latest["at"], int(latest.get("source_position") or 0))
        ):
            latest = fact
    return latest


def project_session_hud(
    provider_facts: object = None,
    runtime_meta: Mapping[str, Any] | None = None,
    *,
    delegation: Any = None,
    fallback_model: str | None = None,
    base_hud: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine durable provider facts with live runtime metadata for session HUD surfaces."""
    norm_facts = provider_facts_from_rows(provider_facts)
    meta = dict(runtime_meta) if isinstance(runtime_meta, Mapping) else {}
    base = dict(base_hud) if isinstance(base_hud, Mapping) else {}

    usage_d = usage_latest(norm_facts)
    cost_fact = _newest_fact(norm_facts, SESSION_COST)
    cost_payload = cost_fact["payload"] if cost_fact is not None else {}

    rt_usage = meta.get("session_usage") if isinstance(meta.get("session_usage"), Mapping) else None
    rt_usage_at = _parse_at(meta.get("session_usage_at") or meta.get("observed_at"))
    base_usage = base.get("usage_latest") if isinstance(base.get("usage_latest"), Mapping) else None

    merged_usage: dict[str, Any] | None = None
    if usage_d is not None or rt_usage is not None or base_usage is not None:
        durable_at = _parse_at(usage_d["at"]) if usage_d is not None else None
        base_at = _parse_at(base_usage.get("at")) if base_usage is not None else None
        rt_ctx = rt_usage.get("context") if isinstance(rt_usage, Mapping) and isinstance(rt_usage.get("context"), Mapping) else {}

        rt_is_newer = False
        if rt_usage is not None:
            rt_is_newer = durable_at is None or (rt_usage_at is not None and rt_usage_at > durable_at)
        elif base_usage is not None and (durable_at is None or (base_at is not None and base_at > durable_at)):
            rt_is_newer = True

        model = (usage_d.get("model") if usage_d else None) or (base_usage.get("model") if base_usage else None) or fallback_model
        effort = (usage_d.get("effort") if usage_d else None) or (base_usage.get("effort") if base_usage else None)

        base_window_raw = (
            int(base_usage["context_window"]) if base_usage is not None and type(base_usage.get("context_window")) is int else None
        )
        base_window_source = base_usage.get("context_window_source") if base_usage is not None else None
        base_native_window = base_window_raw if base_window_source != "derived" else None
        base_derived_window = base_window_raw if base_window_source == "derived" else None

        if rt_usage is not None and rt_is_newer:
            context_tokens = int(
                rt_ctx["tokens"] if type(rt_ctx.get("tokens")) is int else (usage_d["context_tokens"] if usage_d is not None else 0)
            )
            context_window = (
                int(rt_ctx["window"]) if type(rt_ctx.get("window")) is int else (usage_d.get("context_window") if usage_d else None)
            )
            at_dt = rt_usage_at or durable_at or datetime.now(UTC)
        elif rt_usage is None and base_usage is not None and rt_is_newer:
            context_tokens = int(base_usage.get("context_tokens") or 0)
            context_window = base_native_window if base_native_window is not None else (usage_d.get("context_window") if usage_d else None)
            at_dt = base_at or durable_at or datetime.now(UTC)
        elif usage_d is not None:
            context_tokens = int(usage_d["context_tokens"])
            context_window = usage_d.get("context_window")
            if context_window is None and type(rt_ctx.get("window")) is int:
                context_window = int(rt_ctx["window"])
            elif context_window is None and base_native_window is not None:
                context_window = base_native_window
            at_dt = durable_at or rt_usage_at or datetime.now(UTC)
        else:
            context_tokens = (
                int(base_usage["context_tokens"]) if base_usage is not None and type(base_usage.get("context_tokens")) is int else 0
            )
            context_window = base_native_window
            at_dt = base_at or datetime.now(UTC)

        output_tokens = (
            int(usage_d["output_tokens"])
            if usage_d is not None
            else (int(base_usage["output_tokens"]) if base_usage is not None and type(base_usage.get("output_tokens")) is int else 0)
        )
        thinking_tokens = (
            usage_d.get("thinking_tokens")
            if usage_d is not None
            else (base_usage.get("thinking_tokens") if base_usage is not None else None)
        )
        context_window_source: str | None = "native" if context_window is not None else None
        if context_window is None:
            from zerg.services.session_state_contract import infer_context_window

            derived_window = infer_context_window(
                base.get("provider") if isinstance(base.get("provider"), str) else None,
                model,
                context_tokens,
            )
            if derived_window is not None:
                context_window = derived_window
                context_window_source = "derived"
            elif base_derived_window is not None:
                context_window = base_derived_window
                context_window_source = "derived"
        rt_pct = _as_float(rt_ctx.get("percent"))
        base_pct = _as_float(base_usage.get("used_percent")) if base_usage is not None else None
        used_percent: float | None = None
        if rt_is_newer and rt_pct is not None:
            used_percent = rt_pct
        elif base_pct is not None and rt_is_newer:
            used_percent = base_pct
        elif rt_pct is not None and context_tokens == rt_ctx.get("tokens"):
            used_percent = rt_pct
        elif context_window and context_window > 0:
            used_percent = round((context_tokens / context_window) * 100.0, 1)

        cost_usd: float | None = None
        for candidate in (
            rt_usage.get("cost_usd") if isinstance(rt_usage, Mapping) else None,
            cost_payload.get("cost_usd"),
            cost_payload.get("total_cost_usd"),
            base_usage.get("cost_usd") if isinstance(base_usage, Mapping) else None,
        ):
            parsed_cost = _as_float(candidate)
            if parsed_cost is not None:
                cost_usd = parsed_cost
                break

        cumulative_duration_ms: int | None = None
        for candidate in (
            cost_payload.get("cumulative_duration_ms"),
            cost_payload.get("total_duration_ms"),
            rt_usage.get("cumulative_duration_ms") if isinstance(rt_usage, Mapping) else None,
            base_usage.get("cumulative_duration_ms") if isinstance(base_usage, Mapping) else None,
        ):
            if type(candidate) is int and candidate >= 0:
                cumulative_duration_ms = candidate
                break
        if (
            cumulative_duration_ms is None
            and isinstance(rt_usage, Mapping)
            and type(rt_usage.get("started_at")) is int
            and rt_usage_at is not None
        ):
            elapsed = int(round(rt_usage_at.timestamp() * 1000)) - int(rt_usage["started_at"])
            if elapsed >= 0:
                cumulative_duration_ms = elapsed

        rate_limits = None
        raw_rate_limits = (
            rt_usage.get("rate_limits")
            if isinstance(rt_usage, Mapping) and isinstance(rt_usage.get("rate_limits"), list)
            else (
                base_usage.get("rate_limits")
                if isinstance(base_usage, Mapping) and isinstance(base_usage.get("rate_limits"), list)
                else None
            )
        )
        if isinstance(raw_rate_limits, list):
            rate_limits = []
            for entry in raw_rate_limits:
                if not isinstance(entry, Mapping):
                    continue
                normalized_entry = dict(entry)
                if "percent_used" in normalized_entry:
                    parsed_pct = _as_float(normalized_entry.get("percent_used"))
                    if parsed_pct is None:
                        normalized_entry.pop("percent_used", None)
                    else:
                        normalized_entry["percent_used"] = parsed_pct
                rate_limits.append(normalized_entry)

        merged_usage = {
            "model": model,
            "effort": effort,
            "context_tokens": context_tokens,
            "context_window": context_window,
            "context_window_source": context_window_source,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
            "used_percent": used_percent,
            "cost_usd": cost_usd,
            "cumulative_duration_ms": cumulative_duration_ms,
            "rate_limits": rate_limits,
            "at": at_dt.isoformat(),
            "label": usage_label(model, effort, context_tokens, context_window),
        }

    rt_mode = str(meta.get("permission_mode") or "").strip() or None
    rt_mode_at = _parse_at(meta.get("permission_mode_at") or meta.get("observed_at"))
    fact_mode_row = _newest_fact(norm_facts, SESSION_PERMISSION_MODE)
    fact_mode = str(fact_mode_row["payload"].get("permission_mode") or "").strip() or None if fact_mode_row else None
    permission_mode = fact_mode
    if rt_mode is not None and (fact_mode_row is None or (rt_mode_at is not None and rt_mode_at >= fact_mode_row["at"])):
        permission_mode = rt_mode
    if permission_mode is None and isinstance(base.get("permission_mode"), str) and base["permission_mode"].strip():
        permission_mode = base["permission_mode"].strip()

    titles = provider_titles(norm_facts)
    provider_title = titles[-1].strip() if titles else (base.get("provider_title") if isinstance(base.get("provider_title"), str) else None)

    wt_fact = _newest_fact(norm_facts, SESSION_WORKTREE)
    worktree: dict[str, Any] | None = None
    if wt_fact is not None:
        wt_payload = wt_fact["payload"]
        if wt_payload.get("active") is not False:
            candidate_wt: dict[str, Any] = {}
            for canonical_key, aliases in (
                ("name", ("name", "worktree_name")),
                ("branch", ("branch", "worktree_branch")),
                ("path", ("path", "worktree_path")),
                ("original_cwd", ("original_cwd",)),
                ("original_branch", ("original_branch",)),
            ):
                for alias in aliases:
                    val = wt_payload.get(alias)
                    if isinstance(val, str) and val.strip():
                        candidate_wt[canonical_key] = val.strip()
                        break
            if candidate_wt:
                worktree = candidate_wt
    elif isinstance(base.get("worktree"), Mapping):
        worktree = dict(base["worktree"])

    session_crons: list[dict[str, Any]] = []
    if isinstance(meta.get("session_crons"), list):
        session_crons = [dict(c) for c in meta["session_crons"] if isinstance(c, Mapping)]
    elif isinstance(base.get("session_crons"), list):
        session_crons = [dict(c) for c in base["session_crons"] if isinstance(c, Mapping)]
    if not session_crons:
        crons_by_id: dict[str, dict[str, Any]] = {}
        scheduled_facts = sorted(
            (fact for fact in norm_facts if fact.get("kind") == DELEGATION_SCHEDULED),
            key=lambda fact: (fact["at"], int(fact.get("source_position") or 0)),
        )
        for fact in scheduled_facts:
            payload = fact["payload"]
            cron_id = str(payload.get("id") or payload.get("task_id") or "").strip()
            if not cron_id:
                continue
            action = str(payload.get("action") or "create").strip().lower()
            if action in {"delete", "cancel", "remove"}:
                crons_by_id.pop(cron_id, None)
                continue
            schedule = str(payload.get("schedule") or payload.get("cron") or "").strip()
            if not schedule and cron_id in crons_by_id:
                schedule = str(crons_by_id[cron_id].get("schedule") or "")
            recurring = (
                payload["recurring"]
                if isinstance(payload.get("recurring"), bool)
                else (str(payload.get("cron_kind") or "").strip().lower() == "recurring")
            )
            entry: dict[str, Any] = {
                "id": cron_id,
                "schedule": schedule or "scheduled",
                "recurring": recurring,
            }
            prompt = payload.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                entry["prompt"] = prompt.strip()
            crons_by_id[cron_id] = entry
        session_crons = list(crons_by_id.values())

    pending_background_agent_count = 0
    delegation_state = getattr(delegation, "state", None)
    delegation_items = getattr(delegation, "items", None)
    delegation_kinds = getattr(delegation, "kinds", None)
    if delegation_state == "pending":
        if isinstance(delegation_items, list):
            pending_background_agent_count = sum(1 for item in delegation_items if getattr(item, "kind", None) in {"subagent", "teammate"})
        elif isinstance(delegation_kinds, Mapping):
            pending_background_agent_count = int(delegation_kinds.get("subagent") or 0) + int(delegation_kinds.get("teammate") or 0)
    elif delegation_state == "unknown":
        waiting_fact = _newest_fact(norm_facts, DELEGATION_WAITING)
        latest_duration = _newest_fact(norm_facts, TURN_DURATION)
        waiting_count = None
        if waiting_fact is not None:
            raw_count = waiting_fact["payload"].get("count")
            raw_pending = waiting_fact["payload"].get("pending_background_agents")
            waiting_count = raw_count if type(raw_count) is int else (raw_pending if type(raw_pending) is int else None)
        if waiting_fact is not None and waiting_count is not None:
            if latest_duration is None or waiting_fact["at"] >= latest_duration["at"]:
                pending_background_agent_count = max(0, waiting_count)
        elif type(base.get("pending_background_agent_count")) is int:
            pending_background_agent_count = max(0, int(base["pending_background_agent_count"]))

    return {
        "usage_latest": merged_usage,
        "permission_mode": permission_mode,
        "provider_title": provider_title,
        "worktree": worktree,
        "session_crons": session_crons,
        "pending_background_agent_count": pending_background_agent_count,
    }


__all__ = [
    "DELEGATION_WAITING",
    "SESSION_COST",
    "SESSION_PERMISSION_MODE",
    "SESSION_RECAP",
    "SESSION_TITLE",
    "SESSION_WORKTREE",
    "TURN_DURATION",
    "TURN_USAGE",
    "last_turn",
    "project_session_hud",
    "provider_facts_from_rows",
    "provider_titles",
    "recap",
    "session_provider_facts",
    "turn_ends_by_event",
    "usage_label",
    "usage_latest",
]
