"""Questions: Claude's AskUserQuestion dialog answered from a Longhouse client instead of the terminal.

- ``question``: the scripted model's first reply is one AskUserQuestion call (one question, two options whose labels carry the
  cell's nonce). The harness waits until the Runtime Host lists it as a pending pause request the app may answer
  (``can_respond``), answers it through the app's route choosing option 2, and checks that Claude's own transcript has a
  tool_result for that call carrying the label, that the scripted model got a follow-up request carrying it (the dialog
  closed and Claude went on), that the turn ended (the model's closing reply is in the transcript), and that the pause
  request resolved.
- ``question_steer``: the same, but the harness answers by sending a message through the session's normal input route, and
  the tool_result must carry that text as the user's own response.

Only Claude Helm can pass: Longhouse's Claude Code mod is what races the dialog against an answer from the app. Every other
cell is ``not_measured`` with the reason.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import time
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.terminal_truth_lanes import TRANSCRIPT_ROOTS
from zerg.qa.terminal_truth_lanes import RuntimeApi
from zerg.qa.terminal_truth_providers import PROVIDERS
from zerg.qa.terminal_truth_providers import ProviderDriver
from zerg.qa.terminal_truth_providers import _version
from zerg.qa.terminal_truth_pty import COLUMNS
from zerg.qa.terminal_truth_pty import ROWS
from zerg.qa.terminal_truth_pty import _tail_text
from zerg.qa.terminal_truth_pty import _wait_ready
from zerg.qa.terminal_truth_scenarios import build_pass
from zerg.qa.terminal_truth_scenarios import stop_pass
from zerg.qa.terminal_truth_verdicts import CellSpec

SCENARIOS = ("question", "question_steer")
NOT_APPLICABLE = "only Claude Helm's AskUserQuestion can be answered from Longhouse (Longhouse's Claude Code mod races the dialog)"
QUESTION_WAIT_S = 60.0  # prompt to a pending pause request the app may answer
ANSWER_WAIT_S = 60.0  # answer to the turn's closing reply in Claude's transcript
# Launch to the Runtime Host offering send_input: the live-control lease the Machine Agent reports for the session. Every
# remote answer needs it, so the prompt waits for it, as a client that shows a send box only when sending works does.
CONTROL_WAIT_S = 90.0


@dataclass
class QuestionPlan:
    spec: CellSpec
    nonce: str
    prompt: str = ""
    question: str = ""
    labels: tuple[str, str] = ("", "")
    response: str = ""  # question_steer: the free text the owner sends
    done: str = ""  # the closing reply's marker
    turns: list[dict[str, Any]] = field(default_factory=list)
    not_measured: str = ""

    @property
    def chosen(self) -> str:
        """What the tool_result must carry: option 2's label, or the owner's free text."""

        return self.response or self.labels[1]


def plan_question_cell(spec: CellSpec) -> QuestionPlan:
    nonce = secrets.token_hex(3)
    if (spec.provider, spec.mode) != ("claude", "helm"):
        return QuestionPlan(spec, nonce, not_measured=NOT_APPLICABLE)
    labels = (f"TTQ{nonce}1", f"TTQ{nonce}2")
    response = f"TTQ{nonce}R neither, do it my way" if spec.scenario == "question_steer" else ""
    plan = QuestionPlan(
        spec,
        nonce,
        prompt=f"TTRUTH{nonce} please ask me the question",
        question="Which option should the test take?",
        labels=labels,
        response=response,
        done=f"TTDONE{nonce}",
    )
    question = {
        "question": plan.question,
        "header": "TT pick",
        "multiSelect": False,
        "options": [{"label": labels[0], "description": "the first option"}, {"label": labels[1], "description": "the second option"}],
    }
    plan.turns = [
        # Matched first: the follow-up request, whose latest message is the tool_result carrying the answer.
        {"id": f"{spec.key}:b", "match": plan.chosen, "match_scope": "latest", "reply": f"{plan.done} got the answer"},
        {
            "id": f"{spec.key}:a",
            "match": f"TTRUTH{nonce}",
            "match_scope": "latest",
            "tool_call": {"name": "AskUserQuestion", "arguments": {"questions": [question]}},
        },
    ]
    return plan


def _helm_argv(pas: Any, workspace: Path, binary: str) -> list[str]:
    return [str(pas.cli), "claude", "--cwd", str(workspace), "--url", pas.proxy.url, "--token", pas.host.token, "--claude-bin", binary]


def _pending_question(api: RuntimeApi, nonce: str) -> tuple[str, dict[str, Any]] | None:
    """(session id, pause request) for this cell's question, as the app lists it."""

    for sid in api.session_ids(fresh=True):
        status, body = api.get(f"/api/sessions/{sid}/pause-requests", browser=True)
        if status != 200:
            continue
        for request in json.loads(body).get("requests") or []:
            if f"TTQ{nonce}" in json.dumps(request):
                return sid, request
    return None


