"""Cursor writes envelopes into `role="user"` records that no human typed."""

from zerg.services.cursor_transcript import _classify_cursor_user_text


def test_subagent_catalogue_preamble_is_context_not_a_prompt():
    text = (
        "<available_subagent_types>\n"
        "- explore: Fast agent for codebases.\n"
        "</available_subagent_types>\n"
        "<available_subagent_models>\n"
        "- fast\n"
        "</available_subagent_models>\n"
        "<dynamic_tool_catalog>\n"
        "<dynamic_tool_namespaces>\n"
        '<namespace name="cursor" tools="CreateGoal" />\n'
        "</dynamic_tool_namespaces>\n"
        "</dynamic_tool_catalog>"
    )

    _, role = _classify_cursor_user_text(text)

    assert role == "system"


def test_a_prompt_that_merely_mentions_a_harness_tag_stays_a_prompt():
    _, role = _classify_cursor_user_text("What does <available_subagent_types> mean in Cursor?")

    assert role == "user"
