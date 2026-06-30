from __future__ import annotations

from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.provider_support_state import CONTRACT_OPERATIONS
from zerg.services.provider_support_state import collect_provider_support_state

CLAUDE_LIVE_CONTROL_OPERATIONS = ["send", "interrupt", "steer", "answer_pause", "launch", "continue"]
OPENCODE_LIVE_CONTROL_OPERATIONS = ["send", "interrupt", "launch", "terminate"]


def _expected_supported_operations(contract) -> list[str]:
    return [operation for operation in CONTRACT_OPERATIONS if bool(getattr(contract, operation))]


def _expected_unsupported_operations(contract) -> list[str]:
    return [operation for operation in CONTRACT_OPERATIONS if not bool(getattr(contract, operation))]


def _expected_live_operations(contract) -> list[str]:
    return [
        operation
        for operation in contract.machine_control_operations
        if operation not in {"run_once", "resume_run_once"}
    ]


def _expected_machine_operations(contract) -> list[str]:
    return list(contract.machine_control_operations)


def test_support_state_provider_capability_axes_match_manifest_contracts() -> None:
    provider_clis = {
        contract.provider: {"path": f"/usr/local/bin/{contract.provider}", "source": "PATH"}
        for contract in all_managed_provider_contracts()
    }
    control_operations_by_provider = {
        contract.provider: _expected_machine_operations(contract) for contract in all_managed_provider_contracts()
    }

    support = collect_provider_support_state(
        provider_clis=provider_clis,
        provider_release_status={"statuses": {}},
        control_channel={
            "status": "connected",
            "control_operations_by_provider": control_operations_by_provider,
        },
    )

    for contract in all_managed_provider_contracts():
        capabilities = support["providers"][contract.provider]["capabilities"]
        assert capabilities["supported_operations"] == _expected_supported_operations(contract)
        assert capabilities["unsupported_operations"] == _expected_unsupported_operations(contract)
        assert "observe_transcript" in capabilities["supported_actions"]
        assert "switch_actor" in capabilities["unknown_actions"]
        assert capabilities["machine_control_supports"] == list(contract.machine_control_supports)
        assert capabilities["machine_control_operations"] == _expected_machine_operations(contract)
        assert capabilities["live_control_operations"] == _expected_live_operations(contract)
        assert capabilities["missing_live_control_operations"] == []


