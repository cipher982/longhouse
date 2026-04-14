#!/usr/bin/env python3
"""Profile Longhouse Claude/Codex hook hot-path latency in a sandbox.

This script executes the installed hook scripts (or provided overrides) in a
temporary HOME-like sandbox so we can measure hook overhead without mutating
the user's real outbox, transcript bindings, or engine state.

It targets the exact question raised during managed-local debugging: how
expensive is the local-only hook hot path, and what extra cost comes from the
managed-session bind branch?

Usage examples:

  python scripts/managed-local/profile_longhouse_hooks.py
  python scripts/managed-local/profile_longhouse_hooks.py --iterations 50 --json
  python scripts/managed-local/profile_longhouse_hooks.py --providers claude
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Sequence


DEFAULT_CLAUDE_HOOK_PATH = Path.home() / ".claude" / "hooks" / "longhouse-hook.sh"
DEFAULT_CODEX_HOOK_PATH = Path.home() / ".codex" / "hooks" / "longhouse-codex-hook.sh"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    hook_path: Path
    event_name: str
    input_payload: dict[str, Any]


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    description: str
    hook_env: dict[str, str]


@dataclass(frozen=True)
class IterationSample:
    elapsed_ms: float
    exit_code: int


@dataclass(frozen=True)
class ScenarioResult:
    provider: str
    scenario: str
    description: str
    iterations: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    exit_codes: list[int]
    http_requests: int
    outbox_files: int
    engine_bind_count: int


PROVIDER_DESCRIPTORS: dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        name="claude",
        hook_path=DEFAULT_CLAUDE_HOOK_PATH,
        event_name="PreToolUse",
        input_payload={
            "hook_event_name": "PreToolUse",
            "session_id": "plain-claude-session",
            "tool_name": "Bash",
            "cwd": "/tmp/longhouse-hook-profile",
            "transcript_path": "",
            "notification_type": "",
        },
    ),
    "codex": ProviderSpec(
        name="codex",
        hook_path=DEFAULT_CODEX_HOOK_PATH,
        event_name="UserPromptSubmit",
        input_payload={
            "hook_event_name": "UserPromptSubmit",
            "session_id": "plain-codex-session",
            "cwd": "/tmp/longhouse-hook-profile",
            "transcript_path": "",
        },
    ),
}


SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        name="plain_outbox",
        description="global hook path, local outbox write only",
        hook_env={},
    ),
    ScenarioSpec(
        name="managed_bind_outbox",
        description="managed-session bind branch plus local outbox write",
        hook_env={"LONGHOUSE_MANAGED_SESSION_ID": "managed-session-123"},
    ),
)


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * pct
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--iterations",
        type=int,
        default=25,
        help="Number of times to run each provider/scenario combination (default: 25).",
    )
    parser.add_argument(
        "--providers",
        type=str,
        default="claude,codex",
        help="Comma-separated subset of providers to profile: claude,codex (default: both).",
    )
    parser.add_argument(
        "--scenarios",
        type=str,
        default=",".join(spec.name for spec in SCENARIOS),
        help="Comma-separated subset of scenarios to profile.",
    )
    parser.add_argument(
        "--claude-hook-path",
        type=Path,
        default=DEFAULT_CLAUDE_HOOK_PATH,
        help=f"Claude hook script to profile (default: {DEFAULT_CLAUDE_HOOK_PATH}).",
    )
    parser.add_argument(
        "--codex-hook-path",
        type=Path,
        default=DEFAULT_CODEX_HOOK_PATH,
        help=f"Codex hook script to profile (default: {DEFAULT_CODEX_HOOK_PATH}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human table.",
    )
    return parser


def _replace_engine_path(script_text: str, engine_path: Path) -> str:
    return re.sub(
        r'^ENGINE="[^"]*"$',
        f'ENGINE="{engine_path}"',
        script_text,
        flags=re.MULTILINE,
    )


def _replace_home_bound_paths(script_text: str, sandbox_home: Path) -> str:
    real_home = str(Path.home())
    return script_text.replace(real_home, str(sandbox_home))


def materialize_hook_script(*, source_path: Path, sandbox_home: Path, sandbox_root: Path, hook_name: str) -> Path:
    text = source_path.read_text(encoding="utf-8")
    engine_dir = sandbox_root / "engine"
    engine_dir.mkdir(parents=True, exist_ok=True)
    engine_stub = engine_dir / "longhouse-engine"
    engine_log = sandbox_root / "engine-bind.log"
    engine_stub.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> "{engine_log}"
            exit 0
            """
        ),
        encoding="utf-8",
    )
    engine_stub.chmod(0o755)

    rewritten = _replace_home_bound_paths(text, sandbox_home)
    rewritten = _replace_engine_path(rewritten, engine_stub)

    materialized_path = sandbox_root / hook_name
    materialized_path.write_text(rewritten, encoding="utf-8")
    materialized_path.chmod(0o755)
    return materialized_path


def _count_outbox_files(outbox_dir: Path) -> int:
    if not outbox_dir.exists():
        return 0
    return len([path for path in outbox_dir.iterdir() if path.is_file() and path.name.startswith("prs.")])


def _count_engine_binds(engine_log_path: Path) -> int:
    if not engine_log_path.exists():
        return 0
    return len([line for line in engine_log_path.read_text(encoding="utf-8").splitlines() if line.strip()])


