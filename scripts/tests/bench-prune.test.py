"""bench.sh removes mirrors nobody has synced for BENCH_MIRROR_DAYS, and only those."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "scripts/ops/bench.sh"
DAY = 86400


def age(path: Path, days: int) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


class PruneTests(unittest.TestCase):
    def prune(self, home: Path, mirror: str, days: str = "14") -> subprocess.CompletedProcess:
        # A fake ssh runs the remote command here, from this HOME as ssh would from the bench's.
        fake = home / "fake-bin"
        fake.mkdir()
        (fake / "ssh").write_text('#!/bin/bash\ncd "$HOME" && exec bash -c "${@: -1}"\n')
        (fake / "ssh").chmod(0o755)
        env = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(home), "BENCH_DIR": mirror, "BENCH_MIRROR_DAYS": days}
        return subprocess.run(["bash", str(BENCH), "prune"], env=env, capture_output=True, text=True)

    def test_only_mirrors_unsynced_for_the_limit_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bench = home / "bench"
            for name in ("current", "synced-recently", "stamped-old", "predates-stamp-old", "predates-stamp-new"):
                (bench / name / "src").mkdir(parents=True)
                (bench / name / "src" / "file").write_text("x")
            (bench / "synced-recently" / ".bench-used").touch()
            (bench / "stamped-old" / ".bench-used").touch()
            age(bench / "stamped-old" / ".bench-used", 20)
            age(bench / "predates-stamp-old", 30)
            age(bench / "predates-stamp-new", 3)
            age(bench / "current", 90)  # the mirror being synced is stamped first, so its age does not matter
            result = self.prune(home, "bench/current")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                sorted(p.name for p in bench.iterdir()),
                ["current", "predates-stamp-new", "synced-recently"],
            )
            self.assertIn("pruned", result.stderr)
            self.assertTrue((bench / "current" / ".bench-used").exists())

    def test_a_long_limit_removes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "bench" / "current").mkdir(parents=True)
            (home / "bench" / "old").mkdir()
            age(home / "bench" / "old", 400)
            self.prune(home, "bench/current", days="9999")
            self.assertTrue((home / "bench" / "old").exists())


if __name__ == "__main__":
    unittest.main()
