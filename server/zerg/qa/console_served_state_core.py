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
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY_S = 2.0
SESSION_CREATE_TIMEOUT_S = 45.0


class ApiError(RuntimeError):
    """An HTTP failure from the Longhouse API, carrying its status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


USER_AGENT = "longhouse-console-served-state-e2e/1"
MARKER_WORDS = (
    "ALDER",
    "BIRCH",
    "CEDAR",
    "DUNE",
    "EMBER",
    "FERN",
    "GROVE",
    "HARBOR",
    "IVORY",
    "JUNIPER",
    "KITE",
    "LANTERN",
    "MAPLE",
    "NOVA",
    "ORCHID",
    "RIVER",
)

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


def event_text(event: Mapping[str, object]) -> str:
    """Read text fields only; structured content and metadata are not replies."""
    text = event.get("content_text")
    if text is None:
        text = event.get("content")
    return text if isinstance(text, str) else ""


def assistant_marker_events(events: Iterable[object], marker: str, *, default_origin: str | None = None) -> list[dict]:
    """Share the durable assistant predicate between archive and served proofs.

    Archive-only endpoints may supply an explicit durable default. A served
    projection must carry its origin: provisional output is never final proof.
    Keep all matches, including duplicates, so callers can reject multiplicity.
    """
    return [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("role") == "assistant"
        and event.get("event_origin", default_origin) == "durable"
        and not event.get("tool_name")
        and bool(marker)
        and marker in event_text(event)
    ]


def assistant_marker_evidence(workspace: dict, session_id: str, marker: str) -> dict:
    projection = workspace.get("projection") or {}
    matches = assistant_marker_events(
        (
            item.get("event")
            for item in projection.get("items") or []
            if isinstance(item, dict) and item.get("kind") == "event" and item.get("session_id") == session_id
        ),
        marker,
    )
    counts = [event_text(event).count(marker) for event in matches]
    event_ids = [event.get("id") for event in matches]
    # A partial page cannot exclude a duplicate that was not served in it.
    complete_page = projection.get("has_more") is False and projection.get("page_offset") == 0
    exact = len(matches) == 1 and counts == [1] and bool(event_ids[0]) and complete_page
    return {
        "event_ids": event_ids,
        "event_count": len(matches),
        "marker_counts": counts,
        "marker_count": sum(counts),
        "complete_page": complete_page,
        "exactly_once": exact,
    }


class StreamWatcher(threading.Thread):
    """Subscribe to the workspace SSE stream and timestamp every frame."""

    def __init__(self, client: Client, session_id: str):
        super().__init__(daemon=True)
        self.client = client
        self.session_id = session_id
        self.frames: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.error: Exception | None = None
        self._response = None
        self._socket: socket.socket | None = None

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
            with urllib.request.urlopen(request, timeout=30) as response:
                self._response = response
                # Closing a buffered reader while another thread reads it can
                # block. Shut down its transport first to release that reader.
                self._socket = response.fp.raw._sock
                event: str | None = None
                if self.stop_flag.is_set():
                    return
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
                if not self.stop_flag.is_set():
                    self.error = RuntimeError("workspace stream ended before observation finished")
        except Exception as exc:  # noqa: BLE001 - reported, never raised into the harness
            if not self.stop_flag.is_set():
                self.error = exc
        finally:
            self._socket = None
            self._response = None

    def close(self) -> None:
        self.stop_flag.set()
        transport = self._socket
        if transport is not None:
            with contextlib.suppress(OSError):
                transport.shutdown(socket.SHUT_RDWR)
        response = self._response
        if response is not None:
            response.close()
        self.join(timeout=5)
        if self.is_alive() and self.error is None:
            self.error = RuntimeError("workspace stream did not stop after observation")

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


def _create_session(client: Client, payload: dict, *, timeout: float = SESSION_CREATE_TIMEOUT_S) -> dict:
    """Wait for the disposable Machine Agent to advertise its adapter.

    The engine creates its wake socket before the control WebSocket has
    registered provider capabilities. A live proof that starts in that small
    window should wait for the same machine, not classify the registration
    race as a served-state failure.
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            return client.request("POST", "/api/agents/sessions", payload)
        except ApiError as exc:
            if exc.status != 409 or "adapter_unavailable" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


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


