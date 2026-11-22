from __future__ import annotations


from zerg.qa.resume_assurance import capability_contract_shape
from zerg.provider_contract.provider_capability_schema import load_capability_assertions


def test_cursor_model_choice_awareness_is_sampled_not_a_release_gate() -> None:
    contract = capability_contract_shape(
        load_capability_assertions(),
        provider="cursor",
        capability="coordination.awareness.create",
    )

    assert len(contract) == 1
    assert contract[0]["assertion_id"] == "coordination_instructions_model_visible"
    assert contract[0]["assurance_priority"] == "sampled"


def _codes(compiled: dict) -> set[str]:
    return {item["code"] for item in compiled["report"]["diagnostics"]}


def test_native_resume_oracle_requires_post_resume_activity_and_variant_cleanup() -> None:
    from zerg.qa.provider_resume_oracles import native_resume_assertions

    observation = {
        "same_longhouse_session": True,
        "same_provider_thread": True,
        "new_run": True,
        "new_connection": True,
        "new_app_server_process": True,
        "provider_neutral_resume_intent": True,
        "native_resume_command": True,
        "bridge_subscribed": True,
        "post_resume_provider_activity": True,
        "post_resume_response_correlated": True,
        "post_resume_marker_in_assistant_transcript": True,
        "stale_input_rejected": True,
        "stale_generation_dispatched": False,
        "concurrent_resume_refused": True,
        "artifact_secret_scan_passed": True,
        "final_cleanup_verified": True,
        "orphan_count": 0,
        "clean_stop_verified": True,
    }
    assert native_resume_assertions("clean_exit", observation) == {"native_provider_resume_proven": True}
    observation["stale_generation_dispatched"] = True
    assert native_resume_assertions("clean_exit", observation) == {"native_provider_resume_proven": False}
    observation["stale_generation_dispatched"] = False
    observation["post_resume_provider_activity"] = False
    assert native_resume_assertions("clean_exit", observation) == {"native_provider_resume_proven": False}


def test_native_resume_oracle_accepts_claude_correlation_without_literal_echo() -> None:
    from zerg.qa.provider_resume_oracles import native_resume_assertions

    observation = {
        "same_longhouse_session": True,
        "same_provider_thread": True,
        "new_run": True,
        "new_connection": True,
        "new_app_server_process": True,
        "provider_neutral_resume_intent": True,
        "native_resume_command": True,
        "bridge_subscribed": True,
        "post_resume_provider_activity": True,
        "post_resume_response_correlated": True,
        "post_resume_marker_in_assistant_transcript": False,
        "stale_input_rejected": True,
        "stale_generation_dispatched": False,
        "concurrent_resume_refused": True,
        "artifact_secret_scan_passed": True,
        "final_cleanup_verified": True,
        "orphan_count": 0,
        "clean_stop_verified": True,
    }

    assert native_resume_assertions("clean_exit", observation) == {"native_provider_resume_proven": True}


def test_scripted_helm_selection_does_not_require_a_provider_token() -> None:
    from zerg.qa import omp_helm_lifecycle
    from zerg.qa.resume_assurance import _registration_bindings
    from zerg.qa.resume_assurance import _select_evidence_class

    registration = omp_helm_lifecycle.REGISTRATION.to_dict()
    cell = {
        "provider": "omp",
        "acceptable_evidence": ["live_token", "scripted_model"],
        "required_evidence_classes": ["live_token", "scripted_model"],
    }
    census = {"credential_binding_ids": ["runtime_host_control"]}

    evidence_class = _select_evidence_class(registration, cell, census)

    assert evidence_class == "scripted_model"
    assert _registration_bindings(registration, provider="omp", evidence_class=evidence_class) == ["runtime_host_control"]