def test_support_state_separates_candidate_release_from_local_readiness() -> None:
    support = collect_provider_support_state(
        provider_clis={
            "codex": {
                "path": "/opt/homebrew/bin/codex",
                "source": "PATH",
                "resolution_error": None,
            }
        },
        provider_release_status={
            "statuses": {
                "codex": {
                    "status": "candidate_newer_than_local",
                    "risk": "none",
                    "current_version": "codex-cli 0.134.0",
                    "artifact_version": "rust-v0.135.0-alpha.2",
                    "artifact_version_delta": 1,
                    "verdict": "yellow",
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {
                "codex": ["send", "interrupt", "steer", "answer_pause", "launch", "continue"],
            },
        },
    )

    codex = support["providers"]["codex"]
    assert codex["state"] == "ready"
    assert codex["version_readiness"]["state"] == "candidate_release_pending_review"
    assert codex["version_readiness"]["risk"] == "none"
    assert codex["capabilities"]["live_control_operations"] == [
        "send",
        "interrupt",
        "steer",
        "answer_pause",
        "launch",
        "continue",
    ]
    assert codex["capabilities"]["missing_live_control_operations"] == []
    assert codex["proof"]["state"] == "mixed"


def test_support_state_uses_release_derived_action_coverage() -> None:
    support = collect_provider_support_state(
        provider_clis={"opencode": {"path": "/opt/homebrew/bin/opencode", "source": "PATH"}},
        provider_release_status={
            "statuses": {
                "opencode": {
                    "status": "ok",
                    "risk": "none",
                    "schema_status": "ok",
                    "artifact_kind_status": "ok",
                    "artifact_provider_status": "ok",
                    "freshness_status": "fresh",
                    "local_version_matches": True,
                    "provider_action_coverage": {
                        "classify_subagents": {
                            "id": "classify_subagents",
                            "product_label": "Classify subagents",
                            "state": "supported",
                            "reason_code": "required_proof_passed",
                            "reason": "Required harness assertions passed.",
                            "proof_refs": [
                                {
                                    "scenario": "opencode_orchestration_projection",
                                    "assertion": "task_child_attached_to_primary_parent",
                                }
                            ],
                        },
                        "fork": {
                            "id": "fork",
                            "product_label": "Create fork",
                            "state": "read_only",
                            "reason_code": "observation_proof_passed",
                            "reason": "Observation proof passed, but no Longhouse control contract exists.",
                            "proof_refs": [],
                        },
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"opencode": OPENCODE_LIVE_CONTROL_OPERATIONS},
        },
    )

    opencode = support["providers"]["opencode"]
    assert opencode["action_coverage"]["classify_subagents"]["state"] == "supported"
    assert "classify_subagents" in opencode["capabilities"]["supported_actions"]
    assert "send_prompt" in opencode["capabilities"]["supported_actions"]
    assert opencode["capabilities"]["read_only_actions"] == ["structured_question", "fork"]


def _opencode_release_info_with_coverage(**applicability) -> dict:
    """An opencode release artifact that proves orchestration coverage green.

    Callers override applicability fields (schema_status/freshness_status/
    local_version_matches) to simulate stale or version-mismatched artifacts.
    """
    info = {
        "status": "ok",
        "risk": "none",
        "schema_status": "ok",
        "artifact_kind_status": "ok",
        "artifact_provider_status": "ok",
        "freshness_status": "fresh",
        "local_version_matches": True,
        "provider_action_coverage": {
            "classify_subagents": {
                "id": "classify_subagents",
                "product_label": "Classify subagents",
                "state": "supported",
                "reason_code": "required_proof_passed",
                "reason": "Required harness assertions passed.",
                "proof_refs": [],
            },
            "fork": {
                "id": "fork",
                "product_label": "Create fork",
                "state": "read_only",
                "reason_code": "observation_proof_passed",
                "reason": "Observation proof passed, but no Longhouse control contract exists.",
                "proof_refs": [],
            },
        },
    }
    info.update(applicability)
    return info


def _collect_opencode_action_coverage(release_info: dict) -> dict:
    support = collect_provider_support_state(
        provider_clis={"opencode": {"path": "/opt/homebrew/bin/opencode", "source": "PATH"}},
        provider_release_status={"statuses": {"opencode": release_info}},
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"opencode": OPENCODE_LIVE_CONTROL_OPERATIONS},
        },
    )
    return support["providers"]["opencode"]


def test_support_state_ignores_stale_release_action_coverage() -> None:
    """A stale artifact must not paint orchestration coverage green."""
    opencode = _collect_opencode_action_coverage(
        _opencode_release_info_with_coverage(status="stale", freshness_status="stale")
    )

    # Local derivation, not the stale release proof: orchestration stays unknown.
    assert opencode["action_coverage"]["classify_subagents"]["state"] == "unknown"
    assert opencode["action_coverage"]["fork"]["state"] == "unknown"
    assert "classify_subagents" not in opencode["capabilities"]["supported_actions"]
    assert opencode["capabilities"]["read_only_actions"] == ["structured_question"]


def test_support_state_ignores_version_mismatched_release_action_coverage() -> None:
    """An artifact generated for a different CLI version must not overlay."""
    opencode = _collect_opencode_action_coverage(
        _opencode_release_info_with_coverage(
            status="candidate_newer_than_local",
            local_version_matches=False,
        )
    )

    assert opencode["action_coverage"]["classify_subagents"]["state"] == "unknown"
    assert opencode["action_coverage"]["fork"]["state"] == "unknown"
    assert "classify_subagents" not in opencode["capabilities"]["supported_actions"]
    assert opencode["capabilities"]["read_only_actions"] == ["structured_question"]


def test_support_state_ignores_schema_mismatched_release_action_coverage() -> None:
    """A schema-mismatched artifact must not overlay even if fresh and matching."""
    opencode = _collect_opencode_action_coverage(
        _opencode_release_info_with_coverage(status="schema_mismatch", schema_status="mismatch")
    )

    assert opencode["action_coverage"]["classify_subagents"]["state"] == "unknown"
    assert opencode["action_coverage"]["fork"]["state"] == "unknown"


def test_support_state_ignores_misidentified_release_action_coverage() -> None:
    """A fresh, version-matched artifact for the wrong provider/kind must not overlay."""
    opencode = _collect_opencode_action_coverage(
        _opencode_release_info_with_coverage(
            artifact_provider_status="mismatch",
            artifact_kind_status="mismatch",
        )
    )

    assert opencode["action_coverage"]["classify_subagents"]["state"] == "unknown"
    assert opencode["action_coverage"]["fork"]["state"] == "unknown"


def test_support_state_overlays_applicable_release_action_coverage() -> None:
    """The positive control: a fresh, schema-ok, version-matched artifact overlays."""
    opencode = _collect_opencode_action_coverage(_opencode_release_info_with_coverage())

    assert opencode["action_coverage"]["classify_subagents"]["state"] == "supported"
    assert opencode["action_coverage"]["fork"]["state"] == "read_only"
    assert "classify_subagents" in opencode["capabilities"]["supported_actions"]
    assert opencode["capabilities"]["read_only_actions"] == ["structured_question", "fork"]


def test_support_state_keeps_one_shot_support_out_of_live_control_readiness() -> None:
    support = collect_provider_support_state(
        provider_clis={"codex": {"path": "/opt/homebrew/bin/codex", "source": "PATH"}},
        provider_release_status={"statuses": {}},
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {
                "codex": ["send", "interrupt", "steer", "answer_pause", "launch", "continue"],
            },
        },
    )

    codex = support["providers"]["codex"]
    assert codex["state"] == "ready"
    assert "run_once" in codex["capabilities"]["supported_operations"]
    assert "codex.run_once" in codex["capabilities"]["machine_control_supports"]
    assert "run_once" not in codex["capabilities"]["live_control_operations"]
    assert codex["capabilities"]["missing_live_control_operations"] == []


def test_support_state_keeps_claude_first_class_with_mixed_proof() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={"statuses": {"claude": {"status": "no_artifact", "risk": "none"}}},
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {
                "claude": ["send", "interrupt", "steer", "answer_pause", "launch", "continue"]
            },
        },
    )

    claude = support["providers"]["claude"]
    assert claude["state"] == "ready"
    assert claude["capabilities"]["live_control_operations"] == [
        "send",
        "interrupt",
        "steer",
        "answer_pause",
        "launch",
        "continue",
    ]
    assert "steer_active_turn" in claude["capabilities"]["supported_operations"]
    assert claude["proof"]["minimum_evidence_level"] == "source_review"
    assert claude["version_readiness"]["state"] == "no_artifact"


