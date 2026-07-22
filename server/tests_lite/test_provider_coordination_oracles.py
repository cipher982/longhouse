from zerg.qa.provider_coordination_oracles import awareness_create_assertions
from zerg.qa.provider_coordination_oracles import awareness_post_compaction_assertions
from zerg.qa.provider_coordination_oracles import message_assertions


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


def test_message_oracle_requires_durable_delivery_and_attribution() -> None:
    assert message_assertions(
        {
            "message_persisted": True,
            "message_delivered": True,
            "message_visible": True,
            "source_session_id": "session-1",
        }
    ) == {
        "directed_message_persisted_and_delivered": True,
        "attributed_message_visible": True,
    }
    assert message_assertions({}) == {
        "directed_message_persisted_and_delivered": False,
        "attributed_message_visible": False,
    }
