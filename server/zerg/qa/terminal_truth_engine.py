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
a run that built only the first leaves it for the next, which then builds only the QA engine.

A miss compiles in a cargo target of its own under the checkout, not in the cache: the cache sits on the CI host's shared
disk, and building there wrote about 600 MB to it on every miss (2026-11-25). The cache keeps one copy of that target,
named by what its compiled dependencies are made of (``dependencies_key``: the registry packages in Cargo.lock, the
manifest without the engine's version, the compiler, the flags, the checkout path). A miss starts from that copy, so it
recompiles the engine crate and not every dependency (28 to 36 s of each miss on the CI runner, 2026-11-24), and writes it
back only when the key has none yet: an engine change writes the two binaries, and a dependency or compiler change the
target once. Prints ``engine=``, ``cli=``, ``fault_engine=``, ``fault_cli=``, ``fault=built|cached|skipped``,
``cache=hit|miss|off`` and ``cache_written_bytes=`` lines.
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
# The cargo target a build with --cache compiles in. The checkout is on the runner's own volume; the cache is shared.
LOCAL_TARGET = REPO_ROOT / ".build" / "terminal-truth-cargo-target"
TARGET_PREFIX = "cargo-target-"


def cache_entry(cache: Path) -> Path:
    return cache / mutations.engine_inputs_digest(flags=f"{FLAGS} | {FAULT_FLAGS}")[:24]


def _without_package_version(manifest: str) -> str:
    """Cargo.toml without ``version =`` in its [package] table, which every release bumps and no dependency depends on."""

    lines, section = [], ""
    for line in manifest.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
        elif section == "[package]" and mutations.re.match(r"version\s*=", stripped):
            continue
        lines.append(line)
    return "\n".join(lines)


def dependencies_key(repo_root: Path = REPO_ROOT) -> str:
    """What the compiled dependencies in a cargo target are made of, and not the engine crate's own sources or version.

    Cargo.lock's packages that come from a registry or git (a path package, the engine itself, carries the release
    version), the manifest without that version, the compiler, both sets of build flags and the checkout path a target
    records (scripts/build/cargo.py refuses a target another checkout made).
    """

    lock = (repo_root / "engine" / "Cargo.lock").read_text()
    packages = [block for block in lock.split("[[package]]") if "\nsource = " in block]
    manifest = _without_package_version((repo_root / "engine" / "Cargo.toml").read_text())
    rustc = subprocess.run(["rustc", "--version"], capture_output=True, text=True, check=False, env=mutations._build_env()).stdout.strip()
    text = "\0".join(["".join(packages), manifest, rustc, FLAGS, FAULT_FLAGS, str(repo_root)])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def cache_target(cache: Path) -> Path:
    """The cache's one copy of a cargo target whose dependencies are compiled, named by ``dependencies_key``."""

    return cache / f"{TARGET_PREFIX}{dependencies_key()}"


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


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


def _seed(target: Path, cache: Path) -> str:
    """Start the local cargo target from the cache's copy; returns "kept" (already there), "seeded" or "cold"."""

    if target.is_dir():
        return "kept"
    kept = cache_target(cache)
    if not kept.is_dir():
        return "cold"
    staging = target.with_name(f".{target.name}.{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(kept, staging, symlinks=True)  # copy2 keeps the mtimes cargo's fingerprints compare
    staging.rename(target)
    return "seeded"


def _keep_target(target: Path, cache: Path) -> int:
    """Copy the local cargo target into the cache when no copy has these dependencies; returns the bytes written."""

    kept = cache_target(cache)
    written = 0
    if not kept.is_dir():
        staging = cache / f".{kept.name}.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(target, staging, symlinks=True)
        written = _size(staging)
        staging.rename(kept)
    for other in cache.iterdir():
        if other != kept and other.is_dir() and other.name.lstrip(".").startswith(TARGET_PREFIX):
            shutil.rmtree(other, ignore_errors=True)
    return written


def provide(out: Path, cache: Path | None, *, fault: bool = True) -> tuple[str, str, int]:
    """Put the measured binaries in ``out`` and, with ``fault``, the QA binaries in ``out/fault``.

    Returns the cache state ("hit", "miss" or "off"), what happened to the QA engine ("built", "cached" or "skipped") and
    the bytes this call wrote into the cache.
    """

    out.mkdir(parents=True, exist_ok=True)
    entry = cache_entry(cache) if cache else None
    target = None
    if cache is not None and not (entry is not None and _complete(entry) and (not fault or _complete(entry / FAULT_DIR))):
        target = LOCAL_TARGET  # something will be built
        try:
            print(f"cargo target: {_seed(target, cache)} ({target})", file=sys.stderr)
        except OSError as exc:
            print(f"::warning::cargo target not taken from the cache: {exc}", file=sys.stderr)
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
        return "off", fault_state, 0
    if not (built_measured or built_fault):
        return "hit", fault_state, 0
    written = 0
    try:
        cache.mkdir(parents=True, exist_ok=True)
        staging = cache / f".{entry.name}.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        if built_measured:
            # A new entry: what this run has, the QA engine too when it has one. A reader sees a whole entry or none.
            _copy_set(out, staging)
            if fault:
                _copy_set(out / FAULT_DIR, staging / FAULT_DIR)
            written += _size(staging)
            shutil.rmtree(entry, ignore_errors=True)
            staging.rename(entry)
        else:
            # The measured engine came from the entry; add the QA engine to it the same way.
            _copy_set(out / FAULT_DIR, staging)
            written += _size(staging)
            shutil.rmtree(entry / FAULT_DIR, ignore_errors=True)
            staging.rename(entry / FAULT_DIR)
        for other in cache.iterdir():
            if other != entry and other.is_dir() and mutations.re.fullmatch(r"\.?[0-9a-f]{24}(\.\d+)?", other.name):
                shutil.rmtree(other, ignore_errors=True)
        if target is not None and target.is_dir():
            written += _keep_target(target, cache)
    except OSError as exc:
        print(f"::warning::engine build not cached: {exc}", file=sys.stderr)
    return "miss", fault_state, written


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
    state, fault_state, written = provide(out, args.cache, fault=args.fault)
    fault_dir = out / FAULT_DIR if args.fault else out
    for directory in {out, fault_dir}:
        for name in mutations.BINARIES:
            if not os.access(directory / name, os.X_OK):
                parser.error(f"{directory / name} is not an executable")
    print(
        f"engine={out / mutations.BINARIES[0]}\ncli={out / mutations.BINARIES[1]}\n"
        f"fault_engine={fault_dir / mutations.BINARIES[0]}\nfault_cli={fault_dir / mutations.BINARIES[1]}\n"
        f"fault={fault_state}\ncache={state}\ncache_written_bytes={written}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
