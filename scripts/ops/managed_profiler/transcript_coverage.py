#!/usr/bin/env python3
"""Measure whether provider-native session content reached the served projection.

This is the coverage half of the provider fidelity harness
(``control-plane/docs/specs/provider-fidelity-harness.md``). Existing SLA
metrics measure how fast content that arrived got painted; they cannot see a
session that stopped propagating, because nothing asserts completeness.

The join is deterministic and needs no screenshot or model: the provider
transcript is the record of what the terminal showed, the served projection is
the record of what Longhouse published, and both sides key events by the
provider-native identity the engine itself derived (record id + part suffix for
prose and reasoning, tool call id for tool events).

Usage (live):
    scripts/ops/managed_profiler/transcript_coverage.py \\
        --provider omp --session <uuid> --transcript <native jsonl> \\
        --token-file ~/.longhouse/machine/device-token --provider-alive

Usage (offline, from a saved events payload):
    scripts/ops/managed_profiler/transcript_coverage.py \\
        --provider omp --transcript <native jsonl> --served-events events.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable

SCHEMA_VERSION = 1
# The hosted runtime sits behind an edge proxy that rejects the default
# python-urllib agent, so identify this client explicitly.
USER_AGENT = "longhouse-provider-fidelity-coverage/1"

EVENT_CLASSES = ("user", "assistant_text", "thinking", "tool_call", "tool_result", "media")

DEFAULT_STALL_AGE_MS = 60_000
# A class below this share of its provider records is a real gap, not an
# in-flight tail. Sampling and the last few seconds of a live turn always leave
# a handful of records that have not landed yet.
DEFAULT_COVERAGE_FLOOR = 0.98
DEFAULT_MISSING_SAMPLE = 5
EVENTS_PAGE_LIMIT = 1000
MAX_EVENT_PAGES = 20

# Verdict severities, mirroring the propagation profiler's report vocabulary.
VERDICT_ORDER = ("pass", "partial", "missing", "stalled", "empty")
PROVIDER_ALIASES = {"all": "all"}


# --------------------------------------------------------------------------
# Provider transcripts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeEvent:
    """One thing the terminal showed, keyed by provider-native identity."""

    event_class: str
    key: str
    timestamp_ms: int | None
    chars: int


def _parse_timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


SESSIONS_DIR_NAME = "sessions"
BLOBS_DIR_NAME = "blobs"
SHA256_HEX_CHARS = 64
BLOB_REF_PREFIX = "blob:sha256:"


def provider_blob_root(transcript: Path) -> Path | None:
    """The provider's content-addressed image store, beside its transcripts.

    Mirrors where Pi and OMP write pasted images; the oracle needs the same
    location rule the engine uses, but not the engine's parsing.
    """
    for parent in transcript.parents:
        if parent.name == SESSIONS_DIR_NAME and parent.parent.name:
            return parent.parent / BLOBS_DIR_NAME
    return None


def media_event(digest: str, timestamp_ms: int | None, byte_size: int = 0) -> NativeEvent:
    """One image the provider's terminal showed, keyed by the bytes it is.

    An image has no provider event identity of its own, so the only honest join
    between what the provider showed and what Longhouse served is the content
    hash both sides can compute independently.
    """
    return NativeEvent("media", digest, timestamp_ms, byte_size)


def pi_image_digest(part: dict[str, Any], blob_root: Path | None) -> str | None:
    """The digest of a Pi/OMP image part, verified against the bytes on disk.

    The reference states a digest; hashing the file it names is what makes this
    an oracle rather than a restatement of the parser's claim.
    """
    data = part.get("data")
    if not isinstance(data, str) or not data.startswith(BLOB_REF_PREFIX):
        return None
    declared = data[len(BLOB_REF_PREFIX) :].strip().lower()
    if len(declared) != SHA256_HEX_CHARS:
        return None
    if blob_root is not None:
        candidate = blob_root / declared
        if candidate.is_file():
            return hashlib.sha256(candidate.read_bytes()).hexdigest()
    return declared


def _content_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [part for part in content if isinstance(part, dict)]


def _text_chars(part: dict[str, Any], field: str) -> int:
    value = part.get(field)
    return len(value) if isinstance(value, str) else 0


def extract_omp_transcript(path: Path) -> list[NativeEvent]:
    """Extract native events from an OMP (Pi-family) session JSONL.

    Keys mirror the engine parser exactly so a served event can be matched
    without guessing: ``{record_id}`` for the first text block of a record,
    ``{record_id}-text-{index}`` / ``{record_id}-thinking-{index}`` for later
    blocks, and the provider tool call id for tool events.
    """

    events: list[NativeEvent] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "message":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            record_id = str(record.get("id") or "")
            if not record_id:
                continue
            timestamp_ms = _parse_timestamp_ms(record.get("timestamp"))
            role = message.get("role")

            if role == "toolResult":
                key = message.get("toolCallId")
                key = str(key) if isinstance(key, str) and key.strip() else record_id
                chars = sum(_text_chars(part, "text") for part in _content_parts(message))
                events.append(NativeEvent("tool_result", key, timestamp_ms, chars))
                continue

            if role not in {"user", "assistant"}:
                continue

            for index, part in enumerate(_content_parts(message)):
                kind = part.get("type")
                if kind == "text":
                    text = part.get("text")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    suffix = "" if index == 0 else f"text-{index}"
                    key = record_id if not suffix else f"{record_id}-{suffix}"
                    event_class = "user" if role == "user" else "assistant_text"
                    events.append(NativeEvent(event_class, key, timestamp_ms, len(text)))
                elif kind == "thinking":
                    text = part.get("thinking")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    # One reasoning part per record is the provider's shape, and
                    # the engine keys that row by the bare record id. Later parts
                    # of the same record carry an explicit suffix.
                    key = record_id if index == 0 else f"{record_id}-thinking-{index}"
                    events.append(NativeEvent("thinking", key, timestamp_ms, len(text)))
                elif kind == "image":
                    digest = pi_image_digest(part, provider_blob_root(path))
                    if digest:
                        events.append(media_event(digest, timestamp_ms))
                elif kind == "toolCall":
                    call_id = part.get("id")
                    key = str(call_id) if isinstance(call_id, str) and call_id.strip() else f"{record_id}-tool-{index}"
                    events.append(NativeEvent("tool_call", key, timestamp_ms, 0))
    return events


def _walk_images(node: Any, found: list[tuple[str, int]]) -> None:
    """Every base64 image anywhere in a record, whatever shape wraps it.

    Claude carries a pasted image directly under ``message.content``, but a
    screenshot produced by a tool sits nested inside that block's own
    ``content``, mirrored again under the record's ``toolUseResult``, and an
    ``attachment`` record keeps one under ``prompt``. An oracle that walked only
    the first shape would report a fraction of what the terminal showed.
    """

    if isinstance(node, dict):
        if node.get("type") == "image":
            source = node.get("source")
            if isinstance(source, dict) and source.get("type") == "base64":
                encoded = source.get("data")
                if isinstance(encoded, str) and encoded:
                    try:
                        raw = base64.b64decode(encoded, validate=False)
                    except (ValueError, TypeError):
                        raw = b""
                    if raw:
                        found.append((hashlib.sha256(raw).hexdigest(), len(raw)))
        for value in node.values():
            _walk_images(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_images(value, found)


def extract_claude_transcript(path: Path) -> list[NativeEvent]:
    """Extract the images a Claude transcript carries.

    Claude nests a pasted image one level deeper than the Pi family
    (``message.content[].source = {type: base64, media_type, data}``) and never
    writes a ``data:`` URL, which is exactly the shape a generic data-URL scan
    misses. Only media is extracted here: an image's identity is the bytes it is,
    so this oracle needs no agreement about event-id schemes, and a wrong guess
    about those could not produce a false alarm in another class.

    The same bytes appear more than once in one record (a tool result is mirrored
    under ``toolUseResult``), and the content hash makes that a single fact.
    """

    seen: set[str] = set()
    events: list[NativeEvent] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            timestamp_ms = _parse_timestamp_ms(record.get("timestamp"))
            found: list[tuple[str, int]] = []
            _walk_images(record, found)
            for digest, byte_size in found:
                if digest in seen:
                    continue
                seen.add(digest)
                events.append(media_event(digest, timestamp_ms, byte_size))
    return events


EXTRACTORS: dict[str, Callable[[Path], list[NativeEvent]]] = {
    "omp": extract_omp_transcript,
    "claude": extract_claude_transcript,
}


def extract_native_events(provider: str, path: Path) -> list[NativeEvent]:
    extractor = EXTRACTORS.get(provider.strip().lower())
    if extractor is None:
        raise SystemExit(f"no transcript extractor for provider {provider!r}; implemented: {', '.join(sorted(EXTRACTORS))}")
    return extractor(path)


# --------------------------------------------------------------------------
# Served projection
# --------------------------------------------------------------------------


def classify_served_event(event: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(event_class, key)`` for a served event, or None if unmapped."""

    event_id = event.get("id")
    event_id = str(event_id) if event_id is not None else ""
    role = str(event.get("role") or "")
    tool_call_id = event.get("tool_call_id")
    tool_name = event.get("tool_name")

    if role == "tool":
        key = str(tool_call_id) if tool_call_id else event_id
        return ("tool_result", key) if key else None
    if role == "assistant" and tool_name:
        key = str(tool_call_id) if tool_call_id else event_id
        return ("tool_call", key) if key else None
    if role == "assistant":
        return ("assistant_text", event_id) if event_id else None
    if role == "system":
        return ("thinking", event_id) if event_id else None
    if role == "user":
        return ("user", event_id) if event_id else None
    return None


