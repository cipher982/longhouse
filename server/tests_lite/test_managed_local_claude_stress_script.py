from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace


def _load_script_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "managed_local_claude_stress.py"
    spec = importlib.util.spec_from_file_location("managed_local_claude_stress", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_stress_prompts_are_unique_and_single_line():
    module = _load_script_module()

    prompts = module.build_stress_prompts(count=4, prefix="lh-test", nonce="abc123")

    assert prompts == [
        "lh-test-01-abc123",
        "lh-test-02-abc123",
        "lh-test-03-abc123",
        "lh-test-04-abc123",
    ]
    assert len(set(prompts)) == 4
    assert all("\n" not in prompt for prompt in prompts)


def test_parse_sse_lines_handles_multi_line_events():
    module = _load_script_module()

    events = list(
        module.parse_sse_lines(
            [
                "event: system",
                'data: {"type":"session_started"}',
                "",
                "event: message",
                "data: line one",
                "data: line two",
                "",
            ]
        )
    )

    assert [(event.event, event.data) for event in events] == [
        ("system", '{"type":"session_started"}'),
        ("message", "line one\nline two"),
    ]


def test_assess_prompt_delivery_rejects_missing_or_duplicate_user_events():
    module = _load_script_module()

    missing = module.assess_prompt_delivery(
        prompt="hello",
        exact_user_events_before=0,
        new_events=[SimpleNamespace(role="assistant", content_text="hi")],
    )
    duplicate = module.assess_prompt_delivery(
        prompt="hello",
        exact_user_events_before=0,
        new_events=[
            SimpleNamespace(role="user", content_text="hello"),
            SimpleNamespace(role="user", content_text="hello"),
        ],
    )
    success = module.assess_prompt_delivery(
        prompt="hello",
        exact_user_events_before=2,
        new_events=[
            SimpleNamespace(role="user", content_text="hello"),
            SimpleNamespace(role="assistant", content_text="received hello"),
        ],
    )

    assert missing.ok is False
    assert missing.error == "Prompt did not appear in new user events"

    assert duplicate.ok is False
    assert duplicate.error == "Prompt appeared more than once in new user events"

    assert success.ok is True
    assert success.exact_user_events_before == 2
    assert success.exact_user_events_after == 3
    assert success.assistant_messages == ("received hello",)
