"""Slash commands sent from a Longhouse client run in the provider exactly as if typed in its terminal.

``slash_command`` (Helm cells): the harness launches the provider through ``longhouse <provider>``, waits until the
Runtime Host offers ``send_input``, sends one warm-up message, then sends every case in the provider contract's
``slash_commands.cases`` (schemas/managed_providers.yml) through the app's own input route, one after the other, and
finally one more message. For each case:

- ``expect: refused``: the input fails with Longhouse's refusal (``was not sent``), and nothing reached the provider.
- ``expect: ran``: the input is delivered (not left queued: a command that opens no model turn must free the session at
  once, or the next case would wait), it took effect, and its text never reached the model as an ordinary message.

"Took effect" is judged per case:

- ``ttcheck``: a custom command the harness writes where the provider reads its own (a ``.claude/commands`` file, an
  OpenCode command, a Pi prompt template, an Antigravity skill). It expands to a marker the scripted model must receive.
- any case whose args carry ``TTNAME``: the nonce-stamped name must be written somewhere in the provider's own files
  other than as the typed command itself (a session title, a thread name).
- other cases: delivery as a command (the send result names the command) is the check.

"Never reached the model as text": the scripted model dumps every request; no user message may contain the exact text
the harness sent.

Cursor has no scripted model (its CLI talks only to Cursor's servers), so its cell runs a fake cursor-agent
(fake_cursor/cursor-agent) that records the keystrokes Longhouse typed and acts on them as Cursor's prompt does; "the model
received" is its prompt log. That proves Longhouse's half (the exact command typed at an idle prompt, the session freed
after), not Cursor's own handling of the command.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.terminal_truth_lanes import RuntimeApi
from zerg.qa.terminal_truth_providers import ANTIGRAVITY_HELM_USER_ARGS
from zerg.qa.terminal_truth_providers import CODEX_HELM_FLAGS
from zerg.qa.terminal_truth_providers import FAKE_PROVIDERS
from zerg.qa.terminal_truth_providers import PROVIDERS
from zerg.qa.terminal_truth_providers import ProviderDriver
from zerg.qa.terminal_truth_providers import _version
from zerg.qa.terminal_truth_providers import real_cursor_home
from zerg.qa.terminal_truth_pty import COLUMNS
from zerg.qa.terminal_truth_pty import ROWS
from zerg.qa.terminal_truth_pty import _tail_text
from zerg.qa.terminal_truth_pty import _wait_ready
from zerg.qa.terminal_truth_scenarios import build_pass
from zerg.qa.terminal_truth_scenarios import stop_pass
from zerg.qa.terminal_truth_verdicts import CellSpec

SCENARIOS = ("slash_command",)
CONTRACTS = Path(__file__).resolve().parents[1] / "config" / "managed_provider_contracts.json"
# Providers with no scripted model. Other input scenarios (steer) read this; slash_command runs Cursor against a fake.
NOT_MEASURABLE = {
    "cursor": "Cursor's CLI talks only to Cursor's own servers, so no scripted model can stand in for it",
}
# Cursor's CLI talks only to Cursor's servers, so no scripted model can stand in for its model. Its cell runs a fake
# cursor-agent (fake_cursor/cursor-agent) that records the keystrokes it was typed; "the model received" is its prompt log.
FAKE_MODEL_PROVIDERS = frozenset({"cursor"})
CONTROL_WAIT_S = 90.0  # launch to the Runtime Host offering send_input
REPLY_WAIT_S = 60.0  # a message to its scripted reply in the model receipt
CASE_WAIT_S = 45.0  # a command to a delivered or failed receipt
EFFECT_WAIT_S = 30.0  # a delivered command to its effect
CUSTOM_COMMAND = "ttcheck"


@dataclass
class CommandPlan:
    spec: CellSpec
    nonce: str
    cases: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    not_measured: str = ""

    @property
    def warm(self) -> str:
        return f"TTRUTH{self.nonce} warm up"

    @property
    def end(self) -> str:
        return f"TTEND{self.nonce} last message"

    @property
    def custom_marker(self) -> str:
        return f"TTCMD{self.nonce}"

    def text_for(self, case: dict[str, Any]) -> str:
        args = str(case.get("args") or "").replace("TTNAME", f"TTNAME{self.nonce}").replace("TTGOAL", f"TTGOAL{self.nonce}")
        return f"/{case['command']} {args}".rstrip()


def contract_cases(provider: str) -> list[dict[str, Any]]:
    payload = json.loads(CONTRACTS.read_text(encoding="utf-8"))
    for item in payload["providers"]:
        if item["provider"] == provider:
            return list((item.get("slash_commands") or {}).get("cases") or [])
    return []


def plan_command_cell(spec: CellSpec) -> CommandPlan:
    nonce = secrets.token_hex(3)
    if spec.provider in NOT_MEASURABLE and spec.provider not in FAKE_MODEL_PROVIDERS:
        return CommandPlan(spec, nonce, not_measured=NOT_MEASURABLE[spec.provider])
    if spec.mode != "helm":
        return CommandPlan(spec, nonce, not_measured="the slash_command scenario runs Helm cells (the terminal a command is typed in)")
    plan = CommandPlan(spec, nonce, cases=contract_cases(spec.provider))
    scope = "any_user" if spec.provider == "antigravity" else "latest"
    # The scripted model takes the first turn that matches, and a later request carries the earlier markers in its
    # history, so the last message is listed first and the warm-up last.
    plan.turns = [
        {"id": f"{spec.key}:end", "match": f"TTEND{nonce}", "match_scope": scope, "reply": f"TTENDOK{nonce} bye"},
        {"id": f"{spec.key}:cmd", "match": plan.custom_marker, "match_scope": "any_user", "reply": f"TTCMDOK{nonce} done"},
        {"id": f"{spec.key}:warm", "match": f"TTRUTH{nonce}", "match_scope": scope, "reply": f"TTWARMOK{nonce} ready"},
    ]
    return plan


def write_custom_command(provider: str, workspace: Path, home: Path, marker: str) -> list[Path]:
    """Where each provider reads a command of the person's own; the body expands to ``marker``."""

    body = f"Reply with {marker} and nothing else.\n"
    targets: list[Path] = []
    if provider in ("claude", "omp"):  # OMP also discovers .claude/commands
        targets.append(workspace / ".claude" / "commands" / f"{CUSTOM_COMMAND}.md")
    if provider == "opencode":
        targets += [
            workspace / ".opencode" / "command" / f"{CUSTOM_COMMAND}.md",
            workspace / ".opencode" / "commands" / f"{CUSTOM_COMMAND}.md",
        ]
    if provider == "pi":  # project templates wait for project trust; the scratch HOME's own load at once
        targets.append(home / ".pi" / "agent" / "prompts" / f"{CUSTOM_COMMAND}.md")
    if provider == "antigravity":  # every skill is a /NAME command
        targets.append(workspace / ".agents" / "skills" / CUSTOM_COMMAND / "SKILL.md")
        body = f"---\nname: {CUSTOM_COMMAND}\ndescription: Terminal Truth check\n---\n{body}"
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return targets


