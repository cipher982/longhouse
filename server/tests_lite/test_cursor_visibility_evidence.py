import hashlib

from zerg.qa.cursor_visibility_evidence import build_visibility_report
from zerg.qa.cursor_visibility_evidence import render_terminal


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _message(role: str, text: str) -> dict:
    return {"role": role, "message": {"content": [{"type": "text", "text": text}]}}


def test_success_correlates_by_receipt_even_when_stop_arrives_first() -> None:
    answer = "VISIBLE_SUCCESS"
    report = build_visibility_report(
        transcript_rows=[
            _message("user", "Reply with exactly VISIBLE_SUCCESS"),
            _message("assistant", answer),
            {"type": "turn_ended", "status": "success"},
        ],
        hook_rows=[
            {
                "event": "afterAgentResponse",
                "text_sha256": _digest("OTHER_RESPONSE"),
                "conversation_id": "other-conversation",
            },
            {"event": "stop", "status": "completed", "generation_id": "generation-1"},
            {
                "event": "afterAgentResponse",
                "text_sha256": _digest(answer),
                "generation_id": "generation-1",
                "conversation_id": "target-conversation",
            },
        ],
        terminal_display=["Reply with exactly VISIBLE_SUCCESS", answer],
        provider_conversation_id="target-conversation",
    )

    turn = report["turns"][0]
    assert turn["terminal_status"] == "success"
    assert turn["assistant_artifact_count"] == 1
    assert turn["assistant_content_groups"][0]["correlation"] == "provider_commit_receipt_unique_artifact"


def test_failed_retries_preserve_ambiguous_artifacts() -> None:
    answer = "VISIBLE_ONCE"
    report = build_visibility_report(
        transcript_rows=[
            _message("user", "Reply with exactly VISIBLE_ONCE"),
            *[_message("assistant", answer) for _ in range(4)],
            {"type": "turn_ended", "status": "error", "error": "WritableIterable is closed"},
        ],
        hook_rows=[{"event": "stop", "status": "error", "generation_id": "generation-1"}],
        terminal_display=["Reply with exactly VISIBLE_ONCE", answer, "WritableIterable is closed"],
    )

    turn = report["turns"][0]
    group = turn["assistant_content_groups"][0]
    assert turn["assistant_artifact_count"] == 4
    assert turn["terminal_error"] == "WritableIterable is closed"
    assert group["artifact_count"] == 4
    assert group["final_terminal_frame_occurrences"] == 1
    assert group["after_agent_response_receipt_count"] == 0
    assert group["correlation"] == "terminal_presented_ambiguous_artifacts"


def test_terminal_replay_applies_cursor_redraws() -> None:
    raw = b"first\r\x1b[2Ksecond"
    display = render_terminal(raw, columns=20, lines=4)
    assert display[0] == "second"
    assert "first" not in display
