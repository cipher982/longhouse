"""The scripted Codex and Claude profiles make no call outside the scripted endpoint."""

import tomllib

from zerg.qa import scripted_model_profiles as profiles

ENDPOINT = "http://127.0.0.1:9999"


def test_codex_profile_disables_websockets_and_keeps_api_key_login(tmp_path):
    environment = {profiles.SCRIPTED_MODEL_BASE_URL_ENV: ENDPOINT}
    profiles.configure_scripted_model_profile("codex", tmp_path, environment)
    config = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    provider = config["model_providers"][config["model_provider"]]
    assert provider["supports_websockets"] is False
    assert provider["base_url"] == f"{ENDPOINT}/v1"
    assert provider["wire_api"] == "responses"
    assert provider["requires_openai_auth"] is True


def test_claude_profile_disables_nonessential_traffic(tmp_path):
    environment = {profiles.SCRIPTED_MODEL_BASE_URL_ENV: ENDPOINT}
    profiles.configure_scripted_model_profile("claude", tmp_path, environment)
    assert environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert environment["ANTHROPIC_BASE_URL"] == ENDPOINT
