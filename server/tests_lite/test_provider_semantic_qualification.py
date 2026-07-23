from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from zerg.qa import antigravity_hook_qualification as antigravity
from zerg.qa import claude_real_print_qualification as claude
from zerg.qa import opencode_server_qualification as opencode
from zerg.qa import provider_qualification
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_release_semantic_oracles as semantic_oracles
from zerg.services.managed_provider_contracts import contract_for_provider

TEST_SHA = "b" * 40
PROFILE_INFO = {
    "claude": (claude.PROFILE, "2.1.198 (Claude Code)", "2.1.198"),
    "opencode": (opencode.PROFILE, "1.17.20", "1.17.20"),
    "antigravity": (antigravity.PROFILE, "1.0.13", "1.0.13"),
}


@pytest.fixture(autouse=True)
def _stable_runner_and_no_live_authority(monkeypatch) -> None:
    monkeypatch.setattr(identity, "git_sha", lambda _root: TEST_SHA)
    monkeypatch.setattr(identity, "git_dirty", lambda _root: False)
    for key in (
        claude.LIVE_ENABLE_ENV,
        *claude.EXPLICIT_CREDENTIAL_ENV,
        antigravity.LIVE_ENABLE_ENV,
        antigravity.QUALIFICATION_HOME_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


def _fake_binary(tmp_path: Path, provider: str) -> tuple[Path, str]:
    _profile, output, _version = PROFILE_INFO[provider]
    binary = tmp_path / provider
    binary.write_text(
        f"#!{sys.executable}\nimport sys\nprint({output!r}) if sys.argv[1:] == ['--version'] else None\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary, f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}"


def _request(tmp_path: Path, provider: str, binary: Path, executable_identity: str) -> Path:
    profile, _output, version = PROFILE_INFO[provider]
    payload = {
        "schema_version": 1,
        "provider": provider,
        "profile": profile,
        "provider_bin": str(binary),
        "expected_provider_version": version,
        "expected_executable_identity": executable_identity,
        "invocation_id": f"{provider}-semantic-1",
        "producer_class": "local_diagnostic",
        "producer_version": "test",
        "longhouse_git_sha": TEST_SHA,
    }
    path = tmp_path / f"{provider}-request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _bundle(output: Path) -> dict:
    return json.loads((output / "proof-bundle.json").read_text())


def _records_by_assertion(output: Path) -> dict[str, dict]:
    return {record["assertion_id"]: record for record in _bundle(output)["records"]}


def _claude_no_token_artifact() -> dict:
    return {
        "provider": "claude",
        "provider_version": "2.1.198 (Claude Code)",
        "verdict": "green",
        "canaries": {
            "binary_identity": {"status": "pass"},
            "command_shape": {"status": "pass"},
            "channels_shape": {"status": "pass"},
            "detached_pty_shape": {"status": "pass"},
        },
    }


def _antigravity_no_token_artifact() -> dict:
    return {
        "provider": "antigravity",
        "provider_version": "1.0.13",
        "verdict": "green",
        "canaries": {
            name: {"status": "pass"}
            for name in (
                "binary_identity",
                "command_shape",
                "plugin_contract",
                "global_hooks_contract",
                "hook_inbox_claim_contract",
            )
        },
    }


def test_claude_without_explicit_authority_publishes_no_token_pass_and_live_blocked(tmp_path: Path, monkeypatch) -> None:
    binary, executable_identity = _fake_binary(tmp_path, "claude")
    monkeypatch.setenv(claude.LIVE_ENABLE_ENV, "1")
    monkeypatch.setenv("ANTHROPIC_MODEL", "model-name-is-not-credential-authority")
    monkeypatch.setattr(claude, "run_provider_live_canary", lambda _args: _claude_no_token_artifact())
    monkeypatch.setattr(
        claude.semantic,
        "load_control_canary_module",
        lambda _root: pytest.fail("live canary must not load without explicit authority"),
    )
    output = tmp_path / "output"

    result = provider_qualification.run(_request(tmp_path, "claude", binary, executable_identity), output)

    records = _records_by_assertion(output)
    assert result["semantic_status"] == "blocked"
    assert records[claude.ASSERTIONS[0]]["outcome"] == "pass"
    assert records[claude.ASSERTIONS[0]]["evidence_class"] == "live_no_token"
    assert records[claude.ASSERTIONS[1]]["outcome"] == "blocked"
    assert records[claude.ASSERTIONS[1]]["evidence_class"] == "live_no_token"
    assert {record["provider_executable_identity"] for record in records.values()} == {executable_identity}
    assert {record["provider"] for record in records.values()} == {"claude"}
    assert {record["scenario_id"] for record in records.values()} == {claude.SCENARIO_ID}
    assert records[claude.ASSERTIONS[0]]["oracle_digest"] == records[claude.ASSERTIONS[1]]["oracle_digest"]
    assert records[claude.ASSERTIONS[0]]["oracle_digest"] != records["exact_executable_identity_observed"]["oracle_digest"]


def test_claude_explicit_token_runs_existing_real_print_and_scrubs_secret(tmp_path: Path, monkeypatch) -> None:
    binary, executable_identity = _fake_binary(tmp_path, "claude")
    secret = "seeded-claude-qualification-token"
    monkeypatch.setattr(claude, "run_provider_live_canary", lambda _args: _claude_no_token_artifact())
    monkeypatch.setenv(claude.LIVE_ENABLE_ENV, "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    def real_print(_args, root: Path):
        assert os.environ["LONGHOUSE_CLAUDE_BIN"] == str(binary)
        assert os.environ["ANTHROPIC_API_KEY"] == secret
        root.joinpath("provider-stderr.log").write_text(secret, encoding="utf-8")
        return {"status": "pass", "canary": "claude_real_print", "secret_echo": secret}

    monkeypatch.setattr(
        claude.semantic,
        "load_control_canary_module",
        lambda _root: SimpleNamespace(run_claude_real_print_canary=real_print),
    )
    output = tmp_path / "output"

    provider_qualification.run(_request(tmp_path, "claude", binary, executable_identity), output)

    record = _records_by_assertion(output)[claude.ASSERTIONS[1]]
    assert record["outcome"] == "pass"
    assert record["evidence_class"] == "live_token"
    retained = b"".join(path.read_bytes() for path in output.rglob("*") if path.is_file())
    assert secret.encode() not in retained
    assert b"[QUALIFICATION_SECRET_1]" in retained


def test_claude_explicit_default_home_runs_without_config_dir(tmp_path: Path, monkeypatch) -> None:
    binary, executable_identity = _fake_binary(tmp_path, "claude")
    default_home = tmp_path / "default-home"
    default_home.mkdir()
    (default_home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(default_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "wrong-credential-namespace"))
    monkeypatch.setenv(claude.LIVE_ENABLE_ENV, "1")
    monkeypatch.setenv(claude.USE_DEFAULT_HOME_ENV, "1")

    def no_token_canary(_args):
        assert Path(os.environ["HOME"]) != default_home
        assert "CLAUDE_CONFIG_DIR" not in os.environ
        return _claude_no_token_artifact()

    def real_print(_args, _root):
        assert os.environ["HOME"] == str(default_home)
        assert "CLAUDE_CONFIG_DIR" not in os.environ
        return {"status": "pass", "canary": "claude_real_print"}

    monkeypatch.setattr(claude, "run_provider_live_canary", no_token_canary)
    monkeypatch.setattr(
        claude.semantic,
        "load_control_canary_module",
        lambda _root: SimpleNamespace(run_claude_real_print_canary=real_print),
    )
    output = tmp_path / "output"

    provider_qualification.run(_request(tmp_path, "claude", binary, executable_identity), output)

    record = _records_by_assertion(output)[claude.ASSERTIONS[1]]
    assert record["outcome"] == "pass"
    assert record["evidence_class"] == "live_token"
    assert os.environ["HOME"] == str(default_home)
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(tmp_path / "wrong-credential-namespace")


def test_opencode_publishes_serve_and_restart_semantics_from_existing_canary(tmp_path: Path, monkeypatch) -> None:
    binary, executable_identity = _fake_binary(tmp_path, "opencode")
    canaries = {
        name: {"status": "pass"}
        for name in (
            "binary_identity",
            "attach_command_shape",
            "server_startup",
            "schema_probe",
            "session_create",
            "session_get",
            "prompt_async_no_reply_delivery",
            "session_abort",
            "process_restart_reattach_contract",
        )
    }
    monkeypatch.setattr(
        opencode,
        "run_provider_live_canary",
        lambda _args: {
            "provider": "opencode",
            "provider_version": "1.17.20",
            "verdict": "green",
            "canaries": canaries,
        },
    )
    output = tmp_path / "output"

    request = _request(tmp_path, "opencode", binary, executable_identity)
    payload = json.loads(request.read_text())
    payload.update(
        producer_class="release_factory",
        run_reference="github-actions:run/123/job/456",
    )
    request.write_text(json.dumps(payload))
    result = provider_qualification.run(request, output)

    records = _records_by_assertion(output)
    assert result["semantic_status"] == "pass"
    for assertion in opencode.ASSERTIONS:
        assert records[assertion]["outcome"] == "pass"
        assert records[assertion]["evidence_class"] == "live_no_token"
        assert records[assertion]["scenario_id"] == opencode.SCENARIO_ID
        assert records[assertion]["provider_executable_identity"] == executable_identity
        assert records[assertion]["producer_class"] == "release_factory"
        assert records[assertion]["run_reference"] == "github-actions:run/123/job/456"


def test_semantic_failure_is_published_instead_of_rejected(tmp_path: Path, monkeypatch) -> None:
    binary, executable_identity = _fake_binary(tmp_path, "opencode")
    monkeypatch.setattr(
        opencode,
        "run_provider_live_canary",
        lambda _args: {
            "provider": "opencode",
            "provider_version": "1.17.20",
            "verdict": "red",
            "canaries": {
                "binary_identity": {"status": "pass"},
                "server_startup": {
                    "status": "fail",
                    "failure_code": "opencode_health_not_ready",
                },
            },
        },
    )
    output = tmp_path / "output"

    result = provider_qualification.run(_request(tmp_path, "opencode", binary, executable_identity), output)

    records = _records_by_assertion(output)
    assert result["valid"] is True
    assert records[opencode.ASSERTIONS[0]]["outcome"] == "semantic_fail"
    assert records[opencode.ASSERTIONS[1]]["outcome"] == "infrastructure_error"


def test_antigravity_no_token_hook_pass_and_live_blocked_are_both_published(tmp_path: Path, monkeypatch) -> None:
    binary, executable_identity = _fake_binary(tmp_path, "antigravity")
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()
    (ambient_home / ".gemini").mkdir()
    monkeypatch.setenv("HOME", str(ambient_home))

    def no_token_canary(_args):
        qualification_home = Path(os.environ["HOME"])
        assert qualification_home != ambient_home
        assert qualification_home.name == "home"
        assert qualification_home.parent.name == "no-token"
        assert not (qualification_home / ".gemini").exists()
        return _antigravity_no_token_artifact()

    monkeypatch.setattr(
        antigravity,
        "run_provider_live_canary",
        no_token_canary,
    )
    monkeypatch.setattr(
        antigravity.semantic,
        "load_control_canary_module",
        lambda _root: pytest.fail("real agy must not run without explicit authority"),
    )
    output = tmp_path / "output"

    provider_qualification.run(_request(tmp_path, "antigravity", binary, executable_identity), output)

    records = _records_by_assertion(output)
    assert records[antigravity.ASSERTIONS[0]]["outcome"] == "pass"
    assert records[antigravity.ASSERTIONS[1]]["outcome"] == "blocked"
    assert records[antigravity.ASSERTIONS[1]]["evidence_class"] == "live_no_token"
    assert {record["scenario_id"] for record in records.values()} == {antigravity.SCENARIO_ID}
    assert os.environ["HOME"] == str(ambient_home)


def test_antigravity_explicit_home_runs_real_agy_canary(tmp_path: Path, monkeypatch) -> None:
    binary, executable_identity = _fake_binary(tmp_path, "antigravity")
    qualification_home = tmp_path / "agy-home"
    monkeypatch.setenv(antigravity.QUALIFICATION_HOME_ENV, str(qualification_home))
    monkeypatch.setattr(
        antigravity,
        "run_provider_live_canary",
        lambda _args: _antigravity_no_token_artifact(),
    )

    def real_agy(_args, _root):
        assert os.environ["LONGHOUSE_ANTIGRAVITY_BIN"] == str(binary)
        assert os.environ["HOME"] == str(qualification_home)
        return {"status": "pass", "canary": "antigravity_real_agy_send"}

    monkeypatch.setattr(
        antigravity.semantic,
        "load_control_canary_module",
        lambda _root: SimpleNamespace(run_antigravity_real_agy_send_canary=real_agy),
    )
    output = tmp_path / "output"

    provider_qualification.run(_request(tmp_path, "antigravity", binary, executable_identity), output)

    record = _records_by_assertion(output)[antigravity.ASSERTIONS[1]]
    assert record["outcome"] == "pass"
    assert record["evidence_class"] == "live_token"


def test_binary_mutation_during_canary_invalidates_semantic_records(tmp_path: Path, monkeypatch) -> None:
    binary, executable_identity = _fake_binary(tmp_path, "opencode")

    def mutating_canary(_args):
        binary.write_text(binary.read_text() + "# mutation\n", encoding="utf-8")
        return {"verdict": "green", "canaries": {}}

    monkeypatch.setattr(opencode, "run_provider_live_canary", mutating_canary)
    output = tmp_path / "output"

    provider_qualification.run(_request(tmp_path, "opencode", binary, executable_identity), output)

    records = _records_by_assertion(output)
    assert {records[assertion]["outcome"] for assertion in opencode.ASSERTIONS} == {"infrastructure_error"}
    observation = json.loads((output / "semantic-evidence" / "semantic-observation.json").read_text())
    assert observation["failure_code"] == "provider_executable_changed_during_semantic_canary"


def test_every_semantic_release_assertion_is_consumed_by_a_declared_capability() -> None:
    profiles = {
        "claude": (claude.SCENARIO_ID, claude.ASSERTIONS),
        "opencode": (opencode.SCENARIO_ID, opencode.ASSERTIONS),
        "antigravity": (antigravity.SCENARIO_ID, antigravity.ASSERTIONS),
    }
    oracle_digest = hashlib.sha256(Path(semantic_oracles.__file__).read_bytes()).hexdigest()

    for provider, (scenario_id, assertion_ids) in profiles.items():
        contract = contract_for_provider(provider)
        assert contract is not None
        declarations = [
            assertion
            for declaration in contract.capabilities.values()
            for assertion in declaration["required_assertions"]
            if assertion["oracle_source"] == "server/zerg/qa/provider_release_semantic_oracles.py"
        ]
        assert {(item["scenario_id"], item["id"]) for item in declarations} == {
            (scenario_id, assertion_id) for assertion_id in assertion_ids
        }
        assert {item["oracle_digest"] for item in declarations} == {oracle_digest}
