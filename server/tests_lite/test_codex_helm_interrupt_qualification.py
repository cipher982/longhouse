from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from zerg.qa import codex_helm_interrupt as profile
from zerg.qa import codex_release_identity
from zerg.qa import provider_qualification


@pytest.fixture(autouse=True)
def _stable_runner_checkout(monkeypatch) -> None:
    monkeypatch.setattr(codex_release_identity, "_git_sha", lambda _root: "test-sha")
    monkeypatch.setattr(codex_release_identity, "_git_dirty", lambda _root: False)
    for name in (
        profile.ENGINE_ENV,
        profile.PACKAGE_ROOT_ENV,
        profile.API_URL_ENV,
        profile.AGENTS_TOKEN_ENV,
        profile.PROVIDER_TOKEN_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _package(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "codex-package"
    for name in sorted(profile.PACKAGE_MEMBERS):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == "bin/codex":
            path.write_text(
                f"#!{sys.executable}\nimport sys\nprint('codex-cli 1.2.3' if sys.argv[1:] == ['--version'] else '')\n",
                encoding="utf-8",
            )
        else:
            path.write_text(name, encoding="utf-8")
        if name in profile._EXECUTABLE_PACKAGE_MEMBERS:
            path.chmod(0o700)
    binary = root / "bin/codex"
    identity = f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}"
    return root, binary, identity


def _engine(tmp_path: Path) -> Path:
    path = tmp_path / "longhouse-engine"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _request(tmp_path: Path, binary: Path, identity: str) -> Path:
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "codex",
                "profile": profile.PROFILE,
                "provider_bin": str(binary),
                "expected_provider_version": "1.2.3",
                "expected_executable_identity": identity,
                "invocation_id": "interrupt-run-1",
                "producer_class": "local_diagnostic",
                "producer_version": "test",
                "longhouse_git_sha": "test-sha",
            }
        ),
        encoding="utf-8",
    )
    return path


def _seed_environment(monkeypatch, package_root: Path, engine: Path) -> tuple[str, str]:
    agents_token = "agents-token-seeded-secret"
    provider_token = "sk-" + "provider-seeded-secret" * 2
    monkeypatch.setenv(profile.ENGINE_ENV, str(engine))
    monkeypatch.setenv(profile.PACKAGE_ROOT_ENV, str(package_root))
    monkeypatch.setenv(profile.API_URL_ENV, "https://runtime.invalid")
    monkeypatch.setenv(profile.AGENTS_TOKEN_ENV, agents_token)
    monkeypatch.setenv(profile.PROVIDER_TOKEN_ENV, provider_token)
    return agents_token, provider_token


def _verified_stop(*, returncode: int = 0, verified: bool = True) -> dict:
    return {
        "attempted": True,
        "evidence": {"returncode": returncode},
        "verification": {
            "verified": verified,
            "terminal_state": verified,
            "socket_absent": verified,
        },
    }


def _successful_fake_canary(expected_engine: Path, agents_token: str, provider_token: str):
    def run(args, evidence_root: Path, codex_bin: str) -> dict:
        assert args.engine == str(expected_engine)
        assert args.api_url == "https://runtime.invalid"
        assert args.agents_token == agents_token
        assert codex_bin.endswith("/bin/codex")
        assert os.environ[profile.PROVIDER_TOKEN_ENV] == provider_token
        assert "PYTHONPATH" not in os.environ
        root = evidence_root / "managed-live-interrupt"
        root.mkdir(parents=True)
        (root / "provider.log").write_text(f"{agents_token}\n{provider_token}\n", encoding="utf-8")
        stop = _verified_stop()
        stop["evidence"]["stderr"] = agents_token
        (root / "stop.json").write_text(
            json.dumps(stop),
            encoding="utf-8",
        )
        return {
            "status": "pass",
            "start_summary": {"session_id": "session-1"},
            "send_summary": {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "turn_status": "inProgress",
            },
            "last_turn_status": "interrupted",
            "message": provider_token,
        }

    return run


