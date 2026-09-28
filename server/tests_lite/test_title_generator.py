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
async def test_generate_initial_session_title_parses_json_response(monkeypatch):
    # Title generation sends the first user message to a model provider, which
    # is opt-in. This test is about parsing the opted-in response.
    monkeypatch.setenv("AI_TITLES_AND_SUMMARIES_ENABLED", "1")
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


@pytest.mark.asyncio
async def test_generate_initial_session_title_rejects_dsml_tool_call_markup(monkeypatch):
    # Hosted session 707f95ae got title "<｜DSML｜tool_calls>" (title_source=ai):
    # DeepSeek emitted its native fullwidth-bar tool-call markup as plain
    # content. That is never a title -- returning None here lets the existing
    # empty_model_response retry/fallback path handle it instead of freezing
    # the write-once anchor on garbage.
    monkeypatch.setenv("AI_TITLES_AND_SUMMARIES_ENABLED", "1")

    async def _create(**_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="<｜DSML｜tool_calls>", tool_calls=None))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    title = await generate_initial_session_title(
        first_user_message="fix the retry loop",
        client=client,
        model="deepseek/deepseek-v4-flash",
        metadata={"project": "longhouse"},
    )

    assert title is None


@pytest.mark.asyncio
async def test_generate_initial_session_title_rejects_conversational_reply(monkeypatch):
    # Hosted session e425ca05 got title "Great to hear you simplified your..."
    # (title_source=ai): the model answered the user's first message in prose
    # instead of returning JSON, and the raw text was frozen as the anchor.
    monkeypatch.setenv("AI_TITLES_AND_SUMMARIES_ENABLED", "1")

    async def _create(**_kwargs):
        reply = "Great to hear you simplified your network! Let me review the Deco setup."
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply, tool_calls=None))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    title = await generate_initial_session_title(
        first_user_message="finally cleaned up my network, can you review it?",
        client=client,
        model="deepseek/deepseek-v4-flash",
    )

    assert title is None


@pytest.mark.asyncio
async def test_generate_initial_session_title_rejects_structured_tool_calls(monkeypatch):
    monkeypatch.setenv("AI_TITLES_AND_SUMMARIES_ENABLED", "1")

    async def _create(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[SimpleNamespace(id="call_1", function=SimpleNamespace(name="title", arguments="{}"))],
                    )
                )
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    title = await generate_initial_session_title(
        first_user_message="fix the retry loop",
        client=client,
        model="deepseek/deepseek-v4-flash",
        metadata={"project": "longhouse"},
    )

    assert title is None


@pytest.mark.asyncio
async def test_generate_initial_session_title_is_off_by_default(monkeypatch):
    """The chokepoint every title path funnels through refuses to call out.

    Both the legacy trigger and the storage-v2 worker land here, so this is the
    single place that decides whether a first user message leaves the machine.
    """

    monkeypatch.delenv("AI_TITLES_AND_SUMMARIES_ENABLED", raising=False)

    async def _create(**_kwargs):
        raise AssertionError("provider called while transcript egress is off")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    title = await generate_initial_session_title(
        first_user_message="make menu bar rows obviously clickable",
        client=client,
        model="deepseek/deepseek-v4-flash",
        metadata={"project": "longhouse"},
    )

    assert title is None