def _helm_argv(pas: Any, provider: str, workspace: Path, binary: str) -> list[str]:
    argv = [str(pas.cli), provider, "--cwd", str(workspace), "--url", pas.proxy.url]
    if provider != "pi":  # `longhouse pi` takes no --token
        argv += ["--token", pas.host.token]
    argv += [f"--{provider}-bin", binary]
    if provider == "opencode":
        argv += ["--model", "longhouse-mock/mock-model"]
    if provider == "pi":
        argv += ["--provider", "longhouse-mock", "--model", "mock-model"]
    if provider == "codex":
        argv += list(CODEX_HELM_FLAGS)
    if provider == "antigravity":
        argv += list(ANTIGRAVITY_HELM_USER_ARGS)
    return argv


def _launched_session(api: RuntimeApi) -> str | None:
    ids = api.session_ids(fresh=True)
    return next((sid for sid in ids if sid in api.launched_sessions), None)


def cursor_store_messages(home: Path) -> list[tuple[int, str, str]]:
    """(row, role, text) for every message real Cursor stored under ``home``: what its model was sent and answered.

    Cursor keeps each chat in ``~/.cursor/chats/<workspace>/<chat>/store.db``; a message is a JSON blob with a role.
    """

    messages: list[tuple[int, str, str]] = []
    for db in sorted((home / ".cursor" / "chats").glob("*/*/store.db")):
        with contextlib.suppress(sqlite3.Error), contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            for row, data in conn.execute("SELECT rowid, data FROM blobs ORDER BY rowid"):
                with contextlib.suppress(ValueError, TypeError, AttributeError):
                    blob = json.loads(bytes(data))
                    content = blob.get("content")
                    if isinstance(content, list):
                        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
                    messages.append((row, str(blob.get("role") or ""), str(content or "")))
    return messages


def cursor_turn_answered(home: Path, marker: str) -> bool:
    """Real Cursor sent a user message carrying ``marker`` to its model and stored the model's answer after it."""

    messages = cursor_store_messages(home)
    asked = [row for row, role, text in messages if role == "user" and marker in text]
    return bool(asked) and any(role == "assistant" and row > asked[0] for row, role, _ in messages)


