from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from zerg.services.session_continuity import ship_session_to_zerg


def main() -> int:
    parser = argparse.ArgumentParser(description="Ship the newest Claude session for a workspace to Longhouse")
    parser.add_argument("workspace", help="Workspace path used for the Claude session")
    parser.add_argument("claude_config_dir", help="Claude config dir containing projects/<encoded cwd>/*.jsonl")
    parser.add_argument("--commis-id", default="provider-smoke")
    parser.add_argument("--continuation-kind")
    parser.add_argument("--origin-label")
    args = parser.parse_args()

    shipped = asyncio.run(
        ship_session_to_zerg(
            workspace_path=Path(args.workspace),
            claude_config_dir=Path(args.claude_config_dir),
            commis_id=args.commis_id,
            continuation_kind=args.continuation_kind,
            origin_label=args.origin_label,
        )
    )
    if not shipped:
        raise SystemExit(1)
    print(shipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
