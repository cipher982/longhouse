"""Credential-free scripted-model profiles and receipts for QA runs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import MutableMapping
from urllib.parse import urlsplit
from urllib.request import urlopen

SCRIPTED_MODEL_BASE_URL_ENV = "LONGHOUSE_SCRIPTED_MODEL_BASE_URL"
_SCRIPTED_MODEL_ENV_PREFIX = "LONGHOUSE_SCRIPTED_MODEL_"
PROVIDER_ENDPOINT_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "CODEX_API_URL",
        "GOOGLE_GEMINI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
    }
)
PROVIDER_PROFILE_ENV_NAMES = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "CODEX_SQLITE_HOME",
        "CURSOR_CONFIG_DIR",
        "CURSOR_HOME",
        "LONGHOUSE_OMP_CONFIG_DIR",
        "LONGHOUSE_OMP_DATA_DIR",
        "LONGHOUSE_OMP_SESSION_DIR",
        "LONGHOUSE_PI_PROVIDER",
        "OMP_AGENT_DIR",
        "OMP_HOME",
        "OMP_PROFILE",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG_DIR",
        "PI_CODING_AGENT_DIR",
        "PI_CONFIG_DIR",
        "PI_CONFIG_FILES",
        "PI_CODING_AGENT_SESSION_DIR",
        "PI_PACKAGE_DIR",
        "PI_PROFILE",
    }
)
SCRIPTED_MODEL_SCRIPT_ENV = "LONGHOUSE_SCRIPTED_MODEL_SCRIPT"
_SCRIPTED_MODEL_DUMMY_KEY = "longhouse-mock-key"
SCRIPTED_MODEL_PROVIDER = "longhouse-mock"
SCRIPTED_MODEL_NAME = "mock-model"
# OMP has no interaction-probe version grammar or execution dispatch here; keep it out of scripted evidence until both exist.
SCRIPTED_MODEL_API_KEY_BY_PROVIDER = {
    "claude": "ANTHROPIC_API_KEY",
    "codex": "CODEX_API_KEY",
    "opencode": "OPENROUTER_API_KEY",
    "pi": "OPENROUTER_API_KEY",
}
NO_TOKEN_AUTH_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_VERTEX",
        "CODEX_AGENTS_TOKEN",
        "CODEX_API_KEY",
        "CURSOR_ACCESS_TOKEN",
        "CURSOR_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "TAVILY_API_KEY",
        "XAI_API_KEY",
        "ZAI_API_KEY",
    }
)
_NO_TOKEN_AUTH_FLAG_ENV_NAMES = frozenset({"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"})
_NO_TOKEN_FALSE_FLAG_VALUES = frozenset({"0", "false", "no", "off"})
_NO_TOKEN_SCRUB_ENV_NAMES = (
    NO_TOKEN_AUTH_ENV_NAMES
    | PROVIDER_ENDPOINT_ENV_NAMES
    | PROVIDER_PROFILE_ENV_NAMES
    | frozenset(
        {
            "ANTHROPIC_MODEL",
            "AWS_DEFAULT_REGION",
            "AWS_EC2_METADATA_DISABLED",
            "AWS_REGION",
            "CODEX_MANAGED_PACKAGE_ROOT",
            "CODEX_MODEL",
            "CURSOR_MODEL",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_REGION",
            "VERTEXAI_LOCATION",
            "VERTEXAI_PROJECT",
            "LONGHOUSE_ANTIGRAVITY_QUALIFICATION_HOME",
            "ANTIGRAVITY_QUALIFICATION_HOME",
            "LONGHOUSE_CLAUDE_QUALIFICATION_LIVE",
            "LONGHOUSE_CLAUDE_INTERACTION_ARTIFACT",
            "LONGHOUSE_CURSOR_GATE0_ARTIFACT",
            "LONGHOUSE_CODEX_BIN",
            "LONGHOUSE_ENGINE_BIN",
            "LONGHOUSE_OPENCODE_BIN",
            "LONGHOUSE_OPENCODE_QUALIFICATION_MODEL",
            "LONGHOUSE_OMP_QUALIFICATION_MODEL",
            "LONGHOUSE_PI_BIN",
            "LONGHOUSE_PI_LIVE",
            "LONGHOUSE_PI_QUALIFICATION_MODEL",
            "LONGHOUSE_PROVIDER_INTERACTION_ARTIFACT",
            "LONGHOUSE_PROVIDER_INTERACTION_LIVE",
            SCRIPTED_MODEL_BASE_URL_ENV,
            SCRIPTED_MODEL_SCRIPT_ENV,
        }
    )
)
_PI_LIVE_ENV = "LONGHOUSE_PI_LIVE"
_PI_MODEL_ENV = "LONGHOUSE_PI_QUALIFICATION_MODEL"


def _is_provider_profile_environment_name(name: str) -> bool:
    return name in PROVIDER_PROFILE_ENV_NAMES or name.startswith("XDG_")


def credential_free_environment(*, reject_ambient_credentials: bool) -> dict[str, str]:
    credentials = []
    for name in sorted(NO_TOKEN_AUTH_ENV_NAMES):
        value = str(os.environ.get(name) or "").strip()
        if value and (name not in _NO_TOKEN_AUTH_FLAG_ENV_NAMES or value.casefold() not in _NO_TOKEN_FALSE_FLAG_VALUES):
            credentials.append(name)
    if reject_ambient_credentials and credentials:
        raise RuntimeError("live_no_token requires a credential-free environment; found: " + ", ".join(credentials))
    environment = os.environ.copy()
    for name in tuple(environment):
        if name in _NO_TOKEN_SCRUB_ENV_NAMES or _is_provider_profile_environment_name(name) or name.startswith(_SCRIPTED_MODEL_ENV_PREFIX):
            environment.pop(name, None)
    # Prevent a provider SDK from discovering an instance/container role after
    # the explicit environment has been scrubbed.
    environment["AWS_EC2_METADATA_DISABLED"] = "true"
    return environment


def no_token_environment() -> dict[str, str]:
    return credential_free_environment(reject_ambient_credentials=True)


def live_token_environment() -> dict[str, str]:
    scripted_controls = sorted(
        name
        for name, value in os.environ.items()
        if name.startswith(_SCRIPTED_MODEL_ENV_PREFIX) or (name in PROVIDER_ENDPOINT_ENV_NAMES and str(value).strip())
    )
    if scripted_controls:
        raise RuntimeError("live_token refuses inherited scripted-model controls or endpoint overrides: " + ", ".join(scripted_controls))
    environment = os.environ.copy()
    for name in tuple(environment):
        if (
            name.startswith(_SCRIPTED_MODEL_ENV_PREFIX)
            or name in PROVIDER_ENDPOINT_ENV_NAMES
            or _is_provider_profile_environment_name(name)
        ):
            environment.pop(name, None)
    return environment


def scripted_model_environment(provider: str) -> dict[str, str]:
    try:
        api_key_name = SCRIPTED_MODEL_API_KEY_BY_PROVIDER[provider]
    except KeyError:
        raise ValueError(f"scripted_model interaction probes are unsupported for {provider!r}") from None
    endpoint = str(os.environ.get(SCRIPTED_MODEL_BASE_URL_ENV) or "").strip().rstrip("/")
    script = str(os.environ.get(SCRIPTED_MODEL_SCRIPT_ENV) or "").strip()
    if not endpoint or not script or not Path(script).is_file():
        raise RuntimeError(f"scripted_model requires a local fixture at {SCRIPTED_MODEL_SCRIPT_ENV} and {SCRIPTED_MODEL_BASE_URL_ENV}")
    try:
        parsed_endpoint = urlsplit(endpoint)
        endpoint_address = ipaddress.ip_address(parsed_endpoint.hostname or "")
        endpoint_port = parsed_endpoint.port
    except ValueError as exc:
        raise RuntimeError(f"{SCRIPTED_MODEL_BASE_URL_ENV} must be a loopback HTTP endpoint") from exc
    if (
        parsed_endpoint.scheme != "http"
        or not endpoint_address.is_loopback
        or endpoint_port is None
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.path
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise RuntimeError(f"{SCRIPTED_MODEL_BASE_URL_ENV} must be a loopback HTTP endpoint")
    environment = credential_free_environment(reject_ambient_credentials=False)
    environment[api_key_name] = _SCRIPTED_MODEL_DUMMY_KEY
    environment[SCRIPTED_MODEL_BASE_URL_ENV] = endpoint
    environment[SCRIPTED_MODEL_SCRIPT_ENV] = script
    if provider == "claude":
        environment["ANTHROPIC_BASE_URL"] = endpoint
        environment["ANTHROPIC_MODEL"] = SCRIPTED_MODEL_NAME
    elif provider == "codex":
        environment["CODEX_MODEL"] = SCRIPTED_MODEL_NAME
    elif provider == "opencode":
        environment["LONGHOUSE_OPENCODE_QUALIFICATION_MODEL"] = f"{SCRIPTED_MODEL_PROVIDER}/{SCRIPTED_MODEL_NAME}"
    elif provider == "pi":
        environment[_PI_LIVE_ENV] = "1"
        environment[_PI_MODEL_ENV] = SCRIPTED_MODEL_NAME
        environment["LONGHOUSE_PI_PROVIDER"] = SCRIPTED_MODEL_PROVIDER
    return environment


def configure_scripted_model_profile(
    provider: str,
    profile_root: Path,
    environment: MutableMapping[str, str],
) -> None:
    """Write the shared workflow's mock endpoint configuration into an isolated profile."""

    endpoint = str(environment.get(SCRIPTED_MODEL_BASE_URL_ENV) or "").strip().rstrip("/")
    if not endpoint:
        raise RuntimeError(f"scripted_model profile requires {SCRIPTED_MODEL_BASE_URL_ENV}")
    profile_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    openai_base_url = f"{endpoint}/v1"
    custom_model = {
        "id": SCRIPTED_MODEL_NAME,
        "name": "Longhouse mock",
        "contextWindow": 200000,
        "maxTokens": 8192,
    }

    def write_json(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)

    if provider == "codex":
        environment["HOME"] = str(profile_root)
        codex_home = profile_root / ".codex"
        codex_home.mkdir(mode=0o700, exist_ok=True)
        environment["CODEX_HOME"] = str(codex_home)
        environment["CODEX_MODEL"] = SCRIPTED_MODEL_NAME
        config = codex_home / "config.toml"
        config.write_text(
            f'model_provider = "openai"\nmodel = {json.dumps(SCRIPTED_MODEL_NAME)}\nopenai_base_url = {json.dumps(openai_base_url)}\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
    elif provider == "claude":
        environment["HOME"] = str(profile_root)
        config_dir = profile_root / ".claude"
        config_dir.mkdir(mode=0o700, exist_ok=True)
        environment["CLAUDE_CONFIG_DIR"] = str(config_dir)
        environment["ANTHROPIC_BASE_URL"] = endpoint
        environment["ANTHROPIC_MODEL"] = SCRIPTED_MODEL_NAME
    elif provider == "opencode":
        environment["HOME"] = str(profile_root)
        config_root = profile_root / ".config"
        environment["XDG_CONFIG_HOME"] = str(config_root)
        environment["LONGHOUSE_OPENCODE_QUALIFICATION_MODEL"] = f"{SCRIPTED_MODEL_PROVIDER}/{SCRIPTED_MODEL_NAME}"
        write_json(
            config_root / "opencode" / "opencode.json",
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    SCRIPTED_MODEL_PROVIDER: {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Longhouse mock",
                        "options": {"baseURL": openai_base_url, "apiKey": _SCRIPTED_MODEL_DUMMY_KEY},
                        "models": {SCRIPTED_MODEL_NAME: {"name": "Longhouse mock"}},
                    }
                },
            },
        )
    elif provider == "pi":
        environment["HOME"] = str(profile_root)
        environment["PI_CODING_AGENT_DIR"] = str(profile_root / ".pi" / "agent")
        environment[_PI_LIVE_ENV] = "1"
        environment[_PI_MODEL_ENV] = SCRIPTED_MODEL_NAME
        environment["LONGHOUSE_PI_PROVIDER"] = SCRIPTED_MODEL_PROVIDER
        write_json(
            profile_root / ".pi" / "agent" / "models.json",
            {
                "providers": {
                    SCRIPTED_MODEL_PROVIDER: {
                        "baseUrl": openai_base_url,
                        "api": "openai-completions",
                        "apiKey": _SCRIPTED_MODEL_DUMMY_KEY,
                        "models": [custom_model],
                    }
                }
            },
        )
    elif provider == "omp":
        environment["HOME"] = str(profile_root)
        environment["LONGHOUSE_OMP_QUALIFICATION_MODEL"] = f"{SCRIPTED_MODEL_PROVIDER}/{SCRIPTED_MODEL_NAME}"
        models = profile_root / ".omp" / "agent" / "models.yml"
        models.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        models.write_text(
            "providers:\n"
            f"  {SCRIPTED_MODEL_PROVIDER}:\n"
            f'    baseUrl: "{openai_base_url}"\n'
            "    api: openai-completions\n"
            f'    apiKey: "{_SCRIPTED_MODEL_DUMMY_KEY}"\n'
            "    models:\n"
            f"      - id: {SCRIPTED_MODEL_NAME}\n"
            "        name: Longhouse mock\n"
            "        contextWindow: 200000\n"
            "        maxTokens: 8192\n",
            encoding="utf-8",
        )
        models.chmod(0o600)
    else:
        raise ValueError(f"scripted_model profiles are unsupported for {provider!r}")


