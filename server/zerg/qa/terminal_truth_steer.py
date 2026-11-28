"""A steer sent from a Longhouse client enters the turn that is running, in every Helm and Console cell.

``steer`` (Helm and Console cells): the harness opens a session (Helm: ``longhouse <provider>`` in a PTY; Console:
``POST /api/agents/sessions``), sends one message from the app, and the scripted model answers it with a shell tool
call that sleeps. While the tool runs, the harness sends a second message with ``intent: "steer"``, exactly as the
web composer does when a turn is running. The scripted model holds three answers:

- ``settled``: any request after the steered reply (a neutral answer, so one steer gets one reply);
- ``steered``: any request that carries the steer marker, wherever the provider put it (Claude passes a mid-turn
  steer as hook context in a system message);
- ``closed``: a request that carries the tool's output but not the steer, which is what a provider sends when the steer
  did not enter the running turn (it was queued for the next one, or dropped);
- ``first``: the opening message, answered with the sleeping tool call.

So the turn is steered exactly when the request after the tool result carries the steer: ``steered`` is served and
``closed`` never is. The tool prints its marker through ``printf`` arguments, so the literal marker is only in the
tool's output, never in the call. Then the served transcript must show the steer once, as a user message, followed by
exactly one reply to it; and a last ordinary message must still reach the model (the steer did not leave the session
wedged).

Nothing here needs a real model: the provider binary, the Machine Agent, the Runtime Host and the delivery path are
all real, and only the model endpoint is scripted. Whether a real model obeys a steer is the model's business, not the
harness's. Cursor has no scripted model (its CLI talks only to Cursor's servers), so its cells are ``not_measurable``.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.terminal_truth_command import CASE_WAIT_S
from zerg.qa.terminal_truth_command import CONTROL_WAIT_S
from zerg.qa.terminal_truth_command import NOT_MEASURABLE
from zerg.qa.terminal_truth_command import REPLY_WAIT_S
from zerg.qa.terminal_truth_command import _helm_argv
from zerg.qa.terminal_truth_command import _launched_session
from zerg.qa.terminal_truth_command import _send_action
from zerg.qa.terminal_truth_lanes import RuntimeApi
from zerg.qa.terminal_truth_providers import PROVIDERS
from zerg.qa.terminal_truth_providers import ProviderDriver
from zerg.qa.terminal_truth_providers import _version
from zerg.qa.terminal_truth_pty import COLUMNS
from zerg.qa.terminal_truth_pty import ROWS
from zerg.qa.terminal_truth_pty import _tail_text
from zerg.qa.terminal_truth_pty import _wait_ready
from zerg.qa.terminal_truth_scenarios import _create_console_session
from zerg.qa.terminal_truth_scenarios import build_pass
from zerg.qa.terminal_truth_scenarios import stop_pass
from zerg.qa.terminal_truth_verdicts import CellSpec

SCENARIOS = ("steer",)
TOOL_SLEEP_S = 10  # how long the first turn's tool runs: the window the steer must land in
STEER_ACCEPT_S = 8.0  # the host refuses a steer (409) until it serves the turn as running; it trails the engine by ~0.5 s
STEERED_WAIT_S = TOOL_SLEEP_S + 45.0  # the steer sent to the steered reply in the model receipt
SETTLE_S = 3.0  # after the steered reply, how long a late `closed` request still counts against the cell
SERVED_WAIT_S = 30.0  # the steered reply to the served transcript showing it


@dataclass
class SteerPlan:
    spec: CellSpec
    nonce: str
    turns: list[dict[str, Any]]
    not_measured: str = ""

    @property
    def first(self) -> str:
        return f"TTRUTH{self.nonce} run the sleeping command, then report"

    @property
    def steer(self) -> str:
        return f"TTSTEER{self.nonce} also say steered"

    @property
    def end(self) -> str:
        return f"TTEND{self.nonce} last message"

    @property
    def tool_marker(self) -> str:
        return f"TTSLEPT{self.nonce}"

    def turn_id(self, name: str) -> str:
        return f"{self.spec.key}:{name}"


def plan_steer_cell(spec: CellSpec) -> SteerPlan:
    nonce = secrets.token_hex(3)
    if spec.provider in NOT_MEASURABLE:
        return SteerPlan(spec, nonce, [], not_measured=NOT_MEASURABLE[spec.provider])
    if spec.mode not in ("helm", "console"):
        return SteerPlan(spec, nonce, [], not_measured="the steer scenario runs Helm and Console cells (Shadow has no control path)")
    plan = SteerPlan(spec, nonce, [])
    # agy appends its own context after the person's message and after a tool result (antigravity_scripted_lane.py).
    scope = "any_user" if spec.provider == "antigravity" else "latest"
    # The literal marker is only in the output: printf joins its arguments, the call carries them apart.
    command = f"sleep {TOOL_SLEEP_S}; printf '%s%s\\n' TTSLEP T{nonce}"
    # First match wins and a later request carries every earlier marker, so the newest answer is listed first.
    plan.turns = [
        {"id": plan.turn_id("end"), "match": f"TTEND{nonce}", "match_scope": scope, "reply": f"TTENDOK{nonce} bye"},
        # Once the steer is answered, a later request in the turn (agy reports a background command that finished) gets a
        # neutral reply, never the steered one again: the person sees one answer to one steer.
        {"id": plan.turn_id("settled"), "match": f"TTSTEEROK{nonce}", "match_scope": "any_text", "reply": f"TTSETTLED{nonce} noted"},
        # Anywhere in the request: Claude hands a mid-turn steer to the model as hook context in a system message.
        {"id": plan.turn_id("steered"), "match": f"TTSTEER{nonce}", "match_scope": "any_text", "reply": f"TTSTEEROK{nonce} steered"},
        {"id": plan.turn_id("closed"), "match": plan.tool_marker, "match_scope": "any_input", "reply": f"TTCLOSED{nonce} done"},
        {"id": plan.turn_id("first"), "match": f"TTRUTH{nonce}", "match_scope": scope, "tool_call": {"type": "shell", "command": command}},
    ]
    return plan


def send(api: RuntimeApi, sid: str, text: str, *, intent: str) -> dict[str, Any]:
    """Send ``text`` through the app's input route and follow its receipt to delivered or failed."""

    request_id = f"tt-{secrets.token_hex(5)}"
    status, body = api.post_raw(
        f"/api/sessions/{sid}/input", {"text": text, "intent": intent, "client_request_id": request_id}, browser=True
    )
    out: dict[str, Any] = {"text": text, "intent": intent, "http_status": status, "response": body[:600]}
    if status >= 300:
        out["status"] = "rejected"
        return out
    deadline = time.monotonic() + CASE_WAIT_S
    receipt: dict[str, Any] = {}
    while time.monotonic() < deadline:
        got, rows = api.get(f"/api/sessions/{sid}/inputs?client_request_id={request_id}", browser=True)
        if got == 200:
            with contextlib.suppress(ValueError):
                found = json.loads(rows)
                receipt = found[0] if found else {}
        state = str(receipt.get("delivery_status") or receipt.get("status") or "")
        if state in ("delivered", "failed", "cancelled"):
            break
        time.sleep(0.5)
    out["status"] = str(receipt.get("delivery_status") or receipt.get("status") or "unknown")
    out["last_error"] = receipt.get("last_error")
    return out


