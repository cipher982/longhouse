#!/usr/bin/env python3
"""dogfood-refresh installs longhouse-server from the exact checkout, with its UI.

Sources the real dogfood-runtime.sh and runs install_server_cli_from_source in a
fixture repo with fake `bun` and `uv` on PATH, so nothing is built or installed:
the fakes record what they were asked to do, in order.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FAKE_BUN = """#!/usr/bin/env bash
echo "bun $* @ $(pwd -P)" >> "$FIXTURE_LOG"
if [[ "$1" == run && "$2" == build ]]; then mkdir -p dist && echo built > dist/index.html; fi
"""

FAKE_UV = """#!/usr/bin/env bash
echo "uv $*" >> "$FIXTURE_LOG"
"""


class ServerCliInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="longhouse-server-cli-install-")
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / "scripts" / "dev").mkdir(parents=True)
        (self.repo / "scripts" / "build").mkdir(parents=True)
        (self.repo / "server").mkdir()
        (self.repo / "web" / "src").mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts" / "dev" / "dogfood-runtime.sh", self.repo / "scripts" / "dev" / "dogfood-runtime.sh")
        (self.repo / "scripts" / "build" / "generate_build_identity.py").write_text("")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("bun", FAKE_BUN), ("uv", FAKE_UV)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.home = self.root / "home"
        (self.home / ".local" / "bin").mkdir(parents=True)
        self.log = self.root / "calls.log"

    def tearDown(self):
        self.tmp.cleanup()

    def install(self) -> list[str]:
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", HOME=str(self.home),
                   FIXTURE_LOG=str(self.log), LONGHOUSE_DOGFOOD_RUNTIME_SOURCE_ONLY="1")
        script = self.repo / "scripts" / "dev" / "dogfood-runtime.sh"
        result = subprocess.run(["bash", "-c", f'source "{script}"; install_server_cli_from_source'],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.log.read_text().splitlines()

    def test_a_fresh_checkout_builds_the_frontend_before_the_wheel(self):
        calls = self.install()
        build = next(i for i, call in enumerate(calls) if call.startswith("bun run build"))
        install = next(i for i, call in enumerate(calls) if call.startswith("uv tool install"))
        self.assertLess(build, install, calls)
        self.assertTrue((self.repo / "web" / "dist" / "index.html").exists())

    def test_the_install_is_non_editable_from_this_checkout(self):
        install = next(call for call in self.install() if call.startswith("uv tool install"))
        self.assertNotIn(" -e ", f" {install} ")
        self.assertNotIn("--editable", install)
        self.assertTrue(install.endswith(str(self.repo / "server")), install)

    def test_a_current_dist_is_reused(self):
        (self.repo / "web" / "dist").mkdir()
        (self.repo / "web" / "dist" / "index.html").write_text("built")
        calls = self.install()
        self.assertFalse(any(call.startswith("bun") for call in calls), calls)

    def test_a_stale_dist_is_rebuilt(self):
        dist = self.repo / "web" / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("old")
        os.utime(dist / "index.html", (1, 1))
        (self.repo / "web" / "src" / "app.tsx").write_text("changed")
        calls = self.install()
        self.assertTrue(any(call.startswith("bun run build") for call in calls), calls)


if __name__ == "__main__":
    unittest.main()
