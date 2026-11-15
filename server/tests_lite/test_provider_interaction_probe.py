import hashlib
import json
import shlex
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from subprocess import DEVNULL
from subprocess import PIPE
from subprocess import CompletedProcess
from subprocess import Popen

import pytest

from zerg.qa import provider_interaction_probe
from zerg.qa import provider_interaction_semantics
from zerg.qa import scripted_model_profiles
from zerg.qa import scripted_model_server
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
        scripted_model_profiles.no_token_environment()


@pytest.mark.parametrize("name", ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"))
def test_no_token_environment_allows_explicitly_disabled_provider_flags(monkeypatch, name: str) -> None:
    for auth_name in scripted_model_profiles.NO_TOKEN_AUTH_ENV_NAMES:
        monkeypatch.delenv(auth_name, raising=False)
    monkeypatch.setenv(name, "0")

    environment = scripted_model_profiles.no_token_environment()

    assert name not in environment


def test_no_token_environment_scrubs_provider_controls_and_configuration(monkeypatch) -> None:
    for name in scripted_model_profiles.NO_TOKEN_AUTH_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    profile_names = (
        scripted_model_profiles.PROVIDER_PROFILE_ENV_NAMES
        | scripted_model_profiles.PROVIDER_ENDPOINT_ENV_NAMES
        | {"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"}
    )
    for name in profile_names:
        monkeypatch.setenv(name, "/tmp/scripted-profile")
    monkeypatch.setenv("LONGHOUSE_PROVIDER_INTERACTION_LIVE", "1")
    monkeypatch.setenv("LONGHOUSE_CLAUDE_INTERACTION_ARTIFACT", "/tmp/fixture.json")
    monkeypatch.setenv("LONGHOUSE_CURSOR_GATE0_ARTIFACT", "/tmp/gate0.json")
    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "openrouter/model")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-model")

    environment = scripted_model_profiles.no_token_environment()

    assert "LONGHOUSE_PROVIDER_INTERACTION_LIVE" not in environment
    assert "LONGHOUSE_CLAUDE_INTERACTION_ARTIFACT" not in environment
    assert "LONGHOUSE_CURSOR_GATE0_ARTIFACT" not in environment
    assert "LONGHOUSE_OPENCODE_QUALIFICATION_MODEL" not in environment
    assert "ANTHROPIC_MODEL" not in environment
    assert all(name not in environment for name in profile_names)
    assert environment["AWS_EC2_METADATA_DISABLED"] == "true"


@pytest.mark.parametrize(
    "name",
    (
        "LONGHOUSE_SCRIPTED_MODEL_BASE_URL",
        "LONGHOUSE_SCRIPTED_MODEL_SCRIPT",
        "LONGHOUSE_SCRIPTED_MODEL_INVOCATION_ID",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "CODEX_API_URL",
        "GOOGLE_GEMINI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
    ),
)
def test_live_token_probe_refuses_scripted_controls_and_endpoint_overrides(monkeypatch, tmp_path: Path, name: str) -> None:
    for control in scripted_model_profiles.PROVIDER_PROFILE_ENV_NAMES | scripted_model_profiles.PROVIDER_ENDPOINT_ENV_NAMES:
        monkeypatch.delenv(control, raising=False)
    for control in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "LONGHOUSE_SCRIPTED_MODEL_BASE_URL",
        "LONGHOUSE_SCRIPTED_MODEL_SCRIPT",
        "LONGHOUSE_SCRIPTED_MODEL_INVOCATION_ID",
    ):
        monkeypatch.delenv(control, raising=False)
    monkeypatch.setenv(name, "endpoint-override")
    monkeypatch.setattr(provider_interaction_probe, "_resolve_binary", lambda *_args, **_kwargs: tmp_path / "codex")
    monkeypatch.setattr(
        provider_interaction_probe,
        "_provider_version",
        lambda *_args, **_kwargs: pytest.fail("provider must not run with an endpoint override"),
    )

    with pytest.raises(RuntimeError, match="live_token refuses inherited scripted-model controls or endpoint overrides"):
        provider_interaction_probe.produce_live_observation(
            "codex",
            provider_bin=None,
            artifact_root=tmp_path / "artifacts",
            evidence_class="live_token",
        )


