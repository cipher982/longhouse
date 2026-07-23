from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from zerg.qa import antigravity_release_identity
from zerg.qa import claude_release_identity
from zerg.qa import opencode_release_identity
from zerg.qa import provider_qualification
from zerg.qa import provider_release_identity as identity
from zerg.services.managed_provider_contracts import contract_for_provider

TEST_SHA = "a" * 40
PROFILES = {
    "claude": (claude_release_identity.PROFILE, "2.1.198 (Claude Code)", "2.1.198"),
    "opencode": (opencode_release_identity.PROFILE, "1.17.20", "1.17.20"),
    "antigravity": (antigravity_release_identity.PROFILE, "1.0.13", "1.0.13"),
}


@pytest.fixture(autouse=True)
def _stable_runner_checkout(monkeypatch) -> None:
    monkeypatch.setattr(identity, "git_sha", lambda _root: TEST_SHA)
    monkeypatch.setattr(identity, "git_dirty", lambda _root: False)


def _fake_binary(tmp_path: Path, provider: str, output: str) -> tuple[Path, str, Path]:
    path = tmp_path / provider
    marker = tmp_path / f"{provider}-executed"
    path.write_text(
        f"#!{sys.executable}\nimport pathlib\npathlib.Path({str(marker)!r}).write_text('yes', encoding='utf-8')\nprint({output!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, f"sha256:{digest}", marker


def _request(
    tmp_path: Path,
    *,
    provider: str,
    binary: Path,
    executable_identity: str,
    expected_version: str | None = None,
    profile: str | None = None,
    producer_class: str = "local_diagnostic",
    run_reference: str | None = None,
) -> Path:
    registered_profile, _output, registered_version = PROFILES[provider]
    payload = {
        "schema_version": 1,
        "provider": provider,
        "profile": profile or registered_profile,
        "provider_bin": str(binary),
        "expected_provider_version": expected_version or registered_version,
        "expected_executable_identity": executable_identity,
        "invocation_id": f"{provider}-identity-1",
        "producer_class": producer_class,
        "producer_version": "test",
        "longhouse_git_sha": TEST_SHA,
    }
    if run_reference is not None:
        payload["run_reference"] = run_reference
    path = tmp_path / f"{provider}-request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("provider", sorted(PROFILES))
def test_registered_identity_profiles_emit_provider_scoped_v2_records(tmp_path: Path, provider: str) -> None:
    profile, version_output, expected_version = PROFILES[provider]
    binary, executable_identity, marker = _fake_binary(tmp_path, provider, version_output)
    output = tmp_path / f"{provider}-output"

    result = provider_qualification.run(
        _request(
            tmp_path,
            provider=provider,
            binary=binary,
            executable_identity=executable_identity,
        ),
        output,
    )

    assert marker.is_file()
    assert result["execution_status"] == "completed"
    assert set(result["assertions"].values()) == {"pass"}
    bundle = json.loads((output / "proof-bundle.json").read_text())
    contract = contract_for_provider(provider)
    assert contract is not None
    assert bundle["coverage_manifest"]["profile"] == profile
    assert bundle["coverage_manifest"]["scenario_id"] == f"{provider}_release_identity"
    assert bundle["coverage_manifest"]["evidence_class"] == "live_no_token"
    assert {record["provider"] for record in bundle["records"]} == {provider}
    assert {record["provider_version"] for record in bundle["records"]} == {expected_version}
    assert {record["provider_executable_identity"] for record in bundle["records"]} == {executable_identity}
    assert {record["provider_contract_digest"] for record in bundle["records"]} == {contract.contract_entry_digest}
    assert {record["adapter_digest"] for record in bundle["records"]} == {contract.adapter_digest}
    raw = (output / "raw-observation.json").read_bytes()
    raw_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert bundle["execution_metadata"]["raw_evidence_digest"] == raw_digest
    assert all(record["raw_reference_digests"] == [raw_digest] for record in bundle["records"])


def test_profile_oracle_and_contract_digests_are_provider_scoped(tmp_path: Path) -> None:
    digests: dict[str, tuple[str, str, str]] = {}
    for provider, (_profile, version_output, _expected_version) in PROFILES.items():
        binary, executable_identity, _ = _fake_binary(tmp_path, provider, version_output)
        output = tmp_path / f"{provider}-output"
        provider_qualification.run(
            _request(
                tmp_path,
                provider=provider,
                binary=binary,
                executable_identity=executable_identity,
            ),
            output,
        )
        record = json.loads((output / "proof-bundle.json").read_text())["records"][0]
        digests[provider] = (
            record["provider_contract_digest"],
            record["adapter_digest"],
            record["oracle_digest"],
        )

    assert len({value[0] for value in digests.values()}) == len(PROFILES)
    assert len({value[1] for value in digests.values()}) == len(PROFILES)
    assert len({value[2] for value in digests.values()}) == len(PROFILES)


@pytest.mark.parametrize(
    ("provider", "bad_output"),
    [
        ("claude", "2.1.198"),
        ("claude", "Claude Code 2.1.198"),
        ("opencode", "opencode 1.17.20"),
        ("antigravity", "agy 1.0.13"),
    ],
)
def test_provider_version_output_grammar_is_strict(tmp_path: Path, provider: str, bad_output: str) -> None:
    binary, executable_identity, _ = _fake_binary(tmp_path, provider, bad_output)
    output = tmp_path / "output"

    result = provider_qualification.run(
        _request(
            tmp_path,
            provider=provider,
            binary=binary,
            executable_identity=executable_identity,
        ),
        output,
    )

    assert result["assertions"] == {
        "exact_executable_identity_observed": "pass",
        "reported_version_matches_expected": "semantic_fail",
    }
    assert {record["provider_version"] for record in json.loads((output / "proof-bundle.json").read_text())["records"]} == {"unreported"}


def test_cross_provider_profile_request_is_rejected_before_execution(tmp_path: Path) -> None:
    binary, executable_identity, marker = _fake_binary(tmp_path, "claude", "2.1.198 (Claude Code)")
    request = _request(
        tmp_path,
        provider="claude",
        binary=binary,
        executable_identity=executable_identity,
        profile=opencode_release_identity.PROFILE,
    )
    output = tmp_path / "output"

    with pytest.raises(identity.RequestError, match="unsupported provider/profile"):
        provider_qualification.run(request, output)

    assert not marker.exists()
    assert not output.exists()


@pytest.mark.parametrize("provider", sorted(PROFILES))
def test_digest_mismatch_rejects_each_provider_before_execution(tmp_path: Path, provider: str) -> None:
    _profile, version_output, _expected_version = PROFILES[provider]
    binary, _executable_identity, marker = _fake_binary(tmp_path, provider, version_output)
    request = _request(
        tmp_path,
        provider=provider,
        binary=binary,
        executable_identity="sha256:" + "0" * 64,
    )
    output = tmp_path / "output"

    with pytest.raises(identity.RequestError, match="provider executable identity mismatch"):
        provider_qualification.run(request, output)

    assert not marker.exists()
    assert not output.exists()


def test_release_factory_request_requires_run_reference_before_execution(tmp_path: Path) -> None:
    binary, executable_identity, marker = _fake_binary(tmp_path, "claude", "2.1.198 (Claude Code)")
    output = tmp_path / "output"

    with pytest.raises(identity.RequestError, match="release_factory requests require run_reference"):
        provider_qualification.run(
            _request(
                tmp_path,
                provider="claude",
                binary=binary,
                executable_identity=executable_identity,
                producer_class="release_factory",
            ),
            output,
        )

    assert not marker.exists()
    assert not output.exists()


def test_release_factory_records_preserve_run_reference(tmp_path: Path) -> None:
    binary, executable_identity, _marker = _fake_binary(tmp_path, "claude", "2.1.198 (Claude Code)")
    output = tmp_path / "output"

    provider_qualification.run(
        _request(
            tmp_path,
            provider="claude",
            binary=binary,
            executable_identity=executable_identity,
            producer_class="release_factory",
            run_reference="github-actions:run/123/job/456",
        ),
        output,
    )

    records = json.loads((output / "proof-bundle.json").read_text())["records"]
    assert {record["producer_class"] for record in records} == {"release_factory"}
    assert {record["run_reference"] for record in records} == {"github-actions:run/123/job/456"}