def scripted_model_receipt_snapshot(
    environment: Mapping[str, str],
) -> tuple[str, str, int, list[Mapping[str, Any]]]:
    endpoint = str(environment.get(SCRIPTED_MODEL_BASE_URL_ENV) or "").strip().rstrip("/")
    script = str(environment.get(SCRIPTED_MODEL_SCRIPT_ENV) or "").strip()
    if not endpoint or not script:
        raise RuntimeError(f"scripted_model receipt requires {SCRIPTED_MODEL_BASE_URL_ENV} and {SCRIPTED_MODEL_SCRIPT_ENV}")
    with urlopen(f"{endpoint}/_scripted_model/receipt", timeout=5) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, Mapping) or not isinstance(payload.get("scripted_model"), Mapping):
        raise RuntimeError("scripted_model server returned an invalid receipt")
    receipt = payload["scripted_model"]
    if set(receipt) != {"endpoint", "script_sha256", "served_requests"}:
        raise RuntimeError("scripted_model receipt fields do not match the shared contract")
    try:
        script_sha256 = hashlib.sha256(Path(script).read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError("scripted_model script fixture is unreadable") from exc
    served_requests = receipt["served_requests"]
    requests = payload.get("requests")
    if (
        receipt["endpoint"] != endpoint
        or receipt["script_sha256"] != script_sha256
        or type(served_requests) is not int
        or served_requests < 0
        or not isinstance(requests, list)
    ):
        raise RuntimeError("scripted_model receipt does not match the configured endpoint or script")
    request_rows = []
    for request_id, request in enumerate(requests, start=1):
        matched_turn_id = request.get("matched_turn_id") if isinstance(request, Mapping) else None
        if (
            not isinstance(request, Mapping)
            or type(request.get("request_id")) is not int
            or request["request_id"] != request_id
            or request.get("status") not in {"pending", "served", "failed"}
            or (matched_turn_id is not None and (not isinstance(matched_turn_id, str) or not matched_turn_id.strip()))
        ):
            raise RuntimeError("scripted_model server returned an invalid request receipt")
        request_rows.append(request)
    if sum(request["status"] == "served" for request in request_rows) != served_requests:
        raise RuntimeError("scripted_model served request count does not match its request statuses")
    return endpoint, script_sha256, len(request_rows), request_rows


def scripted_model_receipt(
    environment: Mapping[str, str],
    *,
    requests_before: int,
    require_served_request: bool = False,
) -> dict[str, Any]:
    endpoint, script_sha256, total_requests, requests = scripted_model_receipt_snapshot(environment)
    if type(requests_before) is not int or requests_before < 0 or total_requests < requests_before:
        raise RuntimeError("scripted_model request window is invalid for this invocation")
    invocation_requests = requests[requests_before:]
    if require_served_request and not invocation_requests:
        raise RuntimeError("scripted_model prompt probe did not serve a request in this invocation")
    matched_turns = []
    for request in invocation_requests:
        matched_turn_id = request.get("matched_turn_id")
        if request["status"] != "served":
            raise RuntimeError("scripted_model invocation request did not complete with a successful response")
        if not isinstance(matched_turn_id, str) or not matched_turn_id.strip():
            raise RuntimeError("scripted_model receipt did not match every invocation request to a fixture turn")
        matched_turns.append(matched_turn_id)
    return {
        "endpoint": endpoint,
        "script_sha256": script_sha256,
        "served_requests": len(invocation_requests),
        "matched_turns": matched_turns,
        "request_window": {
            "after_request_id": requests_before,
            "through_request_id": total_requests,
        },
    }
