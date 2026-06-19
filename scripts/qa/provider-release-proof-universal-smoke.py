#!/usr/bin/env python3
"""Run the all-provider universal release-proof smoke with fake no-token binaries."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SERVER_PATH = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER_PATH))

from zerg.qa.universal_agent_harness import HarnessOptions
from zerg.qa.universal_agent_harness import SUPPORTED_PROVIDERS
from zerg.qa.universal_agent_harness import run_harness

DEFAULT_SCENARIOS = (
    "probe_identity",
    "adapter_conformance",
    "collect_raw_evidence",
    "action_matrix",
    "control_surface",
    "baseline_compare",
    "parse_ingest_project",
    "db_ingest_project",
    "session_projection",
    "timeline_projection",
    "run_prompt_once",
    "launch_managed_session",
    "send_receive",
    "pause_request_detect",
    "tail_output",
    "runtime_phase",
    "transcript_binding",
    "multi_turn_continuity",
    "crash_timeout_cleanup",
)
FAKE_VERSION_BY_PROVIDER = {
    "claude": "2.9.9-fake (Claude Code)",
    "codex": "codex-cli 9.9.9",
    "opencode": "opencode 9.9.9",
    "antigravity": "agy 9.9.9",
}
FAKE_BINARY_BY_PROVIDER = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "antigravity": "agy",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def default_evidence_root() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".build/canaries/provider-release-proof-universal-smoke") / stamp


def write_fake_provider_bins(root: Path) -> dict[str, Path]:
    bin_root = root / "fake-provider-bins"
    bin_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for provider in SUPPORTED_PROVIDERS:
        path = bin_root / FAKE_BINARY_BY_PROVIDER[provider]
        version = FAKE_VERSION_BY_PROVIDER[provider]
        path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys",
                    'if sys.argv[1:] == ["--version"]:',
                    f"    print({version!r})",
                    "    raise SystemExit(0)",
                    'print("unexpected fake provider args: " + repr(sys.argv[1:]), file=sys.stderr)',
                    "raise SystemExit(2)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        result[provider] = path
    return result


def write_parse_fixture(root: Path) -> Path:
    fixture_path = root / "fixtures" / "provider-events.jsonl"
    rows = (
        {"type": "user", "text": "universal smoke hello"},
        {"type": "assistant", "text": "universal smoke world"},
        {
            "type": "tool",
            "tool_name": "shell",
            "tool_call_id": "tool-smoke",
            "text": "ok",
        },
    )
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    return fixture_path


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    evidence_root = (args.evidence_root or default_evidence_root()).expanduser()
    artifact_path = (
        args.artifact or (evidence_root / "provider-release-proof-universal-smoke.json")
    ).expanduser()
    scenarios = tuple(args.scenario or DEFAULT_SCENARIOS)
    provider_bins = write_fake_provider_bins(evidence_root)
    fixture_path = write_parse_fixture(evidence_root)
    harness = run_harness(
        HarnessOptions(
            providers=SUPPORTED_PROVIDERS,
            scenarios=scenarios,
            evidence_root=evidence_root / "universal-agent-harness",
            provider_bins=provider_bins,
            fixture_path=fixture_path,
            prompt="Longhouse release-proof universal fake/no-token smoke.",
        )
    )
    artifact = {
        "schema_version": 1,
        "artifact_kind": "provider_release_proof_universal_smoke",
        "generated_at": utc_now(),
        "verdict": harness.get("verdict"),
        "providers": list(SUPPORTED_PROVIDERS),
        "scenarios": list(scenarios),
        "result_count": len(harness.get("results") or []),
        "evidence_root": str(evidence_root),
        "universal_harness_artifact": str(
            evidence_root / "universal-agent-harness" / "universal-agent-harness.json"
        ),
        "provider_support_matrix_path": harness.get("provider_support_matrix_path"),
        "provider_support_matrix": harness.get("provider_support_matrix"),
    }
    artifact["artifact_path"] = str(artifact_path)
    write_json(artifact_path, artifact)
    return artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument(
        "--scenario",
        action="append",
        help="Universal scenario to run. Repeatable; defaults to fake/no-token smoke surface.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the smoke artifact as JSON."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    artifact = run_smoke(args)
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"verdict: {artifact['verdict']}")
        print(f"artifact: {artifact['artifact_path']}")
    return 1 if artifact.get("verdict") == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
