#!/usr/bin/env python3
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "install-playwright.sh"


def make_fake_bunx(bin_dir: Path, log_path: Path) -> None:
    bunx = bin_dir / "bunx"
    bunx.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'if [[ "$LONGHOUSE_PLAYWRIGHT_UNAME" == Linux ]]; then\n'
        '  test ! -e "$LONGHOUSE_APT_SOURCES_DIR/microsoft-prod.list"\n'
        '  test ! -e "$LONGHOUSE_APT_SOURCES_DIR/google-chrome.sources"\n'
        '  test -e "$LONGHOUSE_APT_SOURCES_DIR/ubuntu.sources"\n'
        "fi\n"
        'printf \'%s\\n\' "$PWD|$*" >> "$LONGHOUSE_FAKE_BUNX_LOG"\n'
        'exit "${LONGHOUSE_FAKE_BUNX_EXIT:-0}"\n',
        encoding="utf-8",
    )
    bunx.chmod(bunx.stat().st_mode | stat.S_IXUSR)


def run_script(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_vendor_sources_are_disabled_only_during_install_and_restored_after_failure() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sources = tmp_path / "sources"
        sources.mkdir()
        microsoft = sources / "microsoft-prod.list"
        microsoft.write_text(
            "deb https://packages.microsoft.com/ubuntu/24.04/prod noble main\n",
            encoding="utf-8",
        )
        google = sources / "google-chrome.sources"
        google.write_text(
            "URIs: https://dl.google.com/linux/chrome-stable/deb/\n", encoding="utf-8"
        )
        ubuntu = sources / "ubuntu.sources"
        ubuntu.write_text("URIs: http://archive.ubuntu.com/ubuntu\n", encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_path = tmp_path / "bunx.log"
        make_fake_bunx(bin_dir, log_path)

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "LONGHOUSE_APT_SOURCES_DIR": str(sources),
                "LONGHOUSE_FAKE_BUNX_LOG": str(log_path),
                "LONGHOUSE_PLAYWRIGHT_UNAME": "Linux",
            }
        )

        for exit_code in (0, 17):
            env["LONGHOUSE_FAKE_BUNX_EXIT"] = str(exit_code)
            result = run_script("chromium", env=env)
            assert result.returncode == exit_code, result.stderr
            assert (
                microsoft.read_text()
                == "deb https://packages.microsoft.com/ubuntu/24.04/prod noble main\n"
            )
            assert (
                google.read_text()
                == "URIs: https://dl.google.com/linux/chrome-stable/deb/\n"
            )
            assert not microsoft.with_suffix(".list.longhouse-disabled").exists()
            assert not google.with_suffix(".sources.longhouse-disabled").exists()
            assert ubuntu.exists()


def test_non_linux_install_does_not_request_system_deps() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sources = tmp_path / "sources"
        sources.mkdir()
        microsoft = sources / "azure-cli.list"
        microsoft.write_text(
            "deb https://packages.microsoft.com/repos/azure-cli noble main\n",
            encoding="utf-8",
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_path = tmp_path / "bunx.log"
        make_fake_bunx(bin_dir, log_path)

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "LONGHOUSE_APT_SOURCES_DIR": str(sources),
                "LONGHOUSE_FAKE_BUNX_LOG": str(log_path),
                "LONGHOUSE_PLAYWRIGHT_UNAME": "Darwin",
            }
        )

        result = run_script("chromium", "firefox", env=env)

        assert result.returncode == 0
        assert microsoft.exists()


if __name__ == "__main__":
    test_vendor_sources_are_disabled_only_during_install_and_restored_after_failure()
    test_non_linux_install_does_not_request_system_deps()
    print("playwright install wrapper tests passed")
