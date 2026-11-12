#!/usr/bin/env python3
"""Install the provider CLIs the terminal-truth lane uses at the exact versions in terminal-truth-providers.toml, and verify them.

  terminal_truth_providers.py install --providers codex,pi --root DIR --home DIR
  terminal_truth_providers.py verify  --providers codex,pi
  terminal_truth_providers.py list

`install` puts npm packages under ROOT/node_modules/.bin and omp and agy under HOME/.local/bin. `verify` runs each
binary found on PATH with --version and exits 1 unless the first line is exactly the pinned `reports` value. Standard
library only. Linux x86_64 is the lane's platform (the agy tarball is the Linux one).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the lane runs Python 3.12
    sys.exit("Python 3.11 or newer is required")

PINS = Path(__file__).with_name("terminal-truth-providers.toml")


def load_pins() -> dict[str, dict[str, str]]:
    return tomllib.loads(PINS.read_text())


def select(pins: dict[str, dict[str, str]], names: str) -> list[str]:
    wanted = list(pins) if names == "all" else [n for n in names.split(",") if n]
    unknown = [n for n in wanted if n not in pins]
    if unknown:
        raise SystemExit(f"no pinned version for: {', '.join(unknown)} (pinned: {', '.join(pins)})")
    return wanted


def clean_env(home: Path) -> dict[str, str]:
    """Installers are untrusted network inputs: a disposable HOME and no credentials."""

    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LANG": "C.UTF-8"}
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    return env


def fetch(url: str, target: Path) -> None:
    """curl, not urllib: omp.sh answers Python's default user agent with 403."""

    subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", str(target), url], check=True)


def install(pins: dict[str, dict[str, str]], names: list[str], root: Path, home: Path) -> None:
    env = clean_env(home)
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    npm = [f"{pins[n]['package']}@{pins[n]['version']}" for n in names if pins[n]["kind"] == "npm"]
    if npm:
        subprocess.run(["npm", "install", "--prefix", str(root), "--no-save", "--save-exact", *npm], check=True, env=env)
    for name in names:
        pin = pins[name]
        if pin["kind"] == "omp":
            with tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp) / "omp-install.sh"
                fetch("https://omp.sh/install", script)
                subprocess.run(["sh", str(script), "--binary", "--ref", f"v{pin['version']}"], check=True, env=env)
        elif pin["kind"] == "agy":
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "agy.tar.gz"
                fetch(pin["url"], archive)
                digest = hashlib.sha512(archive.read_bytes()).hexdigest()
                if digest != pin["sha512"]:
                    raise SystemExit(f"antigravity {pin['version']}: sha512 {digest[:16]}... does not match the pinned {pin['sha512'][:16]}...")
                with tarfile.open(archive) as tar:
                    member = tar.getmember("antigravity")
                    source = tar.extractfile(member)
                    assert source is not None
                    target = bin_dir / pin["binary"]
                    with target.open("wb") as out:
                        shutil.copyfileobj(source, out)
                    target.chmod(0o755)
            # the vendor installer's last step; it absorbs non-fatal exits
            subprocess.run([str(bin_dir / pin["binary"]), "install", "--dir", str(bin_dir)], env=env, check=False)


def first_line(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        return "<not installed>"
    out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    return out.splitlines()[0].strip() if out else "<no output>"


def verify(pins: dict[str, dict[str, str]], names: list[str]) -> int:
    bad = 0
    for name in names:
        got = first_line(pins[name]["binary"])
        ok = got == pins[name]["reports"]
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {name:<12} pinned {pins[name]['reports']!r:<26} got {got!r}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--providers", default="all")
        if name == "install":
            p.add_argument("--root", type=Path, required=True)
            p.add_argument("--home", type=Path, required=True)
    sub.add_parser("list")
    args = parser.parse_args(argv)
    pins = load_pins()
    if args.command == "list":
        for name, pin in pins.items():
            print(f"{name}\t{pin['version']}")
        return 0
    names = select(pins, args.providers)
    if args.command == "install":
        install(pins, names, args.root, args.home)
        return 0
    return verify(pins, names)


if __name__ == "__main__":
    raise SystemExit(main())
