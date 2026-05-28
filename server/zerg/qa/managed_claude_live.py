"""Managed Claude live-session proof helpers.

This module backs both the operator POC script and provider-live canaries. It
owns the PTY/channel/probe loop so Longhouse does not maintain two different
definitions of "managed Claude live proof".
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SESSION_ID_RE = re.compile(r"Session ID:\s*([0-9a-fA-F-]{36})")
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_output_root(repo_root: Path | None = None) -> Path:
    return (repo_root or default_repo_root()) / "artifacts" / "managed-claude-poc"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def run_id_now() -> str:
    return datetime.now(UTC).strftime("managed-claude-poc-%Y%m%dT%H%M%SZ")


@dataclass
class ManagedClaudeLiveConfig:
    cwd: Path
    project: str = "zerg"
    name: str = "Claude propagation POC"
    model: str = "claude-sonnet-4-6"
    prompt: str = "Please reply with exactly: LONGHOUSE CLAUDE PROFILE READY"
    expected: str = "LONGHOUSE CLAUDE PROFILE READY"
    steer_text: str | None = None
    steer_expected: str | None = None
    steer_delay_secs: float = 2.0
    run_id: str | None = None
    output_dir: Path | None = None
    launch_timeout_secs: float = 45.0
    response_timeout_secs: float = 60.0
    post_close_probe_secs: float = 0.0
    skip_post_close_probe: bool = False
    skip_live_probe: bool = False
    session_id_file: Path | None = None
    repo_root: Path | None = None


class Recorder:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id

    def write(self, event: str, **payload: Any) -> None:
        row = {
            "schema": "managed_claude_poc.v1",
            "run_id": self.run_id,
            "observed_at_wall": utc_now(),
            "observed_at_monotonic_ms": monotonic_ms(),
            "event": event,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def append_terminal_log(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(data)
    return data.decode("utf-8", errors="replace")


def strip_terminal_controls(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "\n")


def compact_terminal_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def wait_for_channel_ready(session_id: str, *, timeout_secs: float) -> bool:
    state_path = Path.home() / ".claude" / "channels" / "longhouse" / "sessions" / f"{session_id}.json"
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = None
        if isinstance(state, dict) and state.get("ready"):
            return True
        time.sleep(0.1)
    return False


def transcript_paths(session_id: str) -> list[Path]:
    return sorted((Path.home() / ".claude" / "projects").glob(f"**/{session_id}.jsonl"))


def text_fragments(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        fragments: list[str] = []
        for key in ("text", "content"):
            if key in value:
                fragments.extend(text_fragments(value[key]))
        return fragments
    if isinstance(value, list):
        fragments = []
        for item in value:
            fragments.extend(text_fragments(item))
        return fragments
    return []


def transcript_line_counts(session_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in transcript_paths(session_id):
        try:
            counts[str(path)] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return counts


def assistant_transcript_contains(
    session_id: str,
    expected: str,
    *,
    after_line_counts: dict[str, int] | None = None,
) -> tuple[bool, str | None, int | None, str | None]:
    for path in transcript_paths(session_id):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        first_candidate_line = int((after_line_counts or {}).get(str(path), 0) or 0) + 1
        for index, line in enumerate(lines, start=1):
            if index < first_candidate_line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "assistant":
                continue
            fragments = text_fragments(row.get("message"))
            if any(expected in fragment for fragment in fragments):
                timestamp = row.get("timestamp")
                return True, str(path), index, timestamp if isinstance(timestamp, str) else None
    return False, None, None, None


def read_json_file(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def run_probe(
    session_id: str,
    *,
    output_dir: Path,
    run_id: str,
    duration_secs: float,
    repo_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    root = repo_root or default_repo_root()
    probe = root / "scripts" / "ops" / "probe-managed-claude-truth.py"
    return subprocess.run(
        [
            str(probe),
            "--session-id",
            session_id,
            "--duration-secs",
            str(duration_secs),
            "--interval-secs",
            "1",
            "--run-id",
            run_id,
            "--output-dir",
            str(output_dir),
        ],
        cwd=str(root),
        text=True,
        capture_output=True,
        check=False,
        timeout=max(30.0, duration_secs + 20.0),
    )


def build_channel_send_command(session_id: str, text: str, *, meta: dict[str, str] | None = None) -> list[str]:
    command = ["longhouse", "claude-channel", "send", "--session-id", session_id, "--text", text]
    for key, value in (meta or {}).items():
        command.extend(["--meta", f"{key}={value}"])
    return command


def channel_send(
    session_id: str,
    text: str,
    *,
    meta: dict[str, str] | None = None,
    repo_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        build_channel_send_command(session_id, text, meta=meta),
        cwd=str(repo_root or default_repo_root()),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _terminate_process(process: subprocess.Popen[Any], recorder: Recorder, session_id: str | None) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        recorder.write("process_terminated_after_timeout", session_id=session_id)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        recorder.write("process_killed_after_timeout", session_id=session_id)
        process.wait(timeout=10)


def _write_summary_markdown(path: Path, summary: dict[str, Any]) -> None:
    hosted_terminal = (
        f"{summary.get('hosted_terminal_state') or '-'} / "
        f"{summary.get('hosted_terminal_reason') or '-'} / "
        f"{summary.get('hosted_terminal_source') or '-'}"
    )
    serializer_avg = summary.get("hosted_write_serializer_avg_wait_ms")
    serializer_max = summary.get("hosted_write_serializer_max_wait_ms")
    write_serializer = f"{serializer_avg} / {serializer_max}"
    lines = [
        "# Managed Claude POC",
        "",
        f"- Run: `{summary.get('run_id')}`",
        f"- Session: `{summary.get('session_id') or '-'}`",
        f"- Channel ready: `{summary.get('channel_ready')}`",
        f"- Prompt sent: `{summary.get('sent_prompt')}`",
        f"- Prompt send return code: `{summary.get('prompt_send_returncode')}`",
        f"- Steer requested: `{summary.get('steer_requested')}`",
        f"- Steer sent: `{summary.get('steer_sent')}`",
        f"- Steer send return code: `{summary.get('steer_send_returncode')}`",
        f"- Expected response observed: `{summary.get('observed_expected')}`",
        f"- Success: `{summary.get('success')}`",
        f"- Process return code: `{summary.get('process_returncode')}`",
        f"- Hosted terminal: `{hosted_terminal}`",
        f"- Expected graceful terminal source: `{summary.get('expected_terminal_source_ok')}`",
        f"- Hosted archive events: `{summary.get('hosted_archive_event_count')}`",
        f"- Hosted assistant archive events: `{summary.get('hosted_archive_assistant_events')}`",
        f"- Hosted terminal event sources: `{', '.join(summary.get('hosted_terminal_event_sources') or []) or '-'}`",
        f"- Hosted WriteSerializer avg/max wait ms: `{write_serializer}`",
        f"- Terminal log: `{summary.get('terminal_log')}`",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_managed_claude_live_session(config: ManagedClaudeLiveConfig) -> dict[str, Any]:
    repo_root = config.repo_root or default_repo_root()
    run_id = config.run_id or run_id_now()
    output_dir = config.output_dir or (default_output_root(repo_root) / run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(output_dir / "events.jsonl", run_id)
    terminal_log = output_dir / "terminal.log"

    command = [
        "longhouse",
        "claude",
        "--cwd",
        str(config.cwd.resolve()),
        "--project",
        config.project,
        "--name",
        config.name,
        "--no-open",
    ]
    env = os.environ.copy()
    if config.model:
        env["ANTHROPIC_MODEL"] = config.model

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        command,
        cwd=str(repo_root),
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)
    set_nonblocking(master_fd)
    selector = selectors.DefaultSelector()
    selector.register(master_fd, selectors.EVENT_READ)

    recorder.write("launch_started", command=command, model=config.model, output_dir=str(output_dir))
    clean_buffer = ""
    compact_buffer = ""
    session_id: str | None = None
    confirmed_workspace_trust = False
    confirmed_warning = False
    channel_ready = False
    prompt_send_attempted = False
    sent_prompt = False
    prompt_send_returncode: int | None = None
    prompt_sent_at: float | None = None
    steer_send_attempted = False
    steer_sent = False
    steer_send_returncode: int | None = None
    steer_transcript_cursor: dict[str, int] | None = None
    observed_expected = False
    transcript_path: str | None = None
    transcript_line: int | None = None
    transcript_timestamp: str | None = None
    exit_sent = False
    probe_proc: subprocess.Popen[str] | None = None

    deadline = time.monotonic() + config.launch_timeout_secs + config.response_timeout_secs
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                recorder.write("process_exited", returncode=proc.returncode)
                break
            for key, _ in selector.select(timeout=0.1):
                try:
                    data = os.read(key.fd, 8192)
                except BlockingIOError:
                    continue
                if not data:
                    continue
                text = append_terminal_log(terminal_log, data)
                stripped = strip_terminal_controls(text)
                clean_buffer = (clean_buffer + stripped)[-12000:]
                compact_buffer = (compact_buffer + compact_terminal_text(stripped))[-12000:]

                if not session_id:
                    match = SESSION_ID_RE.search(clean_buffer)
                    if match:
                        session_id = match.group(1)
                        recorder.write("session_id_observed", session_id=session_id)
                        if config.session_id_file:
                            config.session_id_file.parent.mkdir(parents=True, exist_ok=True)
                            config.session_id_file.write_text(session_id + "\n", encoding="utf-8")

                if not confirmed_workspace_trust and "Yes,Itrustthisfolder" in compact_buffer:
                    os.write(master_fd, b"\r")
                    confirmed_workspace_trust = True
                    recorder.write("workspace_trust_confirmed", session_id=session_id)

                if not confirmed_warning and "Iamusingthisforlocaldevelopment" in compact_buffer:
                    os.write(master_fd, b"\r")
                    confirmed_warning = True
                    recorder.write("development_channel_warning_confirmed", session_id=session_id)

                if session_id and confirmed_warning and not prompt_send_attempted:
                    channel_ready = wait_for_channel_ready(session_id, timeout_secs=0.2)
                    if channel_ready:
                        if not config.skip_live_probe:
                            probe_output_dir = output_dir / "live_probe"
                            probe_proc = subprocess.Popen(
                                [
                                    str(repo_root / "scripts" / "ops" / "probe-managed-claude-truth.py"),
                                    "--session-id",
                                    session_id,
                                    "--duration-secs",
                                    str(max(5.0, config.response_timeout_secs - 5.0)),
                                    "--interval-secs",
                                    "1",
                                    "--output-dir",
                                    str(probe_output_dir),
                                    "--run-id",
                                    f"{run_id}-live",
                                ],
                                cwd=str(repo_root),
                                text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                            )
                        send = channel_send(session_id, config.prompt, repo_root=repo_root)
                        prompt_send_attempted = True
                        prompt_send_returncode = send.returncode
                        recorder.write(
                            "prompt_sent",
                            session_id=session_id,
                            returncode=send.returncode,
                            stdout=send.stdout[-1000:],
                            stderr=send.stderr[-1000:],
                        )
                        sent_prompt = send.returncode == 0
                        prompt_sent_at = time.monotonic()

            if (
                session_id
                and sent_prompt
                and config.steer_text
                and not steer_send_attempted
                and prompt_sent_at is not None
                and time.monotonic() >= prompt_sent_at + config.steer_delay_secs
            ):
                steer_transcript_cursor = transcript_line_counts(session_id)
                steer = channel_send(session_id, config.steer_text, meta={"intent": "steer"}, repo_root=repo_root)
                steer_send_attempted = True
                steer_send_returncode = steer.returncode
                recorder.write(
                    "steer_sent",
                    session_id=session_id,
                    returncode=steer.returncode,
                    stdout=steer.stdout[-1000:],
                    stderr=steer.stderr[-1000:],
                    transcript_cursor=steer_transcript_cursor,
                )
                steer_sent = steer.returncode == 0

            if session_id and sent_prompt and (not config.steer_text or steer_sent) and not observed_expected:
                (
                    observed_expected,
                    transcript_path,
                    transcript_line,
                    transcript_timestamp,
                ) = assistant_transcript_contains(
                    session_id,
                    config.steer_expected if config.steer_text else config.expected,
                    after_line_counts=steer_transcript_cursor if config.steer_text else None,
                )
                if observed_expected:
                    recorder.write(
                        "assistant_transcript_observed",
                        session_id=session_id,
                        expected=config.steer_expected if config.steer_text else config.expected,
                        transcript_path=transcript_path,
                        transcript_line=transcript_line,
                        transcript_timestamp=transcript_timestamp,
                    )
                    os.write(master_fd, b"/exit\r")
                    exit_sent = True
                    recorder.write("exit_sent", session_id=session_id)

            if sent_prompt and not observed_expected and (time.monotonic() > deadline - 5):
                break

        if sent_prompt and not exit_sent:
            os.write(master_fd, b"/exit\r")
            recorder.write("exit_sent_after_timeout", session_id=session_id)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _terminate_process(proc, recorder, session_id)
        recorder.write("process_exit_final", session_id=session_id, returncode=proc.returncode)
    finally:
        selector.close()
        os.close(master_fd)

    if probe_proc is not None:
        try:
            stdout, stderr = probe_proc.communicate(timeout=config.response_timeout_secs + 30)
        except subprocess.TimeoutExpired:
            probe_proc.terminate()
            stdout, stderr = probe_proc.communicate(timeout=10)
        recorder.write(
            "live_probe_finished",
            session_id=session_id,
            returncode=probe_proc.returncode,
            stdout=(stdout or "")[-1000:],
            stderr=(stderr or "")[-1000:],
        )

    post_close_summary = None
    if session_id and not config.skip_post_close_probe:
        post_close_dir = output_dir / "post_close_probe"
        post = run_probe(
            session_id,
            output_dir=post_close_dir,
            run_id=f"{run_id}-post-close",
            duration_secs=config.post_close_probe_secs,
            repo_root=repo_root,
        )
        post_close_summary = post_close_dir / "summary.json"
        recorder.write(
            "post_close_probe_finished",
            session_id=session_id,
            returncode=post.returncode,
            stdout=post.stdout[-1000:],
            stderr=post.stderr[-1000:],
        )
    post_close_data = read_json_file(post_close_summary)

    summary = {
        "run_id": run_id,
        "session_id": session_id,
        "channel_ready": channel_ready,
        "development_channel_warning_confirmed": confirmed_warning,
        "workspace_trust_confirmed": confirmed_workspace_trust,
        "sent_prompt": sent_prompt,
        "prompt_send_returncode": prompt_send_returncode,
        "steer_requested": bool(config.steer_text),
        "steer_sent": steer_sent,
        "steer_send_returncode": steer_send_returncode,
        "steer_expected": config.steer_expected,
        "observed_expected": observed_expected,
        "observed_transcript_path": transcript_path,
        "observed_transcript_line": transcript_line,
        "observed_transcript_timestamp": transcript_timestamp,
        "process_returncode": proc.returncode,
        "terminal_log": str(terminal_log),
        "events_path": str(output_dir / "events.jsonl"),
        "post_close_summary": str(post_close_summary) if post_close_summary else None,
        "hosted_terminal_state": (post_close_data or {}).get("hosted_terminal_state"),
        "hosted_terminal_reason": (post_close_data or {}).get("hosted_terminal_reason"),
        "hosted_terminal_source": (post_close_data or {}).get("hosted_terminal_source"),
        "hosted_archive_event_count": (post_close_data or {}).get("hosted_archive_event_count"),
        "hosted_archive_assistant_events": (post_close_data or {}).get("hosted_archive_assistant_events"),
        "hosted_transcript_revision": (post_close_data or {}).get("hosted_transcript_revision"),
        "hosted_terminal_event_count": (post_close_data or {}).get("hosted_terminal_event_count"),
        "hosted_terminal_event_sources": (post_close_data or {}).get("hosted_terminal_event_sources"),
        "hosted_write_serializer_avg_wait_ms": (post_close_data or {}).get("hosted_write_serializer_avg_wait_ms"),
        "hosted_write_serializer_max_wait_ms": (post_close_data or {}).get("hosted_write_serializer_max_wait_ms"),
    }
    summary["post_close_skipped"] = config.skip_post_close_probe
    summary["expected_terminal_source_ok"] = (
        True if config.skip_post_close_probe else summary.get("hosted_terminal_source") == "claude_channel_wrapper"
    )
    summary["success"] = bool(
        session_id
        and sent_prompt
        and (not config.steer_text or steer_sent)
        and observed_expected
        and proc.returncode == 0
        and summary["expected_terminal_source_ok"]
    )
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_summary_markdown(output_dir / "summary.md", summary)
    return summary


__all__ = [
    "ManagedClaudeLiveConfig",
    "Recorder",
    "append_terminal_log",
    "assistant_transcript_contains",
    "build_channel_send_command",
    "channel_send",
    "compact_terminal_text",
    "default_output_root",
    "default_repo_root",
    "monotonic_ms",
    "read_json_file",
    "run_id_now",
    "run_managed_claude_live_session",
    "run_probe",
    "set_nonblocking",
    "strip_terminal_controls",
    "text_fragments",
    "transcript_line_counts",
    "transcript_paths",
    "utc_now",
    "wait_for_channel_ready",
]