def test_support_state_reports_missing_provider_cli_without_collapsing_contract() -> None:
    support = collect_provider_support_state(
        provider_clis={
            "opencode": {
                "path": None,
                "source": "missing",
                "resolution_error": "`opencode` not found on PATH",
            }
        },
        provider_release_status={"statuses": {"opencode": {"status": "not_configured", "risk": "none"}}},
        control_channel={"status": "connected", "control_operations_by_provider": {}},
    )

    opencode = support["providers"]["opencode"]
    assert opencode["state"] == "provider_cli_missing"
    assert opencode["cli"]["state"] == "missing"
    assert "send_input" in opencode["capabilities"]["supported_operations"]
    assert "steer_active_turn" in opencode["capabilities"]["unsupported_operations"]


def test_support_state_reports_partial_live_control_operations() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={"statuses": {"claude": {"status": "no_artifact", "risk": "none"}}},
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": ["launch"]},
        },
    )

    claude = support["providers"]["claude"]
    assert claude["state"] == "live_control_partial"
    assert claude["capabilities"]["live_control_state"] == "partial"
    assert claude["capabilities"]["live_control_operations"] == ["launch"]
    assert claude["capabilities"]["missing_live_control_operations"] == [
        "send",
        "interrupt",
        "steer",
        "answer_pause",
        "continue",
    ]


