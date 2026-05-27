#!/usr/bin/env python3
"""Codex provider release canary artifact generator.

This is the Sauron-facing wrapper around Longhouse's managed Codex contract
checks. It emits one JSON artifact with pass/warn/fail status per canary and
keeps raw evidence under an isolated evidence directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

ACTIVE_THREAD_ERROR = "No active thread is available."


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _redact_argv(argv: Any) -> Any:
    if not isinstance(argv, list):
        return argv
    redacted: list[Any] = []
    redact_next = False
    for item in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        redacted.append(item)
        if item in {"--token", "--agents-token"}:
            redact_next = True
    return redacted


def _command_evidence(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "argv": _redact_argv(result.args),
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


def _status(status: str, **fields: Any) -> dict[str, Any]:
    data = {"status": status}
    data.update(fields)
    return data


def _fail(code: str, message: str, **fields: Any) -> dict[str, Any]:
    data = {"status": "fail", "failure_code": code, "message": message}
    data.update(fields)
    return data


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    text = result.stdout.strip()
    if not text:
        raise ValueError("command produced no stdout")
    return json.loads(text)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _git_commit(repo_root: Path) -> str | None:
    result = _run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _resolve_executable(value: str | None, fallback_name: str) -> str | None:
    if value:
        return value
    return shutil.which(fallback_name)


def _forbidden_codex_path(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    real = os.path.realpath(path).replace("\\", "/")
    candidates = [normalized, real]
    for candidate in candidates:
        if candidate.endswith("/longhouse-codex") or candidate == "longhouse-codex":
            return "longhouse_codex_launcher"
        if "/.longhouse/runtimes/codex" in candidate:
            return "longhouse_owned_runtime"
    return None


def run_binary_identity(args: argparse.Namespace) -> dict[str, Any]:
    codex_bin = _resolve_executable(args.codex_bin, "codex")
    if not codex_bin:
        return _fail("codex_not_found", "codex binary was not found on PATH")

    override = os.environ.get("LONGHOUSE_CODEX_BIN")
    if override and not args.allow_codex_bin_override:
        return _fail(
            "codex_bin_override_set",
            "LONGHOUSE_CODEX_BIN is set outside an explicit debug lane",
            env_var="LONGHOUSE_CODEX_BIN",
            value=override,
            path=codex_bin,
        )

    forbidden = _forbidden_codex_path(codex_bin)
    if forbidden:
        return _fail(
            forbidden,
            "canary would exercise a forbidden Longhouse-owned Codex path",
            path=codex_bin,
            real_path=os.path.realpath(codex_bin),
        )

    result = _run([codex_bin, "--version"], timeout=20)
    if result.returncode != 0:
        return _fail(
            "codex_version_failed",
            "codex --version failed",
            path=codex_bin,
            real_path=os.path.realpath(codex_bin),
            evidence=_command_evidence(result),
        )

    return _status(
        "pass",
        path=codex_bin,
        real_path=os.path.realpath(codex_bin),
        version=result.stdout.strip() or result.stderr.strip(),
    )


def run_static_contract(args: argparse.Namespace) -> dict[str, Any]:
    script = args.repo_root / "scripts/qa/check-managed-codex-contract.sh"
    env = os.environ.copy()
    env["MANAGED_CODEX_CONTRACT_ROOT"] = str(args.repo_root)
    result = _run(["bash", str(script)], cwd=args.repo_root, env=env, timeout=60)
    if result.returncode != 0:
        return _fail(
            "static_contract_failed",
            "managed Codex static contract guard failed",
            evidence=_command_evidence(result),
        )
    return _status("pass", evidence=result.stdout.strip())


def run_fake_app_server_unit(args: argparse.Namespace) -> dict[str, Any]:
    cargo_bin = _resolve_executable(args.cargo_bin, "cargo")
    if not cargo_bin:
        return _fail("cargo_not_found", "cargo binary was not found")
    result = _run(
        [
            cargo_bin,
            "test",
            "--manifest-path",
            str(args.repo_root / "engine/Cargo.toml"),
            "--bin",
            "longhouse-engine",
            "canary_runs_against_fake_codex_app_server",
        ],
        cwd=args.repo_root,
        timeout=args.fake_app_server_timeout_secs,
    )
    if result.returncode != 0:
        return _fail(
            "fake_app_server_unit_failed",
            "fake app-server unit contract test failed",
            evidence=_command_evidence(result),
        )
    return _status("pass", evidence=result.stdout[-1200:])


def run_raw_fresh_remote(args: argparse.Namespace, evidence_root: Path, codex_bin: str) -> dict[str, Any]:
    engine = _resolve_executable(args.engine, "longhouse-engine")
    if not engine:
        return _fail("engine_not_found", "longhouse-engine binary was not found")

    root = evidence_root / "raw-fresh-remote"
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    summary_path = root / "summary.json"
    jsonl_path = root / "canary.jsonl"
    remote_tui_log = root / "remote-tui.log"

    command = [
        engine,
        "codex-app-server-canary",
        "--prompt",
        "Reply exactly CANARY_OK.",
        "--cwd",
        str(workspace),
        "--codex-bin",
        codex_bin,
        "--app-server-transport",
        "websocket",
        "--spawn-remote-tui",
        "--approval-policy",
        "never",
        "--sandbox",
        "read-only",
        "--event-timeout-secs",
        str(args.canary_timeout_secs),
        "--remote-tui-grace-ms",
        str(args.remote_tui_grace_ms),
        "--remote-tui-subscribe-phase",
        "after_rollout",
        "--remote-tui-log",
        str(remote_tui_log),
        "--log-jsonl",
        str(jsonl_path),
        "--json",
    ]
    if args.model:
        command.extend(["--model", args.model])

    result = _run(command, cwd=args.repo_root, timeout=args.canary_timeout_secs + 20)
    summary_path.write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        return _fail(
            "raw_fresh_remote_failed",
            "raw fresh remote TUI canary command failed",
            evidence_root=str(root),
            evidence=_command_evidence(result),
        )

    remote_log = remote_tui_log.read_text(encoding="utf-8", errors="replace") if remote_tui_log.exists() else ""
    summary = _load_json_stdout(result)
    if ACTIVE_THREAD_ERROR in remote_log:
        return _status(
            "warn",
            evidence=f"raw fresh remote TUI showed: {ACTIVE_THREAD_ERROR}",
            evidence_root=str(root),
            summary=summary,
        )
    return _status("pass", evidence_root=str(root), summary=summary)


def _bridge_state_root(isolation_root: Path) -> Path:
    return isolation_root / "codex-bridge"


def _start_bridge(
    args: argparse.Namespace,
    *,
    evidence_root: Path,
    codex_bin: str,
    launch_mode: str,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str], Path]:
    engine = _resolve_executable(args.engine, "longhouse-engine")
    if not engine:
        raise RuntimeError("longhouse-engine binary was not found")
    if not args.api_url or not args.agents_token:
        raise RuntimeError("--api-url and --agents-token are required for managed bridge canaries")

    session_id = str(uuid.uuid4())
    isolation_root = evidence_root / f"bridge-{launch_mode}-{session_id}"
    workspace = isolation_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    log_file = isolation_root / "bridge.log"

    command = [
        engine,
        "codex-bridge",
        "start",
        "--session-id",
        session_id,
        "--cwd",
        str(workspace),
        "--url",
        args.api_url,
        "--token",
        args.agents_token,
        "--codex-bin",
        codex_bin,
        "--isolation-root",
        str(isolation_root),
        "--log-file",
        str(log_file),
        "--create-initial-thread",
        "--approval-policy",
        "never",
        "--sandbox",
        "read-only",
        "--start-timeout-secs",
        str(args.bridge_start_timeout_secs),
        "--json",
    ]
    if launch_mode == "detached_ui":
        command.extend(["--launch-mode", "detached-ui"])
    if args.model:
        command.extend(["--model", args.model])

    result = _run(command, cwd=args.repo_root, timeout=args.bridge_start_timeout_secs + 20)
    if result.returncode != 0:
        raise RuntimeError(json.dumps(_command_evidence(result)))
    summary = _load_json_stdout(result)
    return summary, result, isolation_root


def _stop_bridge(args: argparse.Namespace, session_id: str, isolation_root: Path) -> dict[str, Any]:
    engine = _resolve_executable(args.engine, "longhouse-engine")
    if not engine:
        return {"attempted": False, "error": "engine_not_found"}
    result = _run(
        [
            engine,
            "codex-bridge",
            "stop",
            "--session-id",
            session_id,
            "--state-root",
            str(_bridge_state_root(isolation_root)),
            "--reason",
            "provider_release_canary",
        ],
        cwd=args.repo_root,
        timeout=30,
    )
    return {"attempted": True, "evidence": _command_evidence(result)}


def _record_terminal_session(
    args: argparse.Namespace,
    command: list[str],
    recording_path: Path,
) -> subprocess.CompletedProcess[str]:
    script_bin = _resolve_executable(args.script_bin, "script")
    timeout_bin = _resolve_executable(args.timeout_bin, "timeout") or shutil.which("gtimeout")
    if not script_bin:
        raise RuntimeError("script binary was not found")
    if not timeout_bin:
        raise RuntimeError("timeout/gtimeout binary was not found")

    wrapped = [
        timeout_bin,
        f"{args.tui_record_secs}s",
        script_bin,
        "-q",
        str(recording_path),
        *command,
    ]
    return _run(wrapped, cwd=args.repo_root, timeout=args.tui_record_secs + 10)


def run_managed_resume(args: argparse.Namespace, evidence_root: Path, codex_bin: str) -> dict[str, Any]:
    root = evidence_root / "managed-resume"
    root.mkdir(parents=True, exist_ok=True)
    isolation_root: Path | None = None
    session_id: str | None = None
    try:
        summary, start_result, isolation_root = _start_bridge(
            args,
            evidence_root=root,
            codex_bin=codex_bin,
            launch_mode="tui",
        )
        session_id = str(summary.get("session_id") or "")
        thread_id = str(summary.get("thread_id") or "")
        ws_url = str(summary.get("ws_url") or "")
        state_file = Path(str(summary.get("state_file") or ""))
        if not thread_id or not ws_url or not state_file.exists():
            return _fail(
                "managed_resume_incomplete_start",
                "managed bridge start did not return ws_url, thread_id, and state_file",
                evidence_root=str(root),
                summary=summary,
                start=_command_evidence(start_result),
            )
        state = _read_json(state_file)
        if state.get("launch_mode") != "tui":
            return _fail(
                "managed_resume_wrong_launch_mode",
                "managed TUI bridge did not persist launch_mode=tui",
                evidence_root=str(root),
                state=state,
            )

        recording = root / "resume-tui.tty"
        terminal_command = [
            codex_bin,
            "-c",
            "check_for_update_on_startup=false",
            "resume",
            thread_id,
            "--enable",
            "tui_app_server",
            "--remote",
            ws_url,
            "--no-alt-screen",
        ]
        tui_result = _record_terminal_session(args, terminal_command, recording)
        recording_text = recording.read_text(encoding="utf-8", errors="replace") if recording.exists() else ""
        if tui_result.returncode not in (0, 124):
            return _fail(
                "managed_resume_tui_failed",
                "managed resume TUI recording command failed",
                evidence_root=str(root),
                evidence=_command_evidence(tui_result),
            )
        if ACTIVE_THREAD_ERROR in recording_text:
            return _fail(
                "managed_resume_active_thread_error",
                f"managed resume attach showed: {ACTIVE_THREAD_ERROR}",
                evidence_root=str(root),
                recording=str(recording),
            )
        return _status(
            "pass",
            thread_id=thread_id,
            ws_url=ws_url,
            state_file=str(state_file),
            recording=str(recording),
            evidence_root=str(root),
        )
    except Exception as exc:  # noqa: BLE001 - canary artifact should keep failure evidence
        return _fail("managed_resume_exception", str(exc), evidence_root=str(root))
    finally:
        if session_id and isolation_root:
            stop = _stop_bridge(args, session_id, isolation_root)
            (root / "stop.json").write_text(json.dumps(stop, indent=2), encoding="utf-8")


def run_detached_ui(args: argparse.Namespace, evidence_root: Path, codex_bin: str) -> dict[str, Any]:
    root = evidence_root / "detached-ui"
    root.mkdir(parents=True, exist_ok=True)
    isolation_root: Path | None = None
    session_id: str | None = None
    try:
        summary, start_result, isolation_root = _start_bridge(
            args,
            evidence_root=root,
            codex_bin=codex_bin,
            launch_mode="detached_ui",
        )
        session_id = str(summary.get("session_id") or "")
        thread_id = str(summary.get("thread_id") or "")
        state_file = Path(str(summary.get("state_file") or ""))
        if not thread_id or not state_file.exists():
            return _fail(
                "detached_ui_incomplete_start",
                "detached-ui bridge start did not return thread_id and state_file",
                evidence_root=str(root),
                summary=summary,
                start=_command_evidence(start_result),
            )
        state = _read_json(state_file)
        if state.get("launch_mode") != "detached_ui":
            return _fail(
                "detached_ui_wrong_launch_mode",
                "detached-ui bridge did not persist launch_mode=detached_ui",
                evidence_root=str(root),
                state=state,
            )
        ipc_socket = state_file.with_suffix(".sock")
        if not ipc_socket.exists():
            return _fail(
                "detached_ui_ipc_missing",
                "detached-ui bridge did not expose its IPC socket",
                evidence_root=str(root),
                state_file=str(state_file),
                expected_ipc_socket=str(ipc_socket),
            )
        return _status(
            "pass",
            thread_id=thread_id,
            state_file=str(state_file),
            ipc_socket=str(ipc_socket),
            evidence_root=str(root),
        )
    except Exception as exc:  # noqa: BLE001
        return _fail("detached_ui_exception", str(exc), evidence_root=str(root))
    finally:
        if session_id and isolation_root:
            stop = _stop_bridge(args, session_id, isolation_root)
            (root / "stop.json").write_text(json.dumps(stop, indent=2), encoding="utf-8")


def classify_artifact(
    canaries: dict[str, dict[str, Any]], source_review: dict[str, Any]
) -> tuple[str, str | None, str]:
    source_status = source_review.get("status")
    if source_status == "fail":
        return "red", "source_review_failed", "block_upgrade_recommendation"
    source_warning = source_status in {"warn", "not_run", None}
    first_warning: str | None = None
    for name, canary in canaries.items():
        status = canary.get("status")
        if status == "fail":
            return "red", str(canary.get("failure_code") or name), "block_upgrade_recommendation"
        if status in {"warn", "not_run"} and first_warning is None:
            first_warning = name
    if source_warning or first_warning:
        return "yellow", None, "investigate_before_upgrade"
    return "green", None, "upgrade_allowed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_repo_root_from_script())
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--engine")
    parser.add_argument("--codex-bin")
    parser.add_argument("--cargo-bin")
    parser.add_argument("--script-bin")
    parser.add_argument("--timeout-bin")
    parser.add_argument("--model")
    parser.add_argument("--api-url")
    parser.add_argument("--agents-token")
    parser.add_argument("--allow-codex-bin-override", action="store_true")
    parser.add_argument("--skip-static-contract", action="store_true")
    parser.add_argument("--run-fake-app-server", action="store_true")
    parser.add_argument("--run-raw-fresh-remote", action="store_true")
    parser.add_argument("--run-managed-resume", action="store_true")
    parser.add_argument("--run-detached-ui", action="store_true")
    parser.add_argument("--run-all-live", action="store_true")
    parser.add_argument(
        "--source-review-status",
        choices=["not_run", "pass", "warn", "fail"],
        default="not_run",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--canary-timeout-secs", type=int, default=90)
    parser.add_argument("--fake-app-server-timeout-secs", type=int, default=120)
    parser.add_argument("--bridge-start-timeout-secs", type=int, default=30)
    parser.add_argument("--remote-tui-grace-ms", type=int, default=3000)
    parser.add_argument("--tui-record-secs", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.resolve()

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = args.evidence_root or args.repo_root / ".build/canaries/codex" / timestamp
    evidence_root.mkdir(parents=True, exist_ok=True)
    artifact_path = args.artifact or evidence_root / "provider-release-canary.json"

    if args.run_all_live:
        args.run_fake_app_server = True
        args.run_raw_fresh_remote = True
        args.run_managed_resume = True
        args.run_detached_ui = True

    canaries: dict[str, dict[str, Any]] = {}
    canaries["binary_identity"] = run_binary_identity(args)
    codex_bin = str(canaries["binary_identity"].get("path") or args.codex_bin or "codex")

    if args.skip_static_contract:
        canaries["static_contract"] = _status("not_run", reason="--skip-static-contract")
    else:
        canaries["static_contract"] = run_static_contract(args)

    canaries["fake_app_server"] = (
        run_fake_app_server_unit(args)
        if args.run_fake_app_server
        else _status("not_run", reason="pass --run-fake-app-server to exercise this canary")
    )
    canaries["raw_fresh_remote"] = (
        run_raw_fresh_remote(args, evidence_root, codex_bin)
        if args.run_raw_fresh_remote
        else _status("not_run", reason="pass --run-raw-fresh-remote to exercise this canary")
    )
    canaries["managed_resume"] = (
        run_managed_resume(args, evidence_root, codex_bin)
        if args.run_managed_resume
        else _status("not_run", reason="pass --run-managed-resume to exercise this canary")
    )
    canaries["detached_ui"] = (
        run_detached_ui(args, evidence_root, codex_bin)
        if args.run_detached_ui
        else _status("not_run", reason="pass --run-detached-ui to exercise this canary")
    )

    source_review = {
        "status": args.source_review_status,
        "note": "Sauron source review should fill this section before publishing a release recommendation.",
    }
    verdict, failure_code, recommendation = classify_artifact(canaries, source_review)
    artifact = {
        "provider": "codex",
        "generated_at": _now_iso(),
        "codex_version": canaries["binary_identity"].get("version"),
        "codex_bin": canaries["binary_identity"].get("path"),
        "longhouse_commit": _git_commit(args.repo_root),
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": recommendation,
        "source_review": source_review,
        "canaries": canaries,
        "evidence_root": str(evidence_root),
    }
    _write_json(artifact_path, artifact)

    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"codex provider release canary: {verdict}")
        print(f"artifact: {artifact_path}")
        print(f"evidence_root: {evidence_root}")
        if failure_code:
            print(f"failure_code: {failure_code}")

    return 1 if verdict == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