def media_keys_of_served_event(event: dict[str, Any]) -> list[str]:
    """Content hashes this event shows, however many it carries."""

    refs = event.get("media_refs")
    if not isinstance(refs, list):
        return []
    keys: list[str] = []
    for ref in refs:
        if isinstance(ref, dict):
            sha = ref.get("sha256")
            if isinstance(sha, str) and sha.strip():
                keys.append(sha.strip().lower())
    return keys


def served_events_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        events = payload.get("events")
        if isinstance(events, list):
            return [event for event in events if isinstance(event, dict)]
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    return []


def fetch_served_events(*, api_url: str, session_id: str, token: str, timeout: float = 30.0) -> list[dict[str, Any]]:
    base = api_url.rstrip("/")
    events: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_EVENT_PAGES):
        params = {"limit": str(EVENTS_PAGE_LIMIT)}
        if cursor:
            params["cursor"] = cursor
        url = f"{base}/api/agents/sessions/{urllib.parse.quote(session_id)}/events?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"X-Agents-Token": token, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise SystemExit(f"served events request failed: HTTP {error.code} {error.reason}") from error
        except urllib.error.URLError as error:
            raise SystemExit(f"served events request failed: {error.reason}") from error
        page = served_events_from_payload(payload)
        events.extend(page)
        if not isinstance(payload, dict) or payload.get("has_more") is not True:
            break
        cursor = payload.get("next_cursor")
        if not cursor:
            break
    return events


