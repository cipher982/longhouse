"""Replay the tool-translation corpora through a served Runtime Host.

The corpora (``tests/fixtures/tool-translation``) hold, per provider, the raw
provider records and the canonical events they translate to. The verifier posts
each provider's canonical events to a served host and reads the tool rows the
host returns. What the host says about each call (disposition, tier, rule,
children) is compared with golden rows generated once from the product's output
and reviewed by eye, so a change to the product's presentation shows up as a
diff in a committed fixture instead of moving with the code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zerg.qa.runtime_host import RuntimeHost
from zerg.qa.served_transcript import ingest_events
from zerg.qa.served_transcript import read_events

GOLDEN_NAME = "served-dispositions.golden.json"


def _canonical_events(corpus_path: Path) -> tuple[int, list[dict[str, Any]]]:
    source_events = 0
    events: list[dict[str, Any]] = []
    for line in corpus_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        source_events += 1
        for event in record.get("canonical") or []:
            events.append(dict(event))
    return source_events, events


def _tool_row(call: dict[str, Any], *, result_ids: set[str]) -> dict[str, Any]:
    presentation = call.get("tool_presentation") or {}
    children = presentation.get("children") or []
    return {
        "tool_call_id": call.get("tool_call_id"),
        "source_tool_name": call.get("tool_name"),
        "paired_with_result": call.get("tool_call_id") in result_ids,
        "disposition": presentation.get("disposition"),
        "tool_name": presentation.get("tool_name"),
        "tier": presentation.get("tier"),
        "aggregate": presentation.get("aggregate"),
        "rule_id": presentation.get("rule_id"),
        "wrapper_recedes": bool(presentation.get("wrapper_recedes")),
        "child_dispositions": [child.get("disposition") for child in children],
    }


def replay_manifest(manifest_path: Path, host: RuntimeHost) -> dict[str, Any]:
    """Post every provider's canonical events to a served host and return what it serves, per provider."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    providers: dict[str, Any] = {}
    for source in manifest["sources"]:
        provider = str(source["provider"])
        source_events, events = _canonical_events(manifest_path.parent / str(source["path"]))
        # The envelope carries tool input as text, the way the engine renders it.
        posted = [{**event, "tool_input_json": _as_text(event.get("tool_input_json"))} for event in events]
        session_id = ingest_events(host.api_url, provider=provider, events=posted)
        served = read_events(host.api_url, session_id)
        result_ids = {str(event.get("tool_call_id")) for event in served if event.get("role") == "tool"}
        calls = [event for event in served if event.get("role") == "assistant" and event.get("tool_name")]
        providers[provider] = {
            "source_events": source_events,
            "served_events": len(served),
            "results": sum(1 for event in served if event.get("role") == "tool"),
            "rows": [_tool_row(call, result_ids=result_ids) for call in calls],
        }
    return {"version": 1, "manifest_id": manifest.get("id"), "providers": providers}


def _as_text(value: Any) -> Any:
    return json.dumps(value) if isinstance(value, (dict, list)) else value


def totals(replay: dict[str, Any]) -> dict[str, int]:
    rows = [row for provider in replay["providers"].values() for row in provider["rows"]]
    counts = {"outer_calls": len(rows), "results": sum(p["results"] for p in replay["providers"].values())}
    for disposition in ("exact", "parsed", "generic", "unknown"):
        counts[disposition] = sum(1 for row in rows if row["disposition"] == disposition)
    counts["paired"] = sum(1 for row in rows if row["paired_with_result"])
    counts["inferred_children"] = sum(len(row["child_dispositions"]) for row in rows if row["disposition"] == "parsed")
    counts["wrappers_retained"] = sum(1 for row in rows if row["child_dispositions"] and not row["wrapper_recedes"])
    return counts


def compare_to_golden(replay: dict[str, Any], golden_path: Path) -> list[str]:
    """Human-readable mismatches between the served replay and the committed golden rows."""

    if not golden_path.is_file():
        return [f"golden file missing: {golden_path.name}"]
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    mismatches: list[str] = []
    for provider in sorted(set(golden["providers"]) | set(replay["providers"])):
        expected = golden["providers"].get(provider)
        actual = replay["providers"].get(provider)
        if expected is None or actual is None:
            mismatches.append(f"{provider}: present on only one side")
            continue
        if actual != expected:
            mismatches.append(f"{provider}: served rows differ from golden")
    return mismatches
