from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.services.provider_capability_schema import load_capability_assertions


def _receipts(provider: str = "claude") -> tuple[dict, dict, dict, dict]:
    identity = {
        "provider": provider,
        "session_id": "session-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "prompt_digest": "sha256:" + "a" * 64,
    }
    dispatch = {"status": "pass", **identity}
    binding = {
        "status": "pass",
        **identity,
        "marker": "LH_MARKER",
        "provider_response_marker_count": 1,
        "provider_response_excerpt": '{"assistant":"LH_MARKER"}',
        "bound_assistant_event_id": "event-1",
        "bound_assistant_event_origin": "durable",
        "bound_assistant_event_excerpt": "LH_MARKER",
        "bound_assistant_marker_count": 1,
        "marker_in_provider_response": True,
        "marker_in_bound_assistant_event": True,
        "assistant_event_count": 1,
        "transcript_converged_exactly_once": True,
    }
    interrupt = {
        "status": "pass",
        "expectation": "supported",
        "interrupt_dispatched": True,
        "active_run_cancelled": True,
        "provider_process_dead": True,
        "post_interrupt_turn_completed": True,
    }
    cleanup = {
        "status": "pass",
        "provider_process_dead": True,
        "process_group_dead": True,
        "orphan_count": 0,
    }
    return dispatch, binding, interrupt, cleanup


def test_registration_covers_each_launch_provider_with_least_authority_credentials():
    registration = lifecycle.REGISTRATION.to_dict()

    assert registration["producer_id"] == "provider.console_lifecycle.v1"
    assert registration["providers"] == list(lifecycle.PROVIDERS)
    assert registration["subject_kind"] == "provider_release"
    assert registration["provider_artifact_required"] is True
    assert registration["credential_binding_ids"] == []
    for provider in lifecycle.PROVIDERS:
        assert registration["credential_binding_ids_by_provider"][provider] == [
            f"{provider}_provider_token",
            "runtime_host_control",
        ]
        assert f"{provider}_console_adapter_lifecycle" in registration["scenario_ids"]


def test_schema_gates_every_console_adapter_on_its_typed_release_assertion():
    assertions = {
        (item.provider, item.variant, item.scenario_id)
        for item in load_capability_assertions()
        if item.capability == "session.turn.start"
        and item.assertion_id == lifecycle.ASSERTION_ID
    }

    assert assertions == {
        (provider, lifecycle._expected_variant(provider), lifecycle._scenario_id(provider))
        for provider in lifecycle.PROVIDERS
    }


def test_console_oracle_accepts_complete_independent_receipts():
    dispatch, binding, interrupt, cleanup = _receipts()
    observation = lifecycle._observation_from_receipts(
        dispatch=dispatch,
        binding=binding,
        interrupt=interrupt,
        cleanup=cleanup,
    )

    assert lifecycle.console_lifecycle_assertions(observation) == {
        lifecycle.ASSERTION_ID: True
    }


@pytest.mark.parametrize(
    ("receipt_index", "field", "value"),
    [
        (0, "run_id", "run-other"),
        (1, "marker_in_provider_response", False),
        (1, "bound_assistant_event_id", None),
        (1, "bound_assistant_marker_count", 2),
        (1, "assistant_event_count", 2),
        (2, "status", "fail"),
        (3, "orphan_count", 1),
    ],
)
def test_console_oracle_fails_closed_on_missing_binding_or_cleanup(
    receipt_index: int, field: str, value: object
):
    receipts = list(_receipts())
    receipts[receipt_index] = copy.deepcopy(receipts[receipt_index])
    receipts[receipt_index][field] = value

    observation = lifecycle._observation_from_receipts(
        dispatch=receipts[0],
        binding=receipts[1],
        interrupt=receipts[2],
        cleanup=receipts[3],
    )

    assert lifecycle.console_lifecycle_assertions(observation) == {
        lifecycle.ASSERTION_ID: False
    }


@pytest.mark.parametrize(
    ("provider", "variant"),
    [
        ("codex", lifecycle.UNSUPPORTED_VARIANT),
        ("claude", lifecycle.SUPPORTED_VARIANT),
        ("opencode", lifecycle.SUPPORTED_VARIANT),
        ("cursor", lifecycle.SUPPORTED_VARIANT),
    ],
)
def test_interrupt_expectation_is_provider_typed(provider: str, variant: str):
    assert lifecycle._expected_variant(provider) == variant


@pytest.mark.parametrize(
    ("provider", "raw", "normalized"),
    [
        ("codex", "codex-cli 1.2.3", "1.2.3"),
        ("claude", "2.1.0 (Claude Code)", "2.1.0"),
        ("opencode", "1.17.20", "1.17.20"),
        ("cursor", "2026.07.23-e383d2b", "2026.07.23-e383d2b"),
    ],
)
def test_provider_version_probe_normalizes_the_staged_release(
    monkeypatch, tmp_path, provider: str, raw: str, normalized: str
):
    binary = tmp_path / provider
    binary.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, raw + "\n", ""),
    )

    assert lifecycle._probe_version(provider, binary) == (normalized, raw)


def test_provider_version_probe_rejects_unrecognized_output(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "unknown build\n", ""),
    )

    with pytest.raises(RuntimeError, match="release grammar"):
        lifecycle._probe_version("codex", binary)


def test_dispatch_claim_must_name_the_exact_staged_binary(tmp_path):
    staged = tmp_path / "provider"
    staged.write_text("fixture", encoding="utf-8")

    assert lifecycle._claim_uses_provider_binary(
        {"result": {"argv": [str(staged), "--print"]}}, staged
    )
    assert not lifecycle._claim_uses_provider_binary(
        {"result": {"argv": [str(tmp_path / "other"), "--print"]}}, staged
    )


def test_provider_console_registration_cli_is_hermetic_without_database_env(tmp_path):
    server_root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(server_root),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "zerg.qa.provider_console_lifecycle", "--registration"],
        cwd=server_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout)["producer_id"] == "provider.console_lifecycle.v1"


def test_console_runtime_wake_socket_stays_below_linux_path_limit():
    _runtime, _evidence, _workspace, longhouse_home = lifecycle._console_runtime_paths(
        Path("/run/lhq/sandbox-home")
    )
    wake_socket = longhouse_home / "agent" / "transcript-wake.sock"

    assert len(os.fsencode(wake_socket)) <= 90
