"""The engine and CLI a terminal-truth run measures: built from this checkout, or taken from a cache of that exact build.

    make terminal-truth-engine ARGS="--out DIR [--cache DIR] [--no-fault-engine]"

Builds ``longhouse-engine`` and ``longhouse`` with the ``ci`` profile and copies both to --out, then the same two with the
``qa-fault-injection`` feature into ``--out/fault``: the one QA engine every mutation check switches a defect on in
(``terminal_truth_mutations.engine_environment``). The measured engine carries no fault seam.

A run with no mutation checks needs no QA engine (``--no-fault-engine``, or ``TT_MUTATIONS=0``, the CI action's own
decision for the run): it is not built, and ``fault_engine=`` and ``fault_cli=`` name the measured binaries, which is what
``terminal_truth_parallel`` uses when it is given no QA engine. Only a mutation check runs on them, and one run on an
engine without the seam fails, because its defect never fires. On the CI runner the QA build was 83 to 107 s of every run
that changed the engine, mutation checks or not (2026-11-24, four runs).

With --cache, a build of the same inputs (``terminal_truth_mutations.engine_inputs_digest``: the engine sources and what
they embed, the build wrapper, the compiler, the flags, the release version) is copied from there instead: on the CI
runner the build is two minutes of every run, and seven pushes in ten do not touch the engine (2026-11-13, 53 of 175
commits in 14 days did). The cache holds one set of inputs; the measured engine and the QA engine are kept apart in it, so
a run that built only the first leaves it for the next, which then builds only the QA engine. The cargo target lives in
the cache too: a fresh runner checkout otherwise recompiled every dependency on each miss (28 to 36 s of it on the CI
runner, 2026-11-24), when only the engine crate changed. Prints ``engine=``, ``cli=``, ``fault_engine=``, ``fault_cli=``,
``fault=built|cached|skipped`` and ``cache=hit|miss|off`` lines.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from zerg.qa import terminal_truth_mutations as mutations

REPO_ROOT = mutations.REPO_ROOT
FLAGS = "--profile ci --bin longhouse-engine --bin longhouse"
FAULT_FLAGS = mutations.BUILD_FLAGS  # the same build with the negative-control seams compiled in
FAULT_DIR = "fault"
# A cached cargo target is dropped and started again above this size. A `ci` build of the engine is about 0.5 GiB; what
# grows it is a dependency bump leaving the old versions' artifacts behind.
TARGET_KEEP_BYTES = 4 * 1024**3


def cache_entry(cache: Path) -> Path:
    return cache / mutations.engine_inputs_digest(flags=f"{FLAGS} | {FAULT_FLAGS}")[:24]


def cache_target(cache: Path) -> Path:
    """The cargo target kept with the cache, one per checkout path (a target records the checkout that made it)."""

    return cache / f"cargo-target-{hashlib.sha256(str(REPO_ROOT).encode()).hexdigest()[:8]}"


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _bounded(target: Path) -> Path:
    if target.is_dir() and _size(target) > TARGET_KEEP_BYTES:
        print(f"cargo target {target} is over {TARGET_KEEP_BYTES >> 30} GiB; starting it again", file=sys.stderr)
        shutil.rmtree(target, ignore_errors=True)
    return target


def _cargo(*args: str, target: Path | None = None, capture: bool = False) -> str:
    extra = {"LONGHOUSE_CARGO_TARGET_DIR": str(target)} if target is not None else {}
    done = subprocess.run(
        [sys.executable, "scripts/build/cargo.py", *args], cwd=REPO_ROOT, env=mutations._build_env(**extra), check=True,
        stdout=subprocess.PIPE if capture else sys.stderr, stderr=sys.stderr, text=True,
    )  # fmt: skip
    return (done.stdout or "").strip()


def build(flags: str = FLAGS, target: Path | None = None) -> dict[str, Path]:
    identity = REPO_ROOT / ".build" / "build-identity.json"
    # engine/build.rs requires the checkout-scoped identity before any cargo invocation.
    subprocess.run(
        [sys.executable, "scripts/build/generate_build_identity.py", "--output", str(identity)], cwd=REPO_ROOT, check=True,
        env=mutations._build_env(**{k: v for k, v in os.environ.items() if k.startswith("GITHUB_")}), stdout=sys.stderr, stderr=sys.stderr,
    )  # fmt: skip
    _cargo("exec", "--", "build", "--manifest-path", str(REPO_ROOT / "engine" / "Cargo.toml"), *flags.split(), target=target)
    return {name: Path(_cargo("artifact", "--profile", "ci", "--bin", name, target=target, capture=True)) for name in mutations.BINARIES}


def _copy_set(source: dict[str, Path] | Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in mutations.BINARIES:
        shutil.copy2((source / name) if isinstance(source, Path) else source[name], destination / name)


def _complete(directory: Path) -> bool:
    return all((directory / name).is_file() for name in mutations.BINARIES)


def provide(out: Path, cache: Path | None, *, fault: bool = True) -> tuple[str, str]:
    """Put the measured binaries in ``out`` and, with ``fault``, the QA binaries in ``out/fault``.

    Returns the cache state ("hit", "miss" or "off") and what happened to the QA engine ("built", "cached" or "skipped").
    """

    out.mkdir(parents=True, exist_ok=True)
    entry = cache_entry(cache) if cache else None
    target = None if cache is None else cache_target(cache)
    if target is not None and not (entry is not None and _complete(entry) and (not fault or _complete(entry / FAULT_DIR))):
        target = _bounded(target)  # something will be built
    built_measured = built_fault = False
    # Both builds write the one cargo artifact path, so each is copied out before the next starts. The second recompiles
    # the engine crate only: the feature is empty, so no dependency changes.
    if entry is not None and _complete(entry):
        _copy_set(entry, out)
    else:
        _copy_set(build(target=target), out)
        built_measured = True
    fault_state = "skipped"
    if fault:
        if entry is not None and _complete(entry / FAULT_DIR):
            _copy_set(entry / FAULT_DIR, out / FAULT_DIR)
            fault_state = "cached"
        else:
            _copy_set(build(FAULT_FLAGS, target=target), out / FAULT_DIR)
            built_fault, fault_state = True, "built"
    if entry is None or cache is None:
        return "off", fault_state
    if not (built_measured or built_fault):
        return "hit", fault_state
    try:
        cache.mkdir(parents=True, exist_ok=True)
        staging = cache / f".{entry.name}.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        if built_measured:
            # A new entry: what this run has, the QA engine too when it has one. A reader sees a whole entry or none.
            _copy_set(out, staging)
            if fault:
                _copy_set(out / FAULT_DIR, staging / FAULT_DIR)
            shutil.rmtree(entry, ignore_errors=True)
            staging.rename(entry)
        else:
            # The measured engine came from the entry; add the QA engine to it the same way.
            _copy_set(out / FAULT_DIR, staging)
            shutil.rmtree(entry / FAULT_DIR, ignore_errors=True)
            staging.rename(entry / FAULT_DIR)
        for other in cache.iterdir():
            if other != entry and other.is_dir() and mutations.re.fullmatch(r"\.?[0-9a-f]{24}(\.\d+)?", other.name):
                shutil.rmtree(other, ignore_errors=True)
    except OSError as exc:
        print(f"::warning::engine build not cached: {exc}", file=sys.stderr)
    return "miss", fault_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="directory that receives longhouse-engine and longhouse")
    parser.add_argument("--cache", type=Path, help="directory that keeps the build between runs")
    parser.add_argument(
        "--no-fault-engine",
        dest="fault",
        action="store_false",
        default=os.environ.get("TT_MUTATIONS") != "0",
        help="build no QA engine: the run has no mutation checks (the default when TT_MUTATIONS=0)",
    )
    args = parser.parse_args(argv)
    out = args.out.resolve()
    state, fault_state = provide(out, args.cache, fault=args.fault)
    fault_dir = out / FAULT_DIR if args.fault else out
    for directory in {out, fault_dir}:
        for name in mutations.BINARIES:
            if not os.access(directory / name, os.X_OK):
                parser.error(f"{directory / name} is not an executable")
    print(
        f"engine={out / mutations.BINARIES[0]}\ncli={out / mutations.BINARIES[1]}\n"
        f"fault_engine={fault_dir / mutations.BINARIES[0]}\nfault_cli={fault_dir / mutations.BINARIES[1]}\n"
        f"fault={fault_state}\ncache={state}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
