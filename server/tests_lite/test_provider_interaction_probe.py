from pathlib import Path
from subprocess import CompletedProcess

import pytest

from zerg.qa import provider_interaction_probe
from zerg.qa.provider_interaction_semantics import evaluate_observation


@pytest.mark.parametrize(
    ("provider", "version_output", "expected"),
    (
        ("claude", "2.1.219 (Claude Code)\n", "2.1.219"),
        ("codex", "codex-cli 0.133.0\n", "0.133.0"),
        ("opencode", "1.2.3\n", "1.2.3"),
        ("antigravity", "1.2.3\n", "1.2.3"),
        ("cursor", "2026.07.23-e383d2b\n", "2026.07.23-e383d2b"),
    ),
)
def test_provider_version_probe_returns_the_request_normalized_value(
    monkeypatch,
    tmp_path: Path,
    provider: str,
    version_output: str,
    expected: str,
) -> None:
    monkeypatch.setattr(
        provider_interaction_probe.subprocess,
        "run",
        lambda *_args, **_kwargs: CompletedProcess([], 0, version_output, ""),
    )

    assert (
        provider_interaction_probe._provider_version(  # noqa: SLF001
            provider,
            tmp_path / provider,
            environment={},
        )
        == expected
    )


@pytest.mark.parametrize(
    "name",
    (
        "AWS_PROFILE",
        "CODEX_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_USE_VERTEX",
    ),
)
def test_no_token_environment_rejects_ambient_provider_credentials(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, "ambient-credential")

    with pytest.raises(RuntimeError, match="credential-free environment"):
        provider_interaction_probe._no_token_environment()  # noqa: SLF001


def test_no_token_environment_scrubs_provider_controls_and_configuration(monkeypatch) -> None:
    for name in provider_interaction_probe._NO_TOKEN_AUTH_ENV_NAMES:  # noqa: SLF001
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LONGHOUSE_PROVIDER_INTERACTION_LIVE", "1")
    monkeypatch.setenv("LONGHOUSE_CLAUDE_INTERACTION_ARTIFACT", "/tmp/fixture.json")
    monkeypatch.setenv("LONGHOUSE_CURSOR_GATE0_ARTIFACT", "/tmp/gate0.json")
    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "openrouter/model")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")

    environment = provider_interaction_probe._no_token_environment()  # noqa: SLF001

    assert "LONGHOUSE_PROVIDER_INTERACTION_LIVE" not in environment
    assert "LONGHOUSE_CLAUDE_INTERACTION_ARTIFACT" not in environment
    assert "LONGHOUSE_CURSOR_GATE0_ARTIFACT" not in environment
    assert "LONGHOUSE_OPENCODE_QUALIFICATION_MODEL" not in environment
    assert "ANTHROPIC_MODEL" not in environment
    assert "ANTHROPIC_BASE_URL" not in environment
    assert "OPENAI_BASE_URL" not in environment
    assert environment["AWS_EC2_METADATA_DISABLED"] == "true"


@pytest.mark.parametrize(
    ("configured", "expected"),
    (
        ("deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-flash"),
        ("openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-flash"),
        ("", None),
    ),
)
def test_opencode_model_argument_is_explicit_and_provider_qualified(configured: str, expected: str | None) -> None:
    assert provider_interaction_probe._opencode_cli_model(configured) == expected  # noqa: SLF001


def test_provider_auth_prompt_detection_does_not_match_bare_status_or_help_text() -> None:
    assert provider_interaction_probe._looks_like_provider_auth_prompt("HTTP 401 retry; API key examples") is False  # noqa: SLF001
    assert provider_interaction_probe._looks_like_provider_auth_prompt("API key required to continue") is True  # noqa: SLF001


