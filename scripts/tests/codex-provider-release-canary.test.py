#!/usr/bin/env python3
"""End-to-end tests for the Codex provider release canary wrapper."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = REPO_ROOT / "scripts/qa/codex-provider-release-canary.py"


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_codex(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log = os.environ.get("FAKE_CODEX_ARGS_LOG")
if log:
    Path(log).write_text(json.dumps(args), encoding="utf-8")
if args == ["--version"]:
    print("codex 0.999.0")
    raise SystemExit(0)

if "resume" in args:
    print("resume attached")
    raise SystemExit(0)

print("fake codex command", json.dumps(args))
raise SystemExit(0)
""",
    )


def _fake_engine(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]

def arg_value(name, default=None):
    if name not in args:
        return default
    index = args.index(name)
    return args[index + 1]

if args[:2] == ["codex-bridge", "start"]:
    if "--create-initial-thread" not in args:
        print("missing --create-initial-thread", file=sys.stderr)
        raise SystemExit(2)
    if "--auto-approve" not in args:
        print("missing --auto-approve", file=sys.stderr)
        raise SystemExit(2)
    session_id = arg_value("--session-id")
    isolation_root = Path(arg_value("--isolation-root"))
    state_root = isolation_root / "codex-bridge"
    state_root.mkdir(parents=True, exist_ok=True)
    state_file = state_root / f"{session_id}.json"
    state_file.with_suffix(".sock").write_text("fake socket", encoding="utf-8")
    thread_id = "thread_fake"
    ws_url = "ws://127.0.0.1:65535/fake"
    launch_mode = "detached_ui" if arg_value("--launch-mode") == "detached-ui" else "tui"
    if "detached-ui" in str(isolation_root) and launch_mode != "detached_ui":
        print("detached bridge missing --launch-mode detached-ui", file=sys.stderr)
        raise SystemExit(2)
    state = {
        "schema_version": 1,
        "session_id": session_id,
        "cwd": arg_value("--cwd"),
        "codex_bin": arg_value("--codex-bin"),
        "launch_mode": launch_mode,
        "ws_url": ws_url,
        "thread_id": thread_id,
        "thread_path": str(isolation_root / "thread.jsonl"),
        "pid": os.getpid(),
        "status": "ready",
        "log_file": arg_value("--log-file"),
        "active_turn_id": None,
        "last_turn_status": None,
        "last_error": None,
        "updated_at": "2026-05-26T00:00:00Z",
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps({
        "session_id": session_id,
        "state_file": str(state_file),
        "log_file": arg_value("--log-file"),
        "pid": os.getpid(),
        "ws_url": ws_url,
        "thread_id": thread_id,
        "thread_path": state["thread_path"],
    }))
    raise SystemExit(0)

if args[:2] == ["codex-bridge", "stop"]:
    calls = os.environ.get("FAKE_ENGINE_CALLS")
    if calls:
        with open(calls, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(args) + "\n")
    raise SystemExit(0)

if args and args[0] == "codex-app-server-canary":
    remote_log = arg_value("--remote-tui-log")
    jsonl_log = arg_value("--log-jsonl")
    if remote_log:
        text = "remote ok\n"
        if os.environ.get("FAKE_RAW_ACTIVE_THREAD_ERROR") == "1":
            text = "■ No active thread is available.\n"
        Path(remote_log).write_text(text, encoding="utf-8")
    if jsonl_log:
        events = [
            {
                "direction": "client_request",
                "payload": {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "test"}},
                },
            },
            {
                "direction": "server_message",
                "payload": {
                    "id": 1,
                    "result": {"platformFamily": "unix", "userAgent": "fake/1.0"},
                },
            },
            {
                "direction": "client_request",
                "payload": {
                    "id": 2,
                    "method": "thread/start",
                    "params": {"cwd": "/tmp/work"},
                },
            },
            {
                "direction": "server_message",
                "payload": {
                    "method": "thread/started",
                    "params": {"thread": {"id": "thread_raw"}},
                },
            },
            {
                "direction": "server_message",
                "payload": {
                    "id": 2,
                    "result": {
                        "thread": {"id": "thread_raw", "path": "/tmp/thread.jsonl"},
                    },
                },
            },
            {
                "direction": "server_message",
                "payload": {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread_raw",
                        "turn": {"id": "turn_raw", "status": "completed"},
                    },
                },
            },
        ]
        Path(jsonl_log).write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "codex_bin": arg_value("--codex-bin"),
        "thread_id": "thread_raw",
        "turn_id": "turn_raw",
        "turn_status": "completed",
        "remote_tui_spawned": True,
        "remote_tui_alive_after_grace": True,
        "remote_tui_alive_before_shutdown": True,
    }))
    raise SystemExit(0)

