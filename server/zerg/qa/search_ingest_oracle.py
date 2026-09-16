"""Shared oracle: a provider's own output is findable through served search.

The Search chip claims that a transcript a provider writes on the user's
machine becomes full-text searchable. That is only proven when all of these
hold for one fresh marker:

- the provider *produced* it -- the prompt carries separated pieces and asks
  the model to join them, so the joined token never appears in any user text
  and a prompt echo cannot satisfy the search;
- the provider's native output actually contains it (claim/transcript
  evidence, not a Longhouse-side copy);
- the real Machine Agent shipped it and the Runtime Host indexed it;
- the served machine search surface (``GET /api/agents/sessions?query=``, the
  same storage-v2 lexical index browser and iOS search read) returns the exact
  session, matched on an assistant event, through the lexical lane with no
  degraded fallback.

The oracle is a pure function over a recorded observation so replay tests can
feed it bad evidence. Producers own driving the provider; this module owns the
marker, the probe, and the verdict.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

MARKER_PREFIX = "lhsearch"
FAULT_NAME = "ingest_redact_marker"
FAULT_RECEIPT_NAME = "IngestRedactMarker"

# Typed failure codes, in check order. A negative control is only a pass when
# ingest itself lost the marker (``marker_not_found_in_served_search``).
FAILURE_CODES = (
    "marker_echoed_in_prompt",
    "provider_did_not_produce_marker",
    "transcript_flush_failed",
    "search_probe_failed",
    "search_lane_degraded",
    "marker_not_found_in_served_search",
    "marker_found_in_wrong_session",
    "marker_matched_non_assistant_event",
)


def new_marker() -> dict[str, str]:
    """Return pieces the model must join and the token only its output holds."""

    pieces = [secrets.token_hex(4), secrets.token_hex(4)]
    return {"marker": MARKER_PREFIX + "".join(pieces), "pieces": " ".join([MARKER_PREFIX, *pieces])}


def search_prompt(marker: Mapping[str, str]) -> str:
    return (
        "Join these three pieces into one word with nothing between them, "
        f"then reply with only that word and no other text: {marker['pieces']}"
    )


def probe_served_search(
    api_url: str,
    token: str,
    marker: str,
    *,
    expected_session_id: str,
    timeout: float = 90.0,
    interval: float = 1.0,
) -> dict[str, Any]:
    """Poll the served machine search until the expected session appears."""

    query = urllib.parse.urlencode(
        {
            "query": marker,
            "include_test": "true",
            "include_automation": "true",
            "hide_autonomous": "false",
            "days_back": "1",
            "limit": "20",
        }
    )
    url = f"{api_url.rstrip('/')}/api/agents/sessions?{query}"
    started = time.monotonic()
    attempts = 0
    last: dict[str, Any] = {}
    while True:
        attempts += 1
        request = urllib.request.Request(
            url,
            headers={"X-Agents-Token": token, "Accept": "application/json", "User-Agent": "LonghouseSearchIngestOracle/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read())
            sessions = payload.get("sessions") if isinstance(payload, dict) else None
            last = {
                "http_status": 200,
                "lanes": payload.get("lanes") if isinstance(payload, dict) else None,
                "degraded": payload.get("degraded") if isinstance(payload, dict) else None,
                "hits": [
                    {
                        "session_id": str(item.get("id") or ""),
                        "provider": item.get("provider"),
                        "match_role": item.get("match_role"),
                        "match_snippet": str(item.get("match_snippet") or "")[:400],
                    }
                    for item in (sessions or [])
                    if isinstance(item, dict)
                ],
            }
        except urllib.error.HTTPError as exc:
            last = {"http_status": exc.code, "error": exc.read(1000).decode("utf-8", "replace")}
        except (OSError, ValueError) as exc:
            last = {"http_status": None, "error": f"{type(exc).__name__}: {exc}"}
        if any(hit["session_id"] == expected_session_id for hit in last.get("hits") or []):
            break
        if time.monotonic() - started >= timeout:
            break
        time.sleep(interval)
    return {
        **last,
        "query": marker,
        "surface": "GET /api/agents/sessions?query=",
        "expected_session_id": expected_session_id,
        "attempts": attempts,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def search_ingest_verdict(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Judge one recorded search-ingest observation.

    Expected keys: ``marker``, ``prompt``, ``provider_marker_count``,
    ``flush_ok``, ``session_id``, ``search`` (from :func:`probe_served_search`).
    """

    marker = str(observation.get("marker") or "")
    prompt = str(observation.get("prompt") or "")
    search = observation.get("search") if isinstance(observation.get("search"), Mapping) else {}
    session_id = str(observation.get("session_id") or "")
    hits = [hit for hit in (search.get("hits") or []) if isinstance(hit, Mapping)]
    own = [hit for hit in hits if hit.get("session_id") == session_id]
    count = observation.get("provider_marker_count")

    def verdict(code: str | None) -> dict[str, Any]:
        return {"passed": code is None, "failure_code": code, "own_hits": len(own), "foreign_hits": len(hits) - len(own)}

    if not marker.startswith(MARKER_PREFIX) or marker.lower() in prompt.lower():
        return verdict("marker_echoed_in_prompt")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return verdict("provider_did_not_produce_marker")
    if observation.get("flush_ok") is not True:
        return verdict("transcript_flush_failed")
    if search.get("http_status") != 200:
        return verdict("search_probe_failed")
    if search.get("lanes") != ["lexical"] or search.get("degraded"):
        return verdict("search_lane_degraded")
    if not own:
        return verdict("marker_found_in_wrong_session" if hits else "marker_not_found_in_served_search")
    if len(own) != len(hits):
        return verdict("marker_found_in_wrong_session")
    if not all(hit.get("match_role") == "assistant" for hit in own):
        return verdict("marker_matched_non_assistant_event")
    return verdict(None)


def negative_control_verdict(observation: Mapping[str, Any], *, fault_receipts: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Pass only when the fault fired, preconditions held, and search missed."""

    session_id = str(observation.get("session_id") or "")
    fired = [
        receipt
        for receipt in fault_receipts
        if receipt.get("fault") == FAULT_RECEIPT_NAME and str(receipt.get("session_id") or "") == session_id
    ]
    result = search_ingest_verdict(observation)
    preconditions_held = result["failure_code"] not in {
        "marker_echoed_in_prompt",
        "provider_did_not_produce_marker",
        "transcript_flush_failed",
        "search_probe_failed",
        "search_lane_degraded",
    }
    if not fired or not preconditions_held:
        status = "inconclusive"
    elif result["failure_code"] == "marker_not_found_in_served_search":
        status = "pass"
    else:
        status = "fail"
    return {
        "status": status,
        "fault": FAULT_NAME,
        "fault_fired": bool(fired),
        "preconditions_held": preconditions_held,
        "target_failure_code": result["failure_code"],
    }