def test_support_state_tolerates_missing_health_sections() -> None:
    support = collect_provider_support_state(
        provider_clis=None,
        provider_release_status=None,
        control_channel=None,
    )

    assert support["summary"]["providers_count"] == 5
    assert support["providers"]["claude"]["state"] == "provider_cli_missing"
    assert support["providers"]["codex"]["version_readiness"]["state"] == "not_configured"


def test_support_state_applies_release_operation_evidence_demotions() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={
            "statuses": {
                "claude": {
                    "status": "ok",
                    "risk": "none",
                    "verdict": "green",
                    "generated_at": "2026-05-27T00:00:00Z",
                    "operation_evidence": {
                        "steer_active_turn": {
                            "status": "fail",
                            "level": "none",
                            "source": "scheduled Claude live steer canary",
                            "failure_code": "steer_transcript_missing",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": CLAUDE_LIVE_CONTROL_OPERATIONS},
        },
    )

    claude = support["providers"]["claude"]
    steer = claude["operations"]["steer_active_turn"]
    assert claude["state"] == "needs_attention"
    assert "steer_active_turn" in claude["capabilities"]["supported_operations"]
    assert steer["target_evidence_level"] == "live_token"
    assert steer["evidence_level"] == "none"
    assert steer["evidence_state"] == "release_failed"
    assert steer["release_evidence"]["failure_code"] == "steer_transcript_missing"
    assert claude["proof"]["state"] == "release_failed"
    assert claude["proof"]["release_failed_operations"] == ["steer_active_turn"]


def test_support_state_surfaces_release_gaps_without_removing_capability() -> None:
    support = collect_provider_support_state(
        provider_clis={"opencode": {"path": "/opt/homebrew/bin/opencode", "source": "PATH"}},
        provider_release_status={
            "statuses": {
                "opencode": {
                    "status": "ok",
                    "risk": "none",
                    "verdict": "green",
                    "operation_evidence": {
                        "send_input": {
                            "status": "not_run",
                            "level": "none",
                            "source": "OpenCode prompt_async execution canary",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"opencode": OPENCODE_LIVE_CONTROL_OPERATIONS},
        },
    )

    opencode = support["providers"]["opencode"]
    assert opencode["state"] == "ready"
    assert "send_input" in opencode["capabilities"]["supported_operations"]
    assert opencode["operations"]["send_input"]["evidence_state"] == "release_gap"
    assert opencode["proof"]["state"] == "release_incomplete"
    assert opencode["proof"]["release_gap_operations"] == ["send_input"]


def test_support_state_keeps_release_warning_advisory() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={
            "statuses": {
                "claude": {
                    "status": "caution",
                    "risk": "warning",
                    "verdict": "yellow",
                    "failure_code": "insufficient_coverage",
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {
                "claude": ["send", "interrupt", "steer", "answer_pause", "launch", "continue"]
            },
        },
    )

    claude = support["providers"]["claude"]
    assert claude["state"] == "ready"
    assert claude["version_readiness"]["state"] == "installed_release_needs_attention"
    assert claude["version_readiness"]["risk"] == "warning"


def test_support_state_promotes_operation_proof_from_release_evidence() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={
            "statuses": {
                "claude": {
                    "status": "ok",
                    "risk": "none",
                    "verdict": "green",
                    "operation_evidence": {
                        "steer_active_turn": {
                            "status": "pass",
                            "level": "live_token",
                            "source": "scheduled Claude live steer canary",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": CLAUDE_LIVE_CONTROL_OPERATIONS},
        },
    )

    steer = support["providers"]["claude"]["operations"]["steer_active_turn"]
    assert steer["target_evidence_level"] == "live_token"
    assert steer["evidence_level"] == "live_token"
    assert steer["evidence_state"] == "release_proven"


def test_support_state_promotes_matching_local_live_proof_without_release_artifact() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={"statuses": {"claude": {"status": "no_artifact", "risk": "none"}}},
        provider_live_proof={
            "statuses": {
                "claude": {
                    "status": "ok",
                    "applies": True,
                    "version_match": "match",
                    "current_version": "Claude Code 2.1.153",
                    "artifact_version": "2.1.153",
                    "operation_evidence": {
                        "steer_active_turn": {
                            "status": "pass",
                            "level": "live_token",
                            "source": "local provider-live-canary",
                            "canary": "claude_steer_active_turn_contract",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": CLAUDE_LIVE_CONTROL_OPERATIONS},
        },
    )

    claude = support["providers"]["claude"]
    steer = claude["operations"]["steer_active_turn"]
    assert claude["version_readiness"]["state"] == "no_artifact"
    assert claude["live_proof"]["applies"] is True
    assert steer["evidence_level"] == "live_token"
    assert steer["evidence_origin"] == "local_proof"
    assert steer["evidence_state"] == "local_proof_proven"
    assert steer["local_proof_evidence"]["canary"] == "claude_steer_active_turn_contract"


def test_support_state_does_not_promote_mismatched_local_live_proof() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={"statuses": {"claude": {"status": "no_artifact", "risk": "none"}}},
        provider_live_proof={
            "statuses": {
                "claude": {
                    "status": "version_mismatch",
                    "applies": False,
                    "version_match": "mismatch",
                    "current_version": "Claude Code 2.1.154",
                    "artifact_version": "2.1.153",
                    "operation_evidence": {
                        "steer_active_turn": {
                            "status": "pass",
                            "level": "live_token",
                            "source": "stale local provider-live-canary",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": CLAUDE_LIVE_CONTROL_OPERATIONS},
        },
    )

    claude = support["providers"]["claude"]
    steer = claude["operations"]["steer_active_turn"]
    assert claude["live_proof"]["applies"] is False
    assert steer["evidence_level"] == "live_token"
    assert steer["evidence_origin"] == "manifest"
    assert steer["local_proof_evidence"]["level"] == "live_token"


def test_support_state_keeps_stronger_passing_release_proof_over_local_live_proof() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={
            "statuses": {
                "claude": {
                    "status": "ok",
                    "risk": "none",
                    "operation_evidence": {
                        "steer_active_turn": {
                            "status": "pass",
                            "level": "live_token",
                            "source": "scheduled release canary",
                        }
                    },
                }
            }
        },
        provider_live_proof={
            "statuses": {
                "claude": {
                    "status": "ok",
                    "applies": True,
                    "version_match": "match",
                    "operation_evidence": {
                        "steer_active_turn": {
                            "status": "pass",
                            "level": "live_no_token",
                            "source": "local no-token canary",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": CLAUDE_LIVE_CONTROL_OPERATIONS},
        },
    )

    steer = support["providers"]["claude"]["operations"]["steer_active_turn"]
    assert steer["evidence_level"] == "live_token"
    assert steer["evidence_origin"] == "release"
    assert steer["local_proof_evidence"]["level"] == "live_no_token"


def test_support_state_promotes_stronger_passing_local_live_proof_over_release_proof() -> None:
    support = collect_provider_support_state(
        provider_clis={"opencode": {"path": "/opt/homebrew/bin/opencode", "source": "PATH"}},
        provider_release_status={
            "statuses": {
                "opencode": {
                    "status": "ok",
                    "risk": "none",
                    "operation_evidence": {
                        "send_input": {
                            "status": "pass",
                            "level": "hermetic",
                            "source": "hermetic release contract",
                        }
                    },
                }
            }
        },
        provider_live_proof={
            "statuses": {
                "opencode": {
                    "status": "ok",
                    "applies": True,
                    "version_match": "match",
                    "operation_evidence": {
                        "send_input": {
                            "status": "pass",
                            "level": "live_no_token",
                            "source": "local provider-live-canary",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"opencode": OPENCODE_LIVE_CONTROL_OPERATIONS},
        },
    )

    send = support["providers"]["opencode"]["operations"]["send_input"]
    assert send["evidence_level"] == "live_no_token"
    assert send["evidence_origin"] == "local_proof"
    assert send["release_evidence"]["level"] == "hermetic"


def test_support_state_demotes_failed_matching_local_live_proof() -> None:
    support = collect_provider_support_state(
        provider_clis={"opencode": {"path": "/opt/homebrew/bin/opencode", "source": "PATH"}},
        provider_release_status={"statuses": {"opencode": {"status": "ok", "risk": "none"}}},
        provider_live_proof={
            "statuses": {
                "opencode": {
                    "status": "ok",
                    "applies": True,
                    "version_match": "match",
                    "operation_evidence": {
                        "send_input": {
                            "status": "fail",
                            "level": "none",
                            "source": "local prompt_async canary",
                            "failure_code": "prompt_async_failed",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"opencode": OPENCODE_LIVE_CONTROL_OPERATIONS},
        },
    )

    opencode = support["providers"]["opencode"]
    send = opencode["operations"]["send_input"]
    assert opencode["state"] == "needs_attention"
    assert opencode["proof"]["state"] == "local_proof_failed"
    assert opencode["proof"]["local_proof_failed_operations"] == ["send_input"]
    assert send["evidence_level"] == "none"
    assert send["evidence_origin"] == "local_proof"
    assert send["evidence_state"] == "local_proof_failed"


def test_support_state_does_not_attach_global_live_failure_to_passing_operation() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={"statuses": {"claude": {"status": "ok", "risk": "none"}}},
        provider_live_proof={
            "statuses": {
                "claude": {
                    "status": "ok",
                    "applies": True,
                    "version_match": "match",
                    "verdict": "red",
                    "failure_code": "claude_provider_auth_prompt",
                    "operation_evidence": {
                        "send_input": {
                            "status": "pass",
                            "level": "live_token",
                            "source": "local channel canary",
                        },
                        "transcript_binding": {
                            "status": "fail",
                            "level": "none",
                            "source": "local transcript canary",
                        },
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": CLAUDE_LIVE_CONTROL_OPERATIONS},
        },
    )

    claude = support["providers"]["claude"]
    assert claude["operations"]["send_input"]["local_proof_evidence"]["failure_code"] is None
    assert (
        claude["operations"]["transcript_binding"]["local_proof_evidence"]["failure_code"]
        == "claude_provider_auth_prompt"
    )


def test_support_state_surfaces_red_matching_local_live_proof_without_operation_evidence() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={"statuses": {"claude": {"status": "ok", "risk": "none"}}},
        provider_live_proof={
            "statuses": {
                "claude": {
                    "status": "ok",
                    "applies": True,
                    "version_match": "match",
                    "verdict": "red",
                    "failure_code": "provider_version_failed",
                    "operation_evidence": {},
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": CLAUDE_LIVE_CONTROL_OPERATIONS},
        },
    )

    claude = support["providers"]["claude"]
    assert claude["state"] == "needs_attention"
    assert claude["proof"]["state"] == "local_proof_failed"
    assert claude["proof"]["local_proof_verdict_failed"] is True
