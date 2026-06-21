from __future__ import annotations

from zerg.provider_orchestration_capabilities import provider_orchestration_capabilities
from zerg.provider_orchestration_capabilities import provider_orchestration_capability_manifest


def test_provider_orchestration_capability_manifest_is_complete():
    manifest = provider_orchestration_capability_manifest()

    capabilities = set(manifest["capabilities"])
    states = set(manifest["states"])
    assert states == {"supported", "unsupported", "unknown", "experimental", "observed_only"}
    assert capabilities == {
        "observe_transcript",
        "observe_child_sessions",
        "classify_forks",
        "classify_subagents",
        "send_prompt",
        "send_async_prompt",
        "abort",
        "reattach",
        "fork",
        "switch_actor",
        "background_task_status",
    }

    for provider, table in manifest["providers"].items():
        assert set(table) == capabilities, provider
        for capability, entry in table.items():
            assert entry["state"] in states, (provider, capability)
            assert entry["source"].strip(), (provider, capability)


def test_opencode_orchestration_capabilities_match_lineage_support():
    table = provider_orchestration_capabilities("opencode")

    assert table["observe_child_sessions"]["state"] == "supported"
    assert table["classify_subagents"]["state"] == "supported"
    assert table["classify_forks"]["state"] == "observed_only"
    assert table["send_async_prompt"]["state"] == "observed_only"
    assert table["background_task_status"]["state"] == "experimental"
