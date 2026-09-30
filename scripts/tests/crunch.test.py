#!/usr/bin/env python3
"""crunch.sh refuses bad settings before it touches the guest.

The script is copied into a temp checkout with its output root pointed there (it
otherwise writes the shared /tmp/agents/crunch ssh config), and `ssh`/`rsync` on PATH
are stubs that record a call and fail. A refusal is therefore exit != 0 with the
reason on stderr and no call recorded.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "crunch.sh"


class CrunchSettingsTests(unittest.TestCase):
    def run_crunch(self, *args: str, env: dict | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
        with tempfile.TemporaryDirectory(prefix="longhouse-crunch-test-") as directory:
            root = Path(directory)
            (root / "scripts" / "ops").mkdir(parents=True)
            source = SCRIPT.read_text().replace("OUT_ROOT=/tmp/agents/crunch", f"OUT_ROOT={root / 'out'}", 1)
            self.assertIn(str(root / "out"), source)
            script = root / "scripts" / "ops" / "crunch.sh"
            script.write_text(source)
            script.chmod(0o755)
            key = root / "key"
            key.write_text("not a key\n")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls"
            for name in ("ssh", "rsync"):
                stub = bin_dir / name
                stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit 255\n')
                stub.chmod(0o755)
            environment = {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "HOME": str(root),
                "CRUNCH_KEY": str(key),
            }
            environment.update(env or {})
            result = subprocess.run(
                ["bash", str(script), *args], env=environment, capture_output=True, text=True, timeout=60, cwd=root
            )
            recorded = calls.read_text().splitlines() if calls.exists() else []
            return result, recorded

    def assert_refused(self, result: subprocess.CompletedProcess, calls: list[str], needle: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn(needle, result.stderr)
        self.assertEqual(calls, [], "the guest must not be contacted")

    def test_a_malformed_slot_count_is_refused_not_spun_on(self) -> None:
        for value in ("abc", "0", "2;id", "-1"):
            with self.subTest(value=value):
                result, calls = self.run_crunch("run", "true", env={"CRUNCH_SLOTS": value})
                self.assert_refused(result, calls, "CRUNCH_SLOTS")

    def test_a_malformed_timeout_is_refused(self) -> None:
        for value in ("abc", "10s", "5;id"):
            with self.subTest(value=value):
                result, calls = self.run_crunch("run", "true", env={"CRUNCH_TIMEOUT": value})
                self.assert_refused(result, calls, "CRUNCH_TIMEOUT")

    def test_out_paths_must_stay_inside_the_checkout(self) -> None:
        for value in ("", "/etc/passwd", "..", "../x", "a/../b", "a/.."):
            with self.subTest(value=value):
                result, calls = self.run_crunch("run", "--out", value, "true")
                self.assert_refused(result, calls, "--out")

    def test_a_dotted_name_is_not_a_traversal(self) -> None:
        # Reaches the guest probe (the stub ssh fails it): the flag itself was accepted.
        result, calls = self.run_crunch("run", "--out", "docs/v1..2/file.json", "true")
        self.assertNotIn("--out needs", result.stderr)
        self.assertTrue(calls, "an ordinary --out path should get as far as the guest")


if __name__ == "__main__":
    unittest.main(verbosity=1)
