from __future__ import annotations

from zerg.services.provider_support_state import collect_provider_support_state


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
                "codex": ["send", "interrupt", "steer", "launch", "continue"],
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
        "launch",
        "continue",
    ]
    assert codex["proof"]["state"] == "mixed"


def test_support_state_keeps_claude_first_class_with_mixed_proof() -> None:
    support = collect_provider_support_state(
        provider_clis={"claude": {"path": "/Users/test/.local/bin/claude", "source": "PATH"}},
        provider_release_status={"statuses": {"claude": {"status": "no_artifact", "risk": "none"}}},
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": ["send", "interrupt", "steer", "launch"]},
        },
    )

    claude = support["providers"]["claude"]
    assert claude["state"] == "ready"
    assert claude["capabilities"]["live_control_operations"] == ["send", "interrupt", "steer", "launch"]
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


def test_support_state_tolerates_missing_health_sections() -> None:
    support = collect_provider_support_state(
        provider_clis=None,
        provider_release_status=None,
        control_channel=None,
    )

    assert support["summary"]["providers_count"] == 4
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
            "control_operations_by_provider": {"claude": ["send", "interrupt", "steer", "launch"]},
        },
    )

    claude = support["providers"]["claude"]
    steer = claude["operations"]["steer_active_turn"]
    assert claude["state"] == "needs_attention"
    assert "steer_active_turn" in claude["capabilities"]["supported_operations"]
    assert steer["target_evidence_level"] == "manual_live_token"
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
            "control_operations_by_provider": {"opencode": ["send", "interrupt", "launch"]},
        },
    )

    opencode = support["providers"]["opencode"]
    assert opencode["state"] == "ready"
    assert "send_input" in opencode["capabilities"]["supported_operations"]
    assert opencode["operations"]["send_input"]["evidence_state"] == "release_gap"
    assert opencode["proof"]["state"] == "release_incomplete"
    assert opencode["proof"]["release_gap_operations"] == ["send_input"]


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
                            "level": "scheduled_live_token",
                            "source": "scheduled Claude live steer canary",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": ["send", "interrupt", "steer", "launch"]},
        },
    )

    steer = support["providers"]["claude"]["operations"]["steer_active_turn"]
    assert steer["target_evidence_level"] == "manual_live_token"
    assert steer["evidence_level"] == "scheduled_live_token"
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
                            "level": "scheduled_live_token",
                            "source": "local provider-live-canary",
                            "canary": "claude_steer_active_turn_contract",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": ["send", "interrupt", "steer", "launch"]},
        },
    )

    claude = support["providers"]["claude"]
    steer = claude["operations"]["steer_active_turn"]
    assert claude["version_readiness"]["state"] == "no_artifact"
    assert claude["live_proof"]["applies"] is True
    assert steer["evidence_level"] == "scheduled_live_token"
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
                            "level": "scheduled_live_token",
                            "source": "stale local provider-live-canary",
                        }
                    },
                }
            }
        },
        control_channel={
            "status": "connected",
            "control_operations_by_provider": {"claude": ["send", "interrupt", "steer", "launch"]},
        },
    )

    claude = support["providers"]["claude"]
    steer = claude["operations"]["steer_active_turn"]
    assert claude["live_proof"]["applies"] is False
    assert steer["evidence_level"] == "manual_live_token"
    assert steer["evidence_origin"] == "manifest"
    assert steer["local_proof_evidence"]["level"] == "scheduled_live_token"


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
                            "level": "scheduled_live_token",
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
            "control_operations_by_provider": {"claude": ["send", "interrupt", "steer", "launch"]},
        },
    )

    steer = support["providers"]["claude"]["operations"]["steer_active_turn"]
    assert steer["evidence_level"] == "scheduled_live_token"
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
            "control_operations_by_provider": {"opencode": ["send", "interrupt", "launch"]},
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
            "control_operations_by_provider": {"opencode": ["send", "interrupt", "launch"]},
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
                            "level": "manual_live_token",
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
            "control_operations_by_provider": {"claude": ["send", "interrupt", "steer", "launch"]},
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
            "control_operations_by_provider": {"claude": ["send", "interrupt", "steer", "launch"]},
        },
    )

    claude = support["providers"]["claude"]
    assert claude["state"] == "needs_attention"
    assert claude["proof"]["state"] == "local_proof_failed"
    assert claude["proof"]["local_proof_verdict_failed"] is True
