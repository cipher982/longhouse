from importlib import reload


def test_max_output_tokens_is_passed_to_chat_openai(monkeypatch):
    # Arrange: set env and reload settings to pick it up
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "777")

    # Import late to ensure env is in effect
    import zerg.agents_def.zerg_react_agent as zr

    reload(zr)

    captured_kwargs = {}

    class _CapChat:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)

        def bind_tools(self, tools):  # noqa: D401
            class _Stub:
                def invoke(self, _messages):
                    from langchain_core.messages import AIMessage

                    return AIMessage(content="ok")

            return _Stub()

    monkeypatch.setattr(zr, "ChatOpenAI", _CapChat)

    # Minimal agent row stub
    class _Agent:
        id = 1
        model = "gpt-4o-mini"
        updated_at = None
        config = {}
        allowed_tools = None

    # Act: build runnable (which calls _make_llm under the hood)
    _ = zr.get_runnable(_Agent())

    # Assert: constructor received max_tokens from env
    assert captured_kwargs.get("max_tokens") == 777
