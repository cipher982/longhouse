#!/usr/bin/env python3
"""remote.sh refuses bad settings before it touches the guest.

The script is copied into a temp checkout with its output root pointed there (it
otherwise writes the shared /tmp/agents/remote ssh config), and `ssh`/`rsync` on PATH
are stubs that record a call and fail. A refusal is therefore exit != 0 with the
reason on stderr and no call recorded.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "remote.sh"


class RemoteSettingsTests(unittest.TestCase):
    def run_remote(self, *args: str, env: dict | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
        with tempfile.TemporaryDirectory(prefix="longhouse-remote-test-") as directory:
            root = Path(directory)
            (root / "scripts" / "ops").mkdir(parents=True)
            source = SCRIPT.read_text().replace("OUT_ROOT=/tmp/agents/remote", f"OUT_ROOT={root / 'out'}", 1)
            self.assertIn(str(root / "out"), source)
            script = root / "scripts" / "ops" / "remote.sh"
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
                "REMOTE_KEY": str(key),
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
                result, calls = self.run_remote("run", "true", env={"REMOTE_SLOTS": value})
                self.assert_refused(result, calls, "REMOTE_SLOTS")

    def test_a_malformed_timeout_is_refused(self) -> None:
        for value in ("abc", "10s", "5;id"):
            with self.subTest(value=value):
                result, calls = self.run_remote("run", "true", env={"REMOTE_TIMEOUT": value})
                self.assert_refused(result, calls, "REMOTE_TIMEOUT")

    def test_out_paths_must_stay_inside_the_checkout(self) -> None:
        for value in ("", "/etc/passwd", "..", "../x", "a/../b", "a/.."):
            with self.subTest(value=value):
                result, calls = self.run_remote("run", "--out", value, "true")
                self.assert_refused(result, calls, "--out")

    def test_a_dotted_name_is_not_a_traversal(self) -> None:
        # Reaches the guest probe (the stub ssh fails it): the flag itself was accepted.
        result, calls = self.run_remote("run", "--out", "docs/v1..2/file.json", "true")
        self.assertNotIn("--out needs", result.stderr)
        self.assertTrue(calls, "an ordinary --out path should get as far as the guest")


@unittest.skipUnless(shutil.which("setsid") and shutil.which("flock"), "the guest program needs util-linux")
class RemoteGuestProgramTests(unittest.TestCase):
    def test_output_closes_when_the_command_exits(self) -> None:
        """The client hung for an hour after a finished run: the stdin watchdog's
        `cat` outlived the trap that killed only its parent shell, held the ssh
        channel's stdout open, and ssh waited for EOF that never came."""
        program = subprocess.run(
            ["bash", "-c", f'source <(sed -n "/^remote_program()/,/^}}/p" {SCRIPT}); remote_program'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("watchdog", program)
        with tempfile.TemporaryDirectory(prefix="longhouse-remote-guest-") as directory:
            work = Path(directory) / "work"
            run = "guest-test"
            (work / "agents" / run / "src").mkdir(parents=True)
            body = program.replace("/work/", f"{work}/")
            script = f"run={run} slots=1 timeout_s=30 cmd=true extra_env=''; {body}"
            # stdin stays open the way an ssh channel does while the client waits.
            process = subprocess.Popen(
                ["bash", "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            # Drain both pipes to EOF, which is what ssh waits for before the
            # client can exit. stdin is deliberately left open throughout.
            output: dict[str, bytes] = {}
            readers = [
                threading.Thread(target=lambda name=name, pipe=pipe: output.__setitem__(name, pipe.read()))
                for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))
            ]
            for reader in readers:
                reader.start()
            for reader in readers:
                reader.join(timeout=20)
            hung = any(reader.is_alive() for reader in readers)
            if hung:
                os.killpg(process.pid, 9)
            process.stdin.close()
            process.wait(timeout=10)
            for reader in readers:
                reader.join(timeout=10)
            process.stdout.close()
            process.stderr.close()
            self.assertFalse(hung, "the guest program's output stayed open after its command exited")
            stderr = output.get("stderr", b"")
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual((work / "agents" / run / "exit").read_text().strip(), "0")


if __name__ == "__main__":
    unittest.main(verbosity=1)
