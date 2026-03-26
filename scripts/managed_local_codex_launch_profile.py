#!/usr/bin/env python3
"""Profile hosted managed-local Codex launch latency.

This harness is intentionally narrow:
- launch a real managed-local Codex session through `/api/sessions/managed-local/this-device`
- measure end-to-end API latency separately from local tmux/Codex prompt readiness
- attribute runner-side time to concrete launch phases by parsing the local runner log

It is meant to answer one question: where is managed-local Codex launch time
actually going, and which bucket should we optimize next?
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "server"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_has_session_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
RUNNER_JOB_START_RE = re.compile(r"^\[executor\] Starting job (?P<job_id>[0-9a-f-]+): (?P<command>.*)$")
RUNNER_JOB_COMPLETE_RE = re.compile(
    r"^\[executor\] Job (?P<job_id>[0-9a-f-]+) completed: exit_code=(?P<exit_code>-?\d+), duration=(?P<duration_ms>\d+)ms, timed_out=(?P<timed_out>true|false)$"
)
MCP_TIMEOUT_RE = re.compile(r"MCP client for `([^`]+)` timed out after (\d+) seconds", re.IGNORECASE)

DEFAULT_RUNNER_LOG_PATH = Path.home() / ".local" / "share" / "longhouse-runner" / "state" / "runner.log"


@dataclass(frozen=True)
class RunnerJobSample:
    job_id: str
    kind: str
    duration_ms: int | None
    exit_code: int | None
    timed_out: bool
    command: str


@dataclass(frozen=True)
class LaunchSample:
    sample_index: int
    session_id: str | None
    session_name: str | None
    api_status: int | None
    api_elapsed_ms: int | None
    tmux_appeared_after_api_ms: int | None
    ready_after_api_ms: int | None
    total_to_ready_ms: int | None
    api_unattributed_ms: int | None
    pane_blockers: tuple[str, ...]
    runner_jobs: tuple[RunnerJobSample, ...]
    error: str | None = None
    attach_command: str | None = None
    tmux_tmpdir: str | None = None
    pane_tail: str | None = None


@dataclass(frozen=True)
class ApiLaunchResponse:
    status_code: int
    elapsed_ms: int
    payload: dict[str, object] | None
    error: str | None = None


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text or "").replace("\r", "")


def _run_shell(command: str, *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=True,
        executable="/bin/zsh",
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _parse_tmux_tmpdir_from_attach_command(attach_command: str) -> str | None:
    parts = shlex.split(attach_command)
    if len(parts) < 3 or parts[0] != "zsh" or parts[1] != "-lc":
        return None
    inner = parts[2]
    match = re.search(r"(^|;)\s*export TMUX_TMPDIR=(.+?)(?=;|$)", inner)
    if match is None:
        return None
    assignment = shlex.split(f"TMUX_TMPDIR={match.group(2)}")
    if len(assignment) != 1 or not assignment[0].startswith("TMUX_TMPDIR="):
        return None
    value = assignment[0].split("=", 1)[1].strip()
    return value or None


def _capture_tmux_pane(*, session_name: str, tmux_tmpdir: str | None, lines: int = 200) -> str:
    completed = _run_shell(
        build_tmux_capture_command(session_name=session_name, lines=lines, tmux_tmpdir=tmux_tmpdir),
        timeout=15.0,
    )
    return _strip_ansi(completed.stdout or completed.stderr or "")


def _has_tmux_session(*, session_name: str, tmux_tmpdir: str | None) -> bool:
    completed = _run_shell(
        build_tmux_has_session_command(session_name=session_name, tmux_tmpdir=tmux_tmpdir),
        timeout=10.0,
    )
    return completed.returncode == 0


def _kill_tmux_session(*, session_name: str, tmux_tmpdir: str | None) -> None:
    _run_shell(
        build_tmux_kill_session_command(session_name=session_name, tmux_tmpdir=tmux_tmpdir),
        timeout=10.0,
    )


def _pane_is_ready(pane: str) -> bool:
    text = _strip_ansi(pane)
    blocked_markers = (
        "Starting MCP servers",
        "Loading conversation history",
    )
    return "OpenAI Codex" in text and not any(marker in text for marker in blocked_markers)


def extract_pane_blockers(pane: str) -> tuple[str, ...]:
    text = _strip_ansi(pane)
    blockers: list[str] = []

    if "Starting MCP servers" in text:
        blockers.append("starting_mcp_servers")
    if "Loading conversation history" in text:
        blockers.append("loading_conversation_history")
    if "model:     loading" in text or "model: loading" in text:
        blockers.append("model_loading")
    if "MCP startup incomplete" in text:
        blockers.append("mcp_startup_incomplete")
    for match in MCP_TIMEOUT_RE.finditer(text):
        blockers.append(f"mcp_timeout:{match.group(1)}:{match.group(2)}s")

    deduped: list[str] = []
    for blocker in blockers:
        if blocker not in deduped:
            deduped.append(blocker)
    return tuple(deduped)


def classify_runner_command(command: str) -> str:
    normalized = (command or "").strip().lower()
    if "__longhouse_tmux_tmpdir__=" in normalized:
        return "preflight"
    if "longhouse connect --hooks-only" in normalized:
        return "hooks_ensure"
    if "tmux -l longhouse-managed start-server" in normalized and "new-session -d" in normalized:
        return "tmux_launch"
    if "tmux -l longhouse-managed has-session" in normalized:
        return "tmux_has_session"
    if "tmux -l longhouse-managed display-message" in normalized:
        return "tmux_display"
    if "tmux -l longhouse-managed capture-pane" in normalized:
        return "tmux_capture"
    if "tmux -l longhouse-managed kill-session" in normalized:
        return "tmux_kill_session"
    return "other"


def parse_runner_jobs(log_text: str) -> list[RunnerJobSample]:
    jobs: dict[str, dict[str, object]] = {}
    order: list[str] = []
    current_job_id: str | None = None
    current_command_lines: list[str] = []

    def flush_current() -> None:
        nonlocal current_job_id, current_command_lines
        if current_job_id is None:
            return
        command = "\n".join(current_command_lines).rstrip()
        entry = jobs.setdefault(current_job_id, {"job_id": current_job_id})
        entry["command"] = command
        current_job_id = None
        current_command_lines = []

    for raw_line in log_text.splitlines():
        start_match = RUNNER_JOB_START_RE.match(raw_line)
        if start_match:
            flush_current()
            job_id = start_match.group("job_id")
            jobs.setdefault(job_id, {"job_id": job_id})
            if job_id not in order:
                order.append(job_id)
            current_job_id = job_id
            current_command_lines = [start_match.group("command")]
            continue

        complete_match = RUNNER_JOB_COMPLETE_RE.match(raw_line)
        if complete_match:
            flush_current()
            job_id = complete_match.group("job_id")
            entry = jobs.setdefault(job_id, {"job_id": job_id})
            if job_id not in order:
                order.append(job_id)
            entry["duration_ms"] = int(complete_match.group("duration_ms"))
            entry["exit_code"] = int(complete_match.group("exit_code"))
            entry["timed_out"] = complete_match.group("timed_out") == "true"
            continue

        if current_job_id is not None:
            if raw_line.startswith("["):
                flush_current()
            else:
                current_command_lines.append(raw_line)

    flush_current()

    results: list[RunnerJobSample] = []
    for job_id in order:
        entry = jobs.get(job_id, {})
        command = str(entry.get("command") or "")
        results.append(
            RunnerJobSample(
                job_id=job_id,
                kind=classify_runner_command(command),
                duration_ms=int(entry["duration_ms"]) if "duration_ms" in entry else None,
                exit_code=int(entry["exit_code"]) if "exit_code" in entry else None,
                timed_out=bool(entry.get("timed_out", False)),
                command=command,
            )
        )
    return results


def filter_launch_jobs(
    runner_jobs: Iterable[RunnerJobSample],
    *,
    session_name: str | None,
    session_id: str | None,
) -> tuple[RunnerJobSample, ...]:
    jobs = list(runner_jobs)
    if not session_name and not session_id:
        return ()

    launch_index: int | None = None
    for index, job in enumerate(jobs):
        if job.kind != "tmux_launch":
            continue
        command = job.command
        if session_name and session_name in command:
            launch_index = index
            break
        if session_id and session_id in command:
            launch_index = index
            break

    if launch_index is None:
        return ()

    start = launch_index
    while start > 0 and jobs[start - 1].kind in {"preflight", "hooks_ensure"}:
        start -= 1

    end = launch_index
    while end + 1 < len(jobs) and jobs[end + 1].kind in {"tmux_has_session", "tmux_display", "tmux_capture"}:
        end += 1

    return tuple(jobs[start : end + 1])


def _read_log_slice(path: Path, *, offset: int) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    effective_offset = offset if size >= offset else 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(effective_offset)
        return handle.read()


def _wait_for_tmux_session(
    *,
    session_name: str,
    tmux_tmpdir: str | None,
    timeout_secs: float,
) -> tuple[int | None, str]:
    deadline = time.monotonic() + timeout_secs
    start = time.monotonic()
    last_pane = ""
    while time.monotonic() < deadline:
        if _has_tmux_session(session_name=session_name, tmux_tmpdir=tmux_tmpdir):
            return int((time.monotonic() - start) * 1000), last_pane
        time.sleep(0.5)
    if _has_tmux_session(session_name=session_name, tmux_tmpdir=tmux_tmpdir):
        return int((time.monotonic() - start) * 1000), last_pane
    return None, last_pane


def _wait_for_ready_prompt(
    *,
    session_name: str,
    tmux_tmpdir: str | None,
    timeout_secs: float,
) -> tuple[int | None, str]:
    deadline = time.monotonic() + timeout_secs
    start = time.monotonic()
    last_pane = ""
    while time.monotonic() < deadline:
        pane = _capture_tmux_pane(session_name=session_name, tmux_tmpdir=tmux_tmpdir)
        last_pane = pane
        if _pane_is_ready(pane):
            return int((time.monotonic() - start) * 1000), pane
        time.sleep(1.0)
    return None, last_pane


def _launch_session(
    *,
    api_url: str,
    device_token: str,
    cwd: str,
    project: str,
    display_name: str,
    loop_mode: str,
    machine_name: str | None,
    api_timeout_secs: float,
) -> ApiLaunchResponse:
    body: dict[str, object] = {
        "cwd": cwd,
        "provider": "codex",
        "project": project,
        "display_name": display_name,
        "loop_mode": loop_mode,
    }
    if machine_name:
        body["machine_name"] = machine_name

    timeout = httpx.Timeout(connect=20.0, read=api_timeout_secs, write=20.0, pool=20.0)
    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{api_url.rstrip('/')}/api/sessions/managed-local/this-device",
                headers={"X-Agents-Token": device_token},
                json=body,
            )
    except httpx.TimeoutException:
        return ApiLaunchResponse(
            status_code=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            payload=None,
            error=f"launch request timed out after {api_timeout_secs}s",
        )
    except httpx.HTTPError as exc:
        return ApiLaunchResponse(
            status_code=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            payload=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    payload: dict[str, object] | None = None
    if response.status_code == 200:
        try:
            parsed = response.json()
        except ValueError:
            return ApiLaunchResponse(status_code=response.status_code, elapsed_ms=elapsed_ms, payload=None, error="malformed JSON")
        if isinstance(parsed, dict):
            payload = parsed
    else:
        preview = response.text[:400]
        return ApiLaunchResponse(
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            payload=None,
            error=f"launch status={response.status_code} body={preview}",
        )

    return ApiLaunchResponse(status_code=response.status_code, elapsed_ms=elapsed_ms, payload=payload)


def _sum_phase_durations(runner_jobs: Iterable[RunnerJobSample]) -> int:
    return sum(job.duration_ms or 0 for job in runner_jobs if job.kind != "other")


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    return int(statistics.median(values))


def _build_sample_labels(*, base_project: str, base_display_name: str, index: int, nonce: str) -> tuple[str, str]:
    suffix = f"{index:02d}-{nonce}"
    project = f"{base_project}-{suffix}"
    display_name = f"{base_display_name} {suffix}"
    return project, display_name


def run_sample(
    *,
    sample_index: int,
    api_url: str,
    device_token: str,
    cwd: str,
    base_project: str,
    base_display_name: str,
    loop_mode: str,
    machine_name: str | None,
    api_timeout_secs: float,
    verify_timeout_secs: float,
    runner_log_path: Path,
    keep_session: bool,
    nonce: str,
) -> LaunchSample:
    runner_log_offset = runner_log_path.stat().st_size if runner_log_path.exists() else 0
    project, display_name = _build_sample_labels(
        base_project=base_project,
        base_display_name=base_display_name,
        index=sample_index,
        nonce=nonce,
    )

    launch = _launch_session(
        api_url=api_url,
        device_token=device_token,
        cwd=cwd,
        project=project,
        display_name=display_name,
        loop_mode=loop_mode,
        machine_name=machine_name,
        api_timeout_secs=api_timeout_secs,
    )

    session_id: str | None = None
    session_name: str | None = None
    attach_command: str | None = None
    tmux_tmpdir: str | None = None
    tmux_appeared_after_api_ms: int | None = None
    ready_after_api_ms: int | None = None
    total_to_ready_ms: int | None = None
    pane_tail: str | None = None
    pane_blockers: tuple[str, ...] = ()
    error = launch.error

    if launch.payload is not None:
        session_id = str(launch.payload.get("session_id") or "")
        session_name = str(launch.payload.get("managed_session_name") or "")
        attach_command = str(launch.payload.get("attach_command") or "")
        if session_id and session_name and attach_command:
            tmux_tmpdir = _parse_tmux_tmpdir_from_attach_command(attach_command)

            tmux_appeared_after_api_ms, pane_before_ready = _wait_for_tmux_session(
                session_name=session_name,
                tmux_tmpdir=tmux_tmpdir,
                timeout_secs=verify_timeout_secs,
            )
            if tmux_appeared_after_api_ms is None:
                pane_tail = pane_before_ready[-1200:] if pane_before_ready else None
                error = error or f"tmux session {session_name!r} never appeared"
            else:
                ready_after_api_ms, ready_pane = _wait_for_ready_prompt(
                    session_name=session_name,
                    tmux_tmpdir=tmux_tmpdir,
                    timeout_secs=verify_timeout_secs,
                )
                pane_tail = ready_pane[-1200:] if ready_pane else None
                pane_blockers = extract_pane_blockers(ready_pane)
                if ready_after_api_ms is None:
                    error = error or "Codex never reached an idle prompt before timeout"
                else:
                    total_to_ready_ms = launch.elapsed_ms + ready_after_api_ms

    log_text = _read_log_slice(runner_log_path, offset=runner_log_offset)
    parsed_jobs = parse_runner_jobs(log_text)
    runner_jobs = filter_launch_jobs(parsed_jobs, session_name=session_name, session_id=session_id)
    api_unattributed_ms = None
    if launch.elapsed_ms is not None:
        api_unattributed_ms = launch.elapsed_ms - _sum_phase_durations(runner_jobs)

    if session_name and not keep_session:
        _kill_tmux_session(session_name=session_name, tmux_tmpdir=tmux_tmpdir)

    return LaunchSample(
        sample_index=sample_index,
        session_id=session_id or None,
        session_name=session_name or None,
        api_status=launch.status_code or None,
        api_elapsed_ms=launch.elapsed_ms,
        tmux_appeared_after_api_ms=tmux_appeared_after_api_ms,
        ready_after_api_ms=ready_after_api_ms,
        total_to_ready_ms=total_to_ready_ms,
        api_unattributed_ms=api_unattributed_ms,
        pane_blockers=pane_blockers,
        runner_jobs=runner_jobs,
        error=error,
        attach_command=attach_command,
        tmux_tmpdir=tmux_tmpdir,
        pane_tail=pane_tail,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Hosted Longhouse API base URL.")
    parser.add_argument("--device-token", required=True, help="Agents token for managed-local launch.")
    parser.add_argument("--cwd", required=True, help="Working directory to launch Codex in.")
    parser.add_argument("--project-base", default="managed-local-codex-launch-profile", help="Base project label.")
    parser.add_argument("--display-name-base", default="Managed Local Codex Launch Profile", help="Base display name.")
    parser.add_argument("--loop-mode", default="manual", help="Loop mode for the managed-local session.")
    parser.add_argument("--machine-name", default="", help="Optional explicit machine label override.")
    parser.add_argument("--samples", type=int, default=3, help="Number of launch samples to collect.")
    parser.add_argument("--api-timeout-secs", type=float, default=90.0, help="HTTP timeout for the launch POST.")
    parser.add_argument(
        "--verify-timeout-secs",
        type=float,
        default=45.0,
        help="Seconds to wait for tmux appearance and Codex prompt readiness after launch.",
    )
    parser.add_argument(
        "--runner-log-path",
        default=str(DEFAULT_RUNNER_LOG_PATH),
        help=f"Local runner log path used for phase attribution (default: {DEFAULT_RUNNER_LOG_PATH})",
    )
    parser.add_argument("--keep-session", action="store_true", help="Keep tmux sessions alive after each sample.")
    parser.add_argument("--delay-secs", type=float, default=0.0, help="Delay between samples.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of prose.")
    args = parser.parse_args()

    if args.samples <= 0:
        parser.error("--samples must be positive")
    if args.api_timeout_secs <= 0 or args.verify_timeout_secs <= 0:
        parser.error("timeouts must be positive")
    if args.delay_secs < 0:
        parser.error("--delay-secs must be non-negative")
    return args


def _print_sample(sample: LaunchSample) -> None:
    print(f"Sample {sample.sample_index}")
    if sample.session_id:
        print(f"  session_id: {sample.session_id}")
    if sample.session_name:
        print(f"  tmux_session: {sample.session_name}")
    print(f"  api_status: {sample.api_status}")
    print(f"  api_elapsed_ms: {sample.api_elapsed_ms}")
    print(f"  tmux_appeared_after_api_ms: {sample.tmux_appeared_after_api_ms}")
    print(f"  ready_after_api_ms: {sample.ready_after_api_ms}")
    print(f"  total_to_ready_ms: {sample.total_to_ready_ms}")
    print(f"  api_unattributed_ms: {sample.api_unattributed_ms}")
    if sample.pane_blockers:
        print(f"  pane_blockers: {', '.join(sample.pane_blockers)}")
    if sample.error:
        print(f"  error: {sample.error}")
    if sample.runner_jobs:
        print("  runner_jobs:")
        for job in sample.runner_jobs:
            print(
                f"    - kind={job.kind} duration_ms={job.duration_ms} exit_code={job.exit_code} "
                f"timed_out={int(job.timed_out)}"
            )
    if sample.pane_tail:
        print("  pane_tail:")
        for line in sample.pane_tail.strip().splitlines()[-20:]:
            print(f"    {line}")


def _print_summary(samples: list[LaunchSample]) -> None:
    api_values = [value for value in (sample.api_elapsed_ms for sample in samples) if value is not None]
    ready_values = [value for value in (sample.ready_after_api_ms for sample in samples) if value is not None]
    total_values = [value for value in (sample.total_to_ready_ms for sample in samples) if value is not None]
    unattributed_values = [value for value in (sample.api_unattributed_ms for sample in samples) if value is not None]

    phase_buckets: dict[str, list[int]] = {}
    for sample in samples:
        for job in sample.runner_jobs:
            if job.duration_ms is None:
                continue
            phase_buckets.setdefault(job.kind, []).append(job.duration_ms)

    print("Summary")
    print(f"  samples: {len(samples)}")
    if api_values:
        print(f"  api_elapsed_ms: median={_median(api_values)} avg={int(statistics.mean(api_values))}")
    if ready_values:
        print(f"  ready_after_api_ms: median={_median(ready_values)} avg={int(statistics.mean(ready_values))}")
    if total_values:
        print(f"  total_to_ready_ms: median={_median(total_values)} avg={int(statistics.mean(total_values))}")
    if unattributed_values:
        print(f"  api_unattributed_ms: median={_median(unattributed_values)} avg={int(statistics.mean(unattributed_values))}")
    if phase_buckets:
        print("  phase_averages_ms:")
        for kind in sorted(phase_buckets):
            values = phase_buckets[kind]
            print(f"    {kind}: median={_median(values)} avg={int(statistics.mean(values))}")


def main() -> int:
    args = _parse_args()

    nonce = secrets.token_hex(3)
    runner_log_path = Path(args.runner_log_path).expanduser().resolve()
    samples: list[LaunchSample] = []

    for sample_index in range(1, args.samples + 1):
        sample = run_sample(
            sample_index=sample_index,
            api_url=args.api_url,
            device_token=args.device_token,
            cwd=str(Path(args.cwd).expanduser().resolve()),
            base_project=str(args.project_base).strip() or "managed-local-codex-launch-profile",
            base_display_name=str(args.display_name_base).strip() or "Managed Local Codex Launch Profile",
            loop_mode=str(args.loop_mode).strip() or "manual",
            machine_name=str(args.machine_name).strip() or None,
            api_timeout_secs=float(args.api_timeout_secs),
            verify_timeout_secs=float(args.verify_timeout_secs),
            runner_log_path=runner_log_path,
            keep_session=bool(args.keep_session),
            nonce=nonce,
        )
        samples.append(sample)
        if not args.json:
            _print_sample(sample)
            if sample_index != args.samples:
                print("")
        if args.delay_secs and sample_index != args.samples:
            time.sleep(args.delay_secs)

    if args.json:
        print(json.dumps([asdict(sample) for sample in samples], indent=2))
    else:
        print("")
        _print_summary(samples)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
