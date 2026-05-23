"""Small local-health entrypoint for the macOS menu bar refresh loop."""

from __future__ import annotations

import argparse
import json
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="longhouse-local-health",
        description="Emit a local Longhouse health snapshot.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fast", action="store_true", help="Use the menu-bar fast path.")
    mode.add_argument("--deep", action="store_true", help="Use the deep diagnostic path.")
    parser.add_argument(
        "--claude-dir",
        help="Claude config directory override (maps to the sibling ~/.longhouse state root).",
    )
    return parser


def _collect(claude_dir: str | None, *, fast: bool) -> dict[str, object]:
    from zerg.services.local_health import collect_local_health
    from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home

    state_root = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None
    return collect_local_health(state_root, fast=fast)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        snapshot = _collect(args.claude_dir, fast=bool(args.fast and not args.deep))
    except Exception as exc:
        print(f"longhouse-local-health failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(snapshot, indent=2))
    else:
        print(f"{snapshot.get('headline', 'Longhouse status')} ({snapshot.get('health_state', '-')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
