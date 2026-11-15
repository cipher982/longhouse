"""The engine and CLI a terminal-truth run measures: built from this checkout, or taken from a cache of that exact build.

    make terminal-truth-engine ARGS="--out DIR [--cache DIR]"

Builds ``longhouse-engine`` and ``longhouse`` with the ``ci`` profile and copies both to --out, then the same two with the
``qa-fault-injection`` feature into ``--out/fault``: the one QA engine every mutation check switches a defect on in
(``terminal_truth_mutations.engine_environment``). The measured engine carries no fault seam. With --cache, a build of
the same inputs (``terminal_truth_mutations.engine_inputs_digest``: the engine sources and what they embed, the build
wrapper, the compiler, the flags, the release version) is copied from there instead: on the CI runner the build is two
minutes of every run, and seven pushes in ten do not touch the engine (2026-11-13, 53 of 175 commits in 14 days did).
The cache holds one build. Prints ``engine=``, ``cli=``, ``fault_engine=``, ``fault_cli=`` and ``cache=hit|miss|off`` lines.
"""

from __future__ import annotations

import argparse
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


def cache_entry(cache: Path) -> Path:
    return cache / mutations.engine_inputs_digest(flags=f"{FLAGS} | {FAULT_FLAGS}")[:24]


def _cargo(*args: str, capture: bool = False) -> str:
    done = subprocess.run(
        [sys.executable, "scripts/build/cargo.py", *args], cwd=REPO_ROOT, env=mutations._build_env(), check=True,
        stdout=subprocess.PIPE if capture else sys.stderr, stderr=sys.stderr, text=True,
    )  # fmt: skip
    return (done.stdout or "").strip()


def build(flags: str = FLAGS) -> dict[str, Path]:
    identity = REPO_ROOT / ".build" / "build-identity.json"
    # engine/build.rs requires the checkout-scoped identity before any cargo invocation.
    subprocess.run(
        [sys.executable, "scripts/build/generate_build_identity.py", "--output", str(identity)], cwd=REPO_ROOT, check=True,
        env=mutations._build_env(**{k: v for k, v in os.environ.items() if k.startswith("GITHUB_")}), stdout=sys.stderr, stderr=sys.stderr,
    )  # fmt: skip
    _cargo("exec", "--", "build", "--manifest-path", str(REPO_ROOT / "engine" / "Cargo.toml"), *flags.split())
    return {name: Path(_cargo("artifact", "--profile", "ci", "--bin", name, capture=True)) for name in mutations.BINARIES}


def _copy_set(source: dict[str, Path] | Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in mutations.BINARIES:
        shutil.copy2((source / name) if isinstance(source, Path) else source[name], destination / name)


def provide(out: Path, cache: Path | None) -> str:
    """Put both binaries in ``out``; returns "hit", "miss" or "off"."""

    out.mkdir(parents=True, exist_ok=True)
    entry = cache_entry(cache) if cache else None
    if entry is not None and all((entry / sub / name).is_file() for sub in ("", FAULT_DIR) for name in mutations.BINARIES):
        _copy_set(entry, out)
        _copy_set(entry / FAULT_DIR, out / FAULT_DIR)
        return "hit"
    # Both builds write the one cargo artifact path, so each is copied out before the next starts. The second recompiles
    # the engine crate only: the feature is empty, so no dependency changes.
    _copy_set(build(), out)
    _copy_set(build(FAULT_FLAGS), out / FAULT_DIR)
    if entry is None or cache is None:
        return "off"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        staging = cache / f".{entry.name}.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        _copy_set(out, staging)
        _copy_set(out / FAULT_DIR, staging / FAULT_DIR)
        shutil.rmtree(entry, ignore_errors=True)
        staging.rename(entry)  # a reader sees a whole entry or none
        for other in cache.iterdir():
            if other != entry and other.is_dir() and mutations.re.fullmatch(r"\.?[0-9a-f]{24}(\.\d+)?", other.name):
                shutil.rmtree(other, ignore_errors=True)
    except OSError as exc:
        print(f"::warning::engine build not cached: {exc}", file=sys.stderr)
    return "miss"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="directory that receives longhouse-engine and longhouse")
    parser.add_argument("--cache", type=Path, help="directory that keeps the build between runs")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    state = provide(out, args.cache)
    for directory in (out, out / FAULT_DIR):
        for name in mutations.BINARIES:
            if not os.access(directory / name, os.X_OK):
                parser.error(f"{directory / name} is not an executable")
    print(
        f"engine={out / mutations.BINARIES[0]}\ncli={out / mutations.BINARIES[1]}\n"
        f"fault_engine={out / FAULT_DIR / mutations.BINARIES[0]}\nfault_cli={out / FAULT_DIR / mutations.BINARIES[1]}\ncache={state}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
