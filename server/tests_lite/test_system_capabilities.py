from unittest.mock import patch


def test_system_capabilities_exposes_text_and_embedding_flags():
    from zerg.routers.system import system_capabilities

    def fake_capability(name: str) -> bool:
        return name == "text"

    with patch("zerg.routers.system.is_capability_available", side_effect=fake_capability):
        payload = system_capabilities()

    assert payload["llm_available"] is True
    assert payload["embeddings_available"] is False
