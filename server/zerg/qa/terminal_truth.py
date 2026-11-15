"""Terminal truth: where does live reply text stop between a provider's terminal and a Longhouse client, per Helm and Console cell?

A provider CLI runs against a scripted fake model that streams a paced reply with a unique marker word every N tokens
(each cell has its own nonce). Every marker has a time on one monotonic clock at the terminal (a pyte replay of the owned PTY;
for Console, the instant the model wrote the frame) and a time on each Runtime Host surface that carries it. A cell's verdict
is one rule over five stages: preflight, provider_emitted, engine_posted, host_published, stream_delivered, naming the first
that failed. ``run`` exits non-zero on any fail, any declared cell that did not run, and any known-bad cell that now passes.

The code is split by responsibility; this module is the command line and re-exports the pieces:

- ``terminal_truth_pty``: PTY and screen, readiness, the self-check (fake TUI);
- ``terminal_truth_lanes``: lane observers, the provider's files, the wire between engine and host, delay arithmetic;
- ``terminal_truth_providers``: provider drivers, per-provider home setup, the scratch Runtime Host and scripted model;
- ``terminal_truth_scenarios``: one cell, a pass of cells sharing a host and engine, a suite (one cell cold, the rest shared);
- ``terminal_truth_verdicts``: stages, the verdict rule, the ledger expectations, results and tables;
- ``terminal_truth_mutations``: the five 2026-11-11 defects put back, each of which must fail its cell.

Expectations (live reply text expected, known-bad with a reason, not measurable) come from ``live_stream_signals`` in
schemas/managed_providers.yml, read through ``server/zerg/config/managed_provider_contracts.json``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path

from zerg.qa.scripted_model_server import parse_pacing
from zerg.qa.terminal_truth_kinds import SCENARIOS
from zerg.qa.terminal_truth_kinds import kinds_expectation
from zerg.qa.terminal_truth_kinds import render_expectations
from zerg.qa.terminal_truth_kinds import render_kinds
from zerg.qa.terminal_truth_kinds import run_kinds_suite
from zerg.qa.terminal_truth_lanes import _PUBLISH_LINE  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import FileWatcher  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import Lane  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import LaneStats  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import LaneWatcher  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import RequestScanner  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import RuntimeApi  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import StreamWatcher  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import TeeProxy  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import host_publishes  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import lane_stats  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_lanes import pacing_error  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_providers import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    _AMBIENT_KEEP,
)
from zerg.qa.terminal_truth_providers import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    _AMBIENT_PREFIXES,
)
from zerg.qa.terminal_truth_providers import PROVIDERS  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_providers import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    ProviderDriver,
)
from zerg.qa.terminal_truth_providers import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    ScratchRuntimeHost,
)
from zerg.qa.terminal_truth_providers import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    ScriptedModel,
)
from zerg.qa.terminal_truth_providers import _free_port  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_providers import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    _install_antigravity_hook,
)
from zerg.qa.terminal_truth_providers import _post_json  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_providers import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    _prepare_provider,
)
from zerg.qa.terminal_truth_providers import _stop_group  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_providers import _version  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_providers import pace_json  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_providers import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    provider_environment,
)
from zerg.qa.terminal_truth_providers import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    scrub_ambient_environment,
)
from zerg.qa.terminal_truth_pty import COLUMNS  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import PROMPT  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import REPO_ROOT  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import ROWS  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import SCRATCH_ROOT  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import SERVER_ROOT  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import SelfCheck  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import _new_scratch  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import _read_truth  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import _tail_text  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import _wait_ready  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import clean_environ  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import fake_tui  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import run_selfcheck  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import screen_text  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_pty import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    terminal_first_seen,
)
from zerg.qa.terminal_truth_scenarios import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    HOME_EXCLUSIVE,
)
from zerg.qa.terminal_truth_scenarios import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    NO_REASONING_FAKE,
)
from zerg.qa.terminal_truth_scenarios import CellPlan  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_scenarios import Pass  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_scenarios import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    _create_console_session,
)
from zerg.qa.terminal_truth_scenarios import build_pass  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_scenarios import plan_cell  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_scenarios import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    plan_schedule,
)
from zerg.qa.terminal_truth_scenarios import run_cell_in  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_scenarios import run_suite  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_scenarios import stop_pass  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    EARLY_FRACTION_MIN,
)
from zerg.qa.terminal_truth_verdicts import LEDGER_PATH  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    MEDIAN_DELAY_BUDGET_S,
)
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    PAINT_LAG_BUDGET_S,
)
from zerg.qa.terminal_truth_verdicts import STAGES  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import CellResult  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import CellSpec  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import Expectation  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import Observation  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import RunResult  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import StageResult  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import _fmt  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    check_live_previews,
)
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    check_two_message_previews,
)
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    declared_cells,
)
from zerg.qa.terminal_truth_verdicts import exit_code  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    first_failed_stage,
)
from zerg.qa.terminal_truth_verdicts import judge  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    load_expectations,
)
from zerg.qa.terminal_truth_verdicts import outcome_for  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    preview_segments,
)
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    render_final_preview,
)
from zerg.qa.terminal_truth_verdicts import render_split  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import render_table  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import render_timing  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import (  # noqa: F401 - re-exported: this module is the entry point and the public surface
    render_verdicts,
)
from zerg.qa.terminal_truth_verdicts import stream_checks  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import summary_json  # noqa: F401 - re-exported: this module is the entry point and the public surface
from zerg.qa.terminal_truth_verdicts import write_results  # noqa: F401 - re-exported: this module is the entry point and the public surface


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run",
        help="judge cells; exits 1 on any fail, any declared cell that did not run, or a stale known-bad declaration",
    )
    run.add_argument(
        "--provider", action="append", choices=sorted({*PROVIDERS, "cursor"}), help="repeatable; default every declared provider"
    )
    run.add_argument("--tokens", type=int, default=100)
    run.add_argument("--rate", type=float, default=20.0)
    run.add_argument("--marker-every", type=int, default=20)
    run.add_argument("--initial-delay", type=float, default=1.0)
    run.add_argument(
        "--reasoning-tokens", type=int, default=5, help="reasoning words streamed before the reply, on every protocol (0 turns it off)"
    )
    run.add_argument("--scenario", choices=["long_reply", "two_message", "thinking", "tool", "subagent"], default="long_reply")
    run.add_argument(
        "--kinds-mutation",
        choices=["no_thinking", "no_tool", "no_subagent"],
        help="negative control for the thinking, tool and subagent scenarios: the scripted model skips the thing; every cell must then fail",
    )
    run.add_argument(
        "--expect-kinds",
        action="store_true",
        help="for the thinking, tool and subagent scenarios: exit 1 unless each of the six host-published cells reached the stream with the right kind",
    )
    run.add_argument(
        "--old-client",
        action="store_true",
        help="connect the stream as a client that predates preview kinds (no preview_kinds parameter); thinking must then never arrive",
    )
    run.add_argument(
        "--warm-session",
        action="store_true",
        help="live-kinds scenarios, Helm cells: send one unmarked warm-up turn first, so the session is listed and the stream is open when the scenario "
        "prompt goes (a frame posted in the first half second of a session's first prompt cannot otherwise be seen)",
    )
    run.add_argument(
        "--text-before-tool",
        action="store_true",
        help="tool scenario: the model streams a sentence of reply text and then calls the tool in the same response, as real turns do",
    )
    run.add_argument(
        "--engine-delay-ms",
        type=int,
        default=0,
        help="live-kinds scenarios: hold every chunk between the engine and the host this long, each way (a hosted tenant's network); "
        "0 is loopback, where the engine never has a tool call and reply text queued behind one post",
    )
    run.add_argument("--engine", type=Path, default=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or shutil.which("longhouse-engine") or ""))
    run.add_argument("--mode", action="append", choices=["shadow", "helm", "console"], help="repeatable; default console and helm")
    run.add_argument(
        "--cell",
        action="append",
        metavar="PROVIDER/MODE",
        help="repeatable; run exactly these cells (instead of every --provider in every --mode)",
    )
    run.add_argument(
        "--host-env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="repeatable; added to the scratch Runtime Host's environment only (a mutation test shadows the server package this way)",
    )
    run.add_argument(
        "--engine-env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="repeatable; added to the Machine Agent's and every provider launch's environment (a mutation check switches a QA-engine defect on this way)",
    )
    run.add_argument(
        "--ready-text",
        action="append",
        default=[],
        metavar="PROVIDER=TEXT",
        help="repeatable; wait for this text instead of the driver's own ready text (negative control: a reworded prompt must fail preflight)",
    )
    run.add_argument("--longhouse-cli", type=Path, default=None, help="native longhouse CLI for helm mode (default: PATH)")
    run.add_argument(
        "--cold-cell", default="first", help="the cell run alone in its own host, engine and HOME: 'first', 'none' or provider/mode"
    )
    run.add_argument("--ready-timeout", type=float, default=120.0)
    run.add_argument("--keep", action="store_true")
    run.add_argument("--out", type=Path, help="write all cell results here as one JSON file")
    run.add_argument("--out-dir", type=Path, help="write one JSON per cell and summary.json here")
    render = sub.add_parser("render", help="re-render the live-kinds tables from saved --out JSON files")
    render.add_argument("inputs", nargs="+", type=Path)
    check = sub.add_parser("selfcheck", help="fake TUI + stub lanes; exits non-zero if the harness cannot recover its own schedule")
    check.add_argument("--markers", type=int, default=12)
    check.add_argument("--spacing", type=float, default=0.5)
    check.add_argument("--out", type=Path)
    fake = sub.add_parser("fake-tui", help="internal: the fake TUI run inside the self-check PTY")
    fake.add_argument("--schedule", type=Path, required=True)
    fake.add_argument("--truth", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "fake-tui":
        fake_tui([(m, float(t)) for m, t in json.loads(args.schedule.read_text())], args.truth)
        return 0
    if args.command == "render":
        merged = [r for path in args.inputs for r in json.loads(path.read_text())]
        print(render_kinds(merged))
        print(render_expectations(merged))
        return 0
    if args.command == "selfcheck":
        outcome = run_selfcheck(markers=args.markers, spacing_s=args.spacing)
        text = json.dumps(outcome.to_json(), indent=2)
        if args.out:
            args.out.write_text(text + "\n")
        print(text)
        return 0 if outcome.passed else 1
    pace = parse_pacing(
        {
            "rate": args.rate,
            "tokens": args.tokens,
            "marker_every": args.marker_every,
            "initial_delay": args.initial_delay,
            "marker_prefix": "TT",
            "reasoning_tokens": args.reasoning_tokens,
        }
    )
    if not args.engine.is_file():
        parser.error("no longhouse-engine binary: pass --engine")
    expectations = load_expectations()
    modes = args.mode or ["console", "helm"]
    providers = args.provider or sorted({p for p, _ in expectations})
    declared = [
        CellSpec(provider, mode, args.scenario)
        for mode in modes
        for provider in providers
        if mode == "shadow" or (provider, mode) in expectations
    ]
    if args.cell:
        named = [tuple(cell.split("/", 1)) for cell in args.cell]
        unknown = [f"{p}/{m}" for p, m in named if m != "shadow" and (p, m) not in expectations]
        if unknown or args.provider or args.mode:
            parser.error(f"--cell names undeclared cells: {unknown}" if unknown else "--cell replaces --provider and --mode")
        declared = [CellSpec(provider, mode, args.scenario) for provider, mode in named]
    host_environment = dict(item.split("=", 1) for item in args.host_env) or None
    engine_environment = dict(item.split("=", 1) for item in args.engine_env) or None
    drivers = dict(PROVIDERS)
    for item in args.ready_text:
        provider, _, text = item.partition("=")
        drivers[provider] = dataclasses.replace(drivers[provider], ready_text=text)
    if args.scenario in SCENARIOS:  # the live-kinds scenarios: measurements with preflight checks, no budgets yet
        kind_results = run_kinds_suite(
            declared,
            engine=args.engine,
            longhouse_cli=args.longhouse_cli,
            keep=args.keep,
            ready_timeout=args.ready_timeout,
            drivers=drivers,
            host_environment=host_environment,
            engine_environment=engine_environment,
            mutation=args.kinds_mutation,
            old_client=args.old_client,
            warm_session=args.warm_session,
            text_before_tool=args.text_before_tool,
            engine_delay_ms=args.engine_delay_ms,
            log=lambda message: print(message, file=sys.stderr, flush=True),
        )
        print(render_kinds(kind_results))
        print(render_expectations(kind_results))
        if args.out:
            args.out.write_text(json.dumps(kind_results, indent=2) + "\n")
        code = 1 if any(r["outcome"] == "fail" for r in kind_results) else 0
        if args.expect_kinds and any(e is not None and not e["ok"] for e in map(kinds_expectation, kind_results)):
            code = 1
        print(f"\nterminal truth {args.scenario}: {'PASS' if code == 0 else 'FAIL'} (exit {code})")
        return code
    results = run_suite(
        declared,
        pace=pace,
        engine=args.engine,
        longhouse_cli=args.longhouse_cli,
        keep=args.keep,
        cold=None if args.cold_cell == "none" else args.cold_cell,
        expectations=expectations,
        ready_timeout=args.ready_timeout,
        drivers=drivers,
        host_environment=host_environment,
        engine_environment=engine_environment,
        log=lambda message: print(message, file=sys.stderr, flush=True),
    )
    code = exit_code(results, declared)
    print(render_verdicts(results))
    print()
    print(render_timing(results))
    print()
    print(render_table([r for r in results if r.status == "measured"]))
    print()
    print(render_split(results))
    print()
    print(render_final_preview(results))
    for result in results:
        if result.live_preview_check:
            print(f"live preview check {result.cell}: {json.dumps(result.live_preview_check)}")
    if args.out:
        args.out.write_text(json.dumps([r.to_json() for r in results], indent=2) + "\n")
    if args.out_dir:
        write_results(results, declared, code, args.out_dir)
    print(f"\nterminal truth: {'PASS' if code == 0 else 'FAIL'} (exit {code})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
