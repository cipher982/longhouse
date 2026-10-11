import hashlib
import json
import shlex
import sys
from pathlib import Path
from subprocess import DEVNULL
from subprocess import PIPE
from subprocess import CompletedProcess
from subprocess import Popen

import pytest

from zerg.qa import provider_interaction_probe
from zerg.qa import provider_interaction_semantics
from zerg.qa.provider_interaction_semantics import evaluate_observation


@pytest.mark.parametrize(
    ("provider", "version_output", "expected"),
    (
        ("claude", "2.1.219 (Claude Code)\n", "2.1.219"),
        ("codex", "codex-cli 0.133.0\n", "0.133.0"),
        ("opencode", "1.2.3\n", "1.2.3"),
        ("antigravity", "1.2.3\n", "1.2.3"),
        ("cursor", "2026.07.23-e383d2b\n", "2026.07.23-e383d2b"),
        ("pi", "0.84.1\n", "0.84.1"),
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


def test_pi_version_grammar_accepts_bare_semver_and_rejects_junk() -> None:
    pattern = provider_interaction_probe._PROVIDER_VERSION_PATTERNS["pi"]  # noqa: SLF001
    assert pattern.fullmatch("0.84.1") is not None
    assert pattern.fullmatch("84.1") is None
    assert pattern.fullmatch("0.84") is None
    assert pattern.fullmatch("v0.84.1") is None
    assert pattern.fullmatch("0.84.1-beta") is None
    assert pattern.fullmatch("") is None


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


@pytest.mark.parametrize("name", ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"))
def test_no_token_environment_allows_explicitly_disabled_provider_flags(monkeypatch, name: str) -> None:
    for auth_name in provider_interaction_probe._NO_TOKEN_AUTH_ENV_NAMES:  # noqa: SLF001
        monkeypatch.delenv(auth_name, raising=False)
    monkeypatch.setenv(name, "0")

    environment = provider_interaction_probe._no_token_environment()  # noqa: SLF001

    assert name not in environment


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


def test_opencode_model_argument_rejects_an_unrecognized_bare_vendor() -> None:
    with pytest.raises(ValueError, match="supported bare OpenRouter vendor"):
        provider_interaction_probe._opencode_cli_model("anthropic/claude-sonnet")  # noqa: SLF001

    with pytest.raises(ValueError, match="supported bare OpenRouter vendor"):
        provider_interaction_probe._opencode_cli_model("deepseek/")  # noqa: SLF001

    with pytest.raises(ValueError, match="supported bare OpenRouter vendor"):
        provider_interaction_probe._opencode_cli_model("openrouter/anthropic/claude-sonnet")  # noqa: SLF001


def test_provider_auth_prompt_detection_does_not_match_bare_status_or_help_text() -> None:
    assert provider_interaction_probe._looks_like_provider_auth_prompt("HTTP 401 retry; API key examples") is False  # noqa: SLF001
    assert provider_interaction_probe._looks_like_provider_auth_prompt("API key required to continue") is True  # noqa: SLF001


def test_cursor_stream_json_parser_keeps_native_init_and_result_records() -> None:
    events = provider_interaction_probe._cursor_stream_json_events(  # noqa: SLF001
        "\n".join(
            [
                '{"type":"system","subtype":"init","model":"grok-4.5","apiKeySource":"apiKey","session_id":"s"}',
                '{"type":"result","subtype":"success","session_id":"s","request_id":"r","result":"ok","usage":{"input_tokens":12,"output_tokens":3}}',
                "terminal text that is not a native event",
            ]
        )
    )

    assert [event["type"] for event in events] == ["system", "result"]
    assert events[0]["apiKeySource"] == "apiKey"
    assert events[1]["request_id"] == "r"


def test_cursor_model_probe_selects_the_first_model_bearing_init_event() -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "system", "subtype": "init", "model": "grok-4.5"},
        {"type": "result", "subtype": "success"},
    ]

    assert provider_interaction_probe._cursor_init_event(events) is events[1]  # noqa: SLF001


# Cursor names the model three ways: the --model alias, a display label in the
# print stream's init event, and the base model (no tier) on tool hooks. Only the
# sessionStart hook receipt carries the alias.
_CURSOR_ALIAS = "gpt-5.3-codex-low"
_CURSOR_SESSION = {"conversation_id": "cursor-session-1", "model": _CURSOR_ALIAS}
_CURSOR_SESSION_START = ("sessionStart", _CURSOR_SESSION)


def _cursor_print_stream(*, api_key_source: str = "env") -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "Codex 5.3 Low",
                    "apiKeySource": api_key_source,
                    "session_id": "cursor-session-1",
                }
            ),
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"OK"}]},"session_id":"cursor-session-1"}',
            '{"type":"result","subtype":"success","session_id":"cursor-session-1","request_id":"r","result":"fixture-token",'
            '"usage":{"input_tokens":12,"output_tokens":3}}',
        ]
    )


