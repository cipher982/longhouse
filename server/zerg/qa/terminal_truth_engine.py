"""The engine and CLI a terminal-truth run measures: built from this checkout, or taken from a cache of that exact build.

    make terminal-truth-engine ARGS="--out DIR [--cache DIR]"

Builds ``longhouse-engine`` and ``longhouse`` with the ``ci`` profile and copies both to --out. With --cache, a build of
the same inputs (``terminal_truth_mutations.engine_inputs_digest``: the engine sources and what they embed, the build
wrapper, the compiler, the flags, the release version) is copied from there instead: on the CI runner the build is two
minutes of every run, and seven pushes in ten do not touch the engine (2026-11-13, 53 of 175 commits in 14 days did).
The cache holds one build. Prints ``engine=``, ``cli=`` and ``cache=hit|miss|off`` lines.
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


def cache_entry(cache: Path) -> Path:
    return cache / mutations.engine_inputs_digest(flags=FLAGS)[:24]


def _cargo(*args: str, capture: bool = False) -> str:
    done = subprocess.run(
        [sys.executable, "scripts/build/cargo.py", *args], cwd=REPO_ROOT, env=mutations._build_env(), check=True,
        stdout=subprocess.PIPE if capture else sys.stderr, stderr=sys.stderr, text=True,
    )  # fmt: skip
    return (done.stdout or "").strip()


def build() -> dict[str, Path]:
    identity = REPO_ROOT / ".build" / "build-identity.json"
    # engine/build.rs requires the checkout-scoped identity before any cargo invocation.
    subprocess.run(
        [sys.executable, "scripts/build/generate_build_identity.py", "--output", str(identity)], cwd=REPO_ROOT, check=True,
        env=mutations._build_env(**{k: v for k, v in os.environ.items() if k.startswith("GITHUB_")}), stdout=sys.stderr, stderr=sys.stderr,
    )  # fmt: skip
    _cargo("exec", "--", "build", "--manifest-path", str(REPO_ROOT / "engine" / "Cargo.toml"), *FLAGS.split())
    return {name: Path(_cargo("artifact", "--profile", "ci", "--bin", name, capture=True)) for name in mutations.BINARIES}


def provide(out: Path, cache: Path | None) -> str:
    """Put both binaries in ``out``; returns "hit", "miss" or "off"."""

    out.mkdir(parents=True, exist_ok=True)
    entry = cache_entry(cache) if cache else None
    if entry is not None and all((entry / name).is_file() for name in mutations.BINARIES):
        for name in mutations.BINARIES:
            shutil.copy2(entry / name, out / name)
        return "hit"
    for name, artifact in build().items():
        shutil.copy2(artifact, out / name)
    if entry is None or cache is None:
        return "off"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        staging = cache / f".{entry.name}.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        for name in mutations.BINARIES:
            shutil.copy2(out / name, staging / name)
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
    for name in mutations.BINARIES:
        if not os.access(out / name, os.X_OK):
            parser.error(f"{out / name} is not an executable")
    print(f"engine={out / mutations.BINARIES[0]}\ncli={out / mutations.BINARIES[1]}\ncache={state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
