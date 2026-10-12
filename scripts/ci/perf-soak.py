#!/usr/bin/env python3
"""Measure steady-state Longhouse engine and Desktop costs in a private sandbox."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import perf_soak_fixtures

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "artifacts" / "perf-proof" / "perf-soak.json"
SAMPLE_SECONDS = 0.05
MAC_CHILD_SAMPLE_SECONDS = 0.1
CONSOLE_SKIP_REASON = (
    "Console is omitted: the existing synthetic Runtime Host only implements storage-v2; "
    "a fake Console turn needs the server control/dispatch surface and is not cheaply reachable here."
)


class SoakError(RuntimeError):
    pass


def _git_value(args: Sequence[str]) -> str | None:
    proc = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def default_engine_path() -> Path:
    resolver = ROOT / "scripts" / "build" / "cargo.py"
    result = subprocess.run(
        [sys.executable, str(resolver), "artifact", "--profile", "release", "--bin", "longhouse-engine"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def sandbox_environment(home: Path, longhouse_home: Path, fake_bin: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("LONGHOUSE_") or key == "CLAUDE_CONFIG_DIR":
            env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "CFFIXED_USER_HOME": str(home),
            "LONGHOUSE_HOME": str(longhouse_home),
            "LONGHOUSE_OMP_BIN": str(fake_bin / "omp"),
            "PATH": str(fake_bin) + os.pathsep + os.defpath + os.pathsep + env.get("PATH", ""),
            "TMPDIR": str(home / "tmp"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "RUST_LOG": "error",
        }
    )
    for path_name in ("tmp", ".config", ".cache"):
        (home / path_name).mkdir(parents=True, exist_ok=True)
    return env


def _start_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
        start_new_session=True,
    )


def _stop_process(proc: subprocess.Popen[str] | None, *, timeout: float = 10.0) -> None:
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if proc.poll() is None:
        proc.wait(timeout=5)


def _start_synthetic_host(run_dir: Path, env: dict[str, str]) -> tuple[subprocess.Popen[str], str]:
    port_file = run_dir / "synthetic-host.port"
    host = _start_process(
        [sys.executable, str(ROOT / "scripts" / "qa" / "shipper_synthetic_echo.py"), "--port-file", str(port_file)],
        cwd=ROOT,
        env=env,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if port_file.exists():
            port = port_file.read_text(encoding="utf-8").strip()
            if port:
                return host, f"http://127.0.0.1:{port}"
        if host.poll() is not None:
            raise SoakError("synthetic Runtime Host exited before publishing a port")
        time.sleep(0.05)
    raise SoakError("synthetic Runtime Host did not publish a port")


def _engine_command(engine_bin: Path, url: str) -> list[str]:
    # The installed Machine Agent's cadences (launchd plist on cinder): a soak
    # with the periodic scans turned off would not measure what a machine
    # pays. Archive repair stays paused: it is a bounded backfill, not
    # steady-state cost.
    return [
        str(engine_bin),
        "connect",
        "--url",
        url,
        "--token",
        "synthetic-soak-token",
        "--machine-name",
        "perf-soak-synthetic",
        "--fallback-scan-secs",
        "300",
        "--spool-replay-secs",
        "30",
        "--compression",
        "zstd",
        "--archive-repair-mode",
        "paused",
    ]


def _database_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(path, timeout=0.1) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {"source_epoch_registry", "file_state", "session_binding", "session_phase_state"} <= tables
    except sqlite3.Error:
        return False
def _engine_log_tail(longhouse_home: Path) -> str:
    log_dir = longhouse_home / "agent" / "logs"
    logs = sorted(
        log_dir.glob("engine.log*"),
        key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
    )
    if not logs:
        return "no sandbox engine log was written"
    try:
        return logs[-1].read_text(encoding="utf-8", errors="replace")[-3000:].strip() or "sandbox engine log is empty"
    except OSError as exc:
        return f"could not read sandbox engine log: {exc}"

def _process_stderr_tail(path: Path | None) -> str:
    if path is None or not path.is_file():
        return "no stderr output was captured"
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-3000:].strip() or "stderr was empty"
    except OSError as exc:
        return f"could not read stderr file: {exc}"


def _wait_for_database(
    path: Path, proc: subprocess.Popen[str], stderr_path: Path | None = None
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _database_ready(path):
            return
        if proc.poll() is not None:
            raise SoakError(
                f"engine exited while initializing its database (exit {proc.returncode}); "
                f"{_engine_log_tail(path.parent.parent)}; {_process_stderr_tail(stderr_path)}"
            )
        time.sleep(0.05)
    raise SoakError("engine did not initialize its sandbox database")


def _wait_for_status(
    path: Path, proc: subprocess.Popen[str], stderr_path: Path | None = None
) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        if proc.poll() is not None:
            raise SoakError(
                f"engine exited before writing engine-status.json (exit {proc.returncode}); "
                f"{_engine_log_tail(path.parent.parent)}; {_process_stderr_tail(stderr_path)}"
            )
        time.sleep(0.1)
    raise SoakError("engine did not write engine-status.json within 60 seconds")


def _linux_proc_sample(pid: int) -> dict[str, int | None]:
    proc_dir = Path("/proc") / str(pid)
    try:
        stat = (proc_dir / "stat").read_text(encoding="ascii")
        end = stat.rfind(")")
        fields = stat[end + 2 :].split()
        clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        cpu_ns = (int(fields[11]) + int(fields[12])) * 1_000_000_000 // clock_ticks
        rss = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        write_bytes: int | None = None
        for line in (proc_dir / "io").read_text(encoding="ascii").splitlines():
            if line.startswith("write_bytes:"):
                write_bytes = int(line.split(":", 1)[1].strip())
                break
        return {"cpu_ns": cpu_ns, "write_bytes": write_bytes, "wakeups": None, "rss_bytes": rss}
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError) as exc:
        raise SoakError(f"cannot sample Linux process {pid}: {exc}") from exc


def _linux_children(pid: int) -> set[int]:
    task_dir = Path("/proc") / str(pid) / "task"
    children: set[int] = set()
    try:
        for task in task_dir.iterdir():
            try:
                children.update(int(value) for value in (task / "children").read_text(encoding="ascii").split())
            except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
                continue
    except (FileNotFoundError, PermissionError):
        pass
    return children


_MAC_RUSAGE_TYPE: type[ctypes.Structure] | None = None
_MAC_PROC: Any = None


def _mac_proc_api() -> tuple[Any, type[ctypes.Structure]]:
    global _MAC_PROC, _MAC_RUSAGE_TYPE
    if _MAC_PROC is None:
        class RUsageInfoV4(ctypes.Structure):
            _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
                (name, ctypes.c_uint64)
                for name in (
                    "ri_user_time",
                    "ri_system_time",
                    "ri_pkg_idle_wkups",
                    "ri_interrupt_wkups",
                    "ri_pageins",
                    "ri_wired_size",
                    "ri_resident_size",
                    "ri_phys_footprint",
                    "ri_proc_start_abstime",
                    "ri_proc_exit_abstime",
                    "ri_child_user_time",
                    "ri_child_system_time",
                    "ri_child_pkg_idle_wkups",
                    "ri_child_interrupt_wkups",
                    "ri_child_pageins",
                    "ri_child_elapsed_abstime",
                    "ri_diskio_bytesread",
                    "ri_diskio_byteswritten",
                    "ri_cpu_time_qos_default",
                    "ri_cpu_time_qos_maintenance",
                    "ri_cpu_time_qos_background",
                    "ri_cpu_time_qos_utility",
                    "ri_cpu_time_qos_legacy",
                    "ri_cpu_time_qos_user_initiated",
                    "ri_cpu_time_qos_user_interactive",
                    "ri_billed_system_time",
                    "ri_serviced_system_time",
                    "ri_logical_writes",
                    "ri_lifetime_max_phys_footprint",
                    "ri_instructions",
                    "ri_cycles",
                    "ri_billed_energy",
                    "ri_serviced_energy",
                    "ri_interval_max_phys_footprint",
                    "ri_runnable_time",
                )
            ]
        if ctypes.sizeof(RUsageInfoV4) != 296:
            raise RuntimeError("rusage_info_v4 layout size mismatch")

        _MAC_PROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        _MAC_PROC.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(RUsageInfoV4)]
        _MAC_PROC.proc_pid_rusage.restype = ctypes.c_int
        _MAC_RUSAGE_TYPE = RUsageInfoV4
    assert _MAC_RUSAGE_TYPE is not None
    return _MAC_PROC, _MAC_RUSAGE_TYPE


def _mach_ticks_per_ns() -> tuple[int, int]:
    """`mach_timebase_info` numer/denom: ticks * numer / denom = nanoseconds."""

    class TimebaseInfo(ctypes.Structure):
        _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

    info = TimebaseInfo()
    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    if libsystem.mach_timebase_info(ctypes.byref(info)) != 0 or info.denom == 0:
        raise SoakError("mach_timebase_info failed")
    return info.numer, info.denom


def _mac_proc_sample(pid: int) -> dict[str, int | None]:
    libproc, rusage_type = _mac_proc_api()
    usage = rusage_type()
    if libproc.proc_pid_rusage(pid, 4, ctypes.byref(usage)) != 0:
        error = ctypes.get_errno()
        raise SoakError(f"proc_pid_rusage failed for process {pid}: {os.strerror(error)}")
    # ri_user_time/ri_system_time are mach absolute-time ticks, not ns: on
    # Apple Silicon one tick is 125/3 ns, so reading them as ns understates
    # CPU 41.7x.
    numer, denom = _mach_ticks_per_ns()
    return {
        "cpu_ns": int((usage.ri_user_time + usage.ri_system_time) * numer // denom),
        "write_bytes": int(usage.ri_diskio_byteswritten),
        "wakeups": int(usage.ri_pkg_idle_wkups + usage.ri_interrupt_wkups),
        "rss_bytes": int(usage.ri_resident_size),
    }


def _mac_children(pid: int) -> set[int]:
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid="],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    found: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2:
            try:
                child_pid, parent_pid = map(int, fields)
            except ValueError:
                continue
            if parent_pid == pid:
                found.add(child_pid)
    return found


def _process_sample(pid: int, os_name: str) -> dict[str, int | None]:
    if os_name == "Linux":
        return _linux_proc_sample(pid)
    if os_name == "Darwin":
        return _mac_proc_sample(pid)
    raise SoakError(f"unsupported soak platform: {os_name}")


def _process_children(pid: int, os_name: str) -> set[int]:
    return _linux_children(pid) if os_name == "Linux" else _mac_children(pid)


def _status_stamp(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def sample_window(
    process_ids: dict[str, int],
    *,
    seconds: float,
    os_name: str,
    engine_status_path: Path,
) -> dict[str, Any]:
    start_wall = time.monotonic()
    start_samples = {name: _process_sample(pid, os_name) for name, pid in process_ids.items()}
    peak_rss = {name: int(sample["rss_bytes"] or 0) for name, sample in start_samples.items()}
    initial_children = _process_children(process_ids["engine"], os_name)
    observed_children = set(initial_children)
    last_status_stamp = _status_stamp(engine_status_path)
    status_writes = 0
    deadline = start_wall + seconds
    interval = MAC_CHILD_SAMPLE_SECONDS if os_name == "Darwin" else SAMPLE_SECONDS
    while time.monotonic() < deadline:
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        for name, pid in process_ids.items():
            sample = _process_sample(pid, os_name)
            peak_rss[name] = max(peak_rss[name], int(sample["rss_bytes"] or 0))
        observed_children.update(_process_children(process_ids["engine"], os_name))
        stamp = _status_stamp(engine_status_path)
        if stamp is not None and stamp != last_status_stamp:
            status_writes += 1
            last_status_stamp = stamp
    elapsed = time.monotonic() - start_wall
    end_samples = {name: _process_sample(pid, os_name) for name, pid in process_ids.items()}
    engine_sample = start_samples["engine"]
    engine_end = end_samples["engine"]
    writes = engine_sample["write_bytes"]
    writes_end = engine_end["write_bytes"]
    wakeups = engine_sample["wakeups"]
    wakeups_end = engine_end["wakeups"]
    engine_cpu_percent = max(0.0, int(engine_end["cpu_ns"] or 0) - int(engine_sample["cpu_ns"] or 0)) / (
        elapsed * 10_000_000
    )
    engine_metrics: dict[str, Any] = {
        "cpu_percent": engine_cpu_percent,
        "disk_write_bytes_per_second": (
            max(0, int(writes_end) - int(writes)) / elapsed if writes is not None and writes_end is not None else None
        ),
        "wakeups_per_second": (
            max(0, int(wakeups_end) - int(wakeups)) / elapsed
            if wakeups is not None and wakeups_end is not None
            else None
        ),
        "status_writes_per_minute": status_writes * 60.0 / elapsed,
        "processes_spawned_per_minute": max(0, len(observed_children) - len(initial_children)) * 60.0 / elapsed,
        "peak_rss_bytes": peak_rss["engine"],
        "processes_observed": max(0, len(observed_children) - len(initial_children)),
        "child_poll_interval_ms": int(interval * 1000),
    }
    result: dict[str, Any] = {"engine": engine_metrics}
    if "desktop_app" in process_ids:
        app_start = start_samples["desktop_app"]
        app_end = end_samples["desktop_app"]
        result["desktop_app"] = {
            "cpu_percent": max(0.0, int(app_end["cpu_ns"] or 0) - int(app_start["cpu_ns"] or 0))
            / (elapsed * 10_000_000),
            "disk_write_bytes_per_second": (
                max(0, int(app_end["write_bytes"]) - int(app_start["write_bytes"])) / elapsed
                if app_start["write_bytes"] is not None and app_end["write_bytes"] is not None
                else None
            ),
            "wakeups_per_second": (
                max(0, int(app_end["wakeups"]) - int(app_start["wakeups"])) / elapsed
                if app_start["wakeups"] is not None and app_end["wakeups"] is not None
                else None
            ),
            "peak_rss_bytes": peak_rss["desktop_app"],
        }
    result["duration_seconds"] = elapsed
    result["status_writes_observed"] = status_writes
    return result


def _write_fake_provider_script(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

transcript, db_path, session_id, workspace = sys.argv[1:]
running = True
children = []
def stop(_signum, _frame):
    global running
    running = False
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
db = sqlite3.connect(db_path, timeout=5)
index = 0
while running:
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    phase = 'thinking' if index % 2 == 0 else 'running'
    line = {
        'cwd': workspace,
        'type': 'assistant' if index % 2 else 'user',
        'sessionId': session_id,
        'timestamp': now,
        'message': {'role': 'assistant' if index % 2 else 'user', 'content': [
            {'type': 'text', 'text': 'synthetic steady-state activity: inspect one small file and report progress'}
        ]},
    }
    with open(transcript, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(line, separators=(',', ':')) + '\\n')
        stream.flush()
    db.execute('''INSERT INTO session_phase_state (
        session_id, provider, phase, tool_name, source, observed_at, run_id, revision
    ) VALUES (?, 'claude', ?, ?, 'claude_hook', ?, ?, 1)
    ON CONFLICT(session_id) DO UPDATE SET phase=excluded.phase,
        tool_name=excluded.tool_name, observed_at=excluded.observed_at,
        run_id=excluded.run_id, revision=session_phase_state.revision+1''',
        (session_id, phase, 'Bash' if phase == 'running' else None, now, session_id))
    db.commit()
    if index % 2 == 0:
        children.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.12)']))
    children = [child for child in children if child.poll() is None]
    for child in children:
        child.wait()
    index += 1
    time.sleep(2.0)
for child in children:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
db.close()
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _start_fake_provider(
    run_dir: Path, env: dict[str, str], fixture: dict[str, Any], db_path: Path
) -> subprocess.Popen[str]:
    script = run_dir / "fake-shadow-provider.py"
    _write_fake_provider_script(script)
    return _start_process(
        [
            sys.executable,
            "-u",
            str(script),
            fixture["active_shadow_transcript"],
            str(db_path),
            fixture["active_session_id"],
            fixture["workspace"],
        ],
        cwd=run_dir,
        env=env,
    )


def _prepare_desktop_fixture(source_status: Path, fixture_status: Path, health_snapshot: Path) -> None:
    status = json.loads(source_status.read_text(encoding="utf-8"))
    ledger = status.setdefault("phase_ledger", [])
    if not ledger:
        ledger.append(
            {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "provider": "claude",
                "phase": "thinking",
                "source": "claude_hook",
                "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "valid_until": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
    fixture_status.parent.mkdir(parents=True, exist_ok=True)
    fixture_status.write_text(json.dumps(status, sort_keys=True) + "\n", encoding="utf-8")
    source = json.loads(
        (ROOT / "desktop" / "LonghouseMenuBarHarness" / "Tests" / "LonghouseMenuBarCoreTests" / "Fixtures" / "native-desktop-health.json").read_text(encoding="utf-8")
    )
    source["engine_status"]["path"] = str(fixture_status)
    source["engine_status"]["exists"] = True
    source["engine_status"]["fresh"] = True
    source["engine_status"]["payload"] = status
    source["realtime"] = None
    health_snapshot.write_text(json.dumps(source, sort_keys=True) + "\n", encoding="utf-8")


def _start_desktop_app(
    app_bin: Path, health_snapshot: Path, run_dir: Path, env: dict[str, str], quit_after: float
) -> subprocess.Popen[str]:
    return _start_process(
        [
            str(app_bin),
            "--live",
            "--health-exec",
            "/bin/cat",
            "--health-arg",
            str(health_snapshot),
            "--refresh-seconds",
            "20",
            "--quit-after",
            str(quit_after),
            "--effect-mode",
            "log-only",
        ],
        cwd=run_dir,
        env=env,
    )


def _start_desktop_churner(
    path: Path, health_snapshot: Path | None = None
) -> tuple[Any, Any]:
    import threading

    stop = threading.Event()

    def churn() -> None:
        index = 0
        while not stop.wait(5.0):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                ledger = payload.setdefault("phase_ledger", [])
                if not ledger:
                    ledger.append({"session_id": "11111111-1111-4111-8111-111111111111"})
                phase = "thinking" if index % 2 == 0 else "running"
                ledger[0].update(
                    {
                        "provider": "claude",
                        "phase": phase,
                        "tool_name": "Bash" if phase == "running" else None,
                        "source": "claude_hook",
                        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "valid_until": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                )
                temporary = path.with_name(path.name + ".tmp")
                temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
                os.replace(temporary, path)
                if health_snapshot is not None:
                    health = json.loads(health_snapshot.read_text(encoding="utf-8"))
                    health["engine_status"]["payload"] = payload
                    health_temporary = health_snapshot.with_name(health_snapshot.name + ".tmp")
                    health_temporary.write_text(json.dumps(health, sort_keys=True) + "\n", encoding="utf-8")
                    os.replace(health_temporary, health_snapshot)
                index += 1
            except (OSError, ValueError):
                continue

    thread = threading.Thread(target=churn, name="desktop-status-churn", daemon=True)
    thread.start()
    return stop, thread

def _start_desktop_core_renderer(
    snapshot_bin: Path,
    health_snapshot: Path,
    run_dir: Path,
    env: dict[str, str],
    *,
    scenario: str,
    seconds: float,
    interval_seconds: float,
) -> tuple[subprocess.Popen[str], Path]:
    helper = run_dir / f"desktop-core-{scenario}.py"
    result_path = run_dir / f"desktop-core-{scenario}.json"
    output_png = run_dir / f"desktop-core-{scenario}.png"
    helper.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import time

snapshot_bin, health_snapshot, output_png, duration_raw, interval_raw, result_path = sys.argv[1:]
duration = float(duration_raw)
interval = float(interval_raw)
deadline = time.monotonic() + duration
cpu_seconds = 0.0
peak_rss = 0
runs = 0
error = None
while time.monotonic() < deadline:
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        try:
            os.execv(snapshot_bin, [snapshot_bin, "--input", health_snapshot, "--output", output_png])
        except OSError:
            os._exit(127)
    _, status, usage = os.wait4(pid, 0)
    cpu_seconds += usage.ru_utime + usage.ru_stime
    peak_rss = max(peak_rss, int(usage.ru_maxrss))
    runs += 1
    if status != 0:
        error = f"LonghouseMenuBarHarnessSnapshot exited with wait status {status}"
        break
    remaining = interval - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(min(remaining, max(0.0, deadline - time.monotonic())))
with open(result_path, "w", encoding="utf-8") as result:
    json.dump({
        "status": "ok" if error is None and runs else "failed",
        "cpu_percent": cpu_seconds * 100.0 / duration,
        "peak_rss_bytes": peak_rss,
        "disk_write_bytes_per_second": None,
        "wakeups_per_second": None,
        "renderer_runs": runs,
        "reason": error,
    }, result)
""",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    worker = _start_process(
        [
            sys.executable,
            str(helper),
            str(snapshot_bin),
            str(health_snapshot),
            str(output_png),
            str(seconds),
            str(interval_seconds),
            str(result_path),
        ],
        cwd=run_dir,
        env=env,
    )
    return worker, result_path