def test_cursor_stream_json_parser_keeps_native_init_and_result_records() -> None:
    events = provider_interaction_probe._cursor_stream_json_events(  # noqa: SLF001
        "\n".join(
            [
                '{"type":"system","subtype":"init","model":"grok-4.5","apiKeySource":"apiKey","session_id":"s"}',
                '{"type":"result","subtype":"success","session_id":"s","request_id":"r","result":"ok"}',
                "terminal text that is not a native event",
            ]
        )
    )

    assert [event["type"] for event in events] == ["system", "result"]
    assert events[0]["apiKeySource"] == "apiKey"
    assert events[1]["request_id"] == "r"


@pytest.mark.parametrize(
    ("requested", "observed"),
    (("cursor-grok-4.5-high", "Cursor Grok 4.5 High"), ("gpt-5.6-sol", "gpt-5.6-sol")),
)
def test_cursor_model_identity_accepts_cli_alias_and_display_name(requested: str, observed: str) -> None:
    assert provider_interaction_probe._cursor_model_identity(requested) == provider_interaction_probe._cursor_model_identity(observed)  # noqa: SLF001


def test_cursor_model_probe_binds_stream_events_to_a_native_capture_receipt(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "cursor-agent"
    binary.write_text("fixture", encoding="utf-8")
    stdout = "\n".join(
        [
            '{"type":"system","subtype":"init","model":"grok-4.5","apiKeySource":"apiKey","session_id":"s"}',
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"OK"}]},"session_id":"s"}',
            '{"type":"result","subtype":"success","session_id":"s","request_id":"r","result":"ok"}',
        ]
    )

    monkeypatch.setattr(
        provider_interaction_probe.subprocess,
        "run",
        lambda *_args, **_kwargs: CompletedProcess([], 0, stdout, ""),
    )

    artifact_root = tmp_path / "artifacts"
    rows, _source_rows = provider_interaction_probe._cursor_model_probe(  # noqa: SLF001
        binary=binary,
        artifact_root=artifact_root,
        timeout=1,
        environment={"CURSOR_API_KEY": "fixture-token", "CURSOR_MODEL": "grok-4.5"},
    )
    row = rows[0]

    assert row["status"] == "observed"
    assert row["capture_receipt"]["completion_signal"] == "process_exit"
    assert row["capture_receipt"]["completion_status"] == 0
    assert len(row["native_source_rows"]) == len(row["raw_events"]) == 3
    assert all(source["source_binding"] == "file_bytes_at_offset" for source in row["native_source_rows"])

    observation = {
        "schema_version": 1,
        "artifact_kind": "provider_interaction_semantics_observation",
        "provider": "cursor",
        "evidence_class": "live_token",
        "synthetic": False,
        "probes": rows,
        "raw_events": row["raw_events"],
        "native_source_root": str(artifact_root),
    }
    evaluation = evaluate_observation("cursor", observation)

    assert evaluation["status"] == "pass"
    assert evaluation["provider_status"] == "pass"
    assert evaluation["verification_scope"] == "provider_native"


def test_terminal_acknowledgement_uses_post_submit_delta(tmp_path: Path) -> None:
    terminal = tmp_path / "terminal.raw"
    terminal.write_bytes(b"startup model screen\x1b[2J")
    offset = terminal.stat().st_size
    with terminal.open("ab") as handle:
        handle.write(b"/model\r\nmodel picker\r\n")

    delta = provider_interaction_probe._normalized_terminal_delta(terminal, offset)  # noqa: SLF001

    assert delta == "/model model picker"


def test_claude_command_window_discards_startup_rows_but_keeps_later_model_turn() -> None:
    rows = [
        {"type": "system", "content": "startup metadata"},
        {"type": "user", "content": "<local-command-caveat>"},
        {"type": "user", "content": "<command-name>/effort</command-name>"},
        {"type": "user", "content": "<local-command-stdout>Set effort</local-command-stdout>"},
        {"type": "assistant", "content": "unexpected model turn"},
    ]
    sources = [{"source_offset": index} for index in range(len(rows))]

    retained_rows, retained_sources = provider_interaction_probe._claude_command_window(  # noqa: SLF001
        rows,
        sources,
        command="/effort",
    )

    assert retained_rows == rows[1:]
    assert retained_sources == sources[1:]


