"""Replay the search-ingest oracle over recorded good and bad evidence."""

from __future__ import annotations

import copy

from zerg.qa import search_ingest_oracle as oracle
from zerg.qa import transcript_search_producer as producer

SESSION = "4f9b0c1e-0000-4000-8000-000000000001"
MARKER = {"marker": "lhsearch1a2b3c4d5e6f7a8b", "pieces": "lhsearch 1a2b3c4d 5e6f7a8b"}


def _observation() -> dict:
    return {
        "marker": MARKER["marker"],
        "prompt": oracle.search_prompt(MARKER),
        "provider_marker_count": 1,
        "flush_ok": True,
        "session_id": SESSION,
        "search": {
            "http_status": 200,
            "lanes": ["lexical"],
            "degraded": [],
            "hits": [{"session_id": SESSION, "provider": "codex", "match_role": "assistant", "match_snippet": MARKER["marker"]}],
        },
    }


def _code(observation: dict) -> str | None:
    return oracle.search_ingest_verdict(observation)["failure_code"]


def test_recorded_good_evidence_passes() -> None:
    assert oracle.search_ingest_verdict(_observation())["passed"] is True


def test_new_marker_never_appears_joined_in_its_prompt() -> None:
    for _ in range(50):
        marker = oracle.new_marker()
        prompt = oracle.search_prompt(marker)
        assert marker["marker"] not in prompt
        assert marker["marker"] == "".join(marker["pieces"].split())


def test_prompt_echo_is_rejected() -> None:
    observation = _observation()
    observation["prompt"] = f"Reply exactly {MARKER['marker']}"
    assert _code(observation) == "marker_echoed_in_prompt"


def test_provider_that_never_produced_the_marker_is_not_a_search_verdict() -> None:
    observation = _observation()
    observation["provider_marker_count"] = 0
    assert _code(observation) == "provider_did_not_produce_marker"


def test_silent_ingest_loss_is_caught() -> None:
    observation = _observation()
    observation["search"]["hits"] = []
    assert _code(observation) == "marker_not_found_in_served_search"


def test_hit_on_another_session_is_rejected() -> None:
    observation = _observation()
    observation["search"]["hits"] = [{**observation["search"]["hits"][0], "session_id": "other"}]
    assert _code(observation) == "marker_found_in_wrong_session"
    both = _observation()
    both["search"]["hits"].append({**both["search"]["hits"][0], "session_id": "other"})
    assert _code(both) == "marker_found_in_wrong_session"


def test_user_event_match_is_rejected() -> None:
    observation = _observation()
    observation["search"]["hits"][0]["match_role"] = "user"
    assert _code(observation) == "marker_matched_non_assistant_event"


def test_dense_fallback_does_not_count_as_full_text_search() -> None:
    observation = _observation()
    observation["search"]["lanes"] = ["dense"]
    observation["search"]["degraded"] = [{"lane": "lexical", "code": "search_unavailable"}]
    assert _code(observation) == "search_lane_degraded"


def test_failed_flush_and_failed_probe_are_typed() -> None:
    observation = _observation()
    observation["flush_ok"] = False
    assert _code(observation) == "transcript_flush_failed"
    probe = _observation()
    probe["search"] = {"http_status": 503, "error": "search_unavailable"}
    assert _code(probe) == "search_probe_failed"


def _redacted() -> dict:
    observation = copy.deepcopy(_observation())
    observation["search"]["hits"] = []
    return observation


def _receipt(session_id: str = SESSION) -> dict:
    return {"fault": oracle.FAULT_RECEIPT_NAME, "session_id": session_id, "detail": {"redacted_occurrences": 1}}


def test_negative_control_passes_only_when_fault_fired_and_search_missed() -> None:
    assert oracle.negative_control_verdict(_redacted(), fault_receipts=[_receipt()])["status"] == "pass"


def test_negative_control_without_fault_receipt_is_inconclusive() -> None:
    assert oracle.negative_control_verdict(_redacted(), fault_receipts=[])["status"] == "inconclusive"
    assert oracle.negative_control_verdict(_redacted(), fault_receipts=[_receipt("other")])["status"] == "inconclusive"


def test_negative_control_with_broken_preconditions_is_inconclusive() -> None:
    observation = _redacted()
    observation["provider_marker_count"] = 0
    assert oracle.negative_control_verdict(observation, fault_receipts=[_receipt()])["status"] == "inconclusive"


def test_negative_control_that_still_finds_the_marker_fails() -> None:
    assert oracle.negative_control_verdict(_observation(), fault_receipts=[_receipt()])["status"] == "fail"


def test_registration_names_one_live_token_console_cell_per_provider() -> None:
    registration = producer.REGISTRATION.to_dict()
    assert registration["assertion_cells"] == [{"assertion_id": "transcript_ingested_searchable", "variant": None}] or registration[
        "assertion_cells"
    ] == [["transcript_ingested_searchable", None]]
    assert set(producer.PROVIDERS) == {"claude", "codex", "cursor", "opencode", "pi", "omp"}
    assert registration["evidence_classes"] == ["live_token"]
    assert registration["modes"] == ["console"]
