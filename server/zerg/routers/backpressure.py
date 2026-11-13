"""Retry-After hints for catalog backpressure.

catalogd serializes every write through one writer, so when it is unavailable
the honest answer is not only "503" but "503, come back in N seconds". The
Machine Agent already paces each request lane by ``Retry-After``
(``engine/src/shipping/client.rs``), and without the header every machine
retried on its own cadence into the same queue: one 148s writer hold on
2026-11-13 became 3,315 refused ingest batches and 807 refused storage writes in
75 minutes, each retried seconds later.

Keep the value short. It is a queue-drain hint, not an outage estimate: the
client caps it per lane and its own backlog limiter shrinks work underneath it.
"""

from __future__ import annotations

CATALOG_BACKPRESSURE_RETRY_AFTER_SECONDS = 5


def catalog_backpressure_headers(
    seconds: int = CATALOG_BACKPRESSURE_RETRY_AFTER_SECONDS,
) -> dict[str, str]:
    return {"Retry-After": str(seconds)}
