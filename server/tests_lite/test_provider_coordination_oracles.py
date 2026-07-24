from zerg.qa.provider_coordination_oracles import awareness_create_assertions
from zerg.qa.provider_coordination_oracles import awareness_post_compaction_assertions
from zerg.qa.provider_coordination_oracles import directed_input_assertions


def test_awareness_oracles_require_model_visibility_and_no_duplicate_cards() -> None:
    assert awareness_create_assertions({"coordination_instructions_model_visible": True}) == {
        "coordination_instructions_model_visible": True
    }
    assert awareness_post_compaction_assertions(
        {
            "coordination_instructions_model_visible_after_compaction": True,
            "visible_bootstrap_count": 0,
        }
    ) == {
        "coordination_instructions_model_visible_after_compaction": True,
        "no_duplicate_visible_bootstrap": True,
    }
    assert awareness_post_compaction_assertions(
        {
            "coordination_instructions_model_visible_after_compaction": False,
            "visible_bootstrap_count": 4,
        }
    ) == {
        "coordination_instructions_model_visible_after_compaction": False,
        "no_duplicate_visible_bootstrap": False,
    }


def test_directed_input_oracle_keeps_persistence_receipt_and_visibility_separate() -> None:
    assert directed_input_assertions(
        {
            "input_persisted": True,
            "input_receipt_linked": True,
            "input_visible": True,
            "source_session_id": "session-1",
        }
    ) == {
        "directed_input_persisted": True,
        "provider_input_receipt_linked": True,
        "attributed_input_visible": True,
    }
    assert directed_input_assertions({}) == {
        "directed_input_persisted": False,
        "provider_input_receipt_linked": False,
        "attributed_input_visible": False,
    }
