from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("TESTING", "1")

from zerg.cli.main import app
from zerg.qa import provider_live_canary as plc
from zerg.qa.provider_live_canary import run_provider_live_canary


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_claude(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("2.9.9-fake (Claude Code)")
    raise SystemExit(0)

if args == ["auth", "status", "--json"]:
    print(json.dumps({
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "pro",
        "email": "should-not-appear@example.com",
        "orgId": "org-secret",
    }))
    raise SystemExit(0)

if args == ["--help"]:
    if os.environ.get("FAKE_CLAUDE_MISSING_SESSION_ID") == "1":
        print("--resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
        raise SystemExit(0)
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

if args == ["--dangerously-load-development-channels", "server:longhouse-channel", "--help"]:
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

print("unexpected fake claude args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
    )


def _fake_codex(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("codex 0.999.0")
    raise SystemExit(0)

print("unexpected fake codex args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
    )


def _fake_provider_live_canary(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import argparse
import json
from datetime import UTC, datetime

parser = argparse.ArgumentParser()
parser.add_argument("--provider", required=True)
parser.add_argument("--artifact", required=True)
parser.add_argument("--evidence-root", required=True)
parser.add_argument("--repo-root")
parser.add_argument("--json", action="store_true")
parser.add_argument("--wait-ready-secs")
args = parser.parse_args()

payload = {
    "schema_version": 1,
    "artifact_kind": "provider_live_canary",
    "provider": args.provider,
    "provider_version": "fake",
    "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "verdict": "yellow",
    "failure_code": "insufficient_coverage",
    "recommendation": "investigate_before_upgrade",
    "canaries": {"fake": {"status": "not_run"}},
    "artifact_path": args.artifact,
    "evidence_root": args.evidence_root,
}
with open(args.artifact, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
print(json.dumps(payload))
""",
    )


def test_provider_live_canary_cli_writes_packaged_artifact(tmp_path: Path) -> None:
    fake_bin = _fake_claude(tmp_path / "bin" / "claude")
    artifact_path = tmp_path / "artifact.json"
    evidence_root = tmp_path / "evidence"

    result = CliRunner().invoke(
        app,
        [
            "provider-live",
            "canary",
            "--provider",
            "claude",
            "--provider-bin",
            str(fake_bin),
            "--artifact",
            str(artifact_path),
            "--evidence-root",
            str(evidence_root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload == persisted
    assert payload["artifact_kind"] == "provider_live_canary"
    assert payload["artifact_path"] == str(artifact_path)
    assert payload["evidence_root"] == str(evidence_root)
    assert payload["provider"] == "claude"
    assert payload["verdict"] == "yellow"
    assert payload["failure_code"] == "insufficient_coverage"
    assert payload["operation_evidence"]["launch_local"]["status"] == "pass"
    assert "should-not-appear@example.com" not in json.dumps(payload)


def test_provider_live_canary_uses_packaged_contracts_without_repo_root(tmp_path: Path) -> None:
    fake_bin = _fake_claude(tmp_path / "bin" / "claude")
    artifact_path = tmp_path / "artifact.json"

    payload = run_provider_live_canary(
        {
            "repo_root": str(tmp_path / "not-a-repo"),
            "provider": "claude",
            "provider_bin": str(fake_bin),
            "artifact": str(artifact_path),
            "evidence_root": str(tmp_path / "evidence"),
            "wait_ready_secs": 1.0,
            "json": True,
        }
    )

    assert payload["provider"] == "claude"
    assert payload["artifact_path"] == str(artifact_path)
    assert payload["operation_evidence"]["launch_local"]["status"] == "pass"
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == payload


def test_provider_live_canary_cli_exits_nonzero_on_red(tmp_path: Path) -> None:
    fake_bin = _fake_claude(tmp_path / "bin" / "claude")
    artifact_path = tmp_path / "artifact.json"

    result = CliRunner().invoke(
        app,
        [
            "provider-live",
            "canary",
            "--provider",
            "claude",
            "--provider-bin",
            str(fake_bin),
            "--artifact",
            str(artifact_path),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--json",
        ],
        env={"FAKE_CLAUDE_MISSING_SESSION_ID": "1"},
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["verdict"] == "red"
    assert payload["failure_code"] == "claude_command_contract_missing"
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == payload


def test_provider_live_canary_cli_runs_codex_lightweight_lane(tmp_path: Path) -> None:
    fake_bin = _fake_codex(tmp_path / "bin" / "codex")
    artifact_path = tmp_path / "artifact.json"

    result = CliRunner().invoke(
        app,
        [
            "provider-live",
            "canary",
            "--provider",
            "codex",
            "--provider-bin",
            str(fake_bin),
            "--artifact",
            str(artifact_path),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload == persisted
    assert payload["schema_version"] == 1
    assert payload["artifact_kind"] == "provider_live_canary"
    assert payload["provider"] == "codex"
    assert payload["provider_version"] == "codex 0.999.0"
    assert payload["verdict"] == "yellow"
    assert payload["failure_code"] == "insufficient_coverage"
    assert payload["canaries"]["binary_identity"]["status"] == "pass"
    assert payload["canaries"]["static_contract"]["status"] == "pass"
    assert payload["canaries"]["managed_tui_attach"]["status"] == "not_run"
    assert payload["canaries"]["codex_release_lane"]["status"] == "warn"
    assert "codex_provider_release_canary" in payload["source_artifacts"]
    release_artifact = json.loads(Path(payload["source_artifacts"]["codex_provider_release_canary"]).read_text())
    assert release_artifact["artifact_kind"] == "provider_release_canary"
    assert release_artifact["provider_version"] == "codex 0.999.0"


def test_provider_live_canary_installed_default_evidence_uses_longhouse_home(tmp_path: Path, monkeypatch) -> None:
    fake_bin = _fake_claude(tmp_path / "bin" / "claude")
    longhouse_home = tmp_path / "longhouse-home"
    monkeypatch.setenv("LONGHOUSE_HOME", str(longhouse_home))

    payload = run_provider_live_canary(
        {
            "repo_root": str(tmp_path / "not-a-repo"),
            "provider": "claude",
            "provider_bin": str(fake_bin),
            "artifact": None,
            "evidence_root": None,
            "wait_ready_secs": 1.0,
            "json": True,
        }
    )

    evidence_root = Path(payload["evidence_root"])
    assert evidence_root.parent == longhouse_home / "canaries" / "provider-live" / "claude"
    assert Path(payload["artifact_path"]) == evidence_root / "provider-live-canary.json"
    assert Path(payload["artifact_path"]).is_file()


def test_provider_live_canary_default_evidence_root_avoids_collisions(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "provider-run"
    base.mkdir()
    monkeypatch.setattr(plc, "_default_evidence_root", lambda _repo_root, _provider, _timestamp: base)

    payload = run_provider_live_canary(
        {
            "repo_root": str(tmp_path / "not-a-repo"),
            "provider": "future-provider",
            "provider_bin": None,
            "artifact": None,
            "evidence_root": None,
            "wait_ready_secs": 1.0,
            "json": True,
        }
    )

    assert Path(payload["evidence_root"]) == tmp_path / "provider-run-1"
    assert Path(payload["artifact_path"]) == tmp_path / "provider-run-1" / "provider-live-canary.json"
    assert Path(payload["artifact_path"]).is_file()


def test_opencode_server_start_cleans_up_when_ready_wait_fails(tmp_path: Path, monkeypatch) -> None:
    fake_bin = _write_exe(
        tmp_path / "bin" / "opencode",
        """#!/usr/bin/env python3
import time

time.sleep(60)
""",
    )
    stopped: list[int] = []
    original_stop = plc._stop_process_group

    def fail_ready_wait(log_path, process, timeout_secs):
        assert process.poll() is None
        raise TimeoutError("not ready")

    def recording_stop(process):
        stopped.append(process.pid)
        original_stop(process)

    monkeypatch.setattr(plc, "_wait_for_opencode_server_url", fail_ready_wait)
    monkeypatch.setattr(plc, "_stop_process_group", recording_stop)

    with pytest.raises(TimeoutError, match="not ready"):
        plc._start_opencode_server_process(
            binary=str(fake_bin),
            workspace=tmp_path,
            env=os.environ.copy(),
            log_path=tmp_path / "opencode-server.log",
            wait_ready_secs=0.1,
        )

    assert stopped


def test_provider_live_publish_cli_writes_stable_sidecar(tmp_path: Path) -> None:
    fake_bin = _fake_claude(tmp_path / "bin" / "claude")
    proof_dir = tmp_path / "proof"
    evidence_root = tmp_path / "evidence"
    env = {
        "PATH": f"{fake_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "LONGHOUSE_HOME": str(tmp_path / "longhouse-home"),
    }

    result = CliRunner().invoke(
        app,
        [
            "provider-live",
            "publish",
            "--provider",
            "claude",
            "--proof-dir",
            str(proof_dir),
            "--evidence-root",
            str(evidence_root),
            "--json",
        ],
        env=env,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    stable = proof_dir / "claude.json"
    artifact = json.loads(stable.read_text(encoding="utf-8"))
    assert payload["artifact_kind"] == "provider_live_proof_publish"
    assert payload["results"][0]["status"] == "published"
    assert payload["results"][0]["stable_path"] == str(stable)
    assert artifact["artifact_kind"] == "provider_live_canary"
    assert artifact["provider"] == "claude"
    assert artifact["verdict"] == "yellow"


def test_provider_live_publish_cli_exits_nonzero_on_red_canary(tmp_path: Path) -> None:
    fake_bin = _fake_claude(tmp_path / "bin" / "claude")
    proof_dir = tmp_path / "proof"
    env = {
        "PATH": f"{fake_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_CLAUDE_MISSING_SESSION_ID": "1",
        "LONGHOUSE_HOME": str(tmp_path / "longhouse-home"),
    }

    result = CliRunner().invoke(
        app,
        [
            "provider-live",
            "publish",
            "--provider",
            "claude",
            "--proof-dir",
            str(proof_dir),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--json",
        ],
        env=env,
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    artifact = json.loads((proof_dir / "claude.json").read_text(encoding="utf-8"))
    assert payload["results"][0]["returncode"] == 1
    assert artifact["verdict"] == "red"
    assert artifact["failure_code"] == "claude_command_contract_missing"


def test_provider_live_publish_cli_rejects_unsupported_provider(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "provider-live",
            "publish",
            "--provider",
            "gemini",
            "--proof-dir",
            str(tmp_path / "proof"),
        ],
    )

    assert result.exit_code == 2
    assert "Unsupported provider" in result.output


def test_provider_live_publish_cli_accepts_explicit_codex_but_excludes_it_by_default(tmp_path: Path) -> None:
    fake_bin = _fake_codex(tmp_path / "bin" / "codex")
    proof_dir = tmp_path / "proof"
    env = {
        "PATH": f"{fake_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "LONGHOUSE_HOME": str(tmp_path / "longhouse-home"),
    }

    explicit = CliRunner().invoke(
        app,
        [
            "provider-live",
            "publish",
            "--provider",
            "codex",
            "--proof-dir",
            str(proof_dir),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--json",
        ],
        env=env,
    )

    assert explicit.exit_code == 0, explicit.output
    explicit_payload = json.loads(explicit.output)
    assert explicit_payload["providers"] == ["codex"]
    codex_artifact = json.loads((proof_dir / "codex.json").read_text(encoding="utf-8"))
    assert codex_artifact["artifact_kind"] == "provider_live_canary"
    assert codex_artifact["provider"] == "codex"
    assert codex_artifact["provider_version"] == "codex 0.999.0"

    default_payload = CliRunner().invoke(
        app,
        [
            "provider-live",
            "publish",
            "--proof-dir",
            str(tmp_path / "proof-default"),
            "--evidence-root",
            str(tmp_path / "evidence-default"),
            "--canary-script",
            str(_fake_provider_live_canary(tmp_path / "bin" / "provider-live-canary")),
            "--json",
        ],
        env=env,
    )

    assert default_payload.exit_code == 0
    payload = json.loads(default_payload.output)
    assert "codex" not in payload["providers"]
