"""Mutation tests for the terminal-truth gate: each reintroduces one defect fixed on 2026-11-11 and must fail its cell.

A gate that has never been shown to fail proves little. Each mutation here is a one-place source edit that puts back one
of the five defects the harness found (or an equivalent for the part-type check), a cell that the defect breaks, and the
stage where the verdict must say the text stopped. The command runs the unmutated cell first (it must pass), then the
mutated one (it must fail at the named stage), and exits non-zero on any mismatch.

    make terminal-truth-mutations ARGS="--engine <engine> --longhouse-cli <longhouse>"

Server-side mutations shadow ``zerg`` with a patched copy for the scratch Runtime Host only. Engine mutations are applied in
a scratch git worktree and rebuilt (the build reuses a clone of this checkout's cargo target, so only the engine crates
recompile). Two further checks need no source edit: a reworded ready string must fail preflight, and a fake model
streaming at half speed must still pass.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from zerg.qa import terminal_truth as tt
from zerg.qa.scripted_model_server import parse_pacing

REPO_ROOT = tt.REPO_ROOT
SCRATCH = tt.SCRATCH_ROOT.parent / "tt-mut"


@dataclasses.dataclass(frozen=True)
class Mutation:
    name: str
    defect: str
    side: str  # server | engine
    file: str  # repo-relative
    old: str  # must occur exactly once
    new: str
    cell: str  # provider/mode the defect breaks
    stages: tuple[str, ...]  # the verdict must name one of these as the first failed stage


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "pi-helm-text-forward",
        "drop the Pi Helm extension's forwarding of reply text (2a7854633)",
        "engine",
        "engine/assets/longhouse-pi-helm.ts",
        'if (update?.type === "text_delta" && typeof update.delta === "string" && update.delta) {',
        "if (false) {",
        "pi/helm",
        ("engine_posted",),
    ),
    Mutation(
        "opencode-host-publish",
        "the host stops publishing OpenCode's nested live text (c510accaf)",
        "server",
        "server/zerg/routers/runtime.py",
        '(event.provider or "").strip().lower() in {"pi", "omp", "opencode"}',
        '(event.provider or "").strip().lower() in {"pi", "omp"}',
        "opencode/console",
        ("host_published",),
    ),
    Mutation(
        "omp-session-dir-rule",
        "the launcher picks ~/.local/share/omp when XDG_DATA_HOME is unset, as before 125833163",
        "engine",
        "engine/src/omp_session.rs",
        'if let Some(xdg_data_home) = nonempty_env_path("XDG_DATA_HOME") {',
        '{ let xdg_data_home = std::env::var_os("XDG_DATA_HOME").map(PathBuf::from).filter(|value| !value.as_os_str().is_empty()).unwrap_or_else(|| home_dir().join(".local").join("share"));',
        "omp/helm",
        ("preflight", "engine_posted"),
    ),
    Mutation(
        "opencode-part-type-check",
        "OpenCode reasoning parts are shown as reply text (the part-type check of c510accaf)",
        "engine",
        "engine/src/opencode_bridge.rs",
        "                    Some(_) => return None,\n",
        "                    Some(_) => {}\n",
        "opencode/helm",
        ("stream_delivered",),
    ),
    Mutation(
        "antigravity-step-update-arm",
        "Antigravity Console ignores stream-json step_update records (cdda35f88)",
        "engine",
        "engine/src/antigravity_print.rs",
        'if record.get("event").and_then(Value::as_str) != Some("step_update") {\n            return None;\n        }',
        "if true {\n            return None;\n        }",
        "antigravity/console",
        ("engine_posted",),
    ),
)

# The part-type check has a second guard that also hides reasoning; the mutation removes both.
_PART_TYPE_SECOND_GUARD = (
    'if self.part_types.get(part_id).map(String::as_str) != Some("text") {\n                    return None;\n                }\n                self.preview(part_id, message_id, session_id, now, false)',
    "if self.part_types.get(part_id).is_none() {\n                    return None;\n                }\n                self.preview(part_id, message_id, session_id, now, false)",
)


def mutated_text(mutation: Mutation, text: str) -> str:
    """The file with the mutation applied; refuses unless the edit lands in exactly one place."""

    if text.count(mutation.old) != 1:
        raise RuntimeError(f"mutation {mutation.name}: the text to mutate occurs {text.count(mutation.old)} times in {mutation.file}")
    out = text.replace(mutation.old, mutation.new)
    if mutation.name == "opencode-part-type-check":
        if out.count(_PART_TYPE_SECOND_GUARD[0]) != 1:
            raise RuntimeError("mutation opencode-part-type-check: the second guard moved")
        out = out.replace(*_PART_TYPE_SECOND_GUARD)
    return out


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: float = 1800) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True, timeout=timeout, stdout=sys.stderr, stderr=sys.stderr)


def _build_engine(worktree: Path, target: Path, out: Path) -> tuple[Path, Path]:
    """Build the engine and CLI of ``worktree`` into ``target`` (a clone of a warm cargo target) and copy both binaries to ``out``."""

    rust = Path.home() / ".rustup" / "toolchains" / "1.93.0-aarch64-apple-darwin" / "bin"
    env = dict(os.environ, LONGHOUSE_CARGO_TARGET_DIR=str(target), PATH=f"{rust}{os.pathsep}{os.environ['PATH']}")
    _run([sys.executable, "scripts/build/generate_build_identity.py"], cwd=worktree, env=env)
    build = [
        sys.executable, "scripts/build/cargo.py", "exec", "--", "build", "--manifest-path", "engine/Cargo.toml",
        "--profile", "ci-test", "--features", "qa-fault-injection", "--bin", "longhouse-engine", "--bin", "longhouse",
    ]  # fmt: skip
    lock = shutil.which("lockf")
    _run([lock, "/tmp/agents/longhouse-heavy-build.lock", *build] if lock else build, cwd=worktree, env=env)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("longhouse-engine", "longhouse"):
        shutil.copy2(target / "ci-test" / name, out / name)
    return out / "longhouse-engine", out / "longhouse"


def _clone_dir(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    flag = ["-c"] if sys.platform == "darwin" else ["--reflink=auto"]
    if source.exists():
        subprocess.run(["cp", "-R", *flag, str(source), str(destination)], check=True)
        (destination / ".longhouse-target.json").unlink(missing_ok=True)  # the clone belongs to the scratch worktree now
    else:
        destination.mkdir(parents=True)


def _verdict(results: list[tt.CellResult]) -> tuple[str, str | None, str]:
    cell = results[0]
    return cell.outcome, cell.first_failed_stage, (cell.reason or "").splitlines()[0][:200] if cell.reason else ""


def run_mutations(args: argparse.Namespace) -> int:
    pace = parse_pacing(
        {"rate": args.rate, "tokens": args.tokens, "marker_every": 20, "initial_delay": 1.0, "marker_prefix": "TT", "reasoning_tokens": 5}
    )
    expectations = tt.load_expectations()
    base_engine, base_cli = args.engine.resolve(), args.longhouse_cli.resolve()
    rows: list[dict[str, Any]] = []
    selected = [m for m in MUTATIONS if not args.only or m.name in args.only]
    SCRATCH.mkdir(parents=True, exist_ok=True)
    worktree = SCRATCH / "worktree"
    engine_built: dict[str, tuple[Path, Path]] = {}

    def cell(spec: tt.CellSpec, engine: Path, cli: Path, **extra: Any) -> tt.CellResult:
        return tt.run_suite([spec], pace=extra.pop("pace", pace), engine=engine, longhouse_cli=cli, expectations=expectations, **extra)[0]

    try:
        if any(m.side == "engine" for m in selected) and not args.skip_engine:
            if not worktree.exists():
                _run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=REPO_ROOT)
            _clone_dir(Path(args.cargo_target).resolve(), SCRATCH / "target")
        for m in selected:
            spec = tt.CellSpec(*m.cell.split("/"))
            baseline = cell(spec, base_engine, base_cli)
            row: dict[str, Any] = {
                "check": f"mutation {m.name}",
                "defect": m.defect,
                "cell": m.cell,
                "expected_stage": "/".join(m.stages),
                "baseline": baseline.outcome,
            }
            started = time.monotonic()
            if m.side == "server":
                copy = SCRATCH / f"server-{m.name}"
                shutil.rmtree(copy, ignore_errors=True)
                # The host finds config/ relative to the package, so the copy keeps the repository's layout.
                shutil.copytree(tt.SERVER_ROOT / "zerg", copy / "server" / "zerg", ignore=shutil.ignore_patterns("__pycache__"))
                shutil.copytree(REPO_ROOT / "config", copy / "config")
                target_file = copy / "server" / "zerg" / Path(m.file).relative_to("server/zerg")
                target_file.write_text(mutated_text(m, target_file.read_text()))
                mutated = cell(spec, base_engine, base_cli, host_environment={"PYTHONPATH": str(copy / "server")})
            elif args.skip_engine:
                row.update(observed="skipped", ok=False, note="--skip-engine")
                rows.append(row)
                continue
            else:
                path = worktree / m.file
                path.write_text(mutated_text(m, path.read_text()))
                try:
                    engine_built[m.name] = _build_engine(worktree, SCRATCH / "target", SCRATCH / f"bin-{m.name}")
                finally:
                    _run(["git", "checkout", "--", "."], cwd=worktree)
                mutated = cell(spec, *engine_built[m.name])
            outcome, stage, reason = _verdict([mutated])
            row.update(observed_outcome=outcome, observed_stage=stage, reason=reason, seconds=round(time.monotonic() - started))
            row["ok"] = baseline.outcome == "pass" and outcome == "fail" and stage in m.stages
            rows.append(row)

        if not args.only or "reworded-ready-string" in args.only:
            codex = tt.PROVIDERS["codex"]
            reworded = {**tt.PROVIDERS, "codex": dataclasses.replace(codex, ready_text="Ask Codex to do anything, v2")}
            result = cell(tt.CellSpec("codex", "helm"), base_engine, base_cli, drivers=reworded, ready_timeout=12.0)
            outcome, stage, reason = _verdict([result])
            rows.append(
                {"check": "reworded ready string", "cell": "codex/helm", "expected_stage": "preflight", "observed_outcome": outcome,
                 "observed_stage": stage, "reason": reason, "ok": outcome == "fail" and stage == "preflight"}
            )  # fmt: skip
        if not args.only or "half-speed" in args.only:
            half = dataclasses.replace(pace, rate=pace.rate / 2)
            result = cell(tt.CellSpec("codex", "helm"), base_engine, base_cli, pace=half)
            outcome, stage, reason = _verdict([result])
            rows.append(
                {"check": "half-speed fake model", "cell": "codex/helm", "expected_stage": "(pass)", "observed_outcome": outcome,
                 "observed_stage": stage, "reason": reason, "ok": outcome == "pass"}
            )  # fmt: skip
    finally:
        if worktree.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT, check=False)
        shutil.rmtree(SCRATCH, ignore_errors=True)
    print("| check | cell | baseline | expected | observed | stage | ok |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        print(
            f"| {row['check']} | {row['cell']} | {row.get('baseline', '-')} | {row['expected_stage']} | "
            f"{row.get('observed_outcome', row.get('observed'))} | {row.get('observed_stage') or '-'} | {'yes' if row['ok'] else 'NO'} |"
        )
    if args.out:
        args.out.write_text(json.dumps(rows, indent=2) + "\n")
    return 0 if rows and all(row["ok"] for row in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--engine", type=Path, default=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or shutil.which("longhouse-engine") or "")
    )
    parser.add_argument("--longhouse-cli", type=Path, default=Path(shutil.which("longhouse") or ""))
    parser.add_argument(
        "--cargo-target", type=Path, default=REPO_ROOT / ".build" / "cargo-target", help="warm cargo target to clone for engine rebuilds"
    )
    parser.add_argument("--only", action="append", help="mutation name, 'reworded-ready-string' or 'half-speed'; repeatable")
    parser.add_argument("--skip-engine", action="store_true", help="skip mutations that need an engine rebuild")
    parser.add_argument("--tokens", type=int, default=100)
    parser.add_argument("--rate", type=float, default=20.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not args.engine.is_file() or not args.longhouse_cli.is_file():
        parser.error("pass --engine and --longhouse-cli (built from this checkout)")
    return run_mutations(args)


if __name__ == "__main__":
    raise SystemExit(main())
