#!/usr/bin/env python3
"""Ring locks (scripts/ops/ring_lock.py): one holder, refusal names it, expiry and dead holders reclaim."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "scripts" / "ops" / "ring_lock.py"
LIB = ROOT / "scripts" / "lib" / "ring-lock.sh"


class RingLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="longhouse-ring-lock-test-")
        self.dir = Path(self.tmp.name)
        inherited = {k: v for k, v in os.environ.items()
                     if k not in ("LONGHOUSE_MANAGED_SESSION_ID", "LONGHOUSE_SESSION_ID", "LONGHOUSE_CHANNEL_SESSION_ID")}
        self.env = {**inherited, "LONGHOUSE_RING_LOCK_DIR": str(self.dir), "LONGHOUSE_SESSION_ID": "sess-test"}

    def tearDown(self):
        self.tmp.cleanup()

    def lock(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(LOCK), *args], env=self.env, capture_output=True, text=True)

    def acquire(self, surface="production", pid=None, ttl=600, sha="a" * 40):
        return self.lock("acquire", surface, "--sha", sha, "--ttl", str(ttl), "--pid", str(pid or os.getpid()), "--op", "test op")

    def events(self) -> list[str]:
        path = self.dir / "events.jsonl"
        return [json.loads(line)["event"] for line in path.read_text().splitlines()] if path.exists() else []

    def dead_pid(self) -> int:
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def test_one_holder_and_the_refusal_names_it(self):
        first = self.acquire()
        self.assertEqual(first.returncode, 0, first.stderr)
        token = first.stdout.strip()
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        second = self.acquire(sha="b" * 40)
        self.assertEqual(second.returncode, 1)
        self.assertIn("ring-lock: REFUSED: production held by session sess-test (test op)", second.stderr)
        self.assertIn("target aaaaaaaaaaaa", second.stderr)
        self.assertEqual(self.lock("release", "production", "--token", token).returncode, 0)
        self.assertEqual(self.acquire().returncode, 0)
        self.assertEqual(self.events(), ["acquired", "refused", "released", "acquired"])

    def test_surfaces_are_independent(self):
        self.assertEqual(self.acquire("production").returncode, 0)
        self.assertEqual(self.acquire("dogfood-david010").returncode, 0)
        self.assertEqual(self.acquire("release").returncode, 0)

    def test_only_one_of_many_simultaneous_acquirers_wins(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.acquire().returncode, range(8)))
        self.assertEqual(sorted(results), [0] + [1] * 7)

    def test_an_expired_lock_is_reclaimed_and_the_reclaim_logged(self):
        self.assertEqual(self.acquire(ttl=1).returncode, 0)
        time.sleep(1.2)
        again = self.acquire(sha="c" * 40)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("reclaimed production from session sess-test (expired", again.stderr)
        self.assertEqual(self.events(), ["acquired", "reclaimed", "acquired"])

    def test_a_dead_holder_frees_the_lock_at_once(self):
        self.assertEqual(self.acquire(pid=self.dead_pid(), ttl=3600).returncode, 0)
        again = self.acquire()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("exited", again.stderr)

    def test_a_reused_pid_is_not_mistaken_for_the_holder(self):
        self.assertEqual(self.acquire(ttl=3600).returncode, 0)
        path = self.dir / "production.json"
        record = json.loads(path.read_text())
        record["pid_started"] = "Thu Jan  1 00:00:00 1970"  # the pid is alive, but it is not the process that took it
        path.write_text(json.dumps(record))
        again = self.acquire()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("pid was reused", again.stderr)

    def test_renew_extends_only_the_holders_lock_and_release_never_frees_someone_elses(self):
        token = self.acquire(ttl=1).stdout.strip()
        self.assertEqual(self.lock("renew", "production", "--token", token, "--ttl", "600", "--sha", "d" * 40).returncode, 0)
        time.sleep(1.2)
        self.assertEqual(self.acquire().returncode, 1)  # renewed past its first TTL
        record = json.loads((self.dir / "production.json").read_text())
        self.assertEqual(record["sha"], "d" * 40)
        wrong = self.lock("renew", "production", "--token", "0" * 32, "--ttl", "600")
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("no longer holds production", wrong.stderr)
        self.assertEqual(self.lock("release", "production", "--token", "0" * 32).returncode, 0)
        self.assertTrue((self.dir / "production.json").exists())

    def test_status_never_prints_the_token(self):
        token = self.acquire().stdout.strip()
        status = self.lock("status", "--json")
        self.assertEqual(status.returncode, 0)
        self.assertNotIn(token, status.stdout)
        [entry] = json.loads(status.stdout)
        self.assertEqual((entry["surface"], entry["session"], entry["reclaimable"]), ("production", "sess-test", None))
        self.assertIn("production held by", self.lock("status").stdout)

    def test_bad_arguments_are_errors_not_refusals(self):
        self.assertEqual(self.acquire(surface="Prod/../x").returncode, 2)
        self.assertEqual(self.acquire(ttl=0).returncode, 2)

    def test_the_shell_library_releases_in_its_trap_even_when_the_script_fails(self):
        script = self.dir / "holder.sh"
        script.write_text(f"""set -euo pipefail
ROOT={ROOT}
. {LIB}
trap lh_ring_lock_release EXIT
lh_ring_lock_acquire release {"e" * 40} 600 "fixture release"
lh_ring_lock_keepalive 1 600
python3 {LOCK} status release
false
""")
        result = subprocess.run(["bash", str(script)], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("release held by", result.stdout)
        self.assertFalse((self.dir / "release.json").exists())
        self.assertEqual(self.events(), ["acquired", "released"])


if __name__ == "__main__":
    unittest.main()
