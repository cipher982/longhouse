#!/usr/bin/env python3
"""Console served-state proof: what a viewer receives after a real turn.

Every other Console E2E authenticates as a machine and asserts on machine-side
oracles: archived events (`/api/agents/sessions/{id}/events`) and the local
turn-claim file. Both were correct throughout the 2026-08-23 wedge, where a
finished Console turn kept rendering "Working" for ten hours. The archive had
the reply and the claim read `terminal`; only the served surface was wrong. A
suite whose oracles are all machine-side runs green through that incident.

This harness watches what a browser or phone actually receives:

  1. live delivery -- frames reach the workspace SSE stream *during* the turn
  2. settlement    -- once the reply is served, the state axis stops saying work

Oracle 2 encodes the incident directly. A reply reaching a viewer while the
state axis still says working is exactly what the wedge looked like: the
response was there when you came back, and the bar kept pulsing.

Every signal is remote. An earlier version triggered settlement off the local
turn-claim file, which is the same machine-side coupling this exists to escape
and would have pinned the check to the box that owns the engine. Content
arriving in the served projection is the evidence that the turn finished, and it
is readable from anywhere with API access.

Both oracles are checked against the browser surface, reached with the `zdt_`
device token that `dependencies/browser_auth.py` accepts precisely so a
non-browser client can drive the UI contract.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY_S = 2.0


class ApiError(RuntimeError):
    """An HTTP failure from the Longhouse API, carrying its status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


USER_AGENT = "longhouse-console-served-state-e2e/1"

# The pulsing composer bar is NOT display_phase. SessionChat.tsx:726 renders
# "Working" off isSendLocked, which SessionDetailPage.tsx:574 derives from
# activity.state. display_phase is a presentation string and is partly dynamic
# ("Using shell" when a tool is named), so an allowlist of labels silently
# passes on states it forgot. Assert the structured contract instead.
WORKING_ACTIVITY = {"thinking", "executing"}
WORKING_RUN_LIFECYCLE = {"starting", "running"}
WORKING_PRESENTATION_KEYS = {"starting", "thinking", "executing", "stalled"}


def console_providers() -> list[str]:
    """Every provider with a Console adapter, from the single provider authority.

    Derived rather than listed so a new provider enters this check by existing,
    which is the whole point of `schemas/managed_providers.yml` being the
    authority. A hardcoded tuple is how a provider silently escapes coverage.
    """
    import yaml  # imported lazily so the single-provider path needs no dependency

    schema = yaml.safe_load((ROOT / "schemas" / "managed_providers.yml").read_text(encoding="utf-8"))
    return [str(entry["provider"]) for entry in schema.get("providers") or [] if str(entry.get("console_adapter") or "").strip()]


def _home() -> Path:
    return Path(os.environ.get("LONGHOUSE_HOME") or Path.home() / ".longhouse").expanduser()


# The factory projects the `runtime_host_control` binding as exactly these two
# names (assurance.py:1580). This module only ever read the laptop's names, so
# as a factory producer it would have found no credentials and failed on an
# empty API URL -- an oracle that cannot authenticate is an oracle that never
# runs. Both callers are real: the factory sets the RUNTIME_ names, a manual
# run on a workstation sets the shorter ones or has a machine state file.
_API_URL_ENV = ("LONGHOUSE_RUNTIME_API_URL", "LONGHOUSE_API_URL")
_TOKEN_ENV = ("LONGHOUSE_RUNTIME_AGENTS_TOKEN", "LONGHOUSE_MACHINE_TOKEN")


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _defaults() -> tuple[str, str]:
    api_url = _first_env(_API_URL_ENV)
    if not api_url:
        state = _home() / "machine" / "state.json"
        if state.exists():
            api_url = str(json.loads(state.read_text()).get("runtime_url") or "").strip()
    token = _first_env(_TOKEN_ENV)
    token_path = _home() / "machine" / "device-token"
    if not token and token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
    return api_url.rstrip("/"), token


class Client:
    def __init__(self, api_url: str, token: str):
        self.api_url = api_url
        self.token = token

    def _headers(self, *, browser: bool) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if browser:
            # Browser-owned routes take the device token as a bearer.
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers["X-Agents-Token"] = self.token
        return headers

    def request(self, method: str, path: str, payload=None, *, browser=False, timeout=60) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        last_detail = ""
        for attempt in range(API_RETRY_ATTEMPTS):
            request = urllib.request.Request(f"{self.api_url}{path}", data=body, method=method, headers=self._headers(browser=browser))
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                last_detail = error.read().decode(errors="replace")
                # Catalog and archive routes 503 transiently. Treating the first
                # one as fatal reported "provider unavailable" for what was a
                # server hiccup, which is a lie about which system is at fault.
                if error.code < 500 or attempt == API_RETRY_ATTEMPTS - 1:
                    raise ApiError(error.code, f"{method} {path} returned HTTP {error.code}: {last_detail[:600]}")
                time.sleep(API_RETRY_DELAY_S * (attempt + 1))
        raise ApiError(0, f"{method} {path} exhausted retries: {last_detail[:600]}")

    def served_workspace(self, session_id: str) -> dict:
        return self.request("GET", f"/api/timeline/sessions/{session_id}/workspace", browser=True)