def test_live_pi_probe_uses_a_fresh_home_instead_of_an_inherited_script_profile(monkeypatch, tmp_path: Path) -> None:
    original_home = tmp_path / "user-home"
    script_profile = original_home / ".pi" / "agent" / "models.json"
    script_profile.parent.mkdir(parents=True)
    script_profile.write_text(
        json.dumps({"providers": {"longhouse-mock": {"baseUrl": "http://127.0.0.1:8123/v1"}}}),
        encoding="utf-8",
    )
    binary = tmp_path / "pi"
    binary.write_text("fake Pi binary", encoding="utf-8")
    monkeypatch.setenv("HOME", str(original_home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "live-token")
    monkeypatch.setenv(provider_interaction_probe.PI_LIVE_ENV, "1")
    monkeypatch.setenv(provider_interaction_probe.PI_MODEL_ENV, "deepseek/model")
    for control in scripted_model_profiles.PROVIDER_PROFILE_ENV_NAMES | scripted_model_profiles.PROVIDER_ENDPOINT_ENV_NAMES:
        monkeypatch.delenv(control, raising=False)
    for control in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV,
        scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV,
        "LONGHOUSE_SCRIPTED_MODEL_INVOCATION_ID",
    ):
        monkeypatch.delenv(control, raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(original_home / ".pi" / "agent"))
    monkeypatch.setenv("LONGHOUSE_PI_PROVIDER", "longhouse-mock")
    monkeypatch.setenv("LONGHOUSE_OMP_CONFIG_DIR", str(original_home / ".omp"))
    monkeypatch.setenv("LONGHOUSE_OMP_DATA_DIR", str(original_home / ".local" / "share" / "omp"))
    monkeypatch.setenv("LONGHOUSE_OMP_SESSION_DIR", str(original_home / ".local" / "share" / "omp" / "sessions"))
    monkeypatch.setenv("OMP_AGENT_DIR", str(original_home / ".omp" / "agent"))
    monkeypatch.setenv("OMP_HOME", str(original_home / ".omp"))
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(name, str(original_home / name))
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT",
        json.dumps({"provider": {"longhouse-mock": {"options": {"baseURL": "http://127.0.0.1:8123/v1", "apiKey": "inherited-secret"}}}}),
    )
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(original_home / ".codex-sqlite"))
    monkeypatch.setenv("PI_CONFIG_FILES", str(original_home / "models.yml"))
    monkeypatch.setenv("PI_CONFIG_DIR", str(original_home / ".omp"))
    monkeypatch.setenv("PI_PROFILE", "scripted")
    monkeypatch.setenv("OMP_PROFILE", "scripted")
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(original_home / "sessions"))
    monkeypatch.setenv("PI_PACKAGE_DIR", str(original_home / "packages"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(original_home / "opencode.json"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(original_home / "opencode"))
    monkeypatch.setattr(provider_interaction_probe, "_resolve_binary", lambda *_args, **_kwargs: binary)
    monkeypatch.setattr(provider_interaction_probe, "_provider_version", lambda *_args, **_kwargs: "fixture")

    def fake_run(argv, *, env, **_kwargs):
        isolated_home = Path(env["HOME"])
        assert isolated_home != original_home
        assert env.get("LONGHOUSE_PI_PROVIDER") is None
        assert not any(name.startswith("XDG_") for name in env)
        assert not any(
            name in env
            for name in (
                "LONGHOUSE_OMP_CONFIG_DIR",
                "LONGHOUSE_OMP_DATA_DIR",
                "LONGHOUSE_OMP_SESSION_DIR",
                "OMP_AGENT_DIR",
                "OMP_HOME",
                "OPENCODE_CONFIG_CONTENT",
                "OPENCODE_CONFIG",
                "OPENCODE_CONFIG_DIR",
                "CODEX_SQLITE_HOME",
                "PI_CONFIG_DIR",
                "PI_CONFIG_FILES",
                "PI_PROFILE",
                "OMP_PROFILE",
                "PI_CODING_AGENT_SESSION_DIR",
                "PI_PACKAGE_DIR",
            )
        )
        assert argv[argv.index("--provider") + 1] == "openrouter"
        assert Path(env["PI_CODING_AGENT_DIR"]) == isolated_home / ".pi" / "agent"
        assert not (isolated_home / ".pi" / "agent" / "models.json").exists()
        return CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(provider_interaction_probe.subprocess, "run", fake_run)

    observation = provider_interaction_probe.produce_live_observation(
        "pi",
        provider_bin=None,
        artifact_root=tmp_path / "artifacts",
        evidence_class="live_token",
    )

    assert observation["evidence_class"] == "live_token"
    assert observation["probes"][0]["failure_code"] == "pi_transcript_missing"


def test_scripted_model_receipt_window_excludes_requests_before_local_probe(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("fake provider binary", encoding="utf-8")
    script = tmp_path / "scenario.json"
    script.write_text('{"turns":[{"id":"plain","match":"MARKER","reply":"OK"}]}', encoding="utf-8")
    endpoint = "http://127.0.0.1:8123"
    monkeypatch.setenv(scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV, endpoint)
    monkeypatch.setenv(scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV, str(script))
    for name in scripted_model_profiles.NO_TOKEN_AUTH_ENV_NAMES:
        monkeypatch.setenv(name, f"ambient-{name}")

    monkeypatch.setattr(provider_interaction_probe, "_resolve_binary", lambda *_args, **_kwargs: binary)
    monkeypatch.setattr(provider_interaction_probe, "_provider_version", lambda *_args, **_kwargs: "fixture")

    def fake_probe(*, environment, **_kwargs):
        credentials = {name: environment[name] for name in scripted_model_profiles.NO_TOKEN_AUTH_ENV_NAMES if name in environment}
        assert credentials == {"CODEX_API_KEY": "longhouse-mock-key"}
        assert environment[scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV] == endpoint
        assert environment[scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV] == str(script)
        contract = provider_interaction_probe.contract_for_provider("codex")
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

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "scripted_model": {
                        "endpoint": endpoint,
                        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                        "served_requests": 1,
                    },
                    "requests": [{"request_id": 1, "matched_turn_id": "previous-run", "status": "served"}],
                }
            ).encode()

    def fake_urlopen(url, *, timeout):
        assert url == f"{endpoint}/_scripted_model/receipt"
        assert timeout == 5
        return FakeResponse()

    monkeypatch.setattr(provider_interaction_probe, "_codex_model_probe", fake_probe)
    monkeypatch.setattr(scripted_model_profiles, "urlopen", fake_urlopen)

    observation = provider_interaction_probe.produce_live_observation(
        "codex",
        provider_bin=None,
        artifact_root=tmp_path / "artifacts",
        evidence_class="scripted_model",
    )

    assert observation["evidence_class"] == "scripted_model"
    assert observation["scripted_model"] == {
        "endpoint": endpoint,
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "served_requests": 0,
        "matched_turns": [],
        "request_window": {"after_request_id": 1, "through_request_id": 1},
    }
    assert "token_usage" not in observation["scripted_model"]
    assert "live_model_evidence" not in observation


def test_scripted_model_receipt_binds_new_requests_after_the_snapshot(monkeypatch, tmp_path: Path) -> None:
    endpoint = "http://127.0.0.1:8123"
    script = tmp_path / "scenario.json"
    script.write_text('{"turns":[{"id":"current","match":"CURRENT","reply":"OK"}]}', encoding="utf-8")
    environment = {
        scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV: endpoint,
        scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV: str(script),
    }
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    earlier = [{"request_id": 1, "matched_turn_id": "previous-run", "status": "served"}]
    current = [*earlier, {"request_id": 2, "matched_turn_id": "current", "status": "served"}]
    responses = [
        {"scripted_model": {"endpoint": endpoint, "script_sha256": digest, "served_requests": len(earlier)}, "requests": earlier},
        {"scripted_model": {"endpoint": endpoint, "script_sha256": digest, "served_requests": len(current)}, "requests": current},
    ]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(responses.pop(0)).encode()

    def fake_urlopen(url, *, timeout):
        assert url == f"{endpoint}/_scripted_model/receipt"
        assert timeout == 5
        return FakeResponse()

    monkeypatch.setattr(scripted_model_profiles, "urlopen", fake_urlopen)

    request_start = scripted_model_profiles.scripted_model_receipt_snapshot(environment)[2]
    receipt = scripted_model_profiles.scripted_model_receipt(
        environment,
        requests_before=request_start,
    )

    assert receipt["served_requests"] == 1
    assert receipt["matched_turns"] == ["current"]
    assert receipt["request_window"] == {"after_request_id": 1, "through_request_id": 2}


def test_pi_scripted_probe_rejects_a_prior_request_without_a_current_prompt_request(monkeypatch, tmp_path: Path) -> None:
    endpoint = "http://127.0.0.1:8123"
    script = tmp_path / "scenario.json"
    script.write_text('{"turns":[{"id":"pi-turn","match":"MARKER","reply":"OK"}]}', encoding="utf-8")
    monkeypatch.setenv(scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV, endpoint)
    monkeypatch.setenv(scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV, str(script))
    binary = tmp_path / "fake-pi"
    binary.write_text("fake Pi binary", encoding="utf-8")
    monkeypatch.setattr(provider_interaction_probe, "_resolve_binary", lambda *_args, **_kwargs: binary)
    monkeypatch.setattr(provider_interaction_probe, "_provider_version", lambda *_args, **_kwargs: "fixture")
    monkeypatch.setattr(
        provider_interaction_probe,
        "_pi_model_probe",
        lambda **_kwargs: (
            [{"probe_id": "pi_print_model", "disposition": "implemented", "status": "blocked", "raw_events": []}],
            [],
        ),
    )
    receipt_payload = {
        "scripted_model": {
            "endpoint": endpoint,
            "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "served_requests": 1,
        },
        "requests": [{"request_id": 1, "matched_turn_id": "previous-run", "status": "served"}],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(receipt_payload).encode()

    monkeypatch.setattr(
        scripted_model_profiles,
        "urlopen",
        lambda url, *, timeout: FakeResponse(),
    )

    with pytest.raises(RuntimeError, match="did not serve a request in this invocation"):
        provider_interaction_probe.produce_live_observation(
            "pi",
            provider_bin=None,
            artifact_root=tmp_path / "artifacts",
            evidence_class="scripted_model",
        )


@pytest.mark.parametrize("provider", ("codex", "claude", "opencode", "pi", "omp"))
def test_scripted_profiles_route_fake_provider_binaries_to_loopback_endpoint(monkeypatch, tmp_path: Path, provider: str) -> None:
    class FakeModelHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.requests.append({"path": self.path, "body": json.loads(body)})
            response = json.dumps({"choices": [{"message": {"role": "assistant", "content": "OK"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModelHandler)
    server.requests = []
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    inherited_home = tmp_path / "inherited-home"
    monkeypatch.setenv("HOME", str(inherited_home))
    script = tmp_path / "scenario.json"
    script.write_text('{"turns":[{"id":"plain","match":"FAKE_MODEL_INVOCATION","reply":"OK"}]}', encoding="utf-8")
    monkeypatch.setenv(scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV, endpoint)
    monkeypatch.setenv(scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV, str(script))
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT",
        json.dumps(
            {"provider": {"longhouse-mock": {"options": {"baseURL": "http://127.0.0.1:9999/v1", "apiKey": "inherited-inline-key"}}}}
        ),
    )
    monkeypatch.setenv("PI_CONFIG_FILES", str(tmp_path / "inherited-provider-models.yml"))
    for name in (
        "LONGHOUSE_OMP_CONFIG_DIR",
        "LONGHOUSE_OMP_DATA_DIR",
        "LONGHOUSE_OMP_SESSION_DIR",
        "LONGHOUSE_OMP_QUALIFICATION_MODEL",
    ):
        monkeypatch.setenv(name, f"inherited-{name}")
    if provider == "omp":
        environment = scripted_model_profiles.credential_free_environment(reject_ambient_credentials=False)
        environment[scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV] = endpoint
        environment[scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV] = str(script)
    else:
        environment = scripted_model_profiles.scripted_model_environment(provider)
    environment["FAKE_PROVIDER"] = provider
    environment["FAKE_INHERITED_HOME"] = str(inherited_home)
    profile_root = tmp_path / f"{provider}-profile"
    if provider != "pi":
        scripted_model_profiles.configure_scripted_model_profile(
            provider,
            profile_root,
            environment,
        )

    binary = tmp_path / f"fake-{provider}"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import json
            import os
            import re
            import sys
            import tomllib
            from pathlib import Path
            from urllib.request import Request, urlopen

            provider = os.environ["FAKE_PROVIDER"]
            assert "OPENCODE_CONFIG_CONTENT" not in os.environ
            assert "PI_CONFIG_FILES" not in os.environ
            for name in ("LONGHOUSE_OMP_CONFIG_DIR", "LONGHOUSE_OMP_DATA_DIR", "LONGHOUSE_OMP_SESSION_DIR"):
                assert name not in os.environ
            assert Path(os.environ["HOME"]) != Path(os.environ["FAKE_INHERITED_HOME"])
            if provider == "codex":
                config = tomllib.loads((Path(os.environ["CODEX_HOME"]) / "config.toml").read_text())
                assert config["model_provider"] == "longhouse-mock"
                assert config["model"] == "mock-model"
                assert Path(os.environ["CODEX_HOME"]) == Path(os.environ["HOME"]) / ".codex"
                assert config["model_providers"]["longhouse-mock"]["supports_websockets"] is False
                base_url = config["model_providers"]["longhouse-mock"]["base_url"]
                assert os.environ["CODEX_API_KEY"] == "longhouse-mock-key"
            elif provider == "claude":
                assert Path(os.environ["CLAUDE_CONFIG_DIR"]).is_dir()
                assert os.environ["ANTHROPIC_MODEL"] == "mock-model"
                assert os.environ["ANTHROPIC_API_KEY"] == "longhouse-mock-key"
                base_url = os.environ["ANTHROPIC_BASE_URL"]
            elif provider == "opencode":
                config = json.loads((Path(os.environ["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json").read_text())
                entry = config["provider"]["longhouse-mock"]
                assert entry["models"]["mock-model"]
                assert entry["options"]["apiKey"] == "longhouse-mock-key"
                assert os.environ["LONGHOUSE_OPENCODE_QUALIFICATION_MODEL"] == "longhouse-mock/mock-model"
                base_url = entry["options"]["baseURL"]
            elif provider == "pi":
                pi_config_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
                assert pi_config_dir == Path(os.environ["HOME"]) / ".pi" / "agent"
                config = json.loads((pi_config_dir / "models.json").read_text())
                entry = config["providers"]["longhouse-mock"]
                assert entry["api"] == "openai-completions"
                assert entry["models"][0]["id"] == "mock-model"
                assert entry["apiKey"] == "longhouse-mock-key"
                assert os.environ["LONGHOUSE_PI_LIVE"] == "1"
                assert os.environ["LONGHOUSE_PI_QUALIFICATION_MODEL"] == "mock-model"
                assert sys.argv[sys.argv.index("--provider") + 1] == "longhouse-mock"
                assert sys.argv[sys.argv.index("--model") + 1] == "mock-model"
                base_url = entry["baseUrl"]
            elif provider == "omp":
                models_path = Path(os.environ["HOME"]) / ".omp" / "agent" / "models.yml"
                config = models_path.read_text()
                assert "api: openai-completions" in config
                assert 'apiKey: "longhouse-mock-key"' in config
                assert "id: mock-model" in config
                assert os.environ["LONGHOUSE_OMP_QUALIFICATION_MODEL"] == "longhouse-mock/mock-model"
                match = re.search(r'(?m)^\\s*baseUrl:\\s*"?([^"\\n]+)"?\\s*$', config)
                assert match is not None
                base_url = match.group(1)

            base_url = base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            request = Request(
                base_url + "/chat/completions",
                data=json.dumps({"model": "mock-model", "messages": [{"role": "user", "content": "FAKE_MODEL_INVOCATION"}]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                assert response.status == 200
            """
        ),
        encoding="utf-8",
    )
    binary.chmod(0o755)

    try:
        if provider == "pi":
            rows, _ = provider_interaction_probe._pi_model_probe(  # noqa: SLF001
                binary=binary,
                artifact_root=tmp_path / "artifacts",
                timeout=5,
                environment=environment,
            )
            assert rows[0]["failure_code"] == "pi_transcript_missing"
        else:
            subprocess.run([str(binary)], env=environment, check=True, capture_output=True, text=True, timeout=10)
        assert len(server.requests) == 1
        assert server.requests[0]["path"] == "/v1/chat/completions"
        assert server.requests[0]["body"]["messages"][0]["content"] == "FAKE_MODEL_INVOCATION"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_scripted_pi_persisted_observation_scrubs_nested_live_accounting(monkeypatch, tmp_path: Path) -> None:
    script = tmp_path / "pi-scenario.json"
    script.write_text(
        json.dumps({"turns": [{"id": "pi-print", "match": "LONGHOUSE_PI_PROBE_", "reply": "{{input_marker}}"}]}),
        encoding="utf-8",
    )
    turns, script_sha256 = scripted_model_server.load_script(script)
    server = scripted_model_server.ThreadedServer(
        ("127.0.0.1", 0),
        scripted_model_server.MockHandler,
        script_turns=turns,
        script_sha256=script_sha256,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setenv(scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV, server.endpoint)
    monkeypatch.setenv(scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV, str(script))
    monkeypatch.setattr(provider_interaction_probe, "_provider_version", lambda *_args, **_kwargs: "fixture")

    binary = tmp_path / "fake-pi"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import json
            import os
            import sys
            from pathlib import Path
            from urllib.request import Request, urlopen

            pi_config_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
            config = json.loads((pi_config_dir / "models.json").read_text())
            provider = config["providers"]["longhouse-mock"]
            prompt = sys.argv[sys.argv.index("-p") + 1]
            model = sys.argv[sys.argv.index("--model") + 1]
            request = Request(
                provider["baseUrl"].rstrip("/") + "/chat/completions",
                data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                answer = json.loads(response.read())["choices"][0]["message"]["content"]

            session_dir = Path(sys.argv[sys.argv.index("--session-dir") + 1])
            session_id = sys.argv[sys.argv.index("--session-id") + 1]
            entries = [
                {"type": "session", "id": session_id, "version": 3, "cwd": str(Path.cwd())},
                {"type": "model_change", "id": "model-change", "modelId": model, "provider": "longhouse-mock"},
                {
                    "type": "message",
                    "id": "assistant-reply",
                    "parentId": "model-change",
                    "timestamp": "2026-10-12T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": answer}],
                        "provider": "longhouse-mock",
                        "model": model,
                        "usage": {"input": 12, "output": 3, "total": 15, "cost": {"total": 0.001}},
                    },
                },
            ]
            session_dir.mkdir(parents=True, exist_ok=True)
            session = session_dir / f"{session_id}.jsonl"
            session.write_text("".join(json.dumps(entry) + "\\n" for entry in entries), encoding="utf-8")
            print(answer)
            """
        ),
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setattr(provider_interaction_probe, "_resolve_binary", lambda *_args, **_kwargs: binary)

    try:
        observation = provider_interaction_probe.produce_live_observation(
            "pi",
            provider_bin=None,
            artifact_root=tmp_path / "artifacts",
            evidence_class="scripted_model",
        )
        assert observation["probes"][0]["status"] == "observed"
        assert observation["scripted_model"]["matched_turns"] == ["pi-print"]
        persisted_path = tmp_path / "provider-interaction-observation.json"
        persisted_path.write_text(json.dumps(observation), encoding="utf-8")
        persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
        assert persisted["evidence_class"] == "scripted_model"
        raw_assistant = next(event for event in persisted["raw_events"] if event.get("type") == "message")
        assert raw_assistant["message"]["usage"]["cost"]["total"] == 0.001

        hash_bound_fields = {"raw_events", "native_source_rows", "capture_receipt"}

        def assert_scripted_evidence_is_safe(value: object, *, hash_bound: bool = False) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized_key = str(key).casefold()
                    assert str(key) != "live_model_evidence"
                    if not hash_bound:
                        assert not any(part in normalized_key for part in ("usage", "cost", "token"))
                    assert not (isinstance(item, str) and item == "live_token")
                    assert_scripted_evidence_is_safe(item, hash_bound=hash_bound or key in hash_bound_fields)
            elif isinstance(value, list):
                for item in value:
                    assert not (isinstance(item, str) and item == "live_token")
                    assert_scripted_evidence_is_safe(item, hash_bound=hash_bound)
            elif isinstance(value, str):
                assert value != "live_token"

        assert_scripted_evidence_is_safe(persisted)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_omp_scripted_model_is_rejected_without_version_and_probe_support() -> None:
    with pytest.raises(ValueError, match="scripted_model interaction probes are unsupported for 'omp'"):
        scripted_model_profiles.scripted_model_environment("omp")


@pytest.mark.parametrize(
    ("configured", "expected"),
    (
        ("deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-flash"),
        ("openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-flash"),
        ("longhouse-mock/mock-model", "longhouse-mock/mock-model"),
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
    monkeypatch.setattr(scripted_model_profiles, "no_token_environment", lambda: {})

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