def _cursor_print_run(stdout: str, hook_calls: list[tuple[str, dict[str, object]]], runtime_homes: list[Path] | None = None):
    """Answer the model probe the way Cursor does.

    Nothing here writes a receipt. Each hook call runs the command the probe
    registered for that event in the workspace's ``.cursor/hooks.json``, with
    Cursor's payload on stdin and the environment the probe gave the CLI, so
    the receipt exists only if the probe installed its hooks.
    """

    def fake_run(*_args, **kwargs):
        env = kwargs["env"]
        if runtime_homes is not None:
            runtime_homes.append(Path(env["HOME"]))
        registered = json.loads((Path(kwargs["cwd"]) / ".cursor" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        for event, payload in hook_calls:
            hook = Popen([sys.executable, *shlex.split(registered[event][0]["command"])], stdin=PIPE, stdout=DEVNULL, env=env, text=True)
            hook.communicate(json.dumps(payload))
            assert hook.returncode == 0
        return CompletedProcess([], 0, stdout, "")

    return fake_run


@pytest.mark.parametrize("requested", (_CURSOR_ALIAS, None))
def test_cursor_model_probe_binds_model_identity_to_the_session_start_hook_receipt(
    monkeypatch, tmp_path: Path, requested: str | None
) -> None:
    binary = tmp_path / "cursor-agent"
    binary.write_text("fixture", encoding="utf-8")
    runtime_homes: list[Path] = []
    hook_calls = [
        _CURSOR_SESSION_START,
        ("beforeShellExecution", {**_CURSOR_SESSION, "model": "gpt-5.3-codex"}),
        ("sessionEnd", _CURSOR_SESSION),
    ]
    monkeypatch.setattr(
        provider_interaction_probe.subprocess,
        "run",
        _cursor_print_run(_cursor_print_stream(), hook_calls, runtime_homes),
    )

    artifact_root = tmp_path / "artifacts"
    rows, _source_rows = provider_interaction_probe._cursor_model_probe(  # noqa: SLF001
        binary=binary,
        artifact_root=artifact_root,
        timeout=1,
        environment={"CURSOR_API_KEY": "fixture-token", **({"CURSOR_MODEL": requested} if requested else {})},
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
        "semantic_boundary": provider_interaction_semantics.semantic_boundary_fixture("cursor"),
    }
    evaluation = evaluate_observation("cursor", observation)

    assert evaluation["status"] == "pass"
    assert evaluation["provider_status"] == "pass"
    assert evaluation["verification_scope"] == "provider_native"
    assert evaluation["assertions"][-1]["probe_id"] == "shared_title_boundary"
    evidence = row["live_model_evidence"]
    result_event = evidence["result_event"]
    assert result_event["usage"]["input_tokens"] == 12
    # The alias is the identity; the stream's label is kept beside it and never compared.
    assert evidence["model"] == result_event["model"] == row["provider_model"] == _CURSOR_ALIAS
    assert result_event["model_display_name"] == row["provider_model_display_name"] == "Codex 5.3 Low"
    assert result_event["model_source"] == "provider_hook"
    stream_source, hook_source = evidence["source_artifacts"]
    assert stream_source["kind"] == "provider_jsonl_stream"
    hook_path = Path(hook_source["path"])
    session_start = json.loads(hook_path.read_text(encoding="utf-8").splitlines()[0])
    assert session_start["event"] == "sessionStart"
    assert session_start["longhouse_session_id"].startswith("direct-")
    assert hook_source == {
        "path": str(hook_path),
        "sha256": hashlib.sha256(hook_path.read_bytes()).hexdigest(),
        "kind": "provider_hook_receipt",
        "event_type": "sessionStart",
        "event_sha256": provider_interaction_semantics.raw_event_digest(session_start),
    }
    assert result_event["model_source_event_sha256"] == hook_source["event_sha256"]
    assert runtime_homes and not runtime_homes[0].exists()
    assert all(b"fixture-token" not in path.read_bytes() for path in artifact_root.rglob("*") if path.is_file())


@pytest.mark.parametrize(
    ("hook_calls", "api_key_source", "failure_code"),
    (
        # The receipt names another model: the base model is not the requested tier.
        ([("sessionStart", {**_CURSOR_SESSION, "model": "gpt-5.3-codex"})], "env", "cursor_model_identity_mismatch"),
        # Cursor fired no hook at all.
        ([], "env", "cursor_model_hook_receipt_missing"),
        # A receipt for another session, or a later hook of this one, is not this session's receipt.
        ([("sessionStart", {**_CURSOR_SESSION, "conversation_id": "cursor-session-2"})], "env", "cursor_model_hook_receipt_missing"),
        ([("afterAgentThought", _CURSOR_SESSION)], "env", "cursor_model_hook_receipt_missing"),
        ([("sessionStart", {**_CURSOR_SESSION, "model": ""})], "env", "cursor_model_hook_receipt_missing"),
        ([_CURSOR_SESSION_START], "login", "cursor_api_key_binding_missing"),
    ),
)
def test_cursor_model_probe_fails_closed_without_a_matching_model_receipt(
    monkeypatch,
    tmp_path: Path,
    hook_calls: list[tuple[str, dict[str, object]]],
    api_key_source: str,
    failure_code: str,
) -> None:
    binary = tmp_path / "cursor-agent"
    binary.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        provider_interaction_probe.subprocess,
        "run",
        _cursor_print_run(_cursor_print_stream(api_key_source=api_key_source), hook_calls),
    )

    artifact_root = tmp_path / "artifacts"
    rows, _source_rows = provider_interaction_probe._cursor_model_probe(  # noqa: SLF001
        binary=binary,
        artifact_root=artifact_root,
        timeout=1,
        environment={"CURSOR_API_KEY": "fixture-token", "CURSOR_MODEL": _CURSOR_ALIAS},
    )
    row = rows[0]

    assert (row["status"], row["failure_code"]) == ("blocked", failure_code)
    assert "live_model_evidence" not in row
    if failure_code == "cursor_model_identity_mismatch":
        assert "'gpt-5.3-codex'" in row["message"] and f"'{_CURSOR_ALIAS}'" in row["message"]
    evaluation = evaluate_observation(
        "cursor",
        {
            "provider": "cursor",
            "evidence_class": "live_token",
            "synthetic": False,
            "probes": rows,
            "raw_events": row["raw_events"],
            "native_source_root": str(artifact_root),
            "semantic_boundary": provider_interaction_semantics.semantic_boundary_fixture("cursor"),
        },
    )
    assert evaluation["status"] == evaluation["provider_status"] == "blocked"
    # The verdict row names the probe's own reason beside the evaluator's generic one.
    verdict = evaluation["assertions"][0]
    assert (verdict["failure_code"], verdict["probe_failure_code"]) == ("interaction_post_state_evidence_missing", failure_code)
    assert verdict["probe_message"] == row["message"]


def test_codex_model_probe_keeps_provider_auth_outside_the_artifact_root(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("fixture", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    runtime_homes: list[Path] = []

    def fake_login(_binary, *, environment, **_kwargs):
        codex_home = Path(environment["CODEX_HOME"])
        runtime_homes.append(codex_home)
        (codex_home / "auth.json").write_text("provider secret", encoding="utf-8")
        return {"method": "codex_login_with_api_key_stdin", "auth_path": "isolated_codex_home/auth.json"}

    def fake_probe(**kwargs):
        assert not Path(kwargs["native_root"]).is_relative_to(artifact_root)
        return {
            "probe_id": "codex_model_picker",
            "status": "observed",
            "raw_events": [],
            "native_source_rows": [],
        }

    monkeypatch.setattr(provider_interaction_probe, "login_with_api_key", fake_login)
    monkeypatch.setattr(provider_interaction_probe, "_run_terminal_interaction_probe", fake_probe)

    rows, source_rows = provider_interaction_probe._codex_model_probe(  # noqa: SLF001
        binary=binary,
        artifact_root=artifact_root,
        timeout=1,
        environment={"CODEX_API_KEY": "fixture-token"},
    )

    assert rows[0]["authentication"]["auth_path"] == "isolated_codex_home/auth.json"
    assert source_rows == []
    assert runtime_homes and not runtime_homes[0].exists()
    assert not list(artifact_root.rglob("auth.json"))


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

    assert (
        provider_interaction_probe._transcript_has_unparsed_new_bytes(  # noqa: SLF001
            {},
            config_dir=tmp_path,
        )
        is True
    )


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
    monkeypatch.setattr(provider_interaction_probe, "_pi_model_probe", fake_adapter("pi"))

    for provider in ("codex", "opencode", "cursor", "pi"):
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