def settlement_state(workspace: dict, run_id: str) -> tuple[bool, dict]:
    """Is the served surface done working for this run?

    Fails closed: a missing field is unsettled, never settled. Absence-from-a-set
    as the pass condition is how an oracle goes green on a state it never listed.
    """
    session = workspace.get("session") or {}
    state = session.get("session_state") or {}
    run = state.get("run") or {}
    activity = state.get("activity") or {}
    presentation = (state.get("presentation") or {}).get("primary") or {}

    observed = {
        "run_id": run.get("id"),
        "run_lifecycle": run.get("lifecycle"),
        "activity_state": activity.get("state"),
        "presentation_key": presentation.get("key"),
        "display_phase": session.get("display_phase"),
        "working_set": state.get("working_set"),
    }

    settled = (
        observed["run_id"] == run_id
        and observed["run_lifecycle"] == "ended"
        and observed["activity_state"] not in WORKING_ACTIVITY
        and observed["presentation_key"] is not None
        and observed["presentation_key"] not in WORKING_PRESENTATION_KEYS
        and observed["working_set"] == "history"
    )
    return settled, observed


class StreamWatcher(threading.Thread):
    """Subscribe to the workspace SSE stream and timestamp every frame."""

    def __init__(self, client: Client, session_id: str):
        super().__init__(daemon=True)
        self.client = client
        self.session_id = session_id
        self.frames: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.error: Exception | None = None

    def run(self) -> None:
        request = urllib.request.Request(
            f"{self.client.api_url}/api/timeline/sessions/{self.session_id}/workspace/stream",
            headers={
                "Authorization": f"Bearer {self.client.token}",
                "Accept": "text/event-stream",
                # The edge rejects default Python UAs with 403. Without this the
                # stream yields zero frames and reads as a delivery failure.
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                event: str | None = None
                for raw in response:
                    if self.stop_flag.is_set():
                        return
                    line = raw.decode(errors="replace").rstrip("\n")
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        raw_data = line.split(":", 1)[1].strip()
                        try:
                            payload = json.loads(raw_data)
                        except ValueError:
                            payload = {}
                        self.frames.put((time.monotonic(), event, payload))
                        event = None
        except Exception as exc:  # noqa: BLE001 - reported, never raised into the harness
            self.error = exc

    def drain(self) -> list[tuple[float, str | None, dict]]:
        out: list[tuple[float, str | None, dict]] = []
        while True:
            try:
                out.append(self.frames.get_nowait())
            except queue.Empty:
                return out


def _start_turn(client: Client, session_id: str, message: str) -> dict:
    path = f"/api/agents/sessions/{session_id}/turns"
    payload = {"message": message, "client_request_id": f"served-state-{uuid4()}"}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = client.request("POST", path, payload)
        if result.get("state") in {"active", "starting", "completed"} and result.get("run_id"):
            return result
        if result.get("state") != "queued":
            raise RuntimeError(f"turn was not accepted: {result}")
        time.sleep(0.5)
    raise RuntimeError("queued Console turn was not assigned a run within 30 seconds")


@contextlib.contextmanager
def armed_terminal_drop(session_id: str, enabled: bool):
    """Arm the engine's terminal drop for one session, and always disarm.

    A leaked control file would keep dropping that session's terminals after the
    run exits. Scoped to one session so it cannot reach other work, and released
    on every exit path including a raised ApiError -- which is how it leaked the
    first time.
    """
    control = _home() / "agent" / "fault-drop-runtime-events"
    if not enabled:
        yield None
        return
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(f"{session_id}:terminal_signal", encoding="utf-8")
    try:
        yield control
    finally:
        control.unlink(missing_ok=True)


def run(args: argparse.Namespace) -> dict:
    api_url, token = _defaults()
    api_url = args.api_url.rstrip("/") if args.api_url else api_url
    if not api_url or not token:
        raise RuntimeError("Longhouse API URL and device token are required")

    client = Client(api_url, token)
    marker = f"LH_SERVED_{uuid4().hex[:12]}"
    report: dict = {
        "artifact_kind": "console_served_state_e2e",
        "schema_version": 1,
        "provider": args.provider,
        "device_id": args.device_id,
        # Recorded so a failure artifact says which directory it ran in. With
        # several machines in one run the cwd differs per machine, and "it
        # failed" is not diagnosable without knowing where.
        "cwd": str(Path(args.cwd).resolve()),
        "marker": marker,
    }

    created = client.request(
        "POST",
        "/api/agents/sessions",
        {
            "provider": args.provider,
            "device_id": args.device_id,
            "cwd": str(Path(args.cwd).resolve()),
            "project": "console-served-state-e2e",
            "display_name": "Console served-state E2E",
            "launch_surface": "product-e2e",
        },
    )
    session_id = str(created["session_id"])
    report["session_id"] = session_id

    if args.drop_terminal:
        report["terminal_dropped"] = True
    with armed_terminal_drop(session_id, args.drop_terminal):
        return _observe_turn(client, args, report, session_id, marker)


def _observe_turn(
    client: Client,
    args: argparse.Namespace,
    report: dict,
    session_id: str,
    marker: str,
) -> dict:
    """Drive one turn and judge what a viewer receives."""
    # Subscribe before dispatching, or the turn races the subscription. Waiting a
    # fixed two seconds is not enough: the stream emits a workspace_changed at
    # connect, and on a slow handshake that frame lands after dispatch and passes
    # as live delivery with zero turn frames. Wait for the baseline explicitly and
    # remember its sequence so only newer frames count.
    watcher = StreamWatcher(client, args.watch_session or session_id)
    watcher.start()
    baseline_seq = -1
    saw_connect = False
    handshake_deadline = time.monotonic() + 30
    while time.monotonic() < handshake_deadline:
        for _stamp, event, payload in watcher.drain():
            if event == "connected":
                saw_connect = True
            elif event == "workspace_changed":
                baseline_seq = max(baseline_seq, int(payload.get("pubsub_seq") or 0))
        if saw_connect and baseline_seq >= 0:
            break
        if watcher.error is not None:
            raise RuntimeError(f"stream failed before the turn started: {watcher.error!r}")
        time.sleep(0.25)
    if not saw_connect:
        raise RuntimeError("stream never delivered its connect frame")
    report["baseline_pubsub_seq"] = baseline_seq

    dispatched_at = time.monotonic()
    turn = _start_turn(
        client,
        session_id,
        f"Use the shell tool to run exactly: sleep 6 && echo {marker}. " f"Then reply with exactly {marker} and nothing else.",
    )
    run_id = str(turn["run_id"])
    report["run_id"] = run_id

    # --- Oracle 1: live delivery during the turn ---------------------------
    #
    # The turn is judged finished when its reply reaches the served projection,
    # not when a local claim file flips. That keeps every signal remote, so this
    # runs anywhere with API access rather than only on the box that owns the
    # engine -- and it encodes the incident directly: the wedge was a turn whose
    # content arrived while the state kept saying working.
    first_live: float | None = None
    offsets: list[float] = []
    produced_at: float | None = None
    deadline = time.monotonic() + args.turn_timeout
    while time.monotonic() < deadline:
        for stamp, event, payload in watcher.drain():
            if event in {"connected", "heartbeat"}:
                continue
            sequence = int(payload.get("pubsub_seq") or 0)
            # Only a frame newer than the connect baseline is evidence that this
            # turn reached a viewer.
            if sequence <= baseline_seq:
                continue
            offsets.append(round(stamp - dispatched_at, 2))
            if first_live is None:
                first_live = stamp - dispatched_at
        workspace = client.served_workspace(session_id)
        if marker in json.dumps(workspace.get("projection") or {}):
            produced_at = time.monotonic()
            break
        time.sleep(1.0)

    report["marker_served"] = produced_at is not None
    report["marker_latency_s"] = round(produced_at - dispatched_at, 2) if produced_at else None

    buckets: dict[int, int] = {}
    for offset in offsets:
        buckets[int(offset)] = buckets.get(int(offset), 0) + 1
    report["first_live_frame_s"] = round(first_live, 2) if first_live is not None else None
    report["frame_count"] = len(offsets)
    report["frame_offsets"] = offsets
    # Each frame invalidates the workspace for every connected viewer.
    report["peak_frames_per_sec"] = max(buckets.values()) if buckets else 0
    report["busy_seconds"] = len(buckets)
    # --- Oracle 2: settlement after the reply is served --------------------
    #
    # The incident assertion. A reply reaching a viewer while the state axis
    # still says working is exactly what the wedge looked like: the response was
    # there when you came back, and the bar kept pulsing.
    settled_at: float | None = None
    observed: dict | None = None
    transcript_state: dict | None = None
    if produced_at is not None:
        while time.monotonic() - produced_at < args.settle_budget:
            workspace = client.served_workspace(session_id)
            settled, observed = settlement_state(workspace, run_id)
            transcript_state = ((workspace.get("session") or {}).get("session_state") or {}).get("transcript")
            if settled:
                settled_at = time.monotonic()
                break
            time.sleep(1.0)

    report["served_state_after_reply"] = observed
    report["transcript"] = transcript_state
    report["settle_latency_s"] = round(settled_at - produced_at, 2) if settled_at and produced_at else None

    watcher.stop_flag.set()
    report["stream_error"] = repr(watcher.error) if watcher.error else None

    failures: list[str] = []
    if first_live is None:
        failures.append("no live frame reached the served stream during the turn")
    if produced_at is None:
        failures.append(f"the turn reply never reached the served projection within {args.turn_timeout}s")
    elif settled_at is None:
        failures.append(f"the reply is served but the state axis still says working after {args.settle_budget}s: {observed}")
    if watcher.error is not None:
        # A stream that dies mid-turn is a delivery failure even if early frames
        # arrived; recording it without failing on it makes the oracle decorative.
        failures.append(f"workspace stream died before the turn settled: {watcher.error!r}")
    report["failures"] = failures
    report["verdict"] = "red" if failures else "green"
    return report
