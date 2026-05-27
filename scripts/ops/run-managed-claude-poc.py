#!/usr/bin/env python3
"""Run one managed Claude channel POC under a PTY.

This is still a probe, not an SLA gate. It automates the manual lifecycle:
launch managed Claude, confirm the local channel-development prompt, inject one
managed-channel message, observe the expected response, exit, and capture truth
snapshots.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import selectors
import subprocess
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "managed-claude-poc"
SESSION_ID_RE = re.compile(r"Session ID:\s*([0-9a-fA-F-]{36})")
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def run_id_now() -> str:
    return datetime.now(timezone.utc).strftime("managed-claude-poc-%Y%m%dT%H%M%SZ")


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
) -> subprocess.CompletedProcess[str]:
    probe = ROOT / "scripts" / "ops" / "probe-managed-claude-truth.py"
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
        cwd=str(ROOT),
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


def channel_send(session_id: str, text: str, *, meta: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        build_channel_send_command(session_id, text, meta=meta),
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=ROOT)
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--name", default="Claude propagation POC")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Per-process ANTHROPIC_MODEL override.")
    parser.add_argument("--prompt", default="Please reply with exactly: LONGHOUSE CLAUDE PROFILE READY")
    parser.add_argument("--expected", default="LONGHOUSE CLAUDE PROFILE READY")
    parser.add_argument(
        "--steer-text",
        help="Optional active-turn channel correction to send after the initial prompt.",
    )
    parser.add_argument(
        "--steer-expected",
        help="Assistant text that must appear in a new transcript row after --steer-text is sent.",
    )
    parser.add_argument(
        "--steer-delay-secs",
        type=float,
        default=2.0,
        help="Seconds after prompt send before injecting --steer-text with intent=steer metadata.",
    )
    parser.add_argument("--run-id", default=run_id_now())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--launch-timeout-secs", type=float, default=45.0)
    parser.add_argument("--response-timeout-secs", type=float, default=60.0)
    parser.add_argument("--post-close-probe-secs", type=float, default=0.0)
    parser.add_argument(
        "--skip-post-close-probe",
        action="store_true",
        help="Do not run the post-close truth probe before returning.",
    )
    parser.add_argument(
        "--skip-live-probe",
        action="store_true",
        help="Do not run the concurrent truth probe while waiting for the managed response.",
    )
    parser.add_argument(
        "--session-id-file",
        type=Path,
        help="Optional path to write the managed session id as soon as Claude prints it.",
    )
    args = parser.parse_args()
    if args.steer_delay_secs < 0:
        parser.error("--steer-delay-secs must be >= 0")
    if args.steer_text and not args.steer_expected:
        parser.error("--steer-expected is required when --steer-text is set")
    return args


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or (DEFAULT_OUTPUT_ROOT / args.run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(output_dir / "events.jsonl", args.run_id)
    terminal_log = output_dir / "terminal.log"

    command = [
        "longhouse",
        "claude",
        "--cwd",
        str(args.cwd.resolve()),
        "--project",
        args.project,
        "--name",
        args.name,
        "--no-open",
    ]
    env = os.environ.copy()
    if args.model:
        env["ANTHROPIC_MODEL"] = args.model

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
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

    recorder.write("launch_started", command=command, model=args.model, output_dir=str(output_dir))
    buffer = ""
    clean_buffer = ""
    compact_buffer = ""
    session_id: str | None = None
    confirmed_workspace_trust = False
    confirmed_warning = False
    sent_prompt = False
    prompt_sent_at: float | None = None
    steer_sent = False
    steer_transcript_cursor: dict[str, int] | None = None
    observed_expected = False
    exit_sent = False
    probe_proc: subprocess.Popen[str] | None = None

    deadline = time.monotonic() + args.launch_timeout_secs + args.response_timeout_secs
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
                buffer = (buffer + text)[-12000:]
                clean_buffer = (clean_buffer + strip_terminal_controls(text))[-12000:]
                compact_buffer = (compact_buffer + compact_terminal_text(strip_terminal_controls(text)))[-12000:]

                if not session_id:
                    match = SESSION_ID_RE.search(clean_buffer)
                    if match:
                        session_id = match.group(1)
                        recorder.write("session_id_observed", session_id=session_id)
                        if args.session_id_file:
                            args.session_id_file.parent.mkdir(parents=True, exist_ok=True)
                            args.session_id_file.write_text(session_id + "\n", encoding="utf-8")

                if not confirmed_workspace_trust and "Yes,Itrustthisfolder" in compact_buffer:
                    os.write(master_fd, b"\r")
                    confirmed_workspace_trust = True
                    recorder.write("workspace_trust_confirmed", session_id=session_id)

                if not confirmed_warning and "Iamusingthisforlocaldevelopment" in compact_buffer:
                    os.write(master_fd, b"\r")
                    confirmed_warning = True
                    recorder.write("development_channel_warning_confirmed", session_id=session_id)

                if session_id and confirmed_warning and not sent_prompt:
                    if wait_for_channel_ready(session_id, timeout_secs=0.2):
                        if not args.skip_live_probe:
                            probe_output_dir = output_dir / "live_probe"
                            probe_proc = subprocess.Popen(
                                [
                                    str(ROOT / "scripts" / "ops" / "probe-managed-claude-truth.py"),
                                    "--session-id",
                                    session_id,
                                    "--duration-secs",
                                    str(max(5.0, args.response_timeout_secs - 5.0)),
                                    "--interval-secs",
                                    "1",
                                    "--output-dir",
                                    str(probe_output_dir),
                                    "--run-id",
                                    f"{args.run_id}-live",
                                ],
                                cwd=str(ROOT),
                                text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                            )
                        send = channel_send(session_id, args.prompt)
                        recorder.write(
                            "prompt_sent",
                            session_id=session_id,
                            returncode=send.returncode,
                            stdout=send.stdout[-1000:],
                            stderr=send.stderr[-1000:],
                        )
                        sent_prompt = True
                        prompt_sent_at = time.monotonic()

            if (
                session_id
                and sent_prompt
                and args.steer_text
                and not steer_sent
                and prompt_sent_at is not None
                and time.monotonic() >= prompt_sent_at + args.steer_delay_secs
            ):
                steer_transcript_cursor = transcript_line_counts(session_id)
                steer = channel_send(session_id, args.steer_text, meta={"intent": "steer"})
                recorder.write(
                    "steer_sent",
                    session_id=session_id,
                    returncode=steer.returncode,
                    stdout=steer.stdout[-1000:],
                    stderr=steer.stderr[-1000:],
                    transcript_cursor=steer_transcript_cursor,
                )
                steer_sent = True

            if session_id and sent_prompt and (not args.steer_text or steer_sent) and not observed_expected:
                (
                    observed_expected,
                    transcript_path,
                    transcript_line,
                    transcript_timestamp,
                ) = assistant_transcript_contains(
                    session_id,
                    args.steer_expected if args.steer_text else args.expected,
                    after_line_counts=steer_transcript_cursor if args.steer_text else None,
                )
                if observed_expected:
                    recorder.write(
                        "assistant_transcript_observed",
                        session_id=session_id,
                        expected=args.steer_expected if args.steer_text else args.expected,
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
            proc.terminate()
            recorder.write("process_terminated_after_timeout", session_id=session_id)
            proc.wait(timeout=10)
        recorder.write("process_exit_final", session_id=session_id, returncode=proc.returncode)
    finally:
        selector.close()
        os.close(master_fd)

    if probe_proc is not None:
        try:
            stdout, stderr = probe_proc.communicate(timeout=args.response_timeout_secs + 30)
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
    if session_id and not args.skip_post_close_probe:
        post_close_dir = output_dir / "post_close_probe"
        post = run_probe(
            session_id,
            output_dir=post_close_dir,
            run_id=f"{args.run_id}-post-close",
            duration_secs=args.post_close_probe_secs,
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
        "run_id": args.run_id,
        "session_id": session_id,
        "sent_prompt": sent_prompt,
        "steer_requested": bool(args.steer_text),
        "steer_sent": steer_sent,
        "steer_expected": args.steer_expected,
        "observed_expected": observed_expected,
        "process_returncode": proc.returncode,
        "terminal_log": str(terminal_log),
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
    summary["post_close_skipped"] = args.skip_post_close_probe
    summary["expected_terminal_source_ok"] = (
        True if args.skip_post_close_probe else summary.get("hosted_terminal_source") == "claude_channel_wrapper"
    )
    success = bool(
        session_id
        and sent_prompt
        and (not args.steer_text or steer_sent)
        and observed_expected
        and proc.returncode == 0
        and summary["expected_terminal_source_ok"]
    )
    summary["success"] = success
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hosted_terminal = (
        f"{summary.get('hosted_terminal_state') or '-'} / "
        f"{summary.get('hosted_terminal_reason') or '-'} / "
        f"{summary.get('hosted_terminal_source') or '-'}"
    )
    write_serializer = (
        f"{summary.get('hosted_write_serializer_avg_wait_ms')} / {summary.get('hosted_write_serializer_max_wait_ms')}"
    )
    lines = [
        "# Managed Claude POC",
        "",
        f"- Run: `{args.run_id}`",
        f"- Session: `{session_id or '-'}`",
        f"- Prompt sent: `{sent_prompt}`",
        f"- Steer requested: `{summary.get('steer_requested')}`",
        f"- Steer sent: `{summary.get('steer_sent')}`",
        f"- Expected response observed: `{observed_expected}`",
        f"- Success: `{success}`",
        f"- Process return code: `{proc.returncode}`",
        f"- Hosted terminal: `{hosted_terminal}`",
        f"- Expected graceful terminal source: `{summary.get('expected_terminal_source_ok')}`",
        f"- Hosted archive events: `{summary.get('hosted_archive_event_count')}`",
        f"- Hosted assistant archive events: `{summary.get('hosted_archive_assistant_events')}`",
        f"- Hosted terminal event sources: `{', '.join(summary.get('hosted_terminal_event_sources') or []) or '-'}`",
        f"- Hosted WriteSerializer avg/max wait ms: `{write_serializer}`",
        f"- Terminal log: `{terminal_log}`",
    ]
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output_dir / "summary.md")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