def _build_env(
    *,
    base_env: dict[str, str],
    scenario: ScenarioSpec,
) -> dict[str, str]:
    env = dict(base_env)
    for key, value in scenario.hook_env.items():
        env[key] = value
    return env


def _payload_for_scenario(provider: ProviderSpec, scenario: ScenarioSpec) -> str:
    payload = dict(provider.input_payload)
    if "managed_bind" in scenario.name:
        payload["transcript_path"] = str(Path("/tmp") / f"{provider.name}-profile-transcript.jsonl")
    return json.dumps(payload)


def run_iteration(*, hook_path: Path, env: dict[str, str], payload: str) -> IterationSample:
    started = time.perf_counter()
    result = subprocess.run(
        [str(hook_path)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    ended = time.perf_counter()
    return IterationSample(
        elapsed_ms=(ended - started) * 1000.0,
        exit_code=int(result.returncode),
    )


def profile_provider_scenario(
    *,
    provider: ProviderSpec,
    hook_source_path: Path,
    scenario: ScenarioSpec,
    iterations: int,
) -> ScenarioResult:
    with tempfile.TemporaryDirectory(prefix=f"lh-hook-profile-{provider.name}-{scenario.name}-") as temp_dir:
        sandbox_root = Path(temp_dir)
        sandbox_home = sandbox_root / "home"
        (sandbox_home / ".longhouse" / "agent" / "outbox").mkdir(parents=True, exist_ok=True)
        (sandbox_home / ".claude" / "hindsight").mkdir(parents=True, exist_ok=True)
        (sandbox_home / ".codex").mkdir(parents=True, exist_ok=True)

        materialized_hook = materialize_hook_script(
            source_path=hook_source_path,
            sandbox_home=sandbox_home,
            sandbox_root=sandbox_root,
            hook_name=hook_source_path.name,
        )
        payload = _payload_for_scenario(provider, scenario)
        base_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(sandbox_home),
        }
        outbox_dir = sandbox_home / ".longhouse" / "agent" / "outbox"
        engine_log_path = sandbox_root / "engine-bind.log"

        samples: list[IterationSample] = []
        env = _build_env(base_env=base_env, scenario=scenario)
        for _ in range(iterations):
            samples.append(run_iteration(hook_path=materialized_hook, env=env, payload=payload))

        elapsed_values = [sample.elapsed_ms for sample in samples]
        return ScenarioResult(
            provider=provider.name,
            scenario=scenario.name,
            description=scenario.description,
            iterations=iterations,
            mean_ms=float(statistics.mean(elapsed_values) if elapsed_values else 0.0),
            p50_ms=percentile(elapsed_values, 0.50),
            p95_ms=percentile(elapsed_values, 0.95),
            min_ms=float(min(elapsed_values) if elapsed_values else 0.0),
            max_ms=float(max(elapsed_values) if elapsed_values else 0.0),
            exit_codes=[sample.exit_code for sample in samples],
            http_requests=0,
            outbox_files=_count_outbox_files(outbox_dir),
            engine_bind_count=_count_engine_binds(engine_log_path),
        )


def parse_csv_arg(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def format_human_table(results: Iterable[ScenarioResult]) -> str:
    rows = [
        (
            result.provider,
            result.scenario,
            f"{result.mean_ms:.1f}",
            f"{result.p50_ms:.1f}",
            f"{result.p95_ms:.1f}",
            str(result.http_requests),
            str(result.outbox_files),
            str(result.engine_bind_count),
        )
        for result in results
    ]
    headers = ("provider", "scenario", "mean_ms", "p50_ms", "p95_ms", "http", "outbox", "binds")
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    lines = [
        "  ".join(header.ljust(width) for header, width in zip(headers, widths)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend("  ".join(cell.ljust(width) for cell, width in zip(row, widths)) for row in rows)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    provider_names = parse_csv_arg(args.providers)
    scenario_names = parse_csv_arg(args.scenarios)
    if args.iterations <= 0:
        parser.error("--iterations must be > 0")

    hook_paths = {
        "claude": Path(args.claude_hook_path).expanduser(),
        "codex": Path(args.codex_hook_path).expanduser(),
    }
    for provider_name in provider_names:
        if provider_name not in PROVIDER_DESCRIPTORS:
            parser.error(f"Unknown provider '{provider_name}'. Valid values: {sorted(PROVIDER_DESCRIPTORS)}")
        if not hook_paths[provider_name].exists():
            parser.error(f"Hook script does not exist: {hook_paths[provider_name]}")

    scenarios_by_name = {scenario.name: scenario for scenario in SCENARIOS}
    selected_scenarios: list[ScenarioSpec] = []
    for scenario_name in scenario_names:
        scenario = scenarios_by_name.get(scenario_name)
        if scenario is None:
            parser.error(f"Unknown scenario '{scenario_name}'. Valid values: {sorted(scenarios_by_name)}")
        selected_scenarios.append(scenario)

    results: list[ScenarioResult] = []
    for provider_name in provider_names:
        provider = PROVIDER_DESCRIPTORS[provider_name]
        hook_source_path = hook_paths[provider_name]
        for scenario in selected_scenarios:
            results.append(
                profile_provider_scenario(
                    provider=provider,
                    hook_source_path=hook_source_path,
                    scenario=scenario,
                    iterations=args.iterations,
                )
            )

    if args.json:
        json.dump([result.__dict__ for result in results], sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(format_human_table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
