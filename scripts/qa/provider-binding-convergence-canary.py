#!/usr/bin/env python3
"""Gated live split-row AUDIT for provider-session-binding on the hosted corpus.

Scope, stated honestly: this audits the EXISTING hosted corpus for the split-row
symptom — one provider-native id mapping to more than one Longhouse session. It
does NOT launch a managed OpenCode session, send a marker, or poll transcript
ship. The launch->ship->assert orchestration canary is a separate, heavier
follow-up that needs a managed provider runtime on the QA box.

The load-bearing regression guard for convergence is the hermetic test
(server/tests_lite/test_provider_binding_convergence.py). This audit is
release/dogfood proof layered on top of it.

It is GATED and honest about coverage:
  - No api_url / token, or instance unreachable -> SKIP (exit 0).
  - Zero provider-native ids resolved on the corpus -> SKIP, explicitly "not a
    PASS" (nothing was proven; e.g. an import-only corpus).
  - One native id -> multiple sessions -> FAIL (exit 1), the split-row symptom.

Native ids are resolved per session via the X-Provider-Session-ID header on
GET /api/agents/sessions/{id}. That header is only present on instances running
the build that sets it; older hosted builds return none and this audit SKIPs.

Resolution order for the instance:
  - LONGHOUSE_QA_API_URL / LONGHOUSE_API_URL, else ~/.longhouse/config.toml [shipper] api_url
  - X-Agents-Token from LONGHOUSE_MACHINE_TOKEN or ~/.longhouse/machine/device-token

Usage:
  python3 scripts/qa/provider-binding-convergence-canary.py
  LONGHOUSE_QA_API_URL=https://david010.longhouse.ai python3 scripts/qa/provider-binding-convergence-canary.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1

TIMEOUT_S = 20


def _log(msg: str) -> None:
    print(f"[binding-canary] {msg}", flush=True)


def _resolve_api_url() -> str | None:
    for env_key in ("LONGHOUSE_QA_API_URL", "LONGHOUSE_API_URL"):
        value = os.environ.get(env_key)
        if value:
            return value.rstrip("/")

    config_path = Path(os.environ.get("LONGHOUSE_CONFIG", Path.home() / ".longhouse" / "config.toml"))
    if config_path.exists():
        try:
            try:
                import tomllib  # py3.11+
            except ModuleNotFoundError:  # pragma: no cover
                import tomli as tomllib  # type: ignore
            data = tomllib.loads(config_path.read_text())
            api_url = (data.get("shipper") or {}).get("api_url")
            if api_url:
                # Fallback source — env vars are preferred. Log it so a stale
                # config never silently points the audit at the wrong host.
                _log(f"resolved api_url from {config_path} [shipper].api_url (set LONGHOUSE_QA_API_URL to override)")
                return str(api_url).rstrip("/")
        except Exception as exc:  # noqa: BLE001
            _log(f"could not parse {config_path}: {exc}")
    return None


def _resolve_token() -> str | None:
    token = os.environ.get("LONGHOUSE_MACHINE_TOKEN") or os.environ.get("LONGHOUSE_DEVICE_TOKEN")
    if token:
        return token.strip()
    token_path = Path(os.environ.get("LONGHOUSE_DEVICE_TOKEN_PATH", Path.home() / ".longhouse" / "machine" / "device-token"))
    if token_path.exists():
        try:
            value = token_path.read_text().strip()
            if value:
                return value
        except OSError:
            pass
    return None


# The hosted edge blocks the default Python-urllib User-Agent; send an explicit
# one or every request 403s before reaching the app.
_HEADERS_BASE = {"Accept": "application/json", "User-Agent": "longhouse-binding-canary"}

# Bound the detail crawl so the canary stays fast; logged, never silent. The
# detail header only emits a native id for sessions whose binding resolves, so a
# corpus of purely imported sessions legitimately resolves none and we SKIP.
DETAIL_CRAWL_CAP = 40


def _get_json(api_url: str, token: str, path: str) -> object | None:
    url = f"{api_url}{path}"
    req = urllib.request.Request(url, headers={**_HEADERS_BASE, "X-Agents-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        _log(f"request to {path} failed: {exc}")
        return None


def _provider_session_id_header(api_url: str, token: str, session_id: str) -> str | None:
    """The session detail endpoint emits the native id via X-Provider-Session-ID
    when it resolves; the list endpoint does not expose it."""
    url = f"{api_url}/api/agents/sessions/{session_id}"
    req = urllib.request.Request(url, headers={**_HEADERS_BASE, "X-Agents-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            value = resp.headers.get("X-Provider-Session-ID")
            return value.strip() if value and value.strip() else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        _log(f"detail request for {session_id} failed: {exc}")
        return None


def main() -> int:
    api_url = _resolve_api_url()
    token = _resolve_token()

    if not api_url or not token:
        _log("SKIP: no api_url or machine token resolved (set LONGHOUSE_QA_API_URL + LONGHOUSE_MACHINE_TOKEN to run).")
        return EXIT_OK

    _log(f"target instance: {api_url}")

    # Reachability gate: ingest-health is a cheap authenticated probe.
    health = _get_json(api_url, token, "/api/agents/ingest-health")
    if health is None:
        _log("SKIP: instance unreachable or token rejected.")
        return EXIT_OK

    # Pull recent managed OpenCode sessions and group by provider-native id.
    listing = _get_json(
        api_url,
        token,
        "/api/agents/sessions?provider=opencode&days_back=14&limit=100&include_test=false",
    )
    if not isinstance(listing, dict):
        _log("SKIP: sessions listing unavailable.")
        return EXIT_OK

    sessions = listing.get("sessions") or listing.get("items") or []
    if not isinstance(sessions, list):
        _log("SKIP: unexpected sessions payload shape.")
        return EXIT_OK

    session_ids = [str(s.get("id") or s.get("session_id")) for s in sessions if isinstance(s, dict) and (s.get("id") or s.get("session_id"))]
    if not session_ids:
        _log("SKIP: no opencode sessions in window to inspect.")
        return EXIT_OK

    # The list payload does not carry the native id, so resolve it per session
    # via the detail header. Bound the crawl and LOG the cap — a silent cap
    # would read as "covered everything" when it didn't.
    crawl = session_ids[:DETAIL_CRAWL_CAP]
    if len(session_ids) > DETAIL_CRAWL_CAP:
        _log(f"NOTE: {len(session_ids)} sessions in window; inspecting first {DETAIL_CRAWL_CAP}.")

    by_native: dict[str, set[str]] = {}
    for session_id in crawl:
        native = _provider_session_id_header(api_url, token, session_id)
        if not native:
            continue
        by_native.setdefault(native, set()).add(session_id)

    resolved = sum(len(ids) for ids in by_native.values())
    _log(f"resolved native id for {resolved}/{len(crawl)} inspected session(s) across {len(by_native)} native id(s).")

    if not by_native:
        # Honest: we proved nothing if no native id resolved (e.g. corpus is all
        # imported sessions whose native id the detail endpoint does not emit).
        _log("SKIP: no provider-native ids resolved on this corpus; nothing to prove. Not a PASS.")
        return EXIT_OK

    splits = {native: sorted(ids) for native, ids in by_native.items() if len(ids) > 1}
    if splits:
        _log("FAIL: split rows detected — one provider-native id maps to multiple sessions:")
        for native, ids in splits.items():
            _log(f"  {native} -> {', '.join(ids)}")
        return EXIT_FAIL

    _log("PASS: no split rows; every resolved provider-native id maps to one session.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
