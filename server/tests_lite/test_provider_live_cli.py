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
from zerg.qa import provider_live_proof_publish as plp
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

if args == ["--help"]:
    if os.environ.get("FAKE_CLAUDE_MISSING_SESSION_ID") == "1":
        print("--resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
        raise SystemExit(0)
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

if args == ["--dangerously-load-development-channels", "server:longhouse-channel", "--help"]:
    if os.environ.get("FAKE_CLAUDE_BAD_CHANNELS") == "1":
        print("unknown option --dangerously-load-development-channels", file=sys.stderr)
        raise SystemExit(1)
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

print("unexpected fake claude args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
    )


def _fake_antigravity(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
home = pathlib.Path(os.environ.get("HOME") or ".")
state = home / ".fake-agy-plugins.json"
if args == ["--version"]:
    print("1.9.9")
    raise SystemExit(0)

if args == ["--help"]:
    print("--print --prompt-interactive --conversation plugin")
    raise SystemExit(0)

if args == ["plugin", "--help"]:
    print("install <target>\nlist\nvalidate")
    raise SystemExit(0)

if args[:2] == ["plugin", "validate"] and len(args) == 3:
    print("[ok] " + args[2])
    raise SystemExit(0)

if args[:2] == ["plugin", "install"] and len(args) == 3:
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"installed": ["longhouse-runtime"]}))
    print("[ok] " + args[2])
    raise SystemExit(0)

if args == ["plugin", "list"]:
    if state.exists():
        print("longhouse-runtime")
    raise SystemExit(0)

print("unexpected fake agy args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
    )


def _patch_provider_live_publish(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: str = "green",
    failure_code: str | None = None,
) -> None:
    def fake_canary(args: dict) -> dict:
        artifact_path = Path(args["artifact"])
        payload = {
            "schema_version": 1,
            "artifact_kind": "provider_live_canary",
            "provider": args["provider"],
            "provider_version": "fake",
            "generated_at": "2026-01-01T00:00:00Z",
            "verdict": verdict,
            "failure_code": failure_code,
            "recommendation": "investigate_before_upgrade",
            "canaries": {"fake": {"status": "pass"}},
            "operation_evidence": {
                "send_input": {
                    "status": "pass",
                    "level": "live_no_token",
                    "source": "fake packaged provider live canary",
                }
            },
            "artifact_path": str(artifact_path),
            "evidence_root": str(args["evidence_root"]),
        }
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(plp, "run_provider_live_canary", fake_canary)


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
    assert payload["verdict"] == "green"
    assert payload["failure_code"] is None


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


def test_claude_live_canary_fails_when_channels_contract_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_BAD_CHANNELS", "1")
    fake_bin = _fake_claude(tmp_path / "bin" / "claude")

    payload = run_provider_live_canary(
        {
            "repo_root": str(tmp_path / "not-a-repo"),
            "provider": "claude",
            "provider_bin": str(fake_bin),
            "artifact": str(tmp_path / "artifact.json"),
            "evidence_root": str(tmp_path / "evidence"),
            "wait_ready_secs": 1.0,
            "json": True,
        }
    )

    assert payload["verdict"] == "red"
    assert payload["failure_code"] == "claude_development_channels_contract_missing"


def test_claude_live_canary_fails_when_session_flag_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MISSING_SESSION_ID", "1")
    fake_bin = _fake_claude(tmp_path / "bin" / "claude")

    payload = run_provider_live_canary(
        {
            "repo_root": str(tmp_path / "not-a-repo"),
            "provider": "claude",
            "provider_bin": str(fake_bin),
            "artifact": str(tmp_path / "artifact.json"),
            "evidence_root": str(tmp_path / "evidence"),
            "wait_ready_secs": 1.0,
            "json": True,
        }
    )

    assert payload["verdict"] == "red"
    assert payload["failure_code"] == "claude_command_contract_missing"
    assert payload["canaries"]["command_shape"]["missing"] == ["--session-id"]


def test_antigravity_plugin_argv_unwraps_home_based_debug_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    direct = _write_exe(home / ".local" / "bin" / "agy", "#!/bin/sh\nexit 0\n")
    wrapper = _write_exe(
        tmp_path / "bin" / "agy-dangerously-skip-permissions",
        '#!/bin/sh\nexec "$HOME/.local/bin/agy" --dangerously-skip-permissions "$@"\n',
    )
    monkeypatch.setenv("HOME", str(home))

    assert plc._antigravity_plugin_argv(str(wrapper), "plugin", "list") == [
        str(direct),
        "--dangerously-skip-permissions",
        "plugin",
        "list",
    ]


def test_provider_live_canary_cli_exits_nonzero_on_red(tmp_path: Path) -> None:
    fake_bin = _write_exe(
        tmp_path / "bin" / "claude",
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo broken >&2; exit 3; fi\nexit 2\n',
    )
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
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["verdict"] == "red"
    assert payload["failure_code"] == "provider_version_failed"
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == payload


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
    fake_bin = _fake_claude(tmp_path / "bin" / "claude")
    monkeypatch.setattr(plc, "_default_evidence_root", lambda _repo_root, _provider, _timestamp: base)

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


def test_provider_live_publish_cli_writes_stable_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider_live_publish(monkeypatch)
    proof_dir = tmp_path / "proof"
    evidence_root = tmp_path / "evidence"

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
    assert artifact["verdict"] == "green"


def test_provider_live_publish_cli_exits_nonzero_on_red_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_provider_live_publish(monkeypatch, verdict="red", failure_code="claude_command_contract_missing")
    proof_dir = tmp_path / "proof"

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


def test_provider_live_cli_keeps_codex_out_of_shared_live_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_provider_live_publish(monkeypatch)
    proof_dir = tmp_path / "proof"
    env = {
        "LONGHOUSE_HOME": str(tmp_path / "longhouse-home"),
    }

    canary = CliRunner().invoke(
        app,
        [
            "provider-live",
            "canary",
            "--provider",
            "codex",
            "--artifact",
            str(tmp_path / "artifact.json"),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--json",
        ],
        env=env,
    )
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

    assert canary.exit_code == 2
    assert "Unsupported provider" in canary.output
    assert not (tmp_path / "artifact.json").exists()
    assert explicit.exit_code == 2
    assert "Unsupported provider" in explicit.output
    assert not (proof_dir / "codex.json").exists()

    default_payload = CliRunner().invoke(
        app,
        [
            "provider-live",
            "publish",
            "--proof-dir",
            str(tmp_path / "proof-default"),
            "--evidence-root",
            str(tmp_path / "evidence-default"),
            "--json",
        ],
        env=env,
    )

    assert default_payload.exit_code == 0
    payload = json.loads(default_payload.output)
    assert "codex" not in payload["providers"]
