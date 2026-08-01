"""Provider-native interaction probes used by the universal harness.

The producer owns live PTY/provider-store mechanics. The shared interaction
oracle remains in ``provider_interaction_semantics`` so a raw artifact can be
replayed without starting a provider or spending a model turn.

Only probes with an exact acknowledgement implementation are executed here.
Every other declared probe is emitted as blocked evidence; an incomplete live
adapter can therefore never look like a passing provider qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_ENV_BY_PROVIDER
from zerg.qa.claude_conversation_reset import _terminal_text
from zerg.qa.claude_conversation_reset import _wait
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.pty_session import wait_for_terminal_quiescence
from zerg.services.managed_provider_contracts import contract_for_provider

_DEFAULT_TIMEOUT_SECONDS = 60.0


def _looks_like_claude_auth_prompt(compact_terminal: str) -> bool:
    return any(
        marker in compact_terminal.lower()
        for marker in (
            "selectloginmethod",
            "logintoclaudecode",
            "claudecodecanbeused",
            "please run/login",
            "401invalidauthenticationcredentials",
        )
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_binary(provider: str, configured: str | Path | None) -> Path:
    value = str(configured or os.environ.get(PROVIDER_CLI_ENV_BY_PROVIDER.get(provider) or "") or "").strip()
    if not value:
        value = PROVIDER_CLI_BINARY_BY_PROVIDER.get(provider, provider)
    path = Path(value).expanduser()
    if not path.is_absolute():
        resolved = subprocess.run(
            ["/usr/bin/which", str(path)],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        if not resolved:
            raise RuntimeError(f"{provider} binary not found: {value}")
        path = Path(resolved)
    return path.resolve(strict=True)


def _provider_version(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "--version"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"provider version probe failed ({result.returncode}): {(result.stderr or result.stdout).strip()}")
    return (result.stdout or result.stderr).strip().splitlines()[0].strip()


def _claude_transcript_paths(config_dir: Path) -> list[Path]:
    return list((config_dir / "projects").glob("**/*.jsonl"))


def _transcript_snapshot(config_dir: Path) -> dict[Path, int]:
    snapshot: dict[Path, int] = {}
    for path in _claude_transcript_paths(config_dir):
        try:
            snapshot[path] = path.stat().st_size
        except OSError:
            continue
    return snapshot


def _new_transcript_rows(
    snapshot: dict[Path, int],
    *,
    config_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for path in _claude_transcript_paths(config_dir):
        offset = snapshot.get(path, 0)
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                raw = handle.read()
        except OSError:
            continue
        for relative_offset, line in _jsonl_bytes(raw, start_offset=offset):
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            rows.append(row)
            sources.append(
                {
                    "source_path": str(path),
                    "source_offset": relative_offset,
                    "line_sha256": hashlib.sha256(line).hexdigest(),
                }
            )
    return rows, sources


def _jsonl_bytes(raw: bytes, *, start_offset: int) -> list[tuple[int, bytes]]:
    result: list[tuple[int, bytes]] = []
    cursor = 0
    for line in raw.splitlines(keepends=True):
        line_without_newline = line.rstrip(b"\r\n")
        if line_without_newline:
            result.append((start_offset + cursor, line_without_newline))
        cursor += len(line)
    return result


def _claude_effort_probe(
    *,
    binary: Path,
    artifact_root: Path,
    timeout: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    probe_id = "claude_effort_command"
    invocation = uuid4().hex
    output_root = artifact_root / "claude-effort" / invocation
    output_root.mkdir(parents=True, exist_ok=False)
    workspace = output_root / "workspace"
    workspace.mkdir()
    home = output_root / "home"
    home.mkdir(mode=0o700)
    config_dir = output_root / "claude-config"
    config_dir.mkdir(mode=0o700)
    terminal_path = output_root / "terminal.raw"
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["LONGHOUSE_CLAUDE_BIN"] = str(binary)
    session = ProviderPtySession.start(
        argv=[str(binary), "--name", "Claude provider interaction probe", "--permission-mode", "dontAsk"],
        cwd=workspace,
        env=env,
        terminal_path=terminal_path,
        thread_name="claude-interaction-probe-terminal-drain",
    )
    try:
        confirmed_trust = False

        def launched_state():
            nonlocal confirmed_trust
            if not session.alive():
                raise RuntimeError(f"Claude interaction probe exited during launch ({session.process.returncode})")
            text = _terminal_text(terminal_path)
            compact = re.sub(r"\s+", "", text)
            if _looks_like_claude_auth_prompt(compact):
                raise RuntimeError("missing_isolated_auth: Claude opened its login flow in the isolated provider profile")
            if not confirmed_trust and "Yes,Itrustthisfolder" in compact:
                session.write(b"\r")
                confirmed_trust = True
                return None
            if "Yes,Itrustthisfolder" in compact:
                return None
            return True

        _wait(
            launched_state,
            timeout=timeout,
            message="Claude interaction probe did not become ready",
        )
        wait_for_terminal_quiescence(session, timeout=timeout)
        transcript_before = _transcript_snapshot(config_dir)
        session.submit_line("/effort high")

        def command_rows():
            rows, sources = _new_transcript_rows(transcript_before, config_dir=config_dir)
            rendered = [json.dumps(row, ensure_ascii=False) for row in rows]
            if any("<local-command-stdout>" in value for value in rendered) and any(
                "<command-name>/effort</command-name>" in value for value in rendered
            ):
                return rows, sources
            return None

        raw_rows, source_rows = _wait(
            command_rows,
            timeout=timeout,
            message="Claude did not persist the /effort local-command records",
        )
        events = [
            row
            for row in raw_rows
            if any(
                marker in json.dumps(row, ensure_ascii=False)
                for marker in (
                    "<local-command-caveat>",
                    "<command-name>",
                    "<command-args>",
                    "<local-command-stdout>",
                )
            )
        ]
        provider_session_id = next(
            (
                str(row.get("sessionId") or row.get("session_id") or row.get("uuid") or "")
                for row in raw_rows
                if row.get("sessionId") or row.get("session_id") or row.get("uuid")
            ),
            "",
        )
        return (
            {
                "probe_id": probe_id,
                "disposition": "implemented",
                "status": "observed",
                "input_sequence": ["/effort high"],
                "acknowledgement": "local_command_stdout",
                "provider_session_id": provider_session_id,
                "longhouse_session_id": f"direct-{invocation}",
                "state_path": str(config_dir),
                "native_source_rows": source_rows,
                "raw_events": events,
                "terminal_path": str(terminal_path),
            },
            source_rows,
        )
    finally:
        session.close()


def _declared_probe_rows(provider: str) -> list[dict[str, Any]]:
    contract = contract_for_provider(provider)
    if contract is None:
        raise ValueError(f"managed provider contract missing for {provider!r}")
    rows: list[dict[str, Any]] = []
    for probe in contract.interaction_probes:
        if probe.disposition in {"policy_disabled", "upstream_absent"}:
            rows.append(
                {
                    "probe_id": probe.probe_id,
                    "disposition": probe.disposition,
                    "status": "not_applicable",
                    "raw_events": [],
                }
            )
        else:
            rows.append(
                {
                    "probe_id": probe.probe_id,
                    "disposition": probe.disposition,
                    "status": "blocked",
                    "failure_code": "interaction_probe_canary_not_implemented",
                    "raw_events": [],
                }
            )
    return rows


def produce_live_observation(
    provider: str,
    *,
    provider_bin: Path | None,
    artifact_root: Path,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the implemented no-token probes and return a replayable artifact."""

    contract = contract_for_provider(provider)
    if contract is None:
        raise ValueError(f"managed provider contract missing for {provider!r}")
    if provider == "antigravity":
        return {
            "schema_version": 1,
            "artifact_kind": "provider_interaction_semantics_observation",
            "provider": provider,
            "evidence_class": "hermetic",
            "synthetic": True,
            "probes": _declared_probe_rows(provider),
            "raw_events": [],
            "native_source_rows": [],
            "reason": "Antigravity is Shadow-only; managed TUI control is policy-disabled.",
        }

    binary = _resolve_binary(provider, provider_bin)
    observation: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "provider_interaction_semantics_observation",
        "provider": provider,
        "evidence_class": "live_no_token",
        "synthetic": False,
        "provider_bin": str(binary),
        "provider_version": _provider_version(binary),
        "provider_executable_identity": f"sha256:{_sha256(binary)}",
        "started_at": datetime.now(UTC).isoformat(),
        "probes": _declared_probe_rows(provider),
        "raw_events": [],
        "native_source_rows": [],
    }
    if provider == "claude":
        try:
            probe, source_rows = _claude_effort_probe(binary=binary, artifact_root=artifact_root, timeout=timeout)
        except (OSError, RuntimeError, ValueError) as exc:
            probe = next(row for row in observation["probes"] if row["probe_id"] == "claude_effort_command")
            terminal_path = artifact_root / "claude-effort"
            failure_code = "interaction_probe_setup_failed"
            terminal_files = sorted(terminal_path.glob("**/terminal.raw"))
            if terminal_files:
                terminal_text = re.sub(r"\s+", "", _terminal_text(terminal_files[-1]))
                if str(exc).startswith("missing_isolated_auth") or _looks_like_claude_auth_prompt(terminal_text):
                    failure_code = "missing_isolated_auth"
            probe = {
                **probe,
                "status": "blocked",
                "failure_code": failure_code,
                "message": str(exc),
                "raw_events": [],
            }
            source_rows = []
            observation["setup_failure"] = {"failure_code": failure_code, "message": str(exc)}
        probe_rows = []
        for row in observation["probes"]:
            probe_rows.append(probe if row["probe_id"] == probe["probe_id"] else row)
        observation["probes"] = probe_rows
        observation["raw_events"] = list(probe.get("raw_events") or [])
        observation["native_source_rows"] = source_rows
    return observation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--provider-bin")
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observation = produce_live_observation(
        args.provider,
        provider_bin=Path(args.provider_bin) if args.provider_bin else None,
        artifact_root=args.artifact_root,
        timeout=args.timeout,
    )
    output = args.artifact_root / "provider-interaction-observation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": str(output), "provider": args.provider}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