def _bound_cursor_session(pas: Any, workspace: Path) -> str | None:
    """The session the Cursor launcher bound in ``workspace``.

    The host lists a session under its working directory once a transcript ties the two; real Cursor writes one during
    ``create-chat``, the fake does not. The launcher's binding claim names the session and the workspace directly.
    """

    probes = Path(pas.environment["LONGHOUSE_HOME"]) / "managed-local" / "cursor-helm" / "binding-probes"
    for path in sorted(probes.glob("*.json")) if probes.is_dir() else []:
        with contextlib.suppress(OSError, ValueError):
            claim = json.loads(path.read_text())
            # Pending until a hook from the launched Cursor fires, which real Cursor does only at the first prompt: the
            # session id is the launcher's either way, and the host judges send_input on its own.
            if Path(str(claim.get("cwd") or "")).resolve() == workspace.resolve() and claim.get("session_id"):
                return str(claim["session_id"])
    return None


def _send_action(api: RuntimeApi, sid: str) -> dict[str, Any] | None:
    status, body = api.get(f"/api/agents/sessions/{sid}/state-diagnostics")
    if status != 200:
        return None
    payload = json.loads(body)
    shadow = payload.get("shadow") if payload.get("served_path") == "canonical_session_detail" else None
    control = (shadow or {}).get("control") if isinstance(shadow, dict) else None
    actions = control.get("actions") if isinstance(control, dict) else None
    entry = actions.get("send_input") if isinstance(actions, dict) else None
    return entry if isinstance(entry, dict) else None


def send_input(api: RuntimeApi, sid: str, text: str) -> dict[str, Any]:
    """Send ``text`` through the app's input route and follow its receipt to delivered or failed."""

    request_id = f"tt-{secrets.token_hex(5)}"
    status, body = api.post_raw(
        f"/api/sessions/{sid}/input", {"text": text, "intent": "auto", "client_request_id": request_id}, browser=True
    )
    out: dict[str, Any] = {"text": text, "http_status": status, "response": body[:600]}
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
    out["receipt"] = receipt
    return out


def _request_user_texts(dump_dir: Path) -> list[str]:
    from zerg.qa.scripted_model_server import _user_text

    texts: list[str] = []
    for path in sorted(dump_dir.glob("*.json")):
        with contextlib.suppress(ValueError, OSError):
            payload = json.loads(path.read_text()).get("payload") or {}
            texts += [str(t) for t in _user_text(payload)]
    return texts


def name_recorded(home: Path, marker: str, *, skip: Sequence[Path] = ()) -> str | None:
    """The provider's own file that stored ``marker`` as a name or title, or None.

    A text file counts only when the marker is the value of a name or title key (a transcript also records the typed
    command, which proves nothing). A binary store (OpenCode's SQLite) counts when it holds the marker at all: the
    typed text lives in its message rows as ``/rename ...``, so the bare marker without the command is checked too.
    Longhouse's own files never count.
    """

    keyed = re.compile(r'"(?:[A-Za-z_]*[Tt]itle|[A-Za-z_]*[Nn]ame)"\s*:\s*"' + re.escape(marker) + '"')
    for path in home.rglob("*"):
        if not path.is_file() or any(p in ("node_modules", ".npm", ".cache") or p.startswith(".longhouse") for p in path.parts):
            continue
        if "history" in path.name.lower():  # a prompt history records what was typed, not what it did
            continue
        if any(str(path).startswith(str(s)) for s in skip):
            continue
        with contextlib.suppress(OSError):
            if path.stat().st_size > 20_000_000:
                continue
            raw = path.read_bytes()
            if marker.encode() not in raw:
                continue
            data = raw.decode("utf-8", errors="replace")
            if keyed.search(data):
                return str(path.relative_to(home))
            if b"\0" in raw and data.replace(f" {marker}", "").count(marker) > 0:
                return f"{path.relative_to(home)} (binary store)"
    return None


def judge_case(
    plan: CommandPlan, case: dict[str, Any], sent: dict[str, Any], effect: str | None, user_texts: Sequence[str]
) -> tuple[bool, str]:
    text = sent["text"]
    expect = case.get("expect")
    if expect == "refused":
        refused = sent.get("status") in ("failed", "rejected") and "was not sent" in json.dumps(sent)
        return refused, f"status={sent.get('status')} error={(sent.get('last_error') or sent.get('response') or '')[:240]}"
    as_text = leaked_as_text(case, text, user_texts)
    delivered = sent.get("status") == "delivered"
    ok = delivered and not as_text and (effect is not None or not _needs_effect(case))
    detail = f"status={sent.get('status')} error={(sent.get('last_error') or '')[:200]} effect={effect} as_text={len(as_text)}"
    return ok, detail