def fetch_session(api_url: str, session_id: str, token: str, timeout: float = 30.0) -> dict[str, Any]:
    url = f"{api_url.rstrip('/')}/api/agents/sessions/{urllib.parse.quote(session_id)}"
    request = urllib.request.Request(url, headers={"X-Agents-Token": token, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        raise SystemExit(f"session lookup failed: {error}") from error
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


def _coverage_for_class(
    provider_events: list[NativeEvent],
    served_keys: set[str],
    *,
    observed_at_ms: int,
    missing_sample: int,
) -> dict[str, Any]:
    matched = [event for event in provider_events if event.key in served_keys]
    missing = [event for event in provider_events if event.key not in served_keys]
    ages = [
        observed_at_ms - event.timestamp_ms for event in missing if event.timestamp_ms is not None and observed_at_ms >= event.timestamp_ms
    ]
    missing_chars = sum(event.chars for event in missing)
    return {
        "provider": len(provider_events),
        "served_matched": len(matched),
        "missing": len(missing),
        "coverage": round(len(matched) / len(provider_events), 6) if provider_events else None,
        "provider_chars": sum(event.chars for event in provider_events),
        "missing_chars": missing_chars,
        "max_unpropagated_age_ms": max(ages) if ages else None,
        "missing_sample": [event.key for event in missing[:missing_sample]],
    }


def coverage_report(
    native: list[NativeEvent],
    served: Iterable[dict[str, Any]],
    *,
    provider: str,
    session_id: str | None,
    transcript: str | None,
    observed_at_ms: int,
    stall_age_ms: int = DEFAULT_STALL_AGE_MS,
    coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
    missing_sample: int = DEFAULT_MISSING_SAMPLE,
    provider_alive: bool | None = None,
    served_ended_at: str | None = None,
    served_event_count: int | None = None,
) -> dict[str, Any]:
    served_keys: dict[str, set[str]] = {name: set() for name in EVENT_CLASSES}
    unmapped = 0
    for event in served:
        for key in media_keys_of_served_event(event):
            served_keys["media"].add(key)
        classified = classify_served_event(event)
        if classified is None:
            if not media_keys_of_served_event(event):
                unmapped += 1
            continue
        served_keys[classified[0]].add(classified[1])

    by_class: dict[str, dict[str, Any]] = {}
    for name in EVENT_CLASSES:
        provider_events = [event for event in native if event.event_class == name]
        entry = _coverage_for_class(
            provider_events,
            served_keys[name],
            observed_at_ms=observed_at_ms,
            missing_sample=missing_sample,
        )
        entry["served"] = len(served_keys[name])
        by_class[name] = entry

    provider_total = len(native)
    served_total = sum(len(keys) for keys in served_keys.values())
    oldest_age = max(
        (entry["max_unpropagated_age_ms"] for entry in by_class.values() if entry["max_unpropagated_age_ms"] is not None),
        default=None,
    )
    incomplete = [name for name in EVENT_CLASSES if by_class[name]["provider"] and by_class[name]["coverage"] not in (None, 1.0)]

    # `ended_at` on an interactive managed session tracks the end of its last
    # run, so it advances while the session is alive. It is reported as a
    # signal, never as the verdict: only unpropagated age and coverage say
    # whether content is actually being lost.
    ended_at_ms = _parse_timestamp_ms(served_ended_at)
    if provider_alive is None or ended_at_ms is None:
        liveness = "unknown"
    elif provider_alive:
        liveness = "served_marked_ended"
    else:
        liveness = "consistent"

    # "Stalled" and "gap" are different failures and conflating them makes the
    # verdict useless. A record that never arrived is a gap, however old it is —
    # measured on 2026-09-14, a live session with a 14 KB lag and three
    # shipments in eight minutes read as STALLED because one record from an
    # earlier epoch boundary had never propagated. Stalling is a property of the
    # lane, which this snapshot cannot see; it needs the watch mode's arrival
    # stream. Here, old unpropagated records are a gap.
    stalled = False
    gapped = oldest_age is not None and oldest_age >= stall_age_ms
    lagging = [name for name in incomplete if (by_class[name]["coverage"] or 0.0) < coverage_floor]
    if provider_total == 0:
        verdict = "empty"
    elif stalled:
        verdict = "stalled"
    elif gapped:
        verdict = "gap"
    elif served_total == 0:
        verdict = "missing"
    elif lagging:
        verdict = "partial"
    else:
        verdict = "pass"

    return {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "session_id": session_id,
        "transcript": transcript,
        "observed_at": datetime.fromtimestamp(observed_at_ms / 1000, tz=timezone.utc).isoformat(),
        "verdict": verdict,
        "classes": by_class,
        "totals": {
            "provider_events": provider_total,
            "served_events": served_total if served_event_count is None else served_event_count,
            "served_mapped_events": served_total,
            "served_unmapped_events": unmapped,
            "incomplete_classes": incomplete,
            "lagging_classes": lagging,
        },
        "liveness": {
            "provider_alive": provider_alive,
            "served_ended_at": served_ended_at,
            "verdict": liveness,
        },
        "stall_age_ms": stall_age_ms,
        "oldest_unpropagated_age_ms": oldest_age,
    }


def _percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile over a non-empty sample."""

    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def summarise_serve_latency(
    native: list[NativeEvent],
    first_seen: dict[tuple[str, str], int],
    *,
    window_started_at_ms: int,
    baseline_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Per-class serve latency for records that arrived inside the window.

    Latency is ``first_observed - provider_recorded_at``: how long after the
    provider wrote a record a poll found it served. Only a record absent from
    the first poll has a measurable arrival time; anything already served then
    is pre-existing rather than instant.

    Keys are (class, identity) pairs because a tool call and its result share
    the provider's call id.

    Two series are reported per class. ``p50``/``p95``/``max`` cover every
    measurable arrival, which includes a lane draining backlog and therefore
    measures queueing as well as the hop. ``fresh_*`` restrict to records the
    provider wrote inside the window, which is the steady-state number. Both
    are upper bounds with a resolution of the sampling interval.
    """

    baseline = baseline_keys or set()
    by_class: dict[str, dict[str, Any]] = {}
    for name in EVENT_CLASSES:
        lags: list[int] = []
        fresh_lags: list[int] = []
        pre_existing = 0
        never_seen = 0
        for event in native:
            if event.event_class != name:
                continue
            identity = (name, event.key)
            if identity in baseline or event.timestamp_ms is None:
                pre_existing += 1
                continue
            seen_at = first_seen.get(identity)
            if seen_at is None:
                never_seen += 1
                continue
            lag = max(0, seen_at - event.timestamp_ms)
            lags.append(lag)
            if event.timestamp_ms > window_started_at_ms:
                fresh_lags.append(lag)
        by_class[name] = {
            "samples": len(lags),
            "pre_existing": pre_existing,
            "never_seen": never_seen,
            "min_ms": min(lags) if lags else None,
            "p50_ms": _percentile(lags, 0.5) if lags else None,
            "p95_ms": _percentile(lags, 0.95) if lags else None,
            "max_ms": max(lags) if lags else None,
            "fresh_samples": len(fresh_lags),
            "fresh_p50_ms": _percentile(fresh_lags, 0.5) if fresh_lags else None,
            "fresh_p95_ms": _percentile(fresh_lags, 0.95) if fresh_lags else None,
            "fresh_max_ms": max(fresh_lags) if fresh_lags else None,
        }
    return {
        "window_started_at": datetime.fromtimestamp(window_started_at_ms / 1000, tz=timezone.utc).isoformat(),
        "classes": by_class,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"provider={report['provider']} session={report['session_id'] or '-'} verdict={report['verdict'].upper()}",
        f"transcript={report['transcript'] or '-'}",
        "",
        f"{'class':<16}{'native':>8}{'served':>8}{'missing':>9}{'coverage':>10}{'oldest_ms':>12}{'missing_chars':>14}",
    ]
    for name in EVENT_CLASSES:
        entry = report["classes"][name]
        coverage = entry["coverage"]
        lines.append(
            f"{name:<16}{entry['provider']:>8}{entry['served']:>8}{entry['missing']:>9}"
            f"{('n/a' if coverage is None else f'{coverage:.3f}'):>10}"
            f"{(entry['max_unpropagated_age_ms'] if entry['max_unpropagated_age_ms'] is not None else '-'):>12}"
            f"{entry['missing_chars']:>14}"
        )
    totals = report["totals"]
    lines.extend(
        [
            "",
            f"provider_events={totals['provider_events']} served_mapped={totals['served_mapped_events']} "
            f"served_unmapped={totals['served_unmapped_events']}",
            f"liveness={report['liveness']['verdict']} "
            f"(provider_alive={report['liveness']['provider_alive']}, ended_at={report['liveness']['served_ended_at']})",
            f"oldest_unpropagated_age_ms={report['oldest_unpropagated_age_ms']} stall_age_ms={report['stall_age_ms']}",
        ]
    )
    for name in totals["incomplete_classes"]:
        sample = report["classes"][name]["missing_sample"]
        lines.append(f"missing[{name}] sample={sample}")
    latency = report.get("latency")
    if latency:
        lines.append("")
        lines.append(f"serve latency since {latency['window_started_at']} (provider record -> served):")
        lines.append("all arrivals (includes backlog drain):")
        lines.append(f"{'class':<16}{'samples':>8}{'p50_ms':>9}{'p95_ms':>9}{'max_ms':>9}{'never':>7}")
        for name in EVENT_CLASSES:
            entry = latency["classes"][name]
            lines.append(
                f"{name:<16}{entry['samples']:>8}"
                f"{(entry['p50_ms'] if entry['p50_ms'] is not None else '-'):>9}"
                f"{(entry['p95_ms'] if entry['p95_ms'] is not None else '-'):>9}"
                f"{(entry['max_ms'] if entry['max_ms'] is not None else '-'):>9}"
                f"{entry['never_seen']:>7}"
            )
        lines.append("records written inside the window (steady state):")
        lines.append(f"{'class':<16}{'samples':>8}{'p50_ms':>9}{'p95_ms':>9}{'max_ms':>9}")
        for name in EVENT_CLASSES:
            entry = latency["classes"][name]
            lines.append(
                f"{name:<16}{entry['fresh_samples']:>8}"
                f"{(entry['fresh_p50_ms'] if entry['fresh_p50_ms'] is not None else '-'):>9}"
                f"{(entry['fresh_p95_ms'] if entry['fresh_p95_ms'] is not None else '-'):>9}"
                f"{(entry['fresh_max_ms'] if entry['fresh_max_ms'] is not None else '-'):>9}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


MANAGED_STATE_DIRS = {
    "omp": "omp-helm",
    "pi": "pi-helm",
}


@dataclass(frozen=True)
class ManagedSession:
    provider: str
    session_id: str
    transcript: Path
    status: str


def discover_managed_sessions(
    state_root: Path,
    *,
    include_stopped: bool = False,
) -> list[ManagedSession]:
    """Find managed sessions from the Machine Agent's launch state files.

    Only providers whose launch state names a native transcript are discovered
    (OMP and Pi today). Codex, Claude, Cursor, and OpenCode carry their session
    paths elsewhere, so their live transcripts are not covered by this sweep —
    a negative result here is not a claim about those providers.
    """

    discovered: list[ManagedSession] = []
    for provider, state_dir in sorted(MANAGED_STATE_DIRS.items()):
        directory = state_root / state_dir
        if not directory.is_dir():
            continue
        for state_file in sorted(directory.glob("*.json")):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict):
                continue
            session_id = str(state.get("session_id") or "").strip()
            transcript_value = str(state.get("session_file") or "").strip()
            status = str(state.get("status") or "").strip() or "unknown"
            if not session_id or not transcript_value:
                continue
            if status == "stopped" and not include_stopped:
                continue
            transcript = Path(transcript_value)
            if not transcript.is_file():
                continue
            discovered.append(
                ManagedSession(
                    provider=provider,
                    session_id=session_id,
                    transcript=transcript,
                    status=status,
                )
            )
    return discovered


VERDICT_SEVERITY = {name: index for index, name in enumerate(("pass", "empty", "partial", "gap", "missing", "stalled"))}


def worst_verdict(reports: list[dict[str, Any]]) -> str:
    """The most severe verdict in a sweep; unknown verdicts rank highest."""

    return max(
        (str(report.get("verdict") or "unknown") for report in reports),
        key=lambda name: VERDICT_SEVERITY.get(name, len(VERDICT_SEVERITY)),
    )


def run_all_live(args: argparse.Namespace) -> int:
    """Sweep every discoverable managed session and report the worst verdict."""

    sessions = discover_managed_sessions(
        Path(args.state_root).expanduser(),
        include_stopped=args.include_stopped,
    )
    if not sessions:
        print(f"no managed sessions discovered under {args.state_root}")
        return 0

    token = _resolve_token(args)
    reports: list[dict[str, Any]] = []
    for discovered in sessions:
        native = extract_native_events(discovered.provider, discovered.transcript)
        served = fetch_served_events(api_url=args.api_url, session_id=discovered.session_id, token=token)
        session = fetch_session(args.api_url, discovered.session_id, token)
        ended_at = session.get("ended_at")
        report = coverage_report(
            native,
            served,
            provider=discovered.provider,
            session_id=discovered.session_id,
            transcript=str(discovered.transcript),
            observed_at_ms=_observed_at_ms(args.observed_at),
            stall_age_ms=args.stall_age_ms,
            coverage_floor=args.coverage_floor,
            missing_sample=args.missing_sample,
            provider_alive=discovered.status != "stopped",
            served_ended_at=str(ended_at) if ended_at else None,
            served_event_count=len(served),
        )
        report["launch_status"] = discovered.status
        reports.append(report)
        print(render_text(report))
        print()

    print(f"--- {len(reports)} managed session(s); worst verdict: {worst_verdict(reports).upper()}")
    for report in reports:
        print(f"{report['provider']:6} {report['session_id']}  status={report['launch_status']:9} verdict={report['verdict']}")

    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"sessions": reports}, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return 0 if all(report["verdict"] in {"pass", "empty"} for report in reports) else 1


def _resolve_token(args: argparse.Namespace) -> str:
    if args.token_env:
        token = os.environ.get(args.token_env)
        if not token:
            raise SystemExit(f"environment variable {args.token_env} is not set")
        return token.strip()
    path = Path(args.token_file).expanduser()
    if not path.is_file():
        raise SystemExit(f"token file not found: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"token file is empty: {path}")
    return token


def _observed_at_ms(value: str | None) -> int:
    if value is None:
        return int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    parsed = _parse_timestamp_ms(value)
    if parsed is None:
        raise SystemExit(f"could not parse --observed-at value {value!r}")
    return parsed


def watch_served_events(
    args: argparse.Namespace,
    token: str,
) -> tuple[
    int,
    dict[tuple[str, str], int],
    set[tuple[str, str]],
    list[dict[str, Any]],
    list[NativeEvent],
]:
    """Poll the served projection, recording when each native record appears.

    The transcript is re-read every poll so records the provider writes *during*
    the window are measured too, not only the ones that already existed.
    """

    transcript = Path(args.transcript).expanduser()
    first_seen: dict[tuple[str, str], int] = {}
    baseline_keys: set[tuple[str, str]] = set()
    started_at_ms = int(time.time() * 1000)
    deadline = time.monotonic() + args.watch_seconds
    served: list[dict[str, Any]] = []
    native: list[NativeEvent] = []
    first_poll = True
    while True:
        served = fetch_served_events(api_url=args.api_url, session_id=args.session, token=token)
        native = extract_native_events(args.provider, transcript)
        seen_at = int(time.time() * 1000)
        for event in served:
            classified = classify_served_event(event)
            if classified is None:
                continue
            first_seen.setdefault(classified, seen_at)
            if first_poll:
                baseline_keys.add(classified)
        first_poll = False
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.5, args.sample_interval))
    return started_at_ms, first_seen, baseline_keys, served, native


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", default=None, help="Provider whose transcript is being read (e.g. omp)")
    parser.add_argument("--transcript", default=None, help="Path to the provider-native session transcript")
    parser.add_argument("--session", default=None, help="Longhouse session id (required for live API reads)")
    parser.add_argument(
        "--all-live",
        action="store_true",
        help="Sweep every managed session the Machine Agent has launch state for, instead of one session",
    )
    parser.add_argument(
        "--state-root",
        default=str(Path.home() / ".longhouse" / "managed-local"),
        help="Launch state root used by --all-live",
    )
    parser.add_argument("--include-stopped", action="store_true", help="Include launches whose state says stopped")
    parser.add_argument("--served-events", default=None, help="Saved events payload instead of a live API read")
    parser.add_argument("--api-url", default=os.environ.get("LONGHOUSE_API_URL", "https://david010.longhouse.ai"))
    parser.add_argument("--token-env", default=None, help="Environment variable holding the device token")
    parser.add_argument(
        "--token-file",
        default=str(Path.home() / ".longhouse" / "machine" / "device-token"),
        help="File holding the device token (never pass a token value on argv)",
    )
    parser.add_argument("--provider-alive", dest="provider_alive", action="store_true", default=None)
    parser.add_argument("--provider-stopped", dest="provider_alive", action="store_false")
    parser.add_argument("--stall-age-ms", type=int, default=DEFAULT_STALL_AGE_MS)
    parser.add_argument(
        "--coverage-floor",
        type=float,
        default=DEFAULT_COVERAGE_FLOOR,
        help=(
            "Share of a class's provider records the served projection must carry before it "
            "counts as a gap rather than an in-flight tail (default: %(default)s)"
        ),
    )
    parser.add_argument("--missing-sample", type=int, default=DEFAULT_MISSING_SAMPLE)
    parser.add_argument("--observed-at", default=None, help="ISO timestamp to evaluate ages against (default: now)")
    parser.add_argument(
        "--watch-seconds",
        type=float,
        default=0.0,
        help="Poll the served projection for this long to measure per-class serve latency (0 = single snapshot)",
    )
    parser.add_argument("--sample-interval", type=float, default=5.0, help="Seconds between polls while watching")
    parser.add_argument("--output", default=None, help="Write the JSON report here")
    parser.add_argument("--json", action="store_true", help="Print the JSON report instead of the table")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.all_live:
        return run_all_live(args)
    if not args.provider or not args.transcript:
        raise SystemExit("--provider and --transcript are required unless --all-live is given")
    transcript = Path(args.transcript).expanduser()
    if not transcript.is_file():
        raise SystemExit(f"transcript not found: {transcript}")

    native = extract_native_events(args.provider, transcript)
    served_event_count: int | None = None
    served_ended_at: str | None = None

    first_seen: dict[tuple[str, str], int] = {}
    baseline_keys: set[tuple[str, str]] = set()
    window_started_at_ms: int | None = None
    if args.served_events:
        payload = json.loads(Path(args.served_events).expanduser().read_text(encoding="utf-8"))
        served = served_events_from_payload(payload)
        served_event_count = len(served)
    else:
        if not args.session:
            raise SystemExit("--session is required when reading the served projection from the API")
        token = _resolve_token(args)
        if args.watch_seconds > 0:
            window_started_at_ms, first_seen, baseline_keys, served, native = watch_served_events(args, token)
        else:
            served = fetch_served_events(api_url=args.api_url, session_id=args.session, token=token)
        served_event_count = len(served)
        session = fetch_session(args.api_url, args.session, token)
        ended_at = session.get("ended_at")
        served_ended_at = str(ended_at) if ended_at else None

    report = coverage_report(
        native,
        served,
        provider=args.provider,
        session_id=args.session,
        transcript=str(transcript),
        observed_at_ms=_observed_at_ms(args.observed_at),
        stall_age_ms=args.stall_age_ms,
        coverage_floor=args.coverage_floor,
        missing_sample=args.missing_sample,
        provider_alive=args.provider_alive,
        served_ended_at=served_ended_at,
        served_event_count=served_event_count,
    )

    if window_started_at_ms is not None:
        report["latency"] = summarise_serve_latency(
            native,
            first_seen,
            window_started_at_ms=window_started_at_ms,
            baseline_keys=baseline_keys,
        )

    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0 if report["verdict"] in {"pass", "empty"} else 1


if __name__ == "__main__":
    sys.exit(main())