def test_live_helm_profile_reuses_canary_emits_scoped_records_and_scrubs_secrets(tmp_path: Path, monkeypatch) -> None:
    package_root, binary, identity = _package(tmp_path)
    engine = _engine(tmp_path)
    agents_token, provider_token = _seed_environment(monkeypatch, package_root, engine)
    monkeypatch.setattr(
        profile.bridge_canary,
        "run_managed_live_interrupt",
        _successful_fake_canary(engine, agents_token, provider_token),
    )

    output = tmp_path / "output"
    result = provider_qualification.run(_request(tmp_path, binary, identity), output)

    assert result["execution_status"] == "completed"
    assert set(result["assertions"].values()) == {"pass"}
    bundle = json.loads((output / "proof-bundle.json").read_text())
    assert {record["mode"] for record in bundle["records"]} == {"helm"}
    assert {record["permission_mode"] for record in bundle["records"]} == {"bypass"}
    assert {record["evidence_class"] for record in bundle["records"]} == {"live_token"}
    execution = bundle["execution_metadata"]
    assert execution["provider_version_probe_invocations"] == 1
    assert execution["managed_bridge_starts_observed"] == 1
    assert "provider_starts" not in execution
    engine_identity = f"sha256:{hashlib.sha256(engine.read_bytes()).hexdigest()}"
    assert {record["longhouse_build_id"] for record in bundle["records"]} == {engine_identity}
    retained = b"".join(path.read_bytes() for path in output.rglob("*") if path.is_file())
    assert agents_token.encode() not in retained
    assert provider_token.encode() not in retained
    assert b"[QUALIFICATION_SECRET_" in retained