def leaked_as_text(case: dict[str, Any], text: str, user_texts: Sequence[str]) -> list[str]:
    """User messages that carried the command as text.

    A custom command is meant to reach the model: the provider expands it (Claude also labels it
    ``<command-name>/ttcheck</command-name>``), and its effect check needs the expansion's marker, which plain
    ``/ttcheck`` text would not carry. So only a built-in command counts here.
    """

    if case.get("command") == CUSTOM_COMMAND:
        return []
    return [t for t in user_texts if text in t]


def _needs_effect(case: dict[str, Any]) -> bool:
    return case.get("command") == CUSTOM_COMMAND or "TTNAME" in str(case.get("args") or "")


def run_command_cell_in(
    pas: Any, plan: CommandPlan, dump_dir: Path, *, ready_timeout: float, drivers: dict[str, ProviderDriver]
) -> dict[str, Any]:
    spec = plan.spec
    provider = spec.provider
    binary = pas.binaries[provider]
    out: dict[str, Any] = {
        "cell": spec.key,
        "provider": provider,
        "mode": spec.mode,
        "scenario": spec.scenario,
        "nonce": plan.nonce,
        "provider_version": _version(binary),
        "engine_version": _version(str(pas.engine)),
    }
    workspace = pas.root / "w" / provider / f"{spec.mode}-{plan.nonce}"
    workspace.mkdir(parents=True)
    out["custom_command_files"] = [str(p) for p in write_custom_command(provider, workspace, pas.home, plan.custom_marker)]
    recording, timing = pas.root / f"terminal-{provider}-{spec.scenario}.raw", pas.root / f"terminal-{provider}-{spec.scenario}.timing"
    api = RuntimeApi(pas.host.url, pas.host.token, workspace=workspace, device_id=pas.host.device_id)
    session: ProviderPtySession | None = None
    checks: list[tuple[str, bool, str]] = []
    started = time.monotonic()
    sends: list[dict[str, Any]] = []
    real_cursor = provider == "cursor" and real_cursor_home() is not None
    fake_model = provider in FAKE_MODEL_PROVIDERS and not real_cursor
    model_log = pas.root / f"{provider}-model.jsonl"
    if real_cursor:
        out["provider_note"] = "real cursor-agent against Cursor's servers: proves Cursor's own command handling"
    elif fake_model:
        out["provider_note"] = "fake cursor-agent: proves the keystrokes Longhouse types, not Cursor's own command handling"
        pas.provider_environments[provider]["FAKE_CURSOR_MODEL_LOG"] = str(model_log)
        pas.provider_environments[provider]["FAKE_CURSOR_INPUT_LOG"] = str(pas.root / f"{provider}-input.jsonl")

    def fake_prompts() -> list[str]:
        if not model_log.exists():
            return []
        return [str(json.loads(row).get("prompt") or "") for row in model_log.read_text().splitlines() if row.strip()]

    def model_got(turn: str, marker: str) -> bool:
        if real_cursor:
            return cursor_turn_answered(pas.home, marker)
        if fake_model:
            return any(marker in prompt for prompt in fake_prompts())
        return pas.model.served_requests([f"{spec.key}:{turn}"]) >= 1

    def user_texts() -> list[str]:
        if real_cursor:
            return [text for _, role, text in cursor_store_messages(pas.home) if role == "user"]
        return fake_prompts() if fake_model else _request_user_texts(dump_dir)

    try:
        driver = drivers[provider]
        session = ProviderPtySession.start(
            argv=_helm_argv(pas, provider, workspace, binary),
            cwd=workspace,
            env=pas.provider_environments[provider],
            terminal_path=recording,
            timing_path=timing,
            rows=ROWS,
            columns=COLUMNS,
        )
        _wait_ready(session, recording, timing, driver.ready_text, ready_timeout, driver.answers)
        sid = None
        action: dict[str, Any] | None = None
        waited = time.monotonic()
        while time.monotonic() < waited + CONTROL_WAIT_S and session.alive():
            sid = sid or (_bound_cursor_session(pas, workspace) if provider == "cursor" else _launched_session(api))
            if sid is not None:
                action = _send_action(api, sid)
                if (action or {}).get("state") == "available":
                    break
            time.sleep(0.5)
        checks.append(("the host offers send_input", (action or {}).get("state") == "available", json.dumps(action)[:240]))
        if sid is None or (action or {}).get("state") != "available":
            raise RuntimeError(f"the Runtime Host never offered send_input within {CONTROL_WAIT_S:.0f}s (session {sid})")
        out["session_id"] = sid

        # A real model takes a bare marker as a task and starts running tools, which stop at Cursor's approval prompt.
        ask = " Reply with the single word OK. Do not use any tools." if real_cursor else ""
        warm = send_input(api, sid, plan.warm + ask)
        sends.append(warm)
        deadline = time.monotonic() + REPLY_WAIT_S
        while time.monotonic() < deadline and not model_got("warm", f"TTRUTH{plan.nonce}"):
            time.sleep(0.5)
        checks.append(("a message from the app reaches the model", model_got("warm", f"TTRUTH{plan.nonce}"), warm.get("status", "")))
        time.sleep(3)  # let the warm turn end so every command starts at an idle session

        for case in plan.cases:
            text = plan.text_for(case)
            sent = send_input(api, sid, text)
            sends.append(sent)
            effect: str | None = None
            if sent.get("status") == "delivered" and case.get("expect") == "ran":
                deadline = time.monotonic() + EFFECT_WAIT_S
                while time.monotonic() < deadline:
                    if case.get("command") == CUSTOM_COMMAND:
                        effect = "custom command expanded for the model" if model_got("cmd", plan.custom_marker) else None
                    elif "TTNAME" in str(case.get("args") or ""):
                        effect = name_recorded(pas.home, f"TTNAME{plan.nonce}", skip=(dump_dir, pas.root / "w"))
                    else:
                        effect = "delivered as a command"
                    if effect:
                        break
                    time.sleep(1)
            sent["effect"] = effect
            ok, detail = judge_case(plan, case, sent, effect, user_texts())
            checks.append((f"{text} ({case.get('expect')})", ok, detail))
            time.sleep(2)

        end = send_input(api, sid, plan.end + ask)
        sends.append(end)
        deadline = time.monotonic() + REPLY_WAIT_S
        while time.monotonic() < deadline and not model_got("end", f"TTEND{plan.nonce}"):
            time.sleep(0.5)
        checks.append(
            (
                "a message after the commands still reaches the model (the session was freed)",
                model_got("end", f"TTEND{plan.nonce}"),
                end.get("status", ""),
            )
        )
        texts = user_texts()
        leaked = [plan.text_for(c) for c in plan.cases if leaked_as_text(c, plan.text_for(c), texts)]
        checks.append(
            ("no command reached the model as text", not leaked, f"leaked: {leaked}" if leaked else f"{len(texts)} user texts checked")
        )
    except Exception as exc:  # noqa: BLE001 - becomes the cell's failure reason
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            session.close()
    out["checks"] = [{"check": name, "ok": ok, "detail": detail} for name, ok, detail in checks]
    out["sends"] = sends
    failed = next(((name, detail) for name, ok, detail in checks if not ok), None)
    out["outcome"] = "pass" if failed is None and "error" not in out and checks else "fail"
    if out["outcome"] == "fail":
        out["first_failed_stage"] = failed[0] if failed else "harness"
        out["reason"] = "; ".join(x for x in (out.get("error"), f"{failed[0]}: {failed[1]}" if failed else None) if x)
        if recording.exists():
            out["last_screen"] = _tail_text(recording)
    out["cell_wall_s"] = time.monotonic() - started
    return out


def run_command_suite(
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
    """Run each slash_command cell in a pass of its own (one host, engine, HOME and scripted model per cell)."""

    drivers = {**FAKE_PROVIDERS, **(drivers or PROVIDERS)}
    cli = longhouse_cli or (Path(shutil.which("longhouse")) if shutil.which("longhouse") else None)
    results: list[dict[str, Any]] = []
    for spec in specs:
        plan = plan_command_cell(spec)
        row = {"cell": spec.key, "provider": spec.provider, "mode": spec.mode, "scenario": spec.scenario}
        if plan.not_measured:
            results.append({**row, "outcome": "not_measured", "reason": plan.not_measured})
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
                results.append(run_command_cell_in(pas, plan, dump_dir, ready_timeout=ready_timeout, drivers=drivers))
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


def render_command(results: Sequence[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        lines.append(f"{r['cell']}:{r['scenario']}  {r['outcome'].upper()}  {r.get('reason') or ''}".rstrip())
        for check in r.get("checks") or []:
            lines.append(f"  {'ok  ' if check['ok'] else 'FAIL'} {check['check']}: {check['detail']}")
    return "\n".join(lines)
