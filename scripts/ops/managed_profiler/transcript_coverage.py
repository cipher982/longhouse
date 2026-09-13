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
import json
import os
import sys
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

EVENT_CLASSES = ("user", "assistant_text", "thinking", "tool_call", "tool_result")

DEFAULT_STALL_AGE_MS = 60_000
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
                    key = f"{record_id}-thinking-{index}"
                    events.append(NativeEvent("thinking", key, timestamp_ms, len(text)))
                elif kind == "toolCall":
                    call_id = part.get("id")
                    key = str(call_id) if isinstance(call_id, str) and call_id.strip() else f"{record_id}-tool-{index}"
                    events.append(NativeEvent("tool_call", key, timestamp_ms, 0))
    return events


EXTRACTORS: dict[str, Callable[[Path], list[NativeEvent]]] = {
    "omp": extract_omp_transcript,
}


def extract_native_events(provider: str, path: Path) -> list[NativeEvent]:
    extractor = EXTRACTORS.get(provider.strip().lower())
    if extractor is None:
        raise SystemExit(
            f"no transcript extractor for provider {provider!r}; "
            f"implemented: {', '.join(sorted(EXTRACTORS))}"
        )
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
        observed_at_ms - event.timestamp_ms
        for event in missing
        if event.timestamp_ms is not None and observed_at_ms >= event.timestamp_ms
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
    missing_sample: int = DEFAULT_MISSING_SAMPLE,
    provider_alive: bool | None = None,
    served_ended_at: str | None = None,
    served_event_count: int | None = None,
) -> dict[str, Any]:
    served_keys: dict[str, set[str]] = {name: set() for name in EVENT_CLASSES}
    unmapped = 0
    for event in served:
        classified = classify_served_event(event)
        if classified is None:
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
        (
            entry["max_unpropagated_age_ms"]
            for entry in by_class.values()
            if entry["max_unpropagated_age_ms"] is not None
        ),
        default=None,
    )
    incomplete = [name for name in EVENT_CLASSES if by_class[name]["provider"] and by_class[name]["coverage"] not in (None, 1.0)]

    ended_at_ms = _parse_timestamp_ms(served_ended_at)
    if provider_alive is None or ended_at_ms is None:
        liveness = "unknown"
    elif provider_alive:
        liveness = "ended_while_alive"
    else:
        liveness = "consistent"

    stalled = liveness == "ended_while_alive" or (oldest_age is not None and oldest_age >= stall_age_ms)
    if provider_total == 0:
        verdict = "empty"
    elif stalled:
        verdict = "stalled"
    elif served_total == 0:
        verdict = "missing"
    elif not incomplete:
        verdict = "pass"
    else:
        verdict = "partial"

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
        },
        "liveness": {
            "provider_alive": provider_alive,
            "served_ended_at": served_ended_at,
            "verdict": liveness,
        },
        "stall_age_ms": stall_age_ms,
        "oldest_unpropagated_age_ms": oldest_age,
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
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", required=True, help="Provider whose transcript is being read (e.g. omp)")
    parser.add_argument("--transcript", required=True, help="Path to the provider-native session transcript")
    parser.add_argument("--session", default=None, help="Longhouse session id (required for live API reads)")
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
    parser.add_argument("--missing-sample", type=int, default=DEFAULT_MISSING_SAMPLE)
    parser.add_argument("--observed-at", default=None, help="ISO timestamp to evaluate ages against (default: now)")
    parser.add_argument("--output", default=None, help="Write the JSON report here")
    parser.add_argument("--json", action="store_true", help="Print the JSON report instead of the table")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    transcript = Path(args.transcript).expanduser()
    if not transcript.is_file():
        raise SystemExit(f"transcript not found: {transcript}")

    native = extract_native_events(args.provider, transcript)
    served_event_count: int | None = None
    served_ended_at: str | None = None

    if args.served_events:
        payload = json.loads(Path(args.served_events).expanduser().read_text(encoding="utf-8"))
        served = served_events_from_payload(payload)
        served_event_count = len(served)
    else:
        if not args.session:
            raise SystemExit("--session is required when reading the served projection from the API")
        token = _resolve_token(args)
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
        missing_sample=args.missing_sample,
        provider_alive=args.provider_alive,
        served_ended_at=served_ended_at,
        served_event_count=served_event_count,
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