def _desktop_core_result(
    worker: subprocess.Popen[str], result_path: Path, seconds: float
) -> dict[str, Any]:
    try:
        worker.wait(timeout=seconds + 10)
    except subprocess.TimeoutExpired:
        _stop_process(worker)
        return {"status": "failed", "reason": "Desktop Core snapshot renderer exceeded its scenario duration"}
    if not result_path.is_file():
        return {"status": "failed", "reason": f"Desktop Core snapshot renderer exited {worker.returncode} without a result"}
    try:
        return json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "failed", "reason": f"could not read Desktop Core renderer result: {exc}"}


def _profile_engine(pid: int, run_dir: Path, name: str, seconds: float) -> dict[str, Any]:
    if platform.system() != "Linux":
        return {"status": "skipped", "reason": "Linux perf is unavailable on macOS; no py-spy profiler is used for the Rust engine."}
    perf = shutil.which("perf")
    if not perf:
        return {"status": "skipped", "reason": "perf executable is not installed on this runner."}
    data_path = run_dir / f"perf-{name}.data"
    record = subprocess.run(
        [perf, "record", "-F", "49", "-g", "-p", str(pid), "-o", str(data_path), "--", "sleep", str(seconds)],
        cwd=run_dir,
        text=True,
        capture_output=True,
        check=False,
        timeout=seconds + 30,
    )
    if record.returncode != 0 or not data_path.exists():
        reason = (record.stderr or record.stdout or f"perf record exited {record.returncode}").strip()
        return {"status": "skipped", "reason": reason[-600:]}
    report = subprocess.run(
        [perf, "report", "--stdio", "--no-children", "--sort", "symbol", "-i", str(data_path)],
        cwd=run_dir,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if report.returncode != 0:
        return {"status": "skipped", "reason": (report.stderr or report.stdout or "perf report failed")[-600:]}
    functions: list[dict[str, Any]] = []
    pattern = re.compile(r"^\s*(\d+(?:\.\d+)?)%\s+(.+?)\s*$")
    for line in report.stdout.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        symbol = match.group(2).split()[-1]
        if symbol in {"[unknown]", "[kernel.kallsyms]"}:
            continue
        functions.append({"symbol": symbol, "percent": float(match.group(1))})
        if len(functions) == 10:
            break
    if not functions:
        return {"status": "skipped", "reason": "perf report produced no symbolized functions."}
    return {"status": "ok", "functions": functions}


def _budgets(
    platform_name: str, scenario_name: str, *, desktop_app_measured: bool
) -> dict[str, dict[str, Any]]:
    idle = scenario_name == "idle"
    budgets: dict[str, dict[str, Any]] = {
        "engine.cpu_percent": {"limit": 5.0 if idle else 40.0, "unit": "%", "comparison": "<"},
        "engine.disk_write_bytes_per_second": {
            "limit": 100_000 if idle else 1_000_000,
            "unit": "bytes/s",
            "comparison": "<",
        },
        "engine.processes_spawned_per_minute": {
            "limit": 0.0 if idle else 10.0,
            "unit": "/min",
            "comparison": "==" if idle else "<",
        },
    }
    if platform_name == "macOS" and desktop_app_measured:
        budgets["desktop_app.cpu_percent"] = {
            "limit": 2.0 if idle else 5.0,
            "unit": "%",
            "comparison": "<",
        }
    return budgets


def _metric_value(scenario: dict[str, Any], dotted_name: str) -> float | None:
    process_name, metric_name = dotted_name.split(".", 1)
    value = ((scenario.get(process_name) or {}).get(metric_name))
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_budgets_and_regressions(
    scenarios: dict[str, dict[str, Any]],
    platform_name: str,
    previous: dict[str, Any] | None,
    *,
    desktop_app_measured: bool = True,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    previous_scenarios = (previous or {}).get("scenarios") or {}
    for scenario_name, scenario in scenarios.items():
        budget_results: dict[str, dict[str, Any]] = {}
        for name, budget in _budgets(
            platform_name, scenario_name, desktop_app_measured=desktop_app_measured
        ).items():
            value = _metric_value(scenario, name)
            limit = float(budget["limit"])
            passed = value is not None and (
                value == 0.0 if budget["comparison"] == "==" else value < limit
            )
            item = {
                "value": value,
                "limit": limit,
                "unit": budget["unit"],
                "comparison": budget["comparison"],
                "status": "pass" if passed else "fail",
            }
            baseline = _metric_value(previous_scenarios.get(scenario_name) or {}, name)
            if value is not None and baseline is not None and limit > 0:
                regressed = baseline > 0 and value > baseline * 1.5 and value > limit / 2
                item["previous_value"] = baseline
                item["regressed_over_50_percent"] = regressed
                if regressed:
                    item["status"] = "fail"
                    item["regression"] = f"{value:.4g} is >50% above previous {baseline:.4g} and > half budget {limit / 2:.4g}"
            budget_results[name] = item
            if item["status"] == "fail":
                findings.append(
                    {
                        "scenario": scenario_name,
                        "metric": name,
                        "value": value,
                        "limit": limit,
                        "unit": budget["unit"],
                        "regression": item.get("regression"),
                    }
                )
        scenario["budgets"] = budget_results
    return findings


def _previous_run_baseline(platform_name: str, work_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    repo = os.environ.get("GITHUB_REPOSITORY")
    current_run = os.environ.get("GITHUB_RUN_ID")
    if not repo or not current_run or os.environ.get("GITHUB_ACTIONS") != "true":
        return None, {"status": "unavailable", "reason": "no GitHub Actions main-run context or no baseline supplied"}
    if not shutil.which("gh"):
        return None, {"status": "unavailable", "reason": "gh CLI is not installed"}
    endpoint = f"repos/{repo}/actions/workflows/perf-proof.yml/runs?branch=main&status=completed&per_page=50"
    result = subprocess.run(["gh", "api", endpoint], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return None, {"status": "unavailable", "reason": (result.stderr or result.stdout or "gh api failed")[-500:]}
    try:
        runs = json.loads(result.stdout).get("workflow_runs", [])
    except (ValueError, AttributeError):
        return None, {"status": "unavailable", "reason": "gh api returned invalid workflow-run JSON"}
    candidates = sorted(
        (
            run
            for run in runs
            if str(run.get("id")) != current_run
            and run.get("head_branch") == "main"
            and run.get("conclusion") == "success"
        ),
        key=lambda run: int(run.get("run_number", 0)),
        reverse=True,
    )
    for run in candidates:
        sha = str(run.get("head_sha") or "")
        if not sha:
            continue
        artifact_name = f"perf-proof-{sha}" if platform_name == "Linux" else f"perf-proof-macos-{sha}"
        destination = work_dir / f"baseline-{run['id']}"
        download = subprocess.run(
            ["gh", "run", "download", str(run["id"]), "--repo", repo, "--name", artifact_name, "--dir", str(destination)],
            text=True,
            capture_output=True,
            check=False,
        )
        baseline_path = destination / f"perf-soak-{platform_name.lower()}-{sha}.json"
        if download.returncode == 0 and baseline_path.is_file():
            try:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            return baseline, {
                "status": "loaded",
                "run_id": str(run["id"]),
                "run_url": run.get("html_url"),
                "sha": sha,
                "artifact": artifact_name,
            }
    return None, {"status": "unavailable", "reason": "no previous successful main run has this platform's soak artifact"}


def _previous_baseline(args: argparse.Namespace, platform_name: str, work_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if args.baseline:
        try:
            baseline_path = args.baseline.resolve()
            return json.loads(baseline_path.read_text(encoding="utf-8")), {
                "status": "loaded",
                "source": str(baseline_path),
            }
        except (OSError, ValueError) as exc:
            return None, {"status": "unavailable", "reason": f"could not read --baseline: {exc}"}
    return _previous_run_baseline(platform_name, work_dir)


def _write_summary(artifact: dict[str, Any], path: Path) -> str:
    platform_name = artifact["platform"]
    measurement = artifact.get("desktop_measurement") or {}
    if measurement.get("status") == "menu_bar_app":
        desktop_summary = "LonghouseMenuBarHarnessMenuBar on a churning sandboxed engine-status.json"
    elif measurement.get("status") == "headless_core_fallback":
        desktop_summary = f"Core snapshot fallback; gap: {measurement.get('gap')}"
    else:
        desktop_summary = measurement.get("reason", measurement.get("status", "not measured"))
    lines = [
        f"## {platform_name} steady-state soak",
        "",
        f"- SHA: `{(artifact.get('git') or {}).get('sha') or 'unknown'}`",
        f"- Fixture shape: `{sum(item.get('count', 0) for item in (artifact.get('fixtures', {}).get('files') or {}).values())}` state files plus `{(artifact.get('fixtures') or {}).get('database_rows', {})}` DB rows; contents invented.",
        f"- Previous successful main run: `{(artifact.get('previous_run') or {}).get('run_id', 'unavailable')}` ({(artifact.get('previous_run') or {}).get('reason', (artifact.get('previous_run') or {}).get('status', 'unknown'))})",
        f"- Console scenario: omitted — {artifact['console']['reason']}",
        f"- Desktop measurement: {desktop_summary}",
        "",
        "| Scenario | Engine CPU / budget | Engine writes MB/s / budget | Wakeups/s | Status writes/min | Engine children/min / budget | Engine peak RSS MB | Desktop CPU / budget | Desktop writes B/s | Desktop wakeups/s | Desktop peak RSS MB |",
        "| --- | --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |",
    ]
    for scenario_name, scenario in artifact["scenarios"].items():
        engine = scenario.get("engine") or {}
        desktop = scenario.get("desktop_app") or {}
        budgets = scenario.get("budgets") or {}
        cpu_budget = budgets.get("engine.cpu_percent", {})
        disk_budget = budgets.get("engine.disk_write_bytes_per_second", {})
        child_budget = budgets.get("engine.processes_spawned_per_minute", {})
        desktop_budget = budgets.get("desktop_app.cpu_percent", {})
        cpu = _budget_cell(engine.get("cpu_percent"), cpu_budget, "%")
        disk = _budget_cell(
            None if engine.get("disk_write_bytes_per_second") is None else engine["disk_write_bytes_per_second"] / 1_000_000,
            {**disk_budget, "limit": disk_budget.get("limit", 0) / 1_000_000},
            "MB/s",
        )
        children = _budget_cell(engine.get("processes_spawned_per_minute"), child_budget, "/min")
        core = scenario.get("desktop_core_renderer") or {}
        if desktop:
            desktop_cpu = _budget_cell(desktop.get("cpu_percent"), desktop_budget, "%")
            desktop_writes = _fmt(desktop.get("disk_write_bytes_per_second"))
            desktop_wakeups = _fmt(desktop.get("wakeups_per_second"))
            desktop_rss = _fmt_mb(desktop.get("peak_rss_bytes"))
        elif core.get("status") == "ok":
            desktop_cpu = f"no app; Core renderer {_fmt(core.get('cpu_percent'))}% (no app budget)"
            desktop_writes = "n/a"
            desktop_wakeups = "n/a"
            desktop_rss = _fmt_mb(core.get("peak_rss_bytes"))
        else:
            desktop_cpu = "not measured"
            desktop_writes = "n/a"
            desktop_wakeups = "n/a"
            desktop_rss = "n/a"
        lines.append(
            "| {scenario} | {cpu} | {disk} | {wakeups} | {status_writes} | {children} | {rss} | {desktop_cpu} | {desktop_writes} | {desktop_wakeups} | {desktop_rss} |".format(
                scenario=scenario_name,
                cpu=cpu,
                disk=disk,
                wakeups=_fmt(engine.get("wakeups_per_second")),
                status_writes=_fmt(engine.get("status_writes_per_minute")),
                children=children,
                rss=_fmt_mb(engine.get("peak_rss_bytes")),
                desktop_cpu=desktop_cpu,
                desktop_writes=desktop_writes,
                desktop_wakeups=desktop_wakeups,
                desktop_rss=desktop_rss,
            )
        )
    lines.append("")
    for scenario_name, scenario in artifact["scenarios"].items():
        lines.append(f"### {scenario_name}: top functions")
        lines.append("")
        profile = scenario.get("top_functions") or {}
        if profile.get("status") == "ok":
            lines.append("| Function | Samples |")
            lines.append("| --- | ---: |")
            for function in profile.get("functions", []):
                lines.append(f"| `{function['symbol']}` | {function['percent']:.1f}% |")
        else:
            lines.append(f"Profile skipped: {profile.get('reason', 'unknown reason')}")
        lines.append("")
    if artifact.get("failures"):
        lines.extend(["### Budget failures and regressions", ""])
        for failure in artifact["failures"]:
            if failure.get("regression"):
                detail = failure["regression"]
            else:
                detail = f"{failure.get('value')} is not below {failure['limit']} {failure['unit']}"
            lines.append(f"- FAIL `{failure['scenario']}` `{failure['metric']}`: {detail}.")
        lines.append("")
    else:
        lines.extend(["All defined budgets passed; no qualifying >50% regression was found.", ""])
    summary = "\n".join(lines)
    path.write_text(summary + "\n", encoding="utf-8")
    return summary


def _budget_cell(value: Any, budget: dict[str, Any], unit: str) -> str:
    if value is None:
        return "n/a (unavailable)"
    status = budget.get("status", "n/a")
    if status == "pass":
        state = "PASS"
    elif status == "fail":
        state = "FAIL"
    else:
        state = "n/a"
    comparator = budget.get("comparison")
    if comparator == "==":
        threshold = f"= {budget.get('limit', 0):g}"
    elif comparator:
        threshold = f"< {budget.get('limit', 0):g}"
    else:
        return f"{float(value):.2f} {unit} (no budget)"
    return f"{float(value):.2f} {unit} / {threshold} {unit} {state}"


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"


def _fmt_mb(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) / 1_000_000:.1f}"


def run_soak(args: argparse.Namespace) -> dict[str, Any]:
    os_name = platform.system()
    platform_name = {"Linux": "Linux", "Darwin": "macOS"}.get(os_name)
    if platform_name is None:
        raise SoakError(f"soak supports Linux and macOS, not {os_name}")
    engine_bin = args.engine_bin.resolve()
    if not engine_bin.is_file() or not os.access(engine_bin, os.X_OK):
        raise SoakError(f"release engine binary is missing or not executable: {engine_bin}")
    desktop_snapshot_bin: Path | None = None
    if platform_name == "macOS" and not args.skip_desktop:
        if args.desktop_bin is None:
            args.desktop_bin = ROOT / "desktop" / "LonghouseMenuBarHarness" / ".build" / "release" / "LonghouseMenuBarHarnessMenuBar"
        args.desktop_bin = args.desktop_bin.resolve()
        if not args.desktop_bin.is_file() or not os.access(args.desktop_bin, os.X_OK):
            raise SoakError(f"Desktop harness binary is missing or not executable: {args.desktop_bin}")
        desktop_snapshot_bin = (
            ROOT
            / "desktop"
            / "LonghouseMenuBarHarness"
            / ".build"
            / "release"
            / "LonghouseMenuBarHarnessSnapshot"
        )
    scratch_root = Path("/tmp/agents")
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lh-soak-", dir=scratch_root) as temporary:
        run_dir = Path(temporary)
        home = run_dir / "home"
        longhouse_home = home / ".longhouse"
        workspace = run_dir / "workspace"
        db_path = longhouse_home / "agent" / "longhouse-shipper.db"
        env = sandbox_environment(home, longhouse_home, home / "fake-bin")
        server: subprocess.Popen[str] | None = None
        bootstrap_engine: subprocess.Popen[str] | None = None
        engine: subprocess.Popen[str] | None = None
        provider: subprocess.Popen[str] | None = None
        desktop_app: subprocess.Popen[str] | None = None
        desktop_core_worker: subprocess.Popen[str] | None = None
        churn_stop: Any = None
        churn_thread: Any = None
        try:
            server, url = _start_synthetic_host(run_dir, env)
            bootstrap_stderr_path = run_dir / "engine-bootstrap.stderr"
            print("soak: synthetic Runtime Host ready", flush=True)
            with bootstrap_stderr_path.open("wb") as bootstrap_stderr:
                bootstrap_engine = _start_process(
                    _engine_command(engine_bin, url), cwd=ROOT, env=env, stderr=bootstrap_stderr
                )
            _wait_for_database(db_path, bootstrap_engine, bootstrap_stderr_path)
            print("soak: engine database initialized", flush=True)
            _stop_process(bootstrap_engine)
            bootstrap_engine = None

            fixture = perf_soak_fixtures.create_fixtures(home, longhouse_home, workspace, db_path, args.shape)
            print(f"soak: generated fixture shape ({sum(item['count'] for item in fixture['files'].values())} state files)", flush=True)
            engine_stderr_path = run_dir / "engine.stderr"
            with engine_stderr_path.open("wb") as engine_stderr:
                engine = _start_process(_engine_command(engine_bin, url), cwd=ROOT, env=env, stderr=engine_stderr)
            status_path = longhouse_home / "agent" / "engine-status.json"
            _wait_for_status(status_path, engine, engine_stderr_path)
            print("soak: engine status available; entering warmup", flush=True)
            time.sleep(args.warmup_seconds)

            process_ids = {"engine": engine.pid}
            desktop_fixture_status = run_dir / "desktop" / "engine-status.json"
            health_snapshot = run_dir / "desktop" / "health-snapshot.json"
            desktop_measurement: dict[str, Any] = {
                "status": "not_applicable" if platform_name == "Linux" else "skipped",
                "reason": (
                    "Desktop App is measured only on macOS"
                    if platform_name == "Linux"
                    else "disabled by --skip-desktop"
                ),
            }
            desktop_fallback_reason: str | None = None
            if platform_name == "macOS" and not args.skip_desktop:
                _prepare_desktop_fixture(status_path, desktop_fixture_status, health_snapshot)
                desktop_app = _start_desktop_app(
                    args.desktop_bin,
                    health_snapshot,
                    run_dir,
                    env,
                    args.idle_seconds + args.active_seconds + args.profile_seconds * 2 + 20,
                )
                time.sleep(2)
                print("soak: Desktop harness launch settled", flush=True)
                if desktop_app.poll() is not None:
                    desktop_fallback_reason = (
                        f"LonghouseMenuBarHarnessMenuBar exited before measurement "
                        f"(exit {desktop_app.returncode})"
                    )
                    _stop_process(desktop_app)
                    desktop_app = None
                    if desktop_snapshot_bin is None or not desktop_snapshot_bin.is_file():
                        raise SoakError(
                            f"{desktop_fallback_reason}; LonghouseMenuBarHarnessSnapshot is also unavailable"
                        )
                    desktop_measurement = {
                        "status": "headless_core_fallback",
                        "reason": desktop_fallback_reason,
                        "gap": (
                            "Snapshot renderer runs at a bounded interval over churned health payloads; "
                            "this does not measure a persistent NSStatusItem process or its filesystem monitor."
                        ),
                    }
                else:
                    process_ids["desktop_app"] = desktop_app.pid
                    desktop_measurement = {
                        "status": "menu_bar_app",
                        "binary": str(args.desktop_bin),
                        "status_file": str(desktop_fixture_status),
                    }
                print(f"soak: Desktop measurement mode={desktop_measurement['status']}", flush=True)

            if desktop_fallback_reason:
                desktop_core_worker, idle_core_result = _start_desktop_core_renderer(
                    desktop_snapshot_bin,
                    health_snapshot,
                    run_dir,
                    env,
                    scenario="idle",
                    seconds=args.idle_seconds,
                    interval_seconds=15.0,
                )
                print("soak: entering idle measurement window", flush=True)
            else:
                idle_core_result = None
            idle = sample_window(
                process_ids,
                seconds=args.idle_seconds,
                os_name=os_name,
                engine_status_path=status_path,
            )
            print("soak: idle measurement window complete", flush=True)
            if desktop_core_worker is not None and idle_core_result is not None:
                idle["desktop_core_renderer"] = _desktop_core_result(
                    desktop_core_worker, idle_core_result, args.idle_seconds
                )
                desktop_core_worker = None
            idle["top_functions"] = _profile_engine(engine.pid, run_dir, "idle", args.profile_seconds)
            print("soak: idle scenario measured", flush=True)

            provider = _start_fake_provider(run_dir, env, fixture, db_path)
            print("soak: fake Shadow provider active", flush=True)
            time.sleep(5)
            if provider.poll() is not None:
                raise SoakError("fake Shadow provider exited before the active scenario")
            if platform_name == "macOS" and not args.skip_desktop:
                churn_stop, churn_thread = _start_desktop_churner(
                    desktop_fixture_status,
                    health_snapshot if desktop_fallback_reason else None,
                )
            if desktop_fallback_reason:
                desktop_core_worker, active_core_result = _start_desktop_core_renderer(
                    desktop_snapshot_bin,
                    health_snapshot,
                    run_dir,
                    env,
                    scenario="active",
                    seconds=args.active_seconds,
                    interval_seconds=5.0,
                )
            else:
                active_core_result = None
            active = sample_window(
                process_ids,
                seconds=args.active_seconds,
                os_name=os_name,
                engine_status_path=status_path,
            )
            if desktop_core_worker is not None and active_core_result is not None:
                active["desktop_core_renderer"] = _desktop_core_result(
                    desktop_core_worker, active_core_result, args.active_seconds
                )
                desktop_core_worker = None
            active["top_functions"] = _profile_engine(engine.pid, run_dir, "active", args.profile_seconds)
            print("soak: active scenario measured", flush=True)
            if churn_stop is not None:
                churn_stop.set()
                churn_thread.join(timeout=2)
                churn_stop = None
                churn_thread = None
            _stop_process(provider)
            print("soak: active fake provider stopped", flush=True)
            provider = None

            scenarios = {"idle": idle, "active": active}
            previous, previous_run = _previous_baseline(args, platform_name, run_dir)
            failures = evaluate_budgets_and_regressions(
                scenarios,
                platform_name,
                previous,
                desktop_app_measured=desktop_app is not None,
            )
            print(f"soak: budget comparison complete ({len(failures)} findings)", flush=True)
            git_info = {
                "sha": _git_value(["rev-parse", "HEAD"]),
                "branch": _git_value(["branch", "--show-current"]),
            }
            environment = {
                "platform": platform_name,
                "machine": os.uname().machine,
                "python": sys.version.split()[0],
                "runner": os.environ.get("RUNNER_NAME"),
                "github_run_id": os.environ.get("GITHUB_RUN_ID"),
                "engine_binary": str(engine_bin),
                "measurement_resolution_ms": int(
                    (MAC_CHILD_SAMPLE_SECONDS if os_name == "Darwin" else SAMPLE_SECONDS) * 1000
                ),
            }
            artifact = {
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "platform": platform_name,
                "git": git_info,
                "environment": environment,
                "fixtures": fixture,
                "scenario_durations_seconds": {
                    "warmup": args.warmup_seconds,
                    "idle": args.idle_seconds,
                    "active": args.active_seconds,
                },
                "scenarios": scenarios,
                "desktop_measurement": desktop_measurement,
                "console": {"status": "omitted", "reason": CONSOLE_SKIP_REASON},
                "previous_run": previous_run,
                "failures": failures,
                "result": "fail" if failures else "pass",
            }
            return artifact
        finally:
            print("soak: stopping sandbox processes", flush=True)
            if churn_stop is not None:
                churn_stop.set()
            if churn_thread is not None:
                churn_thread.join(timeout=2)
            _stop_process(provider)
            _stop_process(desktop_core_worker)
            _stop_process(desktop_app)
            print("soak: terminating engine", flush=True)
            _stop_process(engine)
            _stop_process(bootstrap_engine)
            _stop_process(server)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--engine-bin", type=Path, default=None)
    parser.add_argument("--desktop-bin", type=Path, default=None)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--idle-seconds", type=float, default=120.0)
    parser.add_argument("--active-seconds", type=float, default=60.0)
    parser.add_argument("--warmup-seconds", type=float, default=15.0)
    parser.add_argument("--profile-seconds", type=float, default=5.0)
    parser.add_argument("--skip-desktop", action="store_true")
    parser.add_argument("--shape", type=json.loads, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    for name in ("idle_seconds", "active_seconds", "warmup_seconds", "profile_seconds"):
        if getattr(args, name) < 0 or (name != "warmup_seconds" and getattr(args, name) == 0):
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.engine_bin is None:
        args.engine_bin = default_engine_path()
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"steady-state soak starting for {platform.system()} using {args.engine_bin}", flush=True)
    try:
        artifact = run_soak(args)
    except (SoakError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"steady-state soak failed: {exc}", file=sys.stderr)
        return 2
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    summary_path = (args.summary or output.with_suffix(".md")).resolve()
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = _write_summary(artifact, summary_path)
    print(summary)
    print(f"steady-state soak artifact: {output}")
    print(f"steady-state soak summary: {summary_path}")
    return 1 if artifact["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