print("unexpected fake engine args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
    )


def _fake_script(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
if len(args) < 3 or args[0] != "-q":
    print("unexpected fake script args", args, file=sys.stderr)
    raise SystemExit(2)

recording = Path(args[1])
command = args[2:]
recording.write_text(os.environ.get("FAKE_SCRIPT_RECORDING_TEXT", "remote attached\n"), encoding="utf-8")
result = subprocess.run(command, text=True, capture_output=True, check=False)
with recording.open("a", encoding="utf-8") as handle:
    handle.write(result.stdout)
    handle.write(result.stderr)
raise SystemExit(result.returncode)
""",
    )


def _fake_timeout(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import os
import sys

if len(sys.argv) < 3:
    raise SystemExit(2)
command = sys.argv[2:]
os.execv(command[0], command)
""",
    )


def _fake_cargo(path: Path) -> Path:
    return _write_exe(
        path,
        "#!/usr/bin/env python3\nprint('test canary_runs_against_fake_codex_app_server ... ok')\n",
    )


def _fixture(root: Path) -> dict[str, Path]:
    bin_dir = root / "bin"
    return {
        "codex": _fake_codex(bin_dir / "codex"),
        "engine": _fake_engine(bin_dir / "longhouse-engine"),
        "script": _fake_script(bin_dir / "script"),
        "timeout": _fake_timeout(bin_dir / "timeout"),
        "cargo": _fake_cargo(bin_dir / "cargo"),
        "calls": root / "engine-calls.jsonl",
        "codex_args": root / "codex-args.json",
    }


