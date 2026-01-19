"""Tests for conversation title generation payloads."""

import pytest

from zerg.services.title_generator import generate_conversation_title


@pytest.mark.asyncio
async def test_title_generator_omits_max_output_tokens(monkeypatch):
    captured = {}

    class DummyResponse:
        def __init__(self):
            self.status_code = 200
            self.is_success = True

        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": '{"title": "Test Title"}'}

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return DummyResponse()

    monkeypatch.setattr("zerg.services.title_generator.httpx.AsyncClient", DummyClient)

    result = await generate_conversation_title(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
    )

    assert result == "Test Title"
    assert "max_output_tokens" not in captured["json"]