def _launched_session(api: RuntimeApi) -> str | None:
    ids = api.session_ids(fresh=True)
    return next((sid for sid in ids if sid in api.launched_sessions), None)


def _send_action(api: RuntimeApi, sid: str) -> dict[str, Any] | None:
    """The served ``send_input`` action of the session (state ``available`` when the app may send or answer)."""

    status, body = api.get(f"/api/agents/sessions/{sid}/state-diagnostics")
    if status != 200:
        return None
    payload = json.loads(body)
    shadow = payload.get("shadow") if payload.get("served_path") == "canonical_session_detail" else None
    control = (shadow or {}).get("control") if isinstance(shadow, dict) else None
    actions = control.get("actions") if isinstance(control, dict) else None
    entry = actions.get("send_input") if isinstance(actions, dict) else None
    return entry if isinstance(entry, dict) else None


def _pause_request(api: RuntimeApi, sid: str, pause_id: str) -> dict[str, Any] | None:
    for status_filter in ("resolved", "rejected", "failed", "expired", "pending"):
        status, body = api.get(f"/api/sessions/{sid}/pause-requests?status_filter={status_filter}", browser=True)
        if status == 200:
            match = next((r for r in json.loads(body).get("requests") or [] if r.get("id") == pause_id), None)
            if match is not None:
                return match
    return None


def _control_snapshot(pas: Any, workspace: Path) -> dict[str, Any]:
    import subprocess

    status_file = pas.environment.get("LONGHOUSE_HOME", "")
    engine_status = Path(status_file) / "agent" / "engine-status.json" if status_file else None
    snapshot: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        status = json.loads(engine_status.read_text()) if engine_status else {}
        snapshot["managed_sessions"] = status.get("managed_sessions")
        snapshot["machine_evidence_control"] = (status.get("machine_evidence") or {}).get("control")
    with contextlib.suppress(Exception):
        ps = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,lstart=,command="], capture_output=True, text=True, timeout=10).stdout
        snapshot["processes"] = [line for line in ps.splitlines() if str(pas.root) in line or "claude-channel" in line or "claude" in line][
            :40
        ]
    return snapshot


def transcript_rows(home: Path, config_dir: str | None, nonce: str) -> list[dict[str, Any]]:
    """Every row of the Claude transcript file this cell wrote (the one that carries its prompt)."""

    roots = [Path(config_dir) / "projects"] if config_dir else []
    roots += [home / root for root in TRANSCRIPT_ROOTS["claude"]]
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            text = path.read_text(errors="replace")
            if f"TTRUTH{nonce}" not in text:
                continue
            rows = []
            for line in text.splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return rows
    return []