def _run_canary(
    root: Path,
    fixture: dict[str, Path],
    extra_args: list[str],
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = root / "artifact.json"
    evidence = root / "evidence"
    env = os.environ.copy()
    env.pop("LONGHOUSE_CODEX_BIN", None)
    env["FAKE_ENGINE_CALLS"] = str(fixture["calls"])
    env["FAKE_CODEX_ARGS_LOG"] = str(fixture["codex_args"])
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [
            sys.executable,
            str(CANARY),
            "--repo-root",
            str(REPO_ROOT),
            "--evidence-root",
            str(evidence),
            "--artifact",
            str(artifact),
            "--engine",
            str(fixture["engine"]),
            "--codex-bin",
            str(fixture["codex"]),
            "--cargo-bin",
            str(fixture["cargo"]),
            "--script-bin",
            str(fixture["script"]),
            "--timeout-bin",
            str(fixture["timeout"]),
            "--api-url",
            "http://longhouse.test",
            "--agents-token",
            "secret-token",
            "--json",
            *extra_args,
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return result, payload


def test_full_fake_canary_can_go_green() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fixture = _fixture(root)
        result, payload = _run_canary(
            root,
            fixture,
            ["--run-all-live", "--source-review-status", "pass"],
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        for canary in payload["canaries"].values():
            assert canary["status"] == "pass"
        assert set(payload["operation_evidence"]) == {
            "launch_local",
            "launch_remote",
            "reattach",
            "tail_output",
        }
        assert payload["operation_evidence"]["launch_local"]["canary"] == "managed_tui_attach"
        assert payload["operation_evidence"]["launch_remote"]["canary"] == "detached_ui"
        assert payload["operation_evidence"]["reattach"]["level"] == "live_no_token"
        assert payload["operation_evidence"]["tail_output"]["status"] == "pass"

        attach_args = json.loads(fixture["codex_args"].read_text(encoding="utf-8"))
        assert attach_args[:2] == ["-c", "check_for_update_on_startup=false"]
        assert "resume" not in attach_args
        assert "--enable" in attach_args
        assert "tui_app_server" in attach_args
        assert "--remote" in attach_args

        stop_lines = fixture["calls"].read_text(encoding="utf-8").splitlines()
        assert len(stop_lines) == 2


def test_raw_fresh_remote_warning_is_yellow() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fixture = _fixture(root)
        result, payload = _run_canary(
            root,
            fixture,
            ["--run-raw-fresh-remote", "--source-review-status", "pass"],
            {"FAKE_RAW_ACTIVE_THREAD_ERROR": "1"},
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "yellow"
        assert payload["canaries"]["raw_fresh_remote"]["status"] == "warn"
        assert set(payload["operation_evidence"]) == {"tail_output"}
        assert payload["operation_evidence"]["tail_output"]["status"] == "warn"
        assert payload["operation_evidence"]["tail_output"]["level"] == "live_no_token"
        assert "No active thread is available." in payload["canaries"]["raw_fresh_remote"]["evidence"]
        fingerprints = payload["canaries"]["raw_fresh_remote"]["protocol_fingerprints"]
        assert fingerprints["responses"]["initialize"]["platformFamily"] == "str"
        assert fingerprints["responses"]["thread/start"]["thread"]["id"] == "str"
        assert fingerprints["notifications"]["thread/started"]["thread"]["id"] == "str"
        assert fingerprints["notifications"]["turn/completed"]["turn"]["status"] == "str"


def test_managed_tui_attach_active_thread_error_is_red() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fixture = _fixture(root)
        result, payload = _run_canary(
            root,
            fixture,
            ["--run-managed-tui-attach", "--source-review-status", "pass"],
            {"FAKE_SCRIPT_RECORDING_TEXT": "■ No active thread is available.\n"},
        )
        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "managed_tui_attach_active_thread_error"
        assert payload["operation_evidence"]["launch_local"]["status"] == "fail"
        assert payload["operation_evidence"]["launch_local"]["level"] == "none"
        assert payload["operation_evidence"]["reattach"]["status"] == "fail"


def test_forbidden_longhouse_codex_path_is_red() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fixture = _fixture(root)
        forbidden = _fake_codex(root / "bin" / "longhouse-codex")
        fixture["codex"] = forbidden
        result, payload = _run_canary(
            root,
            fixture,
            ["--source-review-status", "pass"],
        )
        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "longhouse_codex_launcher"
        assert payload["operation_evidence"]["launch_local"]["status"] == "fail"
        assert payload["operation_evidence"]["launch_local"]["level"] == "none"


def test_codex_launch_local_failure_evidence_is_not_overwritten_by_later_canary() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fixture = _fixture(root)
        forbidden = _fake_codex(root / "bin" / "longhouse-codex")
        fixture["codex"] = forbidden
        result, payload = _run_canary(
            root,
            fixture,
            ["--run-managed-tui-attach", "--source-review-status", "pass"],
        )
        assert result.returncode == 1
        assert payload["operation_evidence"]["launch_local"]["status"] == "fail"
        assert payload["operation_evidence"]["launch_local"]["failure_code"] == "longhouse_codex_launcher"
        assert payload["operation_evidence"]["reattach"]["status"] == "pass"


def test_longhouse_codex_bin_env_requires_explicit_override() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fixture = _fixture(root)
        result, payload = _run_canary(
            root,
            fixture,
            ["--source-review-status", "pass"],
            {"LONGHOUSE_CODEX_BIN": str(fixture["codex"])},
        )
        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "codex_bin_override_set"


def test_release_artifact_can_use_upstream_version_without_local_binary_identity() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fixture = _fixture(root)
        result, payload = _run_canary(
            root,
            fixture,
            [
                "--provider-version",
                "rust-v0.134.0",
                "--skip-binary-identity",
                "--source-review-status",
                "pass",
                "--source-review-note",
                "release triage report is in report.md",
            ],
            {"LONGHOUSE_CODEX_BIN": str(fixture["codex"])},
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "insufficient_coverage"
        assert payload["codex_version"] == "rust-v0.134.0"
        assert payload["canaries"]["binary_identity"]["status"] == "not_run"
        assert payload["source_review"]["note"] == "release triage report is in report.md"


def main() -> int:
    tests = [
        test_full_fake_canary_can_go_green,
        test_raw_fresh_remote_warning_is_yellow,
        test_managed_tui_attach_active_thread_error_is_red,
        test_forbidden_longhouse_codex_path_is_red,
        test_codex_launch_local_failure_evidence_is_not_overwritten_by_later_canary,
        test_longhouse_codex_bin_env_requires_explicit_override,
        test_release_artifact_can_use_upstream_version_without_local_binary_identity,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
