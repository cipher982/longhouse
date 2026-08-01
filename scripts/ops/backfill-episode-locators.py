#!/usr/bin/env python3
"""Backfill start positions for episodes embedded before the locator column existed.

Those episodes report unavailable evidence on semantic recall, because a
clean-message index cannot be resolved to a transcript position by anything that
does not reproduce the embedding sanitizer. searchd can: it already holds every
field that projection reads, so this needs no model call and no catalog read.

Run --verify first. It recomputes locators for episodes that already have one
and reports whether the derivation agrees with what the projector wrote. If it
disagrees, the ordering assumption is wrong and a write pass would fill the
corpus with plausible-looking wrong positions, which is worse than the honest
"unavailable" it replaces.

    python3 scripts/ops/backfill-episode-locators.py --verify
    python3 scripts/ops/backfill-episode-locators.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

from zerg.catalogd.client import CatalogClient  # noqa: E402

DEFAULT_SOCKET = Path("/data/.searchd/searchd.sock")
BATCH = 2000


async def _run(socket_path: Path, *, verify: bool, batch: int, max_batches: int) -> int:
    client = CatalogClient(socket_path, default_timeout_seconds=300.0)
    totals = {"scanned": 0, "resolved": 0, "unresolved": 0, "agreed": 0, "disagreed": 0}

    for round_index in range(max_batches):
        result = await client.call(
            "search.embedding.locators.backfill.v2",
            {"limit": batch, "verify": verify},
        )
        for key in totals:
            totals[key] += int(result.get(key) or 0)
        print(
            f"[{round_index + 1}] scanned={result.get('scanned')} "
            + (
                f"agreed={result.get('agreed')} disagreed={result.get('disagreed')}"
                if verify
                else f"resolved={result.get('resolved')}"
            )
            + f" unresolved={result.get('unresolved')}",
            flush=True,
        )
        if result.get("exhausted") is True:
            break
        # Verify never writes, so its query returns the same rows every time.
        # Without this the loop would re-scan the first page forever.
        if verify:
            break

    print(f"\ntotals: {totals}")
    if verify:
        if totals["disagreed"]:
            print("DISAGREEMENT — do not apply; the derivation does not match the projector.")
            return 1
        if not totals["agreed"]:
            print("no already-populated locators to compare against; apply is unproven.")
            return 2
        print("derivation agrees with every projector-written locator.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify", action="store_true", help="Compare against existing locators; write nothing.")
    mode.add_argument("--apply", action="store_true", help="Fill missing locators.")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--max-batches", type=int, default=1000)
    args = parser.parse_args()

    return asyncio.run(
        _run(args.socket, verify=args.verify, batch=args.batch, max_batches=args.max_batches)
    )


if __name__ == "__main__":
    raise SystemExit(main())
