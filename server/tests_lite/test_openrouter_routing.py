"""The factory's OpenRouter routing: one definition, applied to the OpenCode and Pi lanes."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from zerg.qa.openrouter_routing import OPENROUTER_QUALIFICATION_ROUTING
from zerg.qa.openrouter_routing import openrouter_qualification_routing
from zerg.qa.openrouter_routing import pi_openrouter_model_id
from zerg.qa.openrouter_routing import prepare_pi_openrouter_routing


def test_routing_prefers_fast_hosts_and_never_prefers_an_ignored_one() -> None:
    routing = OPENROUTER_QUALIFICATION_ROUTING
    assert set(routing) == {"order", "allow_fallbacks", "ignore"}
    # Fallbacks stay on: one host's outage must not be a red cell.
    assert routing["allow_fallbacks"] is True
    assert routing["order"] and len(set(routing["order"])) == len(routing["order"])
    # The host behind the 2026-09-30 177.7 s stall is excluded, and no host is both.
    assert "open-inference" in routing["ignore"]
    assert not set(routing["order"]) & set(routing["ignore"])


def test_a_caller_gets_a_copy_it_cannot_use_to_edit_the_shared_routing() -> None:
    copy = openrouter_qualification_routing()
    copy["order"].append("anything")
    copy["ignore"].clear()
    assert "anything" not in OPENROUTER_QUALIFICATION_ROUTING["order"]
    assert OPENROUTER_QUALIFICATION_ROUTING["ignore"]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("deepseek/deepseek-v4.1-flash:off", "deepseek/deepseek-v4.1-flash"),
        ("deepseek/deepseek-v4.1-flash:xhigh", "deepseek/deepseek-v4.1-flash"),
        ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash"),
        # An OpenRouter variant suffix is part of the id, not a thinking level.
        ("meta-llama/llama-3.3-70b-instruct:free", "meta-llama/llama-3.3-70b-instruct:free"),
        ("meta-llama/llama-3.3-70b-instruct:free:off", "meta-llama/llama-3.3-70b-instruct:free"),
    ],
)
def test_pi_model_id_drops_only_a_thinking_level(configured: str, expected: str) -> None:
    assert pi_openrouter_model_id(configured) == expected


def test_pi_models_json_overrides_the_one_model_and_holds_no_credential(tmp_path: Path) -> None:
    agent_dir = tmp_path / ".pi"

    receipt = prepare_pi_openrouter_routing(agent_dir, "deepseek/deepseek-v4.1-flash:off")

    path = agent_dir / "models.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {
        "providers": {
            "openrouter": {
                "modelOverrides": {
                    "deepseek/deepseek-v4.1-flash": {"compat": {"openRouterRouting": OPENROUTER_QUALIFICATION_ROUTING}},
                }
            }
        }
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "apiKey" not in path.read_text(encoding="utf-8")
    assert receipt == {
        "provider": "openrouter",
        "model_id": "deepseek/deepseek-v4.1-flash",
        "config_path": str(path),
        "routing": OPENROUTER_QUALIFICATION_ROUTING,
    }


def test_pi_models_json_keeps_what_was_already_configured(tmp_path: Path) -> None:
    agent_dir = tmp_path / ".pi"
    agent_dir.mkdir()
    (agent_dir / "models.json").write_text(
        json.dumps({"providers": {"openrouter": {"baseUrl": "http://127.0.0.1:1", "modelOverrides": {"other/model": {"name": "kept"}}}}}),
        encoding="utf-8",
    )

    prepare_pi_openrouter_routing(agent_dir, "deepseek/deepseek-v4.1-flash")

    openrouter = json.loads((agent_dir / "models.json").read_text(encoding="utf-8"))["providers"]["openrouter"]
    assert openrouter["baseUrl"] == "http://127.0.0.1:1"
    assert openrouter["modelOverrides"]["other/model"] == {"name": "kept"}
    assert openrouter["modelOverrides"]["deepseek/deepseek-v4.1-flash"]["compat"]["openRouterRouting"] == OPENROUTER_QUALIFICATION_ROUTING


def test_pi_routing_refuses_a_missing_model(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        prepare_pi_openrouter_routing(tmp_path / ".pi", "  ")
