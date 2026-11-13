from pathlib import Path

from tests_lite._shard import _HEAVY_FILE_WEIGHTS
from tests_lite._shard import shard_for_path


def test_shards_partition_every_test_file_without_overlap_or_heavy_collision():
    tests_dir = Path(__file__).resolve().parent
    all_files = sorted(f"tests_lite/{p.name}" for p in tests_dir.glob("test_*.py"))
    assert all_files

    known_heavy = {path for path, _ in _HEAVY_FILE_WEIGHTS}
    assert "tests_lite/test_pi_family_helm_fake_lifecycle.py" in known_heavy

    for shard_count in (1, 2, 3, 4, 8):
        assigned = [shard_for_path(path, shard_count) for path in all_files]
        assert all(0 <= idx < shard_count for idx in assigned)
        buckets = {idx: {path for path, owner in zip(all_files, assigned) if owner == idx} for idx in range(shard_count)}
        reconstructed = set().union(*buckets.values())
        assert reconstructed == set(all_files)
        assert sum(len(bucket) for bucket in buckets.values()) == len(all_files)

    # Top two heaviest files must sit on distinct CI shards so xdist loadfile
    # stalls do not stack on a single 200s straggler shard.
    assert shard_for_path("tests_lite/test_pi_family_helm_fake_lifecycle.py", 3) != shard_for_path(
        "tests_lite/test_pi_family_turn_oracle.py", 3
    )
