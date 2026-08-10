"""Exact-binary Pi CLI contract plus opt-in real-print qualification.

Pi (npm @earendil-works/pi-coding-agent) is a standalone Bun coding-agent
CLI whose release lane pins an observed install the same way Cursor's does:
there is no staged-release feed the factory can hand the bridge, so the
qualification request names an exact binary tree. The profile verifies the
exact executable identity and, when live credentials are present
(OPENROUTER_API_KEY plus the LONGHOUSE_PI_LIVE opt-in), runs real pi ``-p``
turns through the universal Pi harness adapter (launch + send) so the
transcript JSONL is parsed, bound, and ingested as live evidence. Without the
live opt-in the adapter reports an honest blocked/unsupported payload and the
profile stays blocked rather than spending tokens.
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
    return bool((os.environ.get("OPENROUTER_API_KEY") or "").strip()) and os.environ.get("LONGHOUSE_PI_LIVE") in {
        "1",
        "true",
        "yes",
        "on",
    }


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    # Harness imports are deferred into run(): provider_qualification imports
    # this module eagerly, and the router must stay importable under
    # `python -S` (no sqlalchemy/site-packages) per
    # test_router_imports_without_optional_server_dependencies.
    from zerg.qa.provider_adapters.pi import PI_LIVE_ENV  # noqa: PLC0415
    from zerg.qa.provider_adapters.pi import PiHarnessAdapter  # noqa: PLC0415
    from zerg.qa.universal_agent_harness import AdapterConfig  # noqa: PLC0415
    from zerg.qa.universal_agent_harness import EvidencePackage  # noqa: PLC0415
    from zerg.qa.universal_agent_harness import STATUS_BLOCKED  # noqa: PLC0415
    from zerg.qa.universal_agent_harness import STATUS_PASS  # noqa: PLC0415

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
    config = AdapterConfig(provider="pi", binary_name="pi", binary_env="LONGHOUSE_PI_BIN")
    adapter = PiHarnessAdapter(config, provider_bin=binary)
    package = EvidencePackage(root=output_root, provider="pi", scenario=SCENARIO_ID)
    adapter.prepare(package)
    launch = adapter.launch_managed_session(package)
    send = adapter.send_receive(package, "Reply with the single word OK.")
    live_enabled = _live_enabled()
    if live_enabled:
        status = (
            STATUS_PASS
            if launch.get("status") == STATUS_PASS and send.get("status") == STATUS_PASS
            else STATUS_BLOCKED
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
    }
    identity.atomic_json(output_root / "request.json", request)
    identity.atomic_json(output_root / "raw-observation.json", observation)
    return observation
