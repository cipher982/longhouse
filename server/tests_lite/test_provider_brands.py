from zerg.generated.provider_brands import provider_display_name


def test_provider_display_names_cover_canonical_alias_and_fallback() -> None:
    assert provider_display_name("opencode") == "OpenCode"
    assert provider_display_name("openai") == "OpenAI"
    assert provider_display_name("gemini") == "Antigravity"
    assert provider_display_name("z.ai") == "Z.ai"
    assert provider_display_name("new_provider") == "New Provider"
    assert provider_display_name("") == "Session"
