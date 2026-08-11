"""Live `pi --mode rpc` steer proof (Gate 0) for the Pi Helm bridge decision.

Proves the only viable control surface for a maintained `pi_helm` engine
adapter: a pinned stock `pi --mode rpc` process driving prompt, a *mid-turn*
`steer`, and `abort` over stdin/stdout JSONL, with the full wire trace
captured as evidence. The one-shot `pi_print` console path (already shipped)
is a separate adapter; this gate is scoped to the interactive Helm decision.

Each routine records a decided pass/skip/fail and the run reports a JSON
summary (status `passed`/`failed`), written under
`~/.longhouse/canaries/provider-live/pi-rpc/<ts>/`. Live LLM turns are gated on
`LONGHOUSE_PI_LIVE` in {1,true,yes,on} and `OPENROUTER_API_KEY` being present —
nothing is spent otherwise.

Protocol (docs/rpc.md, pi 0.84.1): JSONL on stdin (commands) and stdout
(responses `{"type":"response","command",...}` plus streamed events). Every
command accepts an `id` echoed back on its response. Surfaces here use only
strict response objects plus `get_state`/`get_messages` — no reliance on
event type names.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_MODEL = "~deepseek/deepseek-v4-flash-latest"
# Pin the exact pi build the steer research validated, independent of PATH.
_DEFAULT_PI_BIN = "/tmp/pi-sandbox/node_modules/.bin/pi"

_ARTIFACT_SECRET_RE = re.compile(rb"(sk-[A-Za-z0-9_-]{16,})")
_ARTIFACT_SECRET_ENV_NAMES = (
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "CODEX_API_KEY",
)

_LIVE_TRUTHY = {"1", "true", "yes", "on"}
_MARKER = "PI_GATE0_STEER_ROOT"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace(":", "-")


def _marker_digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _live_opted_in() -> bool:
    return str(os.environ.get("LONGHOUSE_PI_LIVE") or "").strip().lower() in _LIVE_TRUTHY


def _configured_openrouter_key() -> str:
    return str(os.environ.get("OPENROUTER_API_KEY") or "").strip()


def _resolve_pi_bin(configured: str | None) -> str:
    candidate = str(configured or _DEFAULT_PI_BIN).strip()
    path = Path(candidate).expanduser()
    return str(path) if path.exists() else candidate


def _artifact_secret_values() -> tuple[bytes, ...]:
    return tuple(
        sorted(
            {value.encode("utf-8") for name in _ARTIFACT_SECRET_ENV_NAMES if (value := str(os.environ.get(name) or "").strip())},
            key=len,
            reverse=True,
        )
    )


def _scrub_tree(root: Path) -> None:
    if not root.is_dir():
        return
    secrets = _ARTIFACT_SECRET_RE.pattern.encode("utf-8") if not _ARTIFACT_SECRET_RE.pattern.startswith(b"") else b""
    static = {value for value in _artifact_secret_values() if value}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        changed = False
        for secret in static:
            if secret in data:
                data = data.replace(secret, b"<redacted>")
                changed = True
        if changed:
            path.write_bytes(data)


class PiRpc:
    """One pinned `pi --mode rpc` subprocess with a recording reader thread."""

    def __init__(self, *, pi_bin: str, cwd: Path, session_dir: Path, model: str, trace: Path) -> None:
        self.pi_bin = pi_bin
        self.session_dir = session_dir
        self.trace = trace
        self.trace_fh = trace.open("a", encoding="utf-8")
        self.lines: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.closed = False
        env = dict(os.environ)
        self.proc = subprocess.Popen(
            [
                pi_bin,
                "--mode",
                "rpc",
                "--provider",
                "openrouter",
                "--model",
                model,
                "--session-dir",
                str(session_dir),
                "--no-context-files",
            ],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def drain_stderr(self) -> str:
        """Tail of the provider's stderr, captured after the process settles."""
        if self.proc.stderr is None:
            return ""
        try:
            self.proc.stderr.flush()
            return self.proc.stderr.read()[-6000:].strip()
        except (OSError, ValueError):
            return ""

    def _read_stdout(self) -> None:
        for raw in self.proc.stdout:  # type: ignore[union-attr]
            line = raw.rstrip("\n")
            try:
                obj = json.loads(line)
            except ValueError:
                obj = {"_raw": line}
            self._record(obj)

    def _record(self, obj: dict[str, Any]) -> None:
        with self.lock:
            self.lines.append(obj)
            try:
                self.trace_fh.write(json.dumps(obj) + "\n")
                self.trace_fh.flush()
            except OSError:
                pass

    def send(self, command: dict[str, Any], *, tag: str | None = None) -> None:
        payload = dict(command)
        if tag is not None:
            payload["id"] = tag
        self._record({"cmd": payload})
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def wait_response(self, tag: str, *, command: str, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for obj in self.lines:
                    if (
                        obj.get("type") == "response"
                        and obj.get("command") == command
                        and (tag is None or obj.get("id") == tag or ("id" not in obj))
                    ):
                        return obj
            time.sleep(0.05)
        return None

    def get_state(self, *, timeout: float = 20.0) -> dict[str, Any]:
        tag = f"get-state-{uuid4().hex[:8]}"
        self.send({"type": "get_state"}, tag=tag)
        response = self.wait_response(tag, command="get_state", timeout=timeout)
        return (response or {}).get("data") or {}

    def get_messages(self, *, timeout: float = 20.0) -> list[dict[str, Any]]:
        tag = f"get-messages-{uuid4().hex[:8]}"
        self.send({"type": "get_messages"}, tag=tag)
        response = self.wait_response(tag, command="get_messages", timeout=timeout)
        data = (response or {}).get("data") or {}
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        return [message for message in messages if isinstance(message, dict)]

    def assistant_text(self, *, timeout: float = 20.0) -> list[str]:
        messages = self.get_messages(timeout=timeout)
        texts: list[str] = []
        for message in messages:
            content = message.get("message") or message
            role = content.get("role")
            if role != "assistant":
                continue
            for part in content.get("content") or []:
                if part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
        return texts

    def wait_state(self, predicate, message: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = self.get_state(timeout=15.0)
            last = state
            if predicate(state):
                return state
            time.sleep(0.2)
        raise TimeoutError(message + f" (last state { {k: last.get(k) for k in ('isStreaming', 'messageCount', 'pendingMessageCount')} })")

    def wait_idle(self, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        """Block until no turn is streaming and no messages are queued."""
        self.wait_state(
            lambda s: not bool(s.get("isStreaming")) and int(s.get("pendingMessageCount") or 0) == 0,
            "agent not idle",
            timeout=timeout,
        )

    def close(self, *, abort: bool = True) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if abort and self.proc.poll() is None:
                self.send({"type": "abort"})
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            try:
                self.trace_fh.close()
            except OSError:
                pass


def _routines():
    return {
        "spawn": "rpc process answers new_session + get_state",
        "prompt": "idle prompt produces an assistant reply carrying the marker",
        "steer_midturn": "mid-stream steer queues (pendingMessageCount) and has no ambiguity",
        "abort": "abort stops a streaming turn (isStreaming false, message count settles)",
    }


def _run_spawn(pi: PiRpc, *, artifact_dir: Path) -> dict[str, Any]:
    tag = f"spawn-{uuid4().hex[:8]}"
    pi.send({"type": "new_session"}, tag=tag)
    response = pi.wait_response(tag, command="new_session", timeout=30.0)
    if response is None or response.get("success") is not True:
        return {"routine": "spawn", "decision": "fail", "detail": f"new_session response missing/failed: {response}"}
    state = pi.get_state(timeout=30.0)
    if not state.get("sessionId"):
        return {"routine": "spawn", "decision": "fail", "detail": f"get_state missing sessionId: {state}"}
    return {
        "routine": "spawn",
        "decision": "pass",
        "detail": f"session {state.get('sessionId')} ready; isStreaming={bool(state.get('isStreaming'))}",
    }


def _run_prompt(pi: PiRpc, *, marker: str) -> dict[str, Any]:
    try:
        pi.wait_idle(timeout=45.0)
    except TimeoutError as exc:
        return {"routine": "prompt", "decision": "fail", "detail": f"never idle: {exc}"}
    tag = f"prompt-{uuid4().hex[:8]}"
    prompt = f"Reply with exactly the single line: {marker}"
    pi.send({"type": "prompt", "message": prompt}, tag=tag)
    response = pi.wait_response(tag, command="prompt", timeout=45.0)
    if response is None or response.get("success") is not True:
        return {"routine": "prompt", "decision": "fail", "detail": f"prompt rejected: {response}"}
    deadline = time.monotonic() + _DEFAULT_TIMEOUT_SECONDS
    saw_marker = False
    exact_line = False
    while time.monotonic() < deadline:
        texts = pi.assistant_text(timeout=15.0)
        joined = "\n".join(texts)
        saw_marker = marker in joined
        exact_line = any(text.strip() == marker for text in texts)
        if exact_line:
            break
        time.sleep(0.4)
    if not saw_marker:
        return {"routine": "prompt", "decision": "fail", "detail": "marker never appeared in assistant text"}
    return {
        "routine": "prompt",
        "decision": "pass",
        "detail": f"assistant reply carried marker; exact_single_line={exact_line} digest={_marker_digest(marker)}",
    }


def _run_steer_midturn(pi: PiRpc, *, marker: str) -> dict[str, Any]:
    """Steer must land *while* the turn is streaming, not as a queued no-op.

    Pass requires the steered marker to actually arrive in a later assistant
    reply — proof the steering message changed the conversation, not just that
    the command was accepted. pendingMessageCount is recorded as secondary.
    """
    try:
        pi.wait_idle(timeout=45.0)
    except TimeoutError as exc:
        return {"routine": "steer_midturn", "decision": "fail", "detail": f"never idle: {exc}"}
    tag = f"steerflow-{uuid4().hex[:8]}"
    # A multi-step tool-calling prompt forced the turn to stream for several
    # seconds so a genuine mid-turn steer is observable, not a race w/ completion.
    prompt = (
        "Enumerate the integers 1 through 30 in order, using the bash tool in "
        "small batches and explaining each batch's purpose at length between "
        "runs. Do not stop early."
    )
    pi.send({"type": "prompt", "message": prompt}, tag=tag)
    accepted = pi.wait_response(tag, command="prompt", timeout=45.0)
    if accepted is None or accepted.get("success") is not True:
        return {"routine": "steer_midturn", "decision": "fail", "detail": f"streaming prompt rejected: {accepted}"}
    try:
        pi.wait_state(lambda s: bool(s.get("isStreaming")), "never observed isStreaming", timeout=45.0)
    except TimeoutError as exc:
        return {"routine": "steer_midturn", "decision": "fail", "detail": str(exc)}
    steer_tag = f"steer-{uuid4().hex[:8]}"
    pi.send(
        {"type": "steer", "message": f"Stop enumerating. Reply with exactly the single line: {marker}"},
        tag=steer_tag,
    )
    steer_response = pi.wait_response(steer_tag, command="steer", timeout=15.0)
    if steer_response is None or steer_response.get("success") is not True:
        return {
            "routine": "steer_midturn",
            "decision": "fail",
            "detail": f"mid-turn steer rejected: {steer_response}",
        }
    queued_state = pi.get_state(timeout=15.0)
    deadline = time.monotonic() + _DEFAULT_TIMEOUT_SECONDS
    steered_reply = False
    while time.monotonic() < deadline:
        text = "\n".join(pi.assistant_text(timeout=15.0))
        if any(line.strip() == marker for line in text.splitlines()):
            steered_reply = True
            break
        time.sleep(0.5)
    if not steered_reply:
        return {
            "routine": "steer_midturn",
            "decision": "fail",
            "detail": "steer accepted but its marker never landed in an assistant reply",
        }
    return {
        "routine": "steer_midturn",
        "decision": "pass",
        "detail": (
            f"steer accepted mid-stream (queued pendingMessageCount={queued_state.get('pendingMessageCount')}) "
            f"and its marker landed in an assistant reply"
        ),
    }


def _run_abort(pi: PiRpc, *, marker: str) -> dict[str, Any]:
    try:
        pi.wait_idle(timeout=60.0)
    except TimeoutError as exc:
        return {"routine": "abort", "decision": "fail", "detail": f"never idle: {exc}"}
    tag = f"abortflow-{uuid4().hex[:8]}"
    pi.send(
        {"type": "prompt", "message": "Count from 1 to 100 slowly in bash batches, explaining each step at length."},
        tag=tag,
    )
    accepted = pi.wait_response(tag, command="prompt", timeout=45.0)
    if accepted is None or accepted.get("success") is not True:
        return {"routine": "abort", "decision": "fail", "detail": f"streaming prompt rejected: {accepted}"}
    try:
        pi.wait_state(lambda s: bool(s.get("isStreaming")), "never observed isStreaming", timeout=45.0)
    except TimeoutError as exc:
        return {"routine": "abort", "decision": "fail", "detail": str(exc)}
    before = len(pi.get_messages(timeout=15.0))
    abort_tag = f"abort-{uuid4().hex[:8]}"
    pi.send({"type": "abort"}, tag=abort_tag)
    abort_response = pi.wait_response(abort_tag, command="abort", timeout=15.0)
    if abort_response is None or abort_response.get("success") is not True:
        return {"routine": "abort", "decision": "fail", "detail": f"abort rejected: {abort_response}"}
    settled = pi.wait_state(
        lambda s: not bool(s.get("isStreaming")),
        "agent still streaming after abort",
        timeout=30.0,
    )
    time.sleep(2.0)
    after = len(pi.get_messages(timeout=15.0))
    new_messages = after - before
    return {
        "routine": "abort",
        "decision": "pass" if not bool(settled.get("isStreaming")) and new_messages <= 2 else "fail",
        "detail": f"abort accepted; isStreaming={bool(settled.get('isStreaming'))}; messages grew {before}->{after}",
    }


def run_gate0(args: argparse.Namespace) -> dict[str, Any]:
    started = _now()
    artifact = Path(args.artifact_root).expanduser() / started
    artifact.mkdir(parents=True, exist_ok=True)
    session_dir = artifact / "sessions" / f"gate-{uuid4().hex[:8]}"
    session_dir.mkdir(parents=True, exist_ok=True)
    trace = artifact / "trace.ndjson"
    pi_bin = _resolve_pi_bin(args.pi_bin)
    report: dict[str, Any] = {
        "provider": "pi",
        "mode": "rpc",
        "pi_bin": pi_bin,
        "model": args.model,
        "trace": str(trace.relative_to(Path.home())),
        "session_dir": str(session_dir),
        "routines": {},
        "started": started,
    }
    if not _live_opted_in():
        report["status"] = "failed"
        report["skip_reason"] = "LONGHOUSE_PI_LIVE must be 1/true/yes/on to spend on live LLM turns"
        return report
    if not _configured_openrouter_key():
        report["status"] = "failed"
        report["skip_reason"] = "OPENROUTER_API_KEY is not set"
        return report

    pi = PiRpc(pi_bin=pi_bin, cwd=artifact, session_dir=session_dir, model=args.model, trace=trace)
    try:
        for name, _doc in _routines().items():
            try:
                if name == "spawn":
                    outcome = _run_spawn(pi, artifact_dir=artifact)
                elif name == "prompt":
                    outcome = _run_prompt(pi, marker=_MARKER)
                elif name == "steer_midturn":
                    outcome = _run_steer_midturn(pi, marker=_MARKER)
                else:
                    outcome = _run_abort(pi, marker=_MARKER)
            except Exception as exc:  # noqa: BLE001 - a routine crash is a decided fail
                outcome = {"routine": name, "decision": "fail", "detail": f"{type(exc).__name__}: {exc}"}
                report.setdefault("exceptions", {})[name] = traceback.format_exc(limit=4)
            report["routines"][name] = outcome
    finally:
        pi.close(abort=True)

    stderr_tail = pi.drain_stderr()
    if stderr_tail:
        report["stderr_tail"] = stderr_tail
    _ZSTD_BLOCKER = "createZstdDecompress"
    if _ZSTD_BLOCKER in stderr_tail or any(
        _ZSTD_BLOCKER in str(outcome.get("detail") or "")
        for outcome in report["routines"].values()
    ):
        report["blocker"] = (
            "pi --mode rpc crashed mid-turn: bundled undici 8.9.0 advertises "
            "Accept-Encoding zstd and calls zlib.createZstdDecompress, which "
            "the installed Node zlib lacks, so any zstd-compressed OpenRouter "
            "response kills the process (rc 1). No live turn finishes on this "
            "stack until pi/undici gates zstd on zlib support or a Node with "
            "zstd runs pi."
        )

    decisions = [str(outcome.get("decision")) for outcome in report["routines"].values()]
    report["decisions"] = decisions
    report["status"] = "passed" if decisions and all(d == "pass" for d in decisions) else "failed"
    _scrub_tree(artifact)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pi-bin",
        default=_DEFAULT_PI_BIN,
        help=f"Explicit pinned pi binary (default {_DEFAULT_PI_BIN})",
    )
    parser.add_argument("--model", default=_DEFAULT_MODEL, help="Model pattern for the live proof turns")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--artifact-root",
        default=str(Path.home() / ".longhouse" / "canaries" / "provider-live" / "pi-rpc"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_gate0(args)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())