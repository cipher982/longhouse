"""Provider-neutral postconditions for Helm Resume factory scenarios."""

from __future__ import annotations

from collections.abc import Mapping

ASSERTIONS_BY_SCENARIO = {
    "helm_cold_resume": ("cold_resume_registers_continuous_thread",),
    "helm_live_reattach": ("live_reattach_does_not_spawn_owner",),
    "console_thread_continue": ("console_continuation_is_distinct",),
    "resume_identity_continuity": ("session_thread_and_machine_identity_continue",),
    "resume_attempt_idempotency": ("resume_attempt_is_idempotent",),
    "resume_single_owner": ("one_local_provider_owner_wins",),
    "resume_input_safety": ("stale_input_is_not_replayed",),
    "resume_failure_cleanup": ("failed_resume_closes_attempt_and_restores_contract",),
    "resume_unsupported": ("unsupported_resume_is_typed_and_side_effect_free",),
}


def assertions_for(scenario_id: str, observation: Mapping[str, object]) -> dict[str, bool]:
    if scenario_id == "helm_cold_resume":
        return {
            "cold_resume_registers_continuous_thread": (
                observation.get("same_longhouse_session") is True
                and observation.get("same_provider_thread") is True
                and observation.get("new_run") is True
                and observation.get("new_connection") is True
                and observation.get("resume_registration_confirmed") is True
            )
        }
    if scenario_id == "helm_live_reattach":
        return {
            "live_reattach_does_not_spawn_owner": (
                observation.get("resume_refused_as_conflict") is True
                and observation.get("reattached_live_owner") is True
                and observation.get("open_run_count") == 1
                and observation.get("provider_owner_spawn_count") == 0
            )
        }
    if scenario_id == "console_thread_continue":
        return {
            "console_continuation_is_distinct": (
                observation.get("mode") == "console"
                and observation.get("used_helm_resume_transaction") is False
                and observation.get("same_provider_thread") is True
                and observation.get("turn_created") is True
                and observation.get("console_run_created") is True
            )
        }
    if scenario_id == "resume_identity_continuity":
        return {
            "session_thread_and_machine_identity_continue": all(
                observation.get(field) is True
                for field in (
                    "same_owner",
                    "same_machine",
                    "same_cwd",
                    "same_provider",
                    "same_longhouse_session",
                    "same_provider_thread",
                )
            )
        }
    if scenario_id == "resume_attempt_idempotency":
        return {
            "resume_attempt_is_idempotent": (
                observation.get("same_attempt_same_run") is True
                and observation.get("created_run_count") == 1
                and observation.get("concurrent_attempt_refused") is True
            )
        }
    if scenario_id == "resume_single_owner":
        return {
            "one_local_provider_owner_wins": (
                observation.get("winner_count") == 1
                and observation.get("loser_count") == 1
                and observation.get("live_provider_owner_count") == 1
            )
        }
    if scenario_id == "resume_input_safety":
        return {
            "stale_input_is_not_replayed": (
                observation.get("old_input_delivery_unknown") is True and observation.get("stale_generation_dispatched") is False
            )
        }
    if scenario_id == "resume_failure_cleanup":
        return {
            "failed_resume_closes_attempt_and_restores_contract": (
                observation.get("attempt_closed") is True
                and observation.get("contract_restored") is True
                and observation.get("orphan_count") == 0
            )
        }
    if scenario_id == "resume_unsupported":
        return {
            "unsupported_resume_is_typed_and_side_effect_free": (
                observation.get("disposition") in {"upstream_absent", "policy_disabled"}
                and observation.get("registration_count") == 0
                and observation.get("provider_spawn_count") == 0
            )
        }
    raise KeyError(scenario_id)


__all__ = ["ASSERTIONS_BY_SCENARIO", "assertions_for"]