@pytest.mark.parametrize(
    "missing",
    [
        profile.ENGINE_ENV,
        profile.PACKAGE_ROOT_ENV,
        profile.API_URL_ENV,
        profile.AGENTS_TOKEN_ENV,
        profile.PROVIDER_TOKEN_ENV,
    ],
)
def test_missing_required_input_blocks_without_provider_or_canary_start(
    tmp_path: Path, monkeypatch, missing: str
) -> None:
    package_root, binary, identity = _package(tmp_path)
    engine = _engine(tmp_path)
    _seed_environment(monkeypatch, package_root, engine)
    monkeypatch.delenv(missing)
    called = False

    def unexpected(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("canary must not start")

    monkeypatch.setattr(profile.bridge_canary, "run_managed_live_interrupt", unexpected)
    output = tmp_path / "output"
    result = provider_qualification.run(_request(tmp_path, binary, identity), output)

    assert result["execution_status"] == "blocked"
    assert set(result["assertions"].values()) == {"blocked"}
    execution = json.loads((output / "execution-summary.json").read_text())
    assert execution["provider_version_probe_invocations"] == 0
    assert execution["managed_bridge_starts_observed"] == 0
    assert called is False


def test_completed_canary_shape_failure_is_semantic_evidence(tmp_path: Path, monkeypatch) -> None:
    package_root, binary, identity = _package(tmp_path)
    engine = _engine(tmp_path)
    _seed_environment(monkeypatch, package_root, engine)

    def semantic_failure(_args, evidence_root: Path, _codex_bin: str) -> dict:
        root = evidence_root / "managed-live-interrupt"
        root.mkdir(parents=True)
        (root / "stop.json").write_text(json.dumps(_verified_stop()), encoding="utf-8")
        return {
            "status": "pass",
            "start_summary": {"session_id": "session-1"},
            "send_summary": {"thread_id": "thread-1", "turn_id": "turn-1", "turn_status": "inProgress"},
            "last_turn_status": "completed",
        }

    monkeypatch.setattr(profile.bridge_canary, "run_managed_live_interrupt", semantic_failure)
    result = provider_qualification.run(_request(tmp_path, binary, identity), tmp_path / "output")

    assert result["execution_status"] == "completed"
    assert result["assertions"]["active_managed_turn_observed"] == "pass"
    assert result["assertions"]["interrupt_terminal_cancelled_or_interrupted"] == "semantic_fail"
    assert result["assertions"]["managed_bridge_cleanup_completed"] == "pass"


def test_timeout_failure_preserves_observed_active_turn_without_inventing_terminal_state(
    tmp_path: Path, monkeypatch
) -> None:
    package_root, binary, identity = _package(tmp_path)
    engine = _engine(tmp_path)
    _seed_environment(monkeypatch, package_root, engine)

    def timeout_failure(_args, evidence_root: Path, _codex_bin: str) -> dict:
        root = evidence_root / "managed-live-interrupt"
        root.mkdir(parents=True)
        (root / "stop.json").write_text(json.dumps(_verified_stop()), encoding="utf-8")
        return {
            "status": "fail",
            "failure_code": "managed_live_interrupt_timeout",
            "start_summary": {"session_id": "session-1"},
            "send_summary": {"thread_id": "thread-1", "turn_id": "turn-1", "turn_status": "inProgress"},
            "state": {"active_turn_id": "turn-1", "last_turn_status": "inProgress"},
        }

    monkeypatch.setattr(profile.bridge_canary, "run_managed_live_interrupt", timeout_failure)
    result = provider_qualification.run(_request(tmp_path, binary, identity), tmp_path / "output")

    assert result["execution_status"] == "completed"
    assert result["assertions"]["active_managed_turn_observed"] == "pass"
    assert result["assertions"]["interrupt_terminal_cancelled_or_interrupted"] == "semantic_fail"
    assert result["assertions"]["managed_bridge_cleanup_completed"] == "pass"


def test_missing_active_turn_evidence_is_infrastructure_not_invented_semantic_failure(
    tmp_path: Path, monkeypatch
) -> None:
    package_root, binary, identity = _package(tmp_path)
    engine = _engine(tmp_path)
    _seed_environment(monkeypatch, package_root, engine)

    def incomplete_failure(_args, evidence_root: Path, _codex_bin: str) -> dict:
        root = evidence_root / "managed-live-interrupt"
        root.mkdir(parents=True)
        (root / "stop.json").write_text(json.dumps(_verified_stop()), encoding="utf-8")
        return {
            "status": "fail",
            "failure_code": "managed_live_interrupt_not_interrupted",
            "start_summary": {"session_id": "session-1", "thread_id": "thread-1"},
            "send_summary": {},
            "state": {"active_turn_id": None, "last_turn_status": "completed"},
        }

    monkeypatch.setattr(profile.bridge_canary, "run_managed_live_interrupt", incomplete_failure)
    result = provider_qualification.run(_request(tmp_path, binary, identity), tmp_path / "output")

    assert result["execution_status"] == "infrastructure_error"
    assert result["assertions"]["active_managed_turn_observed"] == "infrastructure_error"
    assert result["assertions"]["interrupt_terminal_cancelled_or_interrupted"] == "semantic_fail"
    assert result["assertions"]["managed_bridge_cleanup_completed"] == "pass"


def test_cleanup_failure_is_infrastructure_not_semantic(tmp_path: Path, monkeypatch) -> None:
    package_root, binary, identity = _package(tmp_path)
    engine = _engine(tmp_path)
    agents_token, provider_token = _seed_environment(monkeypatch, package_root, engine)
    canary = _successful_fake_canary(engine, agents_token, provider_token)

    def failed_cleanup(args, evidence_root: Path, codex_bin: str) -> dict:
        result = canary(args, evidence_root, codex_bin)
        stop = evidence_root / "managed-live-interrupt" / "stop.json"
        stop.write_text(json.dumps(_verified_stop(verified=False)), encoding="utf-8")
        return result

    monkeypatch.setattr(profile.bridge_canary, "run_managed_live_interrupt", failed_cleanup)
    result = provider_qualification.run(_request(tmp_path, binary, identity), tmp_path / "output")

    assert result["execution_status"] == "infrastructure_error"
    assert result["assertions"]["active_managed_turn_observed"] == "pass"
    assert result["assertions"]["interrupt_terminal_cancelled_or_interrupted"] == "pass"
    assert result["assertions"]["managed_bridge_cleanup_completed"] == "infrastructure_error"


def test_package_must_be_complete_before_canary_start(tmp_path: Path, monkeypatch) -> None:
    package_root, binary, identity = _package(tmp_path)
    engine = _engine(tmp_path)
    _seed_environment(monkeypatch, package_root, engine)
    (package_root / "codex-path/rg").unlink()
    monkeypatch.setattr(
        profile.bridge_canary,
        "run_managed_live_interrupt",
        lambda *_args, **_kwargs: pytest.fail("canary must not start"),
    )

    with pytest.raises(codex_release_identity.RequestError, match="package members mismatch"):
        provider_qualification.run(_request(tmp_path, binary, identity), tmp_path / "output")
