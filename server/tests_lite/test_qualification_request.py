import json
from pathlib import Path

import pytest

from zerg.qa.qualification_request import QualificationRequestError
from zerg.qa.qualification_request import semantic_digest
from zerg.qa.qualification_request import validate


def _request() -> dict:
    request = {
        "schema_version": 2,
        "kind": "provider_qualification",
        "provider": "claude",
        "release_identity": "2.1.219",
        "release_tag": "2.1.219",
        "profile": "claude_real_print_v1",
        "scenario_ids": ["claude_real_print_v1", "interaction_semantics"],
        "scenario_evidence": {
            "claude_real_print_v1": "live_no_token",
            "interaction_semantics": "live_no_token",
        },
        "evidence_class": "live_no_token",
        "auth_mode": "none",
        "expected_provider_version": "2.1.219",
        "expected_executable_identity": "sha256:" + "a" * 64,
        "expected_provider_build_identity": "sha256:" + "b" * 64,
        "expected_provider_build_granularity": "single_asset",
        "factory_source_sha": "d" * 40,
        "selection_input_digests": {"schemas/managed_providers.yml": "sha256:" + "e" * 64},
        "provider_bin": "/tmp/provider",
        "invocation_id": "run-1",
        "producer_class": "release_factory",
        "producer_version": "2",
        "run_reference": "provider-factory://claude/run-1",
        "longhouse_git_sha": "c" * 40,
        "trigger": "release_poll",
    }
    request["semantic_digest"] = semantic_digest(request)
    return request


def test_v2_request_validates_and_has_stable_semantic_digest() -> None:
    request = _request()

    assert validate(request, provider="claude", profile="claude_real_print_v1") == request

    request["invocation_id"] = "run-2"
    request["provider_bin"] = "/tmp/another-provider"
    request["run_reference"] = "provider-factory://claude/run-2"
    assert validate(request, provider="claude", profile="claude_real_print_v1")["semantic_digest"] == semantic_digest(request)


def test_v2_request_rejects_tampered_semantic_content() -> None:
    request = _request()
    request["release_tag"] = "2.1.220"

    with pytest.raises(QualificationRequestError, match="semantic_digest"):
        validate(request)


def test_v2_request_rejects_unknown_keys_and_token_mismatch() -> None:
    request = _request()
    request["unexpected"] = True
    with pytest.raises(QualificationRequestError, match="unknown keys"):
        validate(request)

    request = _request()
    request["auth_mode"] = "factory_token"
    with pytest.raises(QualificationRequestError, match="factory credentials"):
        validate(request)


def test_v2_request_rejects_unknown_build_granularity() -> None:
    request = _request()
    request["expected_provider_build_granularity"] = "unknown"

    with pytest.raises(QualificationRequestError, match="granularity"):
        validate(request)


def test_v2_request_requires_evidence_for_every_declared_scenario() -> None:
    request = _request()
    request["scenario_evidence"].pop("claude_real_print_v1")
    request["semantic_digest"] = semantic_digest(request)

    with pytest.raises(QualificationRequestError, match="every declared scenario"):
        validate(request)


def test_v2_request_rejects_duplicate_scenarios() -> None:
    request = _request()
    request["scenario_ids"] = ["claude_real_print_v1", "claude_real_print_v1"]
    request["scenario_evidence"] = {"claude_real_print_v1": "live_no_token"}
    request["semantic_digest"] = semantic_digest(request)

    with pytest.raises(QualificationRequestError, match="duplicates"):
        validate(request)


def test_golden_request_vector_matches_the_private_contract() -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "qualification-request-v2-golden.json").read_text(encoding="utf-8")
    )

    assert validate(payload, provider="claude", profile="claude_real_print_v1") == payload
