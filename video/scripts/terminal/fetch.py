#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Fetch pinned provider CLIs into hermetic sandboxes.

Reads providers.yml, downloads each provider's pinned artifact into
.sandbox/<provider>/<version>/ (gitignored), and records the resolved version +
sha256 in providers.lock.json (committable).

npm sources: `npm pack pkg@version` (sha256 of the tarball we actually
installed), then `npm install --prefix <sandbox>` from that tarball.
release sources: curl the URL, sha256 the archive, extract.

Usage:
  uv run fetch.py [provider ...]        # default: all providers
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile

from datetime import datetime, timezone
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SANDBOX = SCRIPT_DIR / ".sandbox"
LOCK_PATH = SCRIPT_DIR / "providers.lock.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(argv)}", file=sys.stderr)
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kw)


def fetch_npm(name: str, spec: dict, root: Path) -> dict:
    pkg, version = spec["package"], spec["version"]
    tarball_dir = root / ".tarball"
    tarball_dir.mkdir(parents=True, exist_ok=True)
    out = run(["npm", "pack", f"{pkg}@{version}", "--pack-destination", str(tarball_dir)])
    tgz = tarball_dir / out.stdout.strip().splitlines()[-1]
    sha = sha256_file(tgz)
    run(["npm", "install", "--prefix", str(root), str(tgz),
         "--no-fund", "--no-audit", "--loglevel=error"])
    pkg_json = json.loads((root / "node_modules" / pkg / "package.json").read_text())
    return {
        "kind": "npm",
        "package": pkg,
        "requested_version": version,
        "resolved_version": pkg_json["version"],
        "tarball": tgz.name,
        "tarball_sha256": sha,
    }


def fetch_release(name: str, spec: dict, root: Path) -> dict:
    url, version = spec["url"], spec["version"]
    archive = root / ".archive.tar.gz"
    root.mkdir(parents=True, exist_ok=True)
    print(f"  GET {url}", file=sys.stderr)
    run(["curl", "-fSL", "--retry", "2", "-o", str(archive), url])
    sha = sha256_file(archive)
    strip = int(spec.get("strip_components", 0))
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            parts = Path(member.name).parts[strip:]
            if not parts:
                continue
            member.name = str(Path(*parts))
            tf.extract(member, root, filter="fully_trusted")
    return {
        "kind": "release",
        "url": url,
        "requested_version": version,
        "resolved_version": version,
        "archive_sha256": sha,
    }


def main() -> None:
    manifest = yaml.safe_load((SCRIPT_DIR / "providers.yml").read_text())
    providers = dict(manifest["providers"])
    providers.update(manifest.get("runtime") or {})
    wanted = sys.argv[1:] or list(providers)

    lock = json.loads(LOCK_PATH.read_text()) if LOCK_PATH.exists() else {}
    for name in wanted:
        prov = providers[name]
        spec = prov["source"]
        version = spec["version"]
        root = SANDBOX / name / version
        if (root / prov["bin"]).exists():
            print(f"[{name}] already fetched at {root}", file=sys.stderr)
            if name in lock:
                continue
        print(f"[{name}] fetching {spec.get('package', spec.get('url'))} {version}",
              file=sys.stderr)
        if root.exists():
            shutil.rmtree(root)
        if spec["kind"] == "npm":
            entry = fetch_npm(name, spec, root)
        elif spec["kind"] == "release":
            entry = fetch_release(name, spec, root)
        else:
            raise SystemExit(f"unknown source kind {spec['kind']!r} for {name}")
        bin_path = root / prov["bin"]
        if not bin_path.exists():
            raise SystemExit(f"[{name}] expected binary missing after fetch: {bin_path}")
        entry["bin"] = str(bin_path.relative_to(SANDBOX))
        entry["fetched_at"] = datetime.now(timezone.utc).isoformat()
        lock[name] = entry
        LOCK_PATH.write_text(json.dumps(lock, indent=2) + "\n")
        print(f"[{name}] ok -> {bin_path}", file=sys.stderr)

    print(f"lockfile: {LOCK_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