def send_steer(api: RuntimeApi, sid: str, text: str) -> dict[str, Any]:
    """A steer, retried while the host does not yet serve the turn as running (409 ``steer_requires_active_turn``).

    The tool sleeps for ``TOOL_SLEEP_S``, so a host that never accepts the steer inside ``STEER_ACCEPT_S`` is a failure,
    not a slow start. Each try is a new request id: a refused steer was never accepted, so it is not a retry of one.
    """

    deadline = time.monotonic() + STEER_ACCEPT_S
    tries = 0
    while True:
        tries += 1
        sent = send(api, sid, text, intent="steer")
        if sent["http_status"] != 409 or "steer_requires_active_turn" not in sent["response"] or time.monotonic() > deadline:
            sent["tries"] = tries
            return sent
        time.sleep(0.3)


#: What the browser detail route says about the session's turn: the runtime columns and the state facts that decide
#: whether a parked send may drain. The machine route (``/api/agents/sessions/{id}``) drops all of these.
HOST_VIEW_SESSION_KEYS = (
    "status",
    "runtime_phase",
    "display_phase",
    "phase_started_at",
    "last_progress_at",
    "last_activity_at",
    "runtime_source",
    "terminal_state",
    "presence_state",
    "presence_tool",
    "presence_updated_at",
    "active_tool",
    "confidence",
)
HOST_VIEW_STATE_KEYS = ("run", "activity", "turn", "control", "host", "presentation")
HOST_VIEW_INPUT_KEYS = ("text", "intent", "status", "disposition", "delivery_status", "turn", "last_error", "created_at")


