from types import SimpleNamespace

import pytest

from zerg.services.title_generator import _build_initial_session_title_prompt
from zerg.services.title_generator import generate_initial_session_title


def test_initial_title_prompt_keeps_context_and_cleans_message():
    prompt = _build_initial_session_title_prompt(
        "```text\n[Image #1]\nplease fix the token handling\n```",
        metadata={"project": "longhouse", "provider": "codex", "git_branch": "main"},
    )

    assert prompt is not None
    assert "Project: longhouse" in prompt
    assert "Provider: codex" in prompt
    assert "Branch: main" in prompt
    assert "[Image #1]" not in prompt
    assert "```" not in prompt
    assert "please fix the token handling" in prompt


@pytest.mark.asyncio
async def test_generate_initial_session_title_parses_json_response():
    captured: dict[str, object] = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"title":"Menu Bar Row Affordance"}'),
                )
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    title = await generate_initial_session_title(
        first_user_message="make menu bar rows obviously clickable",
        client=client,
        model="deepseek/deepseek-v4-flash",
        metadata={"project": "longhouse"},
    )

    assert title == "Menu Bar Row Affordance"
    assert captured["model"] == "deepseek/deepseek-v4-flash"
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][1]["role"] == "user"
