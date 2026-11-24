"""Split the suite across CI jobs by test file: LONGHOUSE_TEST_SHARD=i/n.

A hosted CI runner has four slow vCPUs, and the whole suite on one of them
takes ~5 minutes even under xdist. Runners are free and parallel, so CI runs
n jobs: known heavy test files are greedily balanced (LPT) across shards and
the remaining files hash to an index. Whole files stay together because
module-scoped fixtures assume it. Every xdist worker in a job keeps the
exact same deterministic partition.

Unset (every local run), nothing is deselected.
"""

import os
import zlib


# Known slow files and the seconds their tests take in all, so LPT bin-packing keeps them off the same CI
# shard instead of letting filename CRC32 stack them. Listed heaviest first (LPT places them in this order).
# Measured 2026-11-24: the whole suite in the test container, 8 xdist workers, sum of --durations over 0.3 s.
_HEAVY_FILE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("tests_lite/test_universal_agent_harness.py", 199.0),
    ("tests_lite/test_pi_family_turn_oracle.py", 167.0),
    ("tests_lite/test_pi_family_helm_fake_lifecycle.py", 164.0),
    ("tests_lite/test_provider_observed_install_qualification.py", 145.0),
    ("tests_lite/test_provider_harness_qualification.py", 81.0),
    ("tests_lite/test_provider_resume_factory.py", 61.0),
    ("tests_lite/test_scripted_model_pacing.py", 20.0),
    ("tests_lite/test_agents_storage_v2.py", 14.0),
    ("tests_lite/test_omp_qa_producers.py", 12.0),
    ("tests_lite/test_render_shadow.py", 11.0),
    ("tests_lite/test_catalogd_supervisor.py", 9.0),
    ("tests_lite/test_terminal_truth_kinds.py", 8.0),
)


def _heavy_shard_map(count: int) -> dict[str, int]:
    loads = [0.0] * count
    mapping: dict[str, int] = {}
    for path, weight in _HEAVY_FILE_WEIGHTS:
        target = min(range(count), key=lambda idx: (loads[idx], idx))
        mapping[path] = target
        loads[target] += weight
    return mapping


def _shard() -> tuple[int, int] | None:
    raw = os.environ.get("LONGHOUSE_TEST_SHARD", "").strip()
    if not raw:
        return None
    index, _, count = raw.partition("/")
    index_n, count_n = int(index), int(count)
    if not 0 <= index_n < count_n:
        raise ValueError(f"LONGHOUSE_TEST_SHARD must be i/n with 0 <= i < n, got {raw!r}")
    return index_n, count_n


def shard_for_path(path: str, count: int) -> int:
    heavy = _heavy_shard_map(count)
    if path in heavy:
        return heavy[path]
    return zlib.crc32(path.encode()) % count


def _keeps(path: str, shard: tuple[int, int]) -> bool:
    index, count = shard
    return shard_for_path(path, count) == index


def pytest_ignore_collect(collection_path, config):
    # Skip other shards' test modules before importing them: collection
    # imports every module in every xdist worker, and a worker that imports
    # all of them to keep a third spends most of its collection on the rest.
    shard = _shard()
    if shard is None or not (collection_path.name.startswith("test_") and collection_path.suffix == ".py"):
        return None
    try:
        path = collection_path.relative_to(config.rootpath).as_posix()
    except ValueError:
        return None
    return True if not _keeps(path, shard) else None


def pytest_collection_modifyitems(config, items):
    shard = _shard()
    if shard is None:
        return
    keep, drop = [], []
    for item in items:
        path = item.nodeid.split("::", 1)[0]
        (keep if _keeps(path, shard) else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_report_header(config):
    shard = _shard()
    if shard is not None:
        return f"shard {shard[0]}/{shard[1]} (by test file)"