def _blocks(row: dict[str, Any]) -> list[dict[str, Any]]:
    content = (row.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def tool_use_ids(rows: Sequence[dict[str, Any]], name: str) -> list[str]:
    return [b["id"] for r in rows for b in _blocks(r) if b.get("type") == "tool_use" and b.get("name") == name and b.get("id")]


def tool_result_text(rows: Sequence[dict[str, Any]], tool_use_id: str) -> str | None:
    """The tool_result content Claude recorded for ``tool_use_id`` (as text), or None when it has none."""

    for row in rows:
        for block in _blocks(row):
            if block.get("type") == "tool_result" and block.get("tool_use_id") == tool_use_id and block.get("is_error") is not True:
                content = block.get("content")
                return content if isinstance(content, str) else json.dumps(content)
    return None


def assistant_text(rows: Sequence[dict[str, Any]]) -> str:
    return "\n".join(
        str(b.get("text") or "")
        for r in rows
        if (r.get("message") or {}).get("role") == "assistant"
        for b in _blocks(r)
        if b.get("type") == "text"
    )


def rendered_question(plan: QuestionPlan, request: dict[str, Any]) -> dict[str, Any] | None:
    """The projection's question the app renders: this cell's question text with both option labels, or None."""

    for question in request.get("questions") or []:
        if not isinstance(question, dict) or question.get("question") != plan.question:
            continue
        labels = [str(o.get("label")) for o in question.get("options") or [] if isinstance(o, dict)]
        if labels == list(plan.labels):
            return question
    return None


def user_texts(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Every user message's own text (not tool results): what Claude recorded as the person typing."""

    out: list[str] = []
    for row in rows:
        message = row.get("message") or {}
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        out += [str(b.get("text") or "") for b in _blocks(row) if b.get("type") == "text"]
    return out


def judge_question(plan: QuestionPlan, obs: dict[str, Any]) -> list[tuple[str, bool, str]]:
    """(check, ok, detail) for each oracle, in the order a working path passes them."""

    request = obs.get("pause_request") or {}
    rows = obs.get("transcript_rows") or []
    calls = tool_use_ids(rows, "AskUserQuestion")  # the projection the host serves does not carry the provider's id
    tool_use = calls[0] if len(calls) == 1 else ""
    result = tool_result_text(rows, tool_use) if tool_use else None
    resolved = obs.get("pause_request_after") or {}
    send = obs.get("send_action") or {}
    # The answer and nothing else: option 2 and not option 1 (a steer names neither), and a steer's text is the
    # answer only, never also a turn of its own.
    unchosen = [plan.labels[0]] if not plan.response else list(plan.labels)
    carries = result is not None and plan.chosen in result and not any(label in result for label in unchosen)
    typed = sum(text.count(plan.chosen) for text in user_texts(rows))
    return [
        (
            "the host offers send_input (live-control lease)",
            send.get("state") == "available",
            json.dumps(send)[:300] if send else "no send_input action served",
        ),
        (
            "the app is shown the question and both options",
            rendered_question(plan, request) is not None,
            json.dumps(request.get("questions"))[:300] if request else "no pause request",
        ),
        (
            "the host lists the question as answerable",
            bool(request) and request.get("can_respond") is True,
            f"pause request {request.get('id')} kind={request.get('kind')} tool={request.get('tool_name')} can_respond={request.get('can_respond')}"
            if request
            else "no pending pause request carrying the cell's nonce",
        ),
        ("the answer was accepted", bool(obs.get("answer_ok")), str(obs.get("answer_response"))[:300]),
        (
            "Claude's transcript has the call's tool_result carrying the answer",
            carries and typed == 0,
            f"AskUserQuestion calls {calls}; tool_result for {tool_use or '?'}: {(result or 'none')[:300]}; "
            f"also typed as a user turn {typed} time(s)",
        ),
        (
            "the scripted model got the follow-up request carrying the answer",
            int(obs.get("followup_served") or 0) >= 1,
            f"requests served for {plan.spec.key}:b = {obs.get('followup_served')}",
        ),
        ("the turn ended (the closing reply is in the transcript)", plan.done in assistant_text(rows), plan.done),
        (
            "the pause request resolved",
            resolved.get("status") == "resolved",
            f"status={resolved.get('status')} resolved_at={resolved.get('resolved_at')}" if resolved else "not found after the answer",
        ),
    ]


def run_question_cell_in(pas: Any, plan: QuestionPlan, *, ready_timeout: float, drivers: dict[str, ProviderDriver]) -> dict[str, Any]:
    spec = plan.spec
    binary = pas.binaries[spec.provider]
    out: dict[str, Any] = {
        "cell": spec.key,
        "provider": spec.provider,
        "mode": spec.mode,
        "scenario": spec.scenario,
        "nonce": plan.nonce,
        "provider_version": _version(binary),
        "engine_version": _version(str(pas.engine)),
    }
    workspace = pas.root / "w" / spec.provider / f"{spec.mode}-{plan.nonce}"
    workspace.mkdir(parents=True)
    recording, timing = (
        pas.root / f"terminal-{spec.provider}-{spec.scenario}.raw",
        pas.root / f"terminal-{spec.provider}-{spec.scenario}.timing",
    )
    api = RuntimeApi(pas.host.url, pas.host.token, workspace=workspace, device_id=pas.host.device_id)
    penv = pas.provider_environments[spec.provider]
    obs: dict[str, Any] = {}
    session: ProviderPtySession | None = None
    started = time.monotonic()
    try:
        driver = drivers[spec.provider]
        session = ProviderPtySession.start(
            argv=_helm_argv(pas, workspace, binary),
            cwd=workspace,
            env=penv,
            terminal_path=recording,
            timing_path=timing,
            rows=ROWS,
            columns=COLUMNS,
        )
        _wait_ready(session, recording, timing, driver.ready_text, ready_timeout, driver.answers)
        out["ready_s"] = time.monotonic() - started
        waited = time.monotonic()
        sid = None
        while time.monotonic() < waited + CONTROL_WAIT_S and session.alive():
            sid = sid or _launched_session(api)
            if sid is not None:
                obs["send_action"] = _send_action(api, sid)
                if (obs["send_action"] or {}).get("state") == "available":
                    break
            time.sleep(0.5)
        out["send_input_available_after_ready_s"] = time.monotonic() - waited
        if (obs.get("send_action") or {}).get("state") != "available":
            out["control_at_timeout"] = _control_snapshot(pas, workspace)
            raise RuntimeError(f"the Runtime Host never offered send_input within {CONTROL_WAIT_S:.0f}s (session {sid})")
        submitted = time.monotonic()
        session.submit_line(plan.prompt)
        found = None
        while time.monotonic() < submitted + QUESTION_WAIT_S and session.alive():
            found = _pending_question(api, plan.nonce)
            if found is not None and found[1].get("can_respond") is True:
                break
            time.sleep(0.25)
        if found is None:
            raise RuntimeError(f"no pending pause request for the question within {QUESTION_WAIT_S:.0f}s")
        sid, request = found
        obs["session_id"], obs["pause_request"] = sid, request
        out["question_listed_s"] = time.monotonic() - submitted
        if request.get("can_respond") is not True:
            raise RuntimeError(f"the question is listed but not answerable: {json.dumps(request)[:400]}")
        answered = time.monotonic()
        if plan.response:
            status, body = api.post_raw(
                f"/api/sessions/{sid}/input",
                {"text": plan.response, "intent": "steer", "client_request_id": f"tt-{secrets.token_hex(4)}"},
                browser=True,
            )
        else:
            shown = rendered_question(plan, request)
            if shown is None or not shown.get("id"):
                raise RuntimeError(f"the app is not shown this question with both options: {json.dumps(request)[:400]}")
            key = str(shown["id"])
            status, body = api.post_raw(
                f"/api/sessions/{sid}/pause-requests/{request['id']}/response",
                {"decision": "answer", "answers": {key: [plan.labels[1]]}},
                browser=True,
            )
        obs["answer_ok"], obs["answer_response"] = status < 300, f"HTTP {status} {body[:400]}"
        if status >= 300:  # what the Machine Agent saw of the session's control path when the answer was refused
            out["control_at_answer"] = _control_snapshot(pas, workspace)
        config_dir = penv.get("CLAUDE_CONFIG_DIR")
        while time.monotonic() < answered + ANSWER_WAIT_S and session.alive():
            if plan.done in assistant_text(transcript_rows(pas.home, config_dir, plan.nonce)):
                break
            time.sleep(0.5)
        out["answer_to_done_s"] = time.monotonic() - answered
        # The pause request's own record once the turn is over, whatever its status now.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            obs["pause_request_after"] = _pause_request(api, sid, str(request["id"]))
            if (obs["pause_request_after"] or {}).get("status") == "resolved":
                break
            time.sleep(0.5)
    except Exception as exc:  # noqa: BLE001 - becomes the cell's failure reason
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            session.close()
    obs["transcript_rows"] = transcript_rows(pas.home, penv.get("CLAUDE_CONFIG_DIR"), plan.nonce)
    obs["followup_served"] = pas.model.served_requests([f"{spec.key}:b"])
    checks = judge_question(plan, obs)
    out["checks"] = [{"check": name, "ok": ok, "detail": detail} for name, ok, detail in checks]
    failed = next(((name, detail) for name, ok, detail in checks if not ok), None)
    out["outcome"] = "pass" if failed is None and "error" not in out else "fail"
    if out["outcome"] == "fail":
        out["first_failed_stage"] = failed[0] if failed else "harness"
        out["reason"] = "; ".join(x for x in (out.get("error"), f"{failed[0]}: {failed[1]}" if failed else None) if x)
        if recording.exists():
            out["last_screen"] = _tail_text(recording)
    out["evidence"] = {
        "pause_request": obs.get("pause_request"),
        "pause_request_after": obs.get("pause_request_after"),
        "send_action": obs.get("send_action"),
        "answer_response": obs.get("answer_response"),
        "tool_result_rows": [r for r in obs["transcript_rows"] if any(b.get("type") in ("tool_use", "tool_result") for b in _blocks(r))],
        "model_receipt": json.loads((pas.root / "model-receipt.json").read_text()) if (pas.root / "model-receipt.json").exists() else None,
    }
    out["cell_wall_s"] = time.monotonic() - started
    return out


def run_question_suite(
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
    """Run each question cell in a pass of its own (one host, engine and HOME per cell)."""

    import shutil

    drivers = drivers or PROVIDERS
    cli = longhouse_cli or (Path(shutil.which("longhouse")) if shutil.which("longhouse") else None)
    results: list[dict[str, Any]] = []
    for spec in specs:
        plan = plan_question_cell(spec)
        row = {"cell": spec.key, "provider": spec.provider, "mode": spec.mode, "scenario": spec.scenario}
        if plan.not_measured:
            results.append({**row, "outcome": "not_measured", "reason": plan.not_measured})
            continue
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
            results.append(run_question_cell_in(pas, plan, ready_timeout=ready_timeout, drivers=drivers))
        finally:
            stop_pass(pas, keep)
    return results


def render_question(results: Sequence[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        lines.append(f"{r['cell']}:{r['scenario']}  {r['outcome'].upper()}  {r.get('reason') or ''}".rstrip())
        for check in r.get("checks") or []:
            lines.append(f"  {'ok  ' if check['ok'] else 'FAIL'} {check['check']}: {check['detail']}")
    return "\n".join(lines)
