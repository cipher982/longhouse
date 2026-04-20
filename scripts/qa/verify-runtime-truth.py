#!/usr/bin/env python3
"""MVP: compare the three runtime-truth systems for managed sessions.

Systems inspected (read-only):
  1) Local: ~/.longhouse/agent/longhouse-shipper.db -> session_phase_state
     (written by the engine on every hook/bridge phase signal, LWW)
  2) Local view: `longhouse local-health --json` (the overlay + scans)
  3) Server view: GET /api/agents/sessions/active (SessionRuntimeState +
     SessionPresence, overlayed by resolve_runtime_overlay)

For each session observed in any of the three, print the phase each system
reports. A disagreement is the interesting signal — it's where "three
overlapping truths" is actively lying.

Output is intentionally dense + plain-text so agents can diff two runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionRow:
    session_id: str
    provider: str | None = None
    local_ledger_phase: str | None = None
    local_ledger_observed_at: str | None = None
    local_ledger_source: str | None = None
    engine_status_phase: str | None = None
    engine_status_source: str | None = None
    local_health_phase: str | None = None
    local_health_state: str | None = None
    local_health_observed_at: str | None = None
    server_phase: str | None = None
    server_status: str | None = None
    server_confidence: str | None = None
    server_presence_state: str | None = None
    notes: list[str] = field(default_factory=list)


def _longhouse_home() -> Path:
    env = os.environ.get("LONGHOUSE_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".longhouse"


def _agent_db_path() -> Path:
    return _longhouse_home() / "agent" / "longhouse-shipper.db"


def _engine_status_path() -> Path:
    return _longhouse_home() / "agent" / "engine-status.json"


def read_engine_status_ledger() -> dict[str, dict[str, Any]]:
    """Read `phase_ledger[]` from engine-status.json — the engine's own view
    of the LWW ledger, pre-filtered for freshness. Diverging from the raw
    SQL read (`read_ledger`) would mean the engine and the overlay disagree
    on which rows are current.
    """
    path = _engine_status_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"!! could not read {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("phase_ledger") or []
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        session_id = row.get("session_id")
        if not session_id:
            continue
        out[str(session_id)] = {
            "provider": row.get("provider"),
            "phase": row.get("phase"),
            "tool_name": row.get("tool_name"),
            "source": row.get("source"),
            "observed_at": row.get("observed_at"),
        }
    return out


def read_ledger() -> dict[str, dict[str, Any]]:
    path = _agent_db_path()
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as exc:
        print(f"!! could not open {path}: {exc}", file=sys.stderr)
        return {}
    try:
        rows = conn.execute(
            """
            SELECT session_id, provider, phase, tool_name, source, observed_at
            FROM session_phase_state
            """
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"!! session_phase_state missing/unreadable: {exc}", file=sys.stderr)
        return {}
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for session_id, provider, phase, tool_name, source, observed_at in rows:
        out[str(session_id)] = {
            "provider": provider,
            "phase": phase,
            "tool_name": tool_name,
            "source": source,
            "observed_at": observed_at,
        }
    return out


def read_local_health() -> list[dict[str, Any]]:
    binary = os.environ.get("LONGHOUSE_BIN") or "longhouse"
    try:
        proc = subprocess.run(
            [binary, "local-health", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"!! `{binary} local-health --json` unavailable: {exc}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        print(f"!! local-health exited {proc.returncode}: {proc.stderr.strip()}", file=sys.stderr)
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"!! local-health produced non-JSON: {exc}", file=sys.stderr)
        return []
    if not isinstance(payload, dict):
        print("!! local-health payload is not a JSON object", file=sys.stderr)
        return []
    sessions = payload.get("managed_sessions") or []
    return list(sessions) if isinstance(sessions, list) else []


def read_server_active(base_url: str, token: str | None) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/agents/sessions/active?limit=200"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("X-Agents-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        print(f"!! server {exc.code} at {url}: {exc.read().decode(errors='replace')}", file=sys.stderr)
        return []
    except urllib.error.URLError as exc:
        print(f"!! server unreachable at {url}: {exc.reason}", file=sys.stderr)
        return []
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"!! server returned non-JSON at {url}: {exc}", file=sys.stderr)
        return []
    if not isinstance(payload, dict):
        print(f"!! server payload at {url} is not a JSON object", file=sys.stderr)
        return []
    sessions = payload.get("sessions") or []
    return list(sessions) if isinstance(sessions, list) else []


def merge(
    ledger: dict[str, dict[str, Any]],
    engine_status: dict[str, dict[str, Any]],
    local_sessions: list[dict[str, Any]],
    server_sessions: list[dict[str, Any]],
) -> list[SessionRow]:
    rows: dict[str, SessionRow] = {}
    for session_id, data in ledger.items():
        row = rows.setdefault(session_id, SessionRow(session_id=session_id))
        row.provider = data.get("provider") or row.provider
        row.local_ledger_phase = data.get("phase")
        row.local_ledger_observed_at = data.get("observed_at")
        row.local_ledger_source = data.get("source")
    for session_id, data in engine_status.items():
        row = rows.setdefault(session_id, SessionRow(session_id=session_id))
        row.provider = data.get("provider") or row.provider
        row.engine_status_phase = data.get("phase")
        row.engine_status_source = data.get("source")
    for entry in local_sessions:
        session_id = entry.get("session_id")
        if not session_id:
            continue
        row = rows.setdefault(session_id, SessionRow(session_id=session_id))
        row.provider = entry.get("provider") or row.provider
        row.local_health_phase = entry.get("phase")
        row.local_health_state = entry.get("state")
        row.local_health_observed_at = entry.get("phase_observed_at")
    for entry in server_sessions:
        session_id = entry.get("id") or entry.get("session_id")
        if not session_id:
            continue
        row = rows.setdefault(str(session_id), SessionRow(session_id=str(session_id)))
        row.provider = entry.get("provider") or row.provider
        # ActiveSessionResponse exposes runtime fields at the top level.
        # Prefer `runtime_phase` (canonical) over `display_phase` which can
        # be "Recent"/"Recent progress" and would never round-trip through
        # _canonicalize_phase cleanly.
        row.server_phase = entry.get("runtime_phase") or entry.get("display_phase")
        row.server_status = entry.get("status")
        row.server_confidence = entry.get("confidence")
        row.server_presence_state = entry.get("presence_state")
    return sorted(rows.values(), key=lambda row: row.session_id)


_SINGLE_WORD_PHASE = {
    "thinking": "thinking",
    "running": "running",
    "idle": "idle",
    "blocked": "blocked",
    "completed": "finished",
    "finished": "finished",
}


def _canonicalize_phase(raw: str | None) -> str | None:
    """Map a local-health display label, a server runtime_phase, or a ledger
    phase down to the canonical phase vocabulary.

    Local-health labels come from `_phase_display_label` in
    server/zerg/services/local_health.py. Examples:
      - "running Bash" / "running"            -> running
      - "blocked on shell" / "needs permission" -> blocked
      - "needs you"                            -> needs_user
      - "completed", "idle", "thinking"        -> as named
    """
    if not raw:
        return None
    stripped = raw.strip().lower()
    if not stripped:
        return None
    if stripped == "needs permission":
        return "blocked"
    tokens = stripped.split()
    head = tokens[0]
    if head == "needs" and len(tokens) >= 2 and tokens[1] == "you":
        return "needs_user"
    if head in ("running", "blocked", "idle", "thinking"):
        return head
    if head == "completed" or head == "finished":
        return "finished"
    return _SINGLE_WORD_PHASE.get(head, head)


def classify(row: SessionRow) -> tuple[str, list[str]]:
    """Systems agree if canonical phase matches across all four sources.

    Sources compared:
    - raw SQL ledger (session_phase_state)
    - engine-status.json phase_ledger[]  (the engine's filtered view)
    - local-health managed_sessions      (overlay of ledger + newer outbox)
    - server runtime_phase               (materialized runtime reducer)

    The engine-status view should always be a subset of the raw ledger
    (pre-filtered for freshness), so a divergence there means the engine
    and the overlay filtered the same row differently. The observed_at
    column is not part of the verdict — the overlay intentionally advances
    past the ledger as soon as a newer outbox file lands.
    """
    ledger = _canonicalize_phase(row.local_ledger_phase)
    engine_status = _canonicalize_phase(row.engine_status_phase)
    local = _canonicalize_phase(row.local_health_phase)
    server = _canonicalize_phase(row.server_phase)
    known = [value for value in (ledger, engine_status, local, server) if value]
    if not known:
        return "silent", ["no system reported a phase"]
    if len(set(known)) == 1:
        return "agree", []
    reasons: list[str] = []
    if ledger and engine_status and ledger != engine_status:
        reasons.append(
            f"ledger={row.local_ledger_phase!r} vs engine-status={row.engine_status_phase!r}"
        )
    if ledger and local and ledger != local:
        reasons.append(
            f"ledger={row.local_ledger_phase!r} vs local-health={row.local_health_phase!r}"
        )
    if local and server and local != server:
        reasons.append(
            f"local-health={row.local_health_phase!r} vs server={row.server_phase!r}"
        )
    if ledger and server and ledger != server and not reasons:
        reasons.append(
            f"ledger={row.local_ledger_phase!r} vs server={row.server_phase!r}"
        )
    return "diverge", reasons or ["phase values disagree"]


def print_report(rows: list[SessionRow]) -> int:
    divergences = 0
    silent = 0
    agreeing = 0
    print(
        f"{'session_id':<38}  {'prov':<6}  {'ledger':<22}  {'engine-status':<22}  "
        f"{'local-health':<22}  {'server':<22}  verdict"
    )
    print("-" * 150)
    for row in rows:
        verdict, reasons = classify(row)
        if verdict == "diverge":
            divergences += 1
            row.notes.extend(reasons)
        elif verdict == "silent":
            silent += 1
        else:
            agreeing += 1
        ledger_col = f"{row.local_ledger_phase or '-'} ({(row.local_ledger_source or '-').split('_')[0]})"
        engine_col = f"{row.engine_status_phase or '-'} ({(row.engine_status_source or '-').split('_')[0]})"
        local_col = f"{row.local_health_phase or '-'} [{row.local_health_state or '-'}]"
        server_col = f"{row.server_phase or '-'} [{row.server_confidence or '-'}]"
        print(
            f"{row.session_id:<38}  {(row.provider or '-'):<6}  "
            f"{ledger_col[:22]:<22}  {engine_col[:22]:<22}  "
            f"{local_col[:22]:<22}  {server_col[:22]:<22}  {verdict}"
        )
        for note in row.notes:
            print(f"  -> {note}")

    print()
    print(f"summary: {agreeing} agree, {divergences} diverge, {silent} silent, {len(rows)} total")
    return divergences


def self_check() -> int:
    """Fail fast if `_canonicalize_phase` drifts from local-health's labels."""
    cases = {
        # local-health display labels from _phase_display_label
        "running": "running",
        "running Bash": "running",
        "thinking": "thinking",
        "blocked on shell": "blocked",
        "needs permission": "blocked",
        "needs you": "needs_user",
        "idle": "idle",
        "completed": "finished",
        # raw server runtime_phase values
        "finished": "finished",
        "needs_user": "needs_user",
    }
    failures: list[str] = []
    for raw, expected in cases.items():
        actual = _canonicalize_phase(raw)
        if actual != expected:
            failures.append(f"{raw!r} -> {actual!r} (expected {expected!r})")
    if failures:
        print("self-check failures:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"self-check OK ({len(cases)} cases)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        default=os.environ.get("LONGHOUSE_SERVER", "http://127.0.0.1:8001"),
        help="Base URL for the runtime host (default http://127.0.0.1:8001)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("LONGHOUSE_AGENTS_TOKEN"),
        help="X-Agents-Token header value. Skip server query if unset and AUTH_DISABLED is not on.",
    )
    parser.add_argument(
        "--skip-server",
        action="store_true",
        help="Only compare the two local views.",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Validate the phase canonicalizer against known display labels.",
    )
    args = parser.parse_args()

    if args.self_check:
        return self_check()

    print(f"longhouse home: {_longhouse_home()}")
    print(f"ledger db:      {_agent_db_path()}")
    ledger = read_ledger()
    print(f"ledger rows:    {len(ledger)}")

    engine_status = read_engine_status_ledger()
    print(f"engine-status:  {len(engine_status)} fresh ledger rows from {_engine_status_path()}")

    local_sessions = read_local_health()
    print(f"local-health:   {len(local_sessions)} managed sessions")

    server_sessions: list[dict[str, Any]] = []
    if not args.skip_server:
        server_sessions = read_server_active(args.server, args.token)
        print(f"server:         {len(server_sessions)} active sessions from {args.server}")

    print()
    rows = merge(ledger, engine_status, local_sessions, server_sessions)
    if not rows:
        print("(no sessions in any system)")
        return 0
    divergences = print_report(rows)
    return 1 if divergences else 0


if __name__ == "__main__":
    sys.exit(main())
