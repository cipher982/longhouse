#!/usr/bin/env python3
"""Generate the provider-name-literal census.

Counting rule (provider-factory-coherence.md, "The provider census"): files
tracked by git with extension .py, .ts, .tsx, .rs, excluding paths containing
/generated/ or node_modules, that contain an exact quoted string literal
(single or double quotes, e.g. "codex" or 'codex') for two or more distinct
provider names. Provider names are the `provider:` entries in
schemas/managed_providers.yml.

This is not a duplication scanner. A file appearing here may be intentional
policy (a two-provider experiment, provider-specific historical storage
handling, a backfill rule) rather than something to consolidate. The census
exists so that claim can be checked instead of asserted.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "managed_providers.yml"
OUTPUT_PATH = ROOT / "docs" / "generated" / "provider_census.json"
EXTENSIONS = ("*.py", "*.ts", "*.tsx", "*.rs")
EXCLUDE_SUBSTRINGS = ("/generated/", "node_modules")


def _provider_names() -> list[str]:
    payload = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise SystemExit(f"{SCHEMA_PATH} must contain a YAML mapping with a top-level 'providers' list")
    names = [entry["provider"] for entry in payload["providers"] if isinstance(entry, dict) and "provider" in entry]
    if not names:
        raise SystemExit(f"no provider entries found under 'providers' in {SCHEMA_PATH}")
    return sorted(set(names))


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--"] + list(EXTENSIONS),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    files = result.stdout.splitlines()
    return [f for f in files if not any(marker in f for marker in EXCLUDE_SUBSTRINGS)]


def _literal_pattern(name: str) -> re.Pattern[str]:
    return re.compile(r"""(['"])""" + re.escape(name) + r"""\1""")


def build_census() -> dict:
    providers = _provider_names()
    patterns = {name: _literal_pattern(name) for name in providers}
    entries = []
    for relpath in _tracked_files():
        path = ROOT / relpath
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hit = sorted(name for name, pattern in patterns.items() if pattern.search(text))
        if len(hit) >= 2:
            entries.append({"path": relpath, "providers": hit})
    entries.sort(key=lambda entry: entry["path"])
    return {
        "rule": (
            "git-tracked .py/.ts/.tsx/.rs files, excluding /generated/ and node_modules, "
            "containing an exact quoted literal for two or more distinct provider names "
            "from schemas/managed_providers.yml"
        ),
        "provider_names": providers,
        "file_count": len(entries),
        "files": entries,
    }


def render(census: dict) -> str:
    return json.dumps(census, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Write the generated census artifact.")
    parser.add_argument(
        "--check", action="store_true", help="Fail if the generated census differs from the checked-in artifact."
    )
    args = parser.parse_args()

    rendered = render(build_census())
    current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""

    if args.check:
        if rendered != current:
            print(
                f"{OUTPUT_PATH} is out of date; run scripts/generate_provider_census.py --write",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.write:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(rendered, encoding="utf-8")
        return 0

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
