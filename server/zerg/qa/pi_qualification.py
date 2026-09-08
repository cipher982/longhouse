"""Exact-build Pi qualification with opt-in native model and tool turns.

The published npm installation is bound by executable identity. Live runs
exercise stock tool-enabled print, exact native-file continuation, and native
JSONL accounting. Without explicit credentials, model pin, and live opt-in,
the profile stays blocked rather than spending tokens.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from zerg.qa import provider_release_identity as identity

PROFILE = "pi_print_v1"
SCENARIO_ID = "pi_print"
# pi --version prints a bare semver such as 0.84.1 (no prefix, no suffix).
PI_VERSION_GRAMMAR = re.compile(r"^(?P<version>\d+\.\d+\.\d+)$")
# The credential env the v2 bridge requires for a live pi turn, mirroring
# cursor_observed_install_v1's credential tuple shape.
CREDENTIAL_REQUIREMENT = ("OPENROUTER_API_KEY", "LONGHOUSE_PI_LIVE", "LONGHOUSE_PI_QUALIFICATION_MODEL")
_PROFILE = identity.IdentityProfile(
    provider="pi",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=PI_VERSION_GRAMMAR,
    oracle_source=Path(__file__),
)


def _live_enabled() -> bool:
    """True only when this run should spend a real pi model turn."""
    return (
        bool((os.environ.get("OPENROUTER_API_KEY") or "").strip())
        and bool((os.environ.get("LONGHOUSE_PI_QUALIFICATION_MODEL") or "").strip())
        and os.environ.get("LONGHOUSE_PI_LIVE")
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    # Harness imports are deferred into run(): provider_qualification imports
    # this module eagerly, and the router must stay importable under
    # `python -S` (no sqlalchemy/site-packages) per
    # test_router_imports_without_optional_server_dependencies.
    from zerg.qa.pi_native import pi_native_model_evidence  # noqa: PLC0415
    from zerg.qa.provider_adapters.pi import PI_LIVE_ENV  # noqa: PLC0415
    from zerg.qa.provider_adapters.pi import PiHarnessAdapter  # noqa: PLC0415
    from zerg.qa.universal_agent_harness import STATUS_BLOCKED  # noqa: PLC0415
    from zerg.qa.universal_agent_harness import STATUS_FAIL  # noqa: PLC0415
    from zerg.qa.universal_agent_harness import STATUS_PASS  # noqa: PLC0415
    from zerg.qa.universal_agent_harness import AdapterConfig  # noqa: PLC0415
    from zerg.qa.universal_agent_harness import EvidencePackage  # noqa: PLC0415

    request = identity.load_request(
        request_path,
        provider="pi",
        profile=PROFILE,
        version_grammar=PI_VERSION_GRAMMAR,
    )
    output_root = output_root.expanduser().resolve()
    binary, actual_identity, runner_sha = identity.preflight(
        request,
        output_root,
        repo_root=Path(__file__).resolve().parents[3],
        git_sha_fn=identity.git_sha,
        git_dirty_fn=identity.git_dirty,
    )
    config = AdapterConfig(
        provider="pi",
        binary_name="pi",
        binary_env="LONGHOUSE_PI_BIN",
        safe_run_prompt_once=True,
        safe_managed_session_scenarios=("launch_managed_session", "send_receive"),
    )
    adapter = PiHarnessAdapter(config, provider_bin=binary)
    package = EvidencePackage(root=output_root, provider="pi", scenario=SCENARIO_ID)
    adapter.prepare(package)
    launch = adapter.launch_managed_session(package)
    send = adapter.send_receive(package, "Reply with the single word OK.")
    tool_call = adapter.tool_call_result(package)
    live_enabled = _live_enabled()
    tool_taxonomy = tool_call.get("native_shadow_taxonomy") if isinstance(tool_call, dict) else None
    native_taxonomy_complete = bool(
        isinstance(tool_taxonomy, dict)
        and tool_taxonomy.get("header_present") is True
        and tool_taxonomy.get("tool_pairs")
        and not tool_taxonomy.get("tool_calls_without_results")
        and not tool_taxonomy.get("tool_results_without_calls")
    )
    if live_enabled:
        status = (
            STATUS_PASS
            if all(item.get("status") == STATUS_PASS for item in (launch, send, tool_call)) and native_taxonomy_complete
            else STATUS_FAIL
        )
    else:
        status = STATUS_BLOCKED
    observation: dict[str, Any] = {
        "status": status,
        "provider": "pi",
        "profile": PROFILE,
        "provider_bin": str(binary),
        "executable_identity": actual_identity,
        "expected_executable_identity": request["expected_executable_identity"],
        "expected_provider_version": request["expected_provider_version"],
        "longhouse_git_sha": runner_sha,
        "live_enabled": live_enabled,
        "required_enable_env": PI_LIVE_ENV,
        "accepted_credential_env": list(CREDENTIAL_REQUIREMENT),
        "launch_managed_session": launch,
        "send_receive": send,
        "tool_call_result": tool_call,
        "native_shadow_taxonomy": tool_taxonomy,
        "native_taxonomy_complete": native_taxonomy_complete,
        "exact_native_resume": {
            "session_file": send.get("session_file"),
            "same_provider_session_id": launch.get("provider_session_id") == send.get("provider_session_id"),
            "resume_argv_used": bool(send.get("exact_resume_file")),
        },
    }
    if live_enabled and tool_call.get("session_file"):
        model_evidence = pi_native_model_evidence(
            Path(tool_call["session_file"]),
            source_canary=PROFILE,
            api_key_configured=bool(os.environ.get("OPENROUTER_API_KEY")),
        )
        if model_evidence is not None:
            observation["live_model_evidence"] = model_evidence
    identity.atomic_json(output_root / "request.json", request)
    identity.atomic_json(output_root / "raw-observation.json", observation)
    return observation
