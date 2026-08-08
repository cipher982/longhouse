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

Every fetched artifact is verified against providers.lock.json when a matching
entry exists (same kind + pinned version): a sha256 mismatch is a hard fail.

Usage:
  uv run fetch.py [provider ...]        # default: all providers
  uv run fetch.py --only claude         # just claude (+ srt, always)
"""

from __future__ import annotations

import argparse
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
            tf.extract(member, root, filter="data")
    return {
        "kind": "release",
        "url": url,
        "requested_version": version,
        "resolved_version": version,
        "archive_sha256": sha,
    }


HASH_KEYS = {"npm": "tarball_sha256", "release": "archive_sha256"}


def verify_against_lock(name: str, entry: dict, prior: dict | None) -> None:
    """Hard-fail when the lockfile pins this exact version with another hash."""
    if not prior:
        return
    if prior.get("kind") != entry["kind"]:
        return
    if prior.get("requested_version") != entry["requested_version"]:
        return  # intentional version bump; lock entry will be rewritten
    key = HASH_KEYS[entry["kind"]]
    expected, actual = prior.get(key), entry[key]
    if expected and expected != actual:
        raise SystemExit(
            f"[{name}] sha256 MISMATCH for pinned {entry['requested_version']}:\n"
            f"  lock:    {expected}\n"
            f"  fetched: {actual}\n"
            f"refusing to proceed — artifact does not match providers.lock.json")
    print(f"[{name}] sha256 verified against lock", file=sys.stderr)


def verify_cached(name: str, root: Path, prior: dict) -> None:
    """Re-hash the retained artifact of an already-fetched provider."""
    key = HASH_KEYS.get(prior.get("kind", ""))
    if not key or not prior.get(key):
        return
    if prior["kind"] == "npm":
        art = root / ".tarball" / prior.get("tarball", "")
    else:
        art = root / ".archive.tar.gz"
    if not art.is_file():
        return  # artifact not retained; nothing to hash
    actual = sha256_file(art)
    if actual != prior[key]:
        raise SystemExit(
            f"[{name}] sha256 MISMATCH for cached artifact {art.name}:\n"
            f"  lock:    {prior[key]}\n"
            f"  on-disk: {actual}\n"
            f"refusing to proceed — delete {root} and refetch")
    print(f"[{name}] cached artifact sha256 verified against lock", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*",
                    help="providers/runtime components to fetch (default: all)")
    ap.add_argument("--only", action="append", default=[], metavar="PROV",
                    help="fetch only this provider plus srt (repeatable)")
    args = ap.parse_args()

    manifest = yaml.safe_load((SCRIPT_DIR / "providers.yml").read_text())
    providers = dict(manifest["providers"])
    providers.update(manifest.get("runtime") or {})
    if args.only:
        # srt is the enforcement layer; it always comes along.
        wanted = list(dict.fromkeys(args.only + ["srt"]))
    else:
        wanted = args.names or list(providers)
    unknown = [n for n in wanted if n not in providers]
    if unknown:
        raise SystemExit(f"unknown provider(s): {', '.join(unknown)} "
                         f"(known: {', '.join(providers)})")

    lock = json.loads(LOCK_PATH.read_text()) if LOCK_PATH.exists() else {}
    for name in wanted:
        prov = providers[name]
        spec = prov["source"]
        version = spec["version"]
        root = SANDBOX / name / version
        if (root / prov["bin"]).exists() and name in lock:
            print(f"[{name}] already fetched at {root}", file=sys.stderr)
            verify_cached(name, root, lock[name])
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
        verify_against_lock(name, entry, lock.get(name))
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
