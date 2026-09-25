"""Split the suite across CI jobs by test file: LONGHOUSE_TEST_SHARD=i/n.

A hosted CI runner has four slow vCPUs, and the whole suite on one of them
takes ~5 minutes even under xdist. Runners are free and parallel, so CI runs
n jobs and each keeps only the files that hash to its index. Whole files stay
together because module-scoped fixtures and --dist=loadfile assume it. The
hash is of the path, so every xdist worker in a job keeps the same files.

Unset (every local run), nothing is deselected.
"""

import os
import zlib


def _shard() -> tuple[int, int] | None:
    raw = os.environ.get("LONGHOUSE_TEST_SHARD", "").strip()
    if not raw:
        return None
    index, _, count = raw.partition("/")
    index_n, count_n = int(index), int(count)
    if not 0 <= index_n < count_n:
        raise ValueError(f"LONGHOUSE_TEST_SHARD must be i/n with 0 <= i < n, got {raw!r}")
    return index_n, count_n


def pytest_collection_modifyitems(config, items):
    shard = _shard()
    if shard is None:
        return
    index, count = shard
    keep, drop = [], []
    for item in items:
        path = item.nodeid.split("::", 1)[0]
        (keep if zlib.crc32(path.encode()) % count == index else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_report_header(config):
    shard = _shard()
    if shard is not None:
        return f"shard {shard[0]}/{shard[1]} (by test file)"