def host_view(api: RuntimeApi, sid: str) -> dict[str, Any]:
    """What the host believed when a send stayed queued: its served runtime state and every input receipt.

    A CI failure keeps no host log, so this is the only record of whether the host still saw the turn running (the
    send is parked until a turn boundary) or saw it end and never drained the queue.
    """

    view: dict[str, Any] = {}
    status, body = api.get(f"/api/timeline/sessions/{sid}", browser=True)
    view["session_http_status"] = status
    with contextlib.suppress(ValueError):
        detail = json.loads(body) if status == 200 else {}
        if isinstance(detail, dict):
            view["session"] = {k: detail.get(k) for k in HOST_VIEW_SESSION_KEYS}
            state = detail.get("session_state")
            if isinstance(state, dict):
                view["session_state"] = {k: state.get(k) for k in HOST_VIEW_STATE_KEYS}
    status, body = api.get(f"/api/sessions/{sid}/inputs", browser=True)
    view["inputs_http_status"] = status
    with contextlib.suppress(ValueError):
        rows = json.loads(body) if status == 200 else []
        view["inputs"] = [{k: r.get(k) for k in HOST_VIEW_INPUT_KEYS} for r in rows if isinstance(r, dict)]
    return view


def served_rows(api: RuntimeApi, sid: str) -> list[dict[str, Any]]:
    status, body = api.get(f"/api/agents/sessions/{sid}/events?limit=1000")
    if status != 200:
        return []
    with contextlib.suppress(ValueError):
        events = json.loads(body).get("events")
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    return []


def judge_served(plan: SteerPlan, rows: Sequence[dict[str, Any]]) -> tuple[bool, str]:
    """The steer shows once, as a user message, and the one reply to it is served after it."""

    steer_at = [i for i, r in enumerate(rows) if r.get("role") == "user" and f"TTSTEER{plan.nonce}" in str(r.get("content_text") or "")]
    reply_at = [
        i for i, r in enumerate(rows) if r.get("role") == "assistant" and f"TTSTEEROK{plan.nonce}" in str(r.get("content_text") or "")
    ]
    ok = len(steer_at) == 1 and len(reply_at) == 1 and reply_at[0] > steer_at[0]
    return ok, f"steer user rows={len(steer_at)} steered reply rows={len(reply_at)} of {len(rows)} events"


