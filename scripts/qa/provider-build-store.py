#!/usr/bin/env python3
"""Snapshot an already acquired provider closure into Longhouse's build store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))

from zerg.qa.provider_build_store import ProviderBuildStoreError  # noqa: E402
from zerg.qa.provider_build_store import materialize_staged_provider_build  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--platform")
    parser.add_argument("--architecture")
    parser.add_argument(
        "--closure-granularity",
        choices=("full_installed_tree", "single_asset"),
        required=True,
    )
    args = parser.parse_args(argv)
    try:
        build = materialize_staged_provider_build(
            provider=args.provider,
            version=args.version,
            source_root=args.source_root,
            entrypoint_relative=args.entrypoint,
            store_root=args.store_root,
            platform_name=args.platform,
            architecture=args.architecture,
            closure_granularity=args.closure_granularity,
        )
    except ProviderBuildStoreError as exc:
        parser.error(str(exc))
    print(json.dumps(build.to_evidence(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
