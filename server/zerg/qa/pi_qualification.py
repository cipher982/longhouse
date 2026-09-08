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
from zerg.qa import provider_semantic_qualification as semantic
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

PROFILE = "pi_print_v1"
SCENARIO_ID = "pi_print"
ASSERTIONS = (
    "pi_native_print_turns_complete",
    "pi_native_tool_call_result_paired",
    "pi_exact_native_file_continues",
)
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


def _execute(binary: Path, evidence_root: Path):
    if not _live_enabled():
        return (
            {"status": "blocked", "provider": "pi", "profile": PROFILE, "live_enabled": False},
            tuple(semantic.SemanticAssertion(assertion, AssertionOutcome.BLOCKED, EvidenceClass.LIVE_NO_TOKEN) for assertion in ASSERTIONS),
            (),
        )

    # Keep optional harness/database imports out of the dispatcher's import path.
    from zerg.qa.pi_native import pi_native_model_evidence
    from zerg.qa.provider_adapters.pi import PiHarnessAdapter
    from zerg.qa.universal_agent_harness import AdapterConfig
    from zerg.qa.universal_agent_harness import EvidencePackage

    evidence_root.mkdir(parents=True, exist_ok=True)
    config = AdapterConfig(
        provider="pi",
        binary_name="pi",
        binary_env="LONGHOUSE_PI_BIN",
        safe_run_prompt_once=True,
        safe_managed_session_scenarios=("launch_managed_session", "send_receive"),
    )
    adapter = PiHarnessAdapter(config, provider_bin=binary)
    package = EvidencePackage(root=evidence_root, provider="pi", scenario=SCENARIO_ID)
    adapter.prepare(package)
    launch = adapter.launch_managed_session(package)
    send = adapter.send_receive(package, "Reply with the single word OK.")
    tool_call = adapter.tool_call_result(package)
    turns_complete = all(item.get("status") == "pass" for item in (launch, send, tool_call))
    tool_taxonomy = tool_call.get("native_shadow_taxonomy") or {}
    tools_paired = bool(
        tool_taxonomy.get("header_present")
        and tool_taxonomy.get("tool_pairs")
        and not tool_taxonomy.get("tool_calls_without_results")
        and not tool_taxonomy.get("tool_results_without_calls")
    )
    exact_resume = bool(
        launch.get("session_file")
        and launch.get("session_file") == send.get("session_file") == tool_call.get("session_file")
        and launch.get("provider_session_id") == send.get("provider_session_id") == tool_call.get("provider_session_id")
        and send.get("exact_resume_file") is True
        and tool_call.get("exact_resume_file") is True
    )
    conditions = (turns_complete, tools_paired, exact_resume)
    observation: dict[str, Any] = {
        "status": "pass" if all(conditions) else "fail",
        "provider": "pi",
        "profile": PROFILE,
        "provider_bin": str(binary),
        "live_enabled": True,
        "launch_managed_session": launch,
        "send_receive": send,
        "tool_call_result": tool_call,
        "native_shadow_taxonomy": tool_taxonomy,
        "exact_native_resume": exact_resume,
    }
    secret = os.environ.get("OPENROUTER_API_KEY", "")
    if tool_call.get("session_file"):
        model_evidence = pi_native_model_evidence(
            Path(tool_call["session_file"]),
            source_canary=PROFILE,
            api_key_configured=bool(secret),
        )
        if model_evidence is not None:
            observation["live_model_evidence"] = model_evidence
    return (
        observation,
        tuple(
            semantic.SemanticAssertion(
                assertion,
                AssertionOutcome.PASS if passed else AssertionOutcome.SEMANTIC_FAIL,
                EvidenceClass.LIVE_TOKEN,
            )
            for assertion, passed in zip(ASSERTIONS, conditions)
        ),
        (secret,) if secret else (),
    )


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        assertion_ids=ASSERTIONS,
        executor=_execute,
        oracle_source=Path(__file__),
    )