def _wait(predicate: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return predicate()


def run_steer_cell_in(pas: Any, plan: SteerPlan, *, ready_timeout: float, drivers: dict[str, ProviderDriver]) -> dict[str, Any]:
    spec = plan.spec
    provider, mode = spec.provider, spec.mode
    binary = pas.binaries[provider]
    out: dict[str, Any] = {
        "cell": spec.key,
        "provider": provider,
        "mode": mode,
        "scenario": spec.scenario,
        "nonce": plan.nonce,
        "provider_version": _version(binary),
        "engine_version": _version(str(pas.engine)),
    }
    workspace = pas.root / "w" / provider / f"{mode}-{plan.nonce}"
    workspace.mkdir(parents=True)
    recording, timing = pas.root / f"terminal-{provider}-{spec.scenario}.raw", pas.root / f"terminal-{provider}-{spec.scenario}.timing"
    api = RuntimeApi(pas.host.url, pas.host.token, workspace=workspace, device_id=pas.host.device_id)
    session: ProviderPtySession | None = None
    checks: list[tuple[str, str, bool, str]] = []  # (stage, what it checks, ok, detail)
    sends: list[dict[str, Any]] = []
    started = time.monotonic()
    served = pas.model.served_requests
    try:
        if mode == "helm":
            session = ProviderPtySession.start(
                argv=_helm_argv(pas, provider, workspace, binary),
                cwd=workspace,
                env=pas.provider_environments[provider],
                terminal_path=recording,
                timing_path=timing,
                rows=ROWS,
                columns=COLUMNS,
            )
            _wait_ready(session, recording, timing, drivers[provider].ready_text, ready_timeout, drivers[provider].answers)
            sid: str | None = None
            action: dict[str, Any] | None = None
            waited = time.monotonic()
            while time.monotonic() < waited + CONTROL_WAIT_S and session.alive():
                sid = sid or _launched_session(api)
                if sid is not None:
                    action = _send_action(api, sid)
                    if (action or {}).get("state") == "available":
                        break
                time.sleep(0.5)
            if sid is None or (action or {}).get("state") != "available":
                raise RuntimeError(f"the Runtime Host never offered send_input within {CONTROL_WAIT_S:.0f}s (session {sid})")
            first = send(api, sid, plan.first, intent="auto")
        else:
            sid = str(_create_console_session(api, provider, pas.shipper.machine_name, workspace)["session_id"])
            api.pinned_sessions = [sid]
            status, body = api.post_raw(
                f"/api/agents/sessions/{sid}/turns", {"message": plan.first, "client_request_id": f"tt-{secrets.token_hex(4)}"}
            )
            first = {
                "text": plan.first,
                "intent": "turn",
                "http_status": status,
                "response": body[:600],
                "status": "delivered" if status < 300 else "rejected",
            }
        out["session_id"] = sid
        sends.append(first)
        tool_called = _wait(lambda: served([plan.turn_id("first")]) >= 1, REPLY_WAIT_S)
        checks.append(
            ("first_turn", "the first message reaches the model, which starts the sleeping tool", tool_called, first.get("status", ""))
        )
        if not tool_called:
            raise RuntimeError("the model never received the first message")

        steer = send_steer(api, sid, plan.steer)
        sends.append(steer)
        detail = f"status={steer.get('status')} http={steer['http_status']} tries={steer['tries']}"
        if steer.get("status") != "delivered":
            detail += f" error={(steer.get('last_error') or steer['response'] or '')[:240]}"
        checks.append(
            (
                "steer_accepted",
                "the host accepts the steer while the tool runs and the provider takes it",
                steer.get("status") == "delivered",
                detail,
            )
        )

        steered = _wait(lambda: served([plan.turn_id("steered")]) >= 1, STEERED_WAIT_S)
        time.sleep(SETTLE_S)
        closed = served([plan.turn_id("closed")])
        answers = served([plan.turn_id("steered")])
        checks.append(("steer_received", "the model receives the steer", steered, f"steered={answers}"))
        checks.append(
            (
                "in_turn",
                "the steer entered the running turn (no request closed the turn without it)",
                steered and closed == 0,
                f"closed={closed}: a request carried the tool's output without the steer" if closed else "closed=0",
            )
        )

        rows: list[dict[str, Any]] = []

        def shown() -> bool:
            nonlocal rows
            rows = served_rows(api, sid)
            return judge_served(plan, rows)[0]

        ok = _wait(shown, SERVED_WAIT_S)
        checks.append(("served", "the served transcript shows the steer once, then one reply to it", ok, judge_served(plan, rows)[1]))

        end = send(api, sid, plan.end, intent="auto")
        sends.append(end)
        ended = _wait(lambda: served([plan.turn_id("end")]) >= 1, REPLY_WAIT_S)
        checks.append(("freed", "a message after the steer still reaches the model (the session was freed)", ended, end.get("status", "")))
        if not ended:
            out["freed_diagnosis"] = host_view(api, sid)
    except Exception as exc:  # noqa: BLE001 - becomes the cell's failure reason
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            session.close()
    out["checks"] = [{"stage": stage, "check": name, "ok": ok, "detail": detail} for stage, name, ok, detail in checks]
    out["sends"] = sends
    failed = next(((stage, name, detail) for stage, name, ok, detail in checks if not ok), None)
    out["outcome"] = "pass" if failed is None and "error" not in out and checks else "fail"
    if out["outcome"] == "fail":
        out["first_failed_stage"] = failed[0] if failed else "harness"
        out["reason"] = "; ".join(x for x in (out.get("error"), f"{failed[1]}: {failed[2]}" if failed else None) if x)
        if recording.exists():
            out["last_screen"] = _tail_text(recording)
    out["cell_wall_s"] = time.monotonic() - started
    return out


def run_steer_suite(
    specs: Sequence[CellSpec],
    *,
    engine: Path,
    longhouse_cli: Path | None = None,
    keep: bool = False,
    ready_timeout: float = 120.0,
    drivers: dict[str, ProviderDriver] | None = None,
    host_environment: dict[str, str] | None = None,
    engine_environment: dict[str, str] | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> list[dict[str, Any]]:
    """Run each steer cell in a pass of its own (one host, engine, HOME and scripted model per cell)."""

    drivers = drivers or PROVIDERS
    cli = longhouse_cli or (Path(shutil.which("longhouse")) if shutil.which("longhouse") else None)
    results: list[dict[str, Any]] = []
    for spec in specs:
        plan = plan_steer_cell(spec)
        row = {"cell": spec.key, "provider": spec.provider, "mode": spec.mode, "scenario": spec.scenario}
        if plan.not_measured:
            results.append({**row, "outcome": "not_measurable", "reason": plan.not_measured})
            continue
        dump_dir = Path(tempfile.mkdtemp(prefix="tt-requests-"))
        previous = os.environ.get("TT_DUMP_REQUESTS_DIR")
        os.environ["TT_DUMP_REQUESTS_DIR"] = str(dump_dir)
        try:
            try:
                pas = build_pass(
                    [spec],
                    {spec.key: plan},
                    engine=engine,
                    cli=cli,
                    host_environment=host_environment,
                    engine_environment=engine_environment,
                    log=log,
                )  # type: ignore[dict-item]
            except Exception as exc:  # noqa: BLE001
                results.append({**row, "outcome": "fail", "reason": f"pass setup failed: {type(exc).__name__}: {exc}"})
                continue
            try:
                log(f"running {spec.key} {spec.scenario}")
                results.append(run_steer_cell_in(pas, plan, ready_timeout=ready_timeout, drivers=drivers))
            finally:
                if keep:
                    shutil.copytree(dump_dir, pas.root / "model-requests", dirs_exist_ok=True)
                stop_pass(pas, keep)
        finally:
            if previous is None:
                os.environ.pop("TT_DUMP_REQUESTS_DIR", None)
            else:
                os.environ["TT_DUMP_REQUESTS_DIR"] = previous
            shutil.rmtree(dump_dir, ignore_errors=True)
    return results


def render_steer(results: Sequence[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        lines.append(f"{r['cell']}:{r['scenario']}  {r['outcome'].upper()}  {r.get('reason') or ''}".rstrip())
        for check in r.get("checks") or []:
            lines.append(f"  {'ok  ' if check['ok'] else 'FAIL'} {check['check']}: {check['detail']}")
    return "\n".join(lines)