def test_claude_command_window_requires_the_native_command_marker() -> None:
    rows = [{"type": "system", "content": "startup metadata"}]
    sources = [{"source_offset": 0}]

    retained_rows, retained_sources = provider_interaction_probe._claude_command_window(  # noqa: SLF001
        rows,
        sources,
        command="/effort",
    )

    assert retained_rows == []
    assert retained_sources == []


def test_transcript_file_signature_tracks_a_growing_partial_record(tmp_path: Path) -> None:
    transcript = tmp_path / "projects" / "fixture" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"type":"user","message":{"role":"user"}}\n')

    before = provider_interaction_probe._transcript_file_signature(  # noqa: SLF001
        config_dir=tmp_path,
    )
    with transcript.open("ab") as handle:
        handle.write(b'{"type":"assistant","message":')
    after = provider_interaction_probe._transcript_file_signature(  # noqa: SLF001
        config_dir=tmp_path,
    )

    assert before != after


def test_transcript_quiescence_detects_an_unparsed_partial_record(tmp_path: Path) -> None:
    transcript = tmp_path / "projects" / "fixture" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"type":"user","message":{"role":"user"}}\n{"partial":')

    assert provider_interaction_probe._transcript_has_unparsed_new_bytes(  # noqa: SLF001
        {},
        config_dir=tmp_path,
    ) is True


def test_live_producer_dispatches_one_adapter_for_every_managed_provider(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "provider"
    binary.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(provider_interaction_probe, "_resolve_binary", lambda *_args, **_kwargs: binary)
    monkeypatch.setattr(provider_interaction_probe, "_provider_version", lambda *_args, **_kwargs: "fixture")
    monkeypatch.setattr(provider_interaction_probe, "_no_token_environment", lambda: {})

    def fake_adapter(provider: str):
        def produce(*, artifact_root, **_kwargs):
            contract = provider_interaction_probe.contract_for_provider(provider)
            assert contract is not None
            rows = [
                {
                    "probe_id": probe.probe_id,
                    "disposition": probe.disposition,
                    "status": "blocked",
                    "failure_code": "fixture_blocked",
                    "raw_events": [],
                    "native_source_rows": [],
                }
                for probe in contract.interaction_probes
            ]
            return rows, []

        return produce

    monkeypatch.setattr(provider_interaction_probe, "_codex_model_probe", fake_adapter("codex"))
    monkeypatch.setattr(provider_interaction_probe, "_opencode_interaction_probes", fake_adapter("opencode"))
    monkeypatch.setattr(provider_interaction_probe, "_cursor_model_probe", fake_adapter("cursor"))

    for provider in ("codex", "opencode", "cursor"):
        observation = provider_interaction_probe.produce_live_observation(
            provider,
            provider_bin=None,
            artifact_root=tmp_path / provider,
            qualification_request_digest="digest",
        )
        assert observation["synthetic"] is False
        assert {row["probe_id"] for row in observation["probes"]} == {
            probe.probe_id for probe in provider_interaction_probe.contract_for_provider(provider).interaction_probes
        }
        assert observation["qualification_request_digest"] == "digest"


def test_terminal_acknowledgement_does_not_become_raw_evidence() -> None:
    contract = provider_interaction_probe.contract_for_provider("opencode")
    assert contract is not None
    probe = next(item for item in contract.interaction_probes if item.probe_id == "opencode_help_command")
    row = provider_interaction_probe._probe_status_row(  # noqa: SLF001
        probe,
        status="blocked",
        failure_code="interaction_native_raw_evidence_missing",
        terminal_acknowledged=True,
    )

    assert row["raw_events"] == []
    assert row["terminal_acknowledged"] is True
