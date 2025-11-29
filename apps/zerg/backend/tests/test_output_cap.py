from importlib import reload


def test_max_output_tokens_is_passed_to_chat_openai(monkeypatch):
    from unittest.mock import MagicMock
    from importlib import reload
    import zerg.config
    import zerg.agents_def.zerg_react_agent as zr

    # Create a mock settings object
    mock_settings = MagicMock()
    mock_settings.max_output_tokens = 777
    mock_settings.llm_token_stream = False
    mock_settings.openai_api_key = "sk-test"

    # Patch get_settings in the config module
    monkeypatch.setattr(zerg.config, "get_settings", lambda: mock_settings)
    
    # Reload agent module to pick up patched get_settings
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

                async def ainvoke(self, _messages, **kwargs):
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

    # Assert: constructor received max_tokens from settings
    assert captured_kwargs.get("max_tokens") == 777