def _served_run_retirement(
    api_url: str,
    token: str,
    client: Client,
    session_id: str,
    report: dict,
) -> dict[str, object]:
    """Prove the served run is terminal before retiring its session."""

    run_id = str(report.get("run_id") or "").strip()
    if not run_id:
        return {
            "retired": True,
            "active_run_count": 0,
            "session_id": session_id,
            "expected_run_id": None,
            "reason": "no_run_started",
        }

    termination: dict[str, object] | None = None
    if report.get("verdict") != "green":
        try:
            termination = client.request("POST", f"/api/agents/sessions/{session_id}/terminate-live")
        except Exception as exc:  # noqa: BLE001 - retirement evidence must survive cleanup failures
            termination = {
                "status": "fail",
                "error": f"{type(exc).__name__}: {exc}",
            }

    try:
        # Reuse the same served-projection oracle as provider Console
        # qualification. The synthetic claim carries only the run identity
        # available to this surface; it is enough to prove the served run
        # reached a terminal lifecycle without coupling this oracle to a
        # provider-specific claim file.
        from zerg.qa.provider_console_lifecycle import _wait_served_run_retirement

        inventory = _wait_served_run_retirement(
            api_url,
            token,
            session_id,
            [{"state": "terminal", "session_id": session_id, "run_id": run_id}],
        )
    except Exception as exc:  # noqa: BLE001 - cleanup evidence must fail closed
        inventory = {
            "retired": False,
            "active_run_count": None,
            "session_id": session_id,
            "expected_run_id": run_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if termination is not None:
        inventory["termination"] = termination
    return inventory


def _retire_session(
    api_url: str,
    token: str,
    client: Client,
    session_id: str | None,
    *,
    provider: str,
    report: dict,
) -> dict[str, object]:
    """Retire one created QA session and retain every cleanup predicate."""

    if not session_id:
        return {
            "status": "pass",
            "session_id": None,
            "hidden": True,
            "archived": True,
            "present_in_served_inventory": False,
            "served_run_retired": True,
            "served_run_inventory": {
                "retired": True,
                "active_run_count": 0,
                "session_id": None,
                "reason": "session_not_created",
            },
        }

    served_run_inventory = _served_run_retirement(api_url, token, client, session_id, report)
    from zerg.qa.live_session_toolkit import retire_qualification_session

    receipt = retire_qualification_session(
        api_url,
        token,
        session_id,
        provider=provider,
        project="console-served-state-e2e",
    )
    served_run_retired = (
        served_run_inventory.get("retired") is True
        and served_run_inventory.get("session_id") == session_id
        and served_run_inventory.get("active_run_count") == 0
    )
    receipt["served_run_inventory"] = served_run_inventory
    receipt["served_run_retired"] = served_run_retired
    if not served_run_retired:
        receipt["status"] = "fail"
    return receipt


def run(
    args: argparse.Namespace,
    *,
    on_session_created: Callable[[str], None] | None = None,
) -> dict:
    api_url, token = _defaults()
    api_url = args.api_url.rstrip("/") if args.api_url else api_url
    if not api_url or not token:
        raise RuntimeError("Longhouse API URL and device token are required")

    client = Client(api_url, token)
    # Human-readable words survive screenshot OCR better than ambiguous hex
    # glyphs. Session/run identity still provides the binding, not this nonce.
    marker = "LH_SERVED_" + "_".join(MARKER_WORDS[int(nibble, 16)] for nibble in uuid4().hex[:6])
    report: dict = {
        "artifact_kind": "console_served_state_e2e",
        "schema_version": 2,
        "provider": args.provider,
        "device_id": args.device_id,
        # Recorded so a failure artifact says which directory it ran in. With
        # several machines in one run the cwd differs per machine, and "it
        # failed" is not diagnosable without knowing where.
        "cwd": str(Path(args.cwd).resolve()),
        "marker": marker,
    }

    create_payload = {
        "provider": args.provider,
        "device_id": args.device_id,
        "cwd": str(Path(args.cwd).resolve()),
        "project": "console-served-state-e2e",
        "display_name": "Console served-state E2E",
        "launch_surface": "product-e2e",
    }
    model = str(getattr(args, "model", "") or "").strip()
    if model:
        create_payload["model"] = model
    created = _create_session(client, create_payload)
    session_id = str(created["session_id"])
    report["session_id"] = session_id
    if on_session_created is not None:
        on_session_created(session_id)

    if args.drop_terminal:
        report["terminal_dropped"] = True
    try:
        with armed_terminal_drop(session_id, args.drop_terminal):
            result = _observe_turn(client, args, report, session_id, marker)
    except Exception as exc:  # noqa: BLE001 - return a typed failure with cleanup evidence
        result = report
        result.setdefault("failures", []).append(f"{type(exc).__name__}: {exc}")
        result["verdict"] = "red"

    cleanup = _retire_session(
        api_url,
        token,
        client,
        session_id,
        provider=args.provider,
        report=result,
    )
    result["cleanup_receipt"] = cleanup
    if cleanup.get("status") != "pass":
        result.setdefault("failures", []).append("qualification session cleanup did not satisfy its required contract")
        result["verdict"] = "red"
    return result


def _observe_turn(
    client: Client,
    args: argparse.Namespace,
    report: dict,
    session_id: str,
    marker: str,
) -> dict:
    """Drive one turn and judge the served API, not client rendering."""
    watcher = StreamWatcher(client, args.watch_session or session_id)
    watcher.start()
    try:
        return _observe_watched_turn(client, args, report, session_id, marker, watcher)
    finally:
        watcher.close()
        report["stream_error"] = repr(watcher.error) if watcher.error else None
        if watcher.error is not None and "failures" in report:
            report["failures"].append(f"workspace stream failed: {watcher.error!r}")
            report["verdict"] = "red"


def _observe_watched_turn(
    client: Client,
    args: argparse.Namespace,
    report: dict,
    session_id: str,
    marker: str,
    watcher: StreamWatcher,
) -> dict:
    # Subscribe before dispatch and exclude the connect snapshot from delivery.
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
    if not saw_connect or baseline_seq < 0:
        raise RuntimeError("stream never delivered its connect frame and baseline workspace")
    report["baseline_pubsub_seq"] = baseline_seq

    # The final marker is absent from both the prompt and the tool output.
    # Only the assistant performs the concatenation, after the delayed tool.
    split = len(marker) // 2
    message = (
        "Use the shell tool to run exactly: sleep 6. "
        f'Then concatenate the prefix "{marker[:split]}" and suffix "{marker[split:]}" '
        "and reply with only the concatenated result, once, and nothing else."
    )
    dispatched_at = time.monotonic()
    turn = _start_turn(client, session_id, message)
    received_at = time.monotonic()
    run_id = str(turn["run_id"])
    report["run_id"] = run_id
    report["turn_receipt"] = {key: turn.get(key) for key in ("turn_id", "run_id", "state")}
    timing = {
        "clock": "monotonic",
        "surface": "served_workspace_api",
        "dispatch_started_at_s": dispatched_at,
        "dispatch_receipt_at_s": received_at,
        "first_assistant_served_at_s": None,
        "settled_at_s": None,
        "poll_interval_s": 1.0,
    }
    report["timing"] = timing
    report["dispatch_latency_s"] = round(received_at - dispatched_at, 3)

    first_live: float | None = None
    offsets: list[float] = []
    produced_at: float | None = None
    settled_at: float | None = None
    samples: list[dict] = []
    report["workspace_samples"] = samples
    deadline = received_at + args.turn_timeout
    duplicate_seen = False
    while time.monotonic() < deadline:
        for stamp, event, payload in watcher.drain():
            if event in {"connected", "heartbeat"} or stamp < dispatched_at:
                continue
            if int(payload.get("pubsub_seq") or 0) <= baseline_seq:
                continue
            offsets.append(round(stamp - dispatched_at, 3))
            if first_live is None:
                first_live = stamp - dispatched_at

        sample_started_at = time.monotonic()
        workspace = client.served_workspace(session_id)
        sample_received_at = time.monotonic()
        evidence = assistant_marker_evidence(workspace, session_id, marker)
        settled, observed = settlement_state(workspace, run_id)
        transcript = ((workspace.get("session") or {}).get("session_state") or {}).get("transcript") or {}
        sample = {
            "request_started_at_s": sample_started_at,
            "response_received_at_s": sample_received_at,
            "assistant_marker": evidence,
            "served_state": observed,
            "transcript": transcript,
        }
        samples.append(sample)
        duplicate_seen = duplicate_seen or evidence["event_count"] > 1 or evidence["marker_count"] > 1
        if produced_at is None and evidence["exactly_once"] and sample_received_at <= deadline:
            produced_at = sample_received_at
            timing["first_assistant_served_at_s"] = produced_at
            report["first_assistant_marker"] = evidence
            # Poll timestamps bound detection; they are not token-generation or
            # rendered-content timestamps. Keep the preceding sample as well.
            report["first_assistant_sample_index"] = len(samples) - 1
            deadline = produced_at + args.settle_budget
        run = ((workspace.get("session") or {}).get("session_state") or {}).get("run") or {}
        if run.get("id") == run_id and run.get("lifecycle") == "ended" and run.get("end_reason") in {"failed", "cancelled"}:
            report["terminal_failure"] = {
                "run_id": run_id,
                "end_reason": run["end_reason"],
                "observed_at_s": sample_received_at,
            }
            break
        if (
            produced_at is not None
            and len(samples) - 1 > report["first_assistant_sample_index"]
            and sample_received_at <= deadline
            and evidence["exactly_once"]
            and not duplicate_seen
            and settled
            and transcript.get("convergence") == "current"
        ):
            settled_at = sample_received_at
            timing["settled_at_s"] = settled_at
            break
        time.sleep(1.0)

    final = samples[-1] if samples else {}
    final_evidence = final.get("assistant_marker") or {}
    report["marker_served"] = produced_at is not None
    report["marker_latency_s"] = round(produced_at - dispatched_at, 3) if produced_at is not None else None
    report["assistant_marker_after_settlement"] = final_evidence
    report["assistant_reply_complete"] = settled_at is not None
    report["duplicate_assistant_marker_seen"] = duplicate_seen
    report["served_state_after_reply"] = final.get("served_state")
    report["transcript"] = final.get("transcript")
    report["settle_latency_s"] = round(settled_at - produced_at, 3) if settled_at is not None and produced_at is not None else None

    buckets: dict[int, int] = {}
    for offset in offsets:
        buckets[int(offset)] = buckets.get(int(offset), 0) + 1
    # These are stream invalidations, not first rendered assistant content.
    report["first_live_frame_s"] = round(first_live, 3) if first_live is not None else None
    report["frame_count"] = len(offsets)
    report["frame_offsets"] = offsets
    report["peak_frames_per_sec"] = max(buckets.values()) if buckets else 0
    report["busy_seconds"] = len(buckets)

    failures: list[str] = []
    if first_live is None:
        failures.append("no live frame reached the served stream during the turn")
    if report.get("terminal_failure"):
        failures.append(f"the current provider turn ended unsuccessfully: {report['terminal_failure']}")
    elif produced_at is None:
        failures.append(f"exactly one durable assistant reply was not served within {args.turn_timeout}s: {final_evidence}")
    elif settled_at is None:
        failures.append(
            f"assistant reply and current run did not settle completely within {args.settle_budget}s: "
            f"{final_evidence}, state={final.get('served_state')}, transcript={final.get('transcript')}"
        )
    if duplicate_seen:
        failures.append("the served assistant reply contained duplicate marker output")
    report["failures"] = failures
    report["verdict"] = "red" if failures else "green"
    return report
