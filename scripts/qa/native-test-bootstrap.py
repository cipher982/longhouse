#!/usr/bin/env python3
"""Run native fixture tests only on a native runner: a GitHub-hosted macOS VM or the wisp bench."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from test_boundary import boundary_active, create_boundary, native_runner

ROOT = Path(__file__).resolve().parents[2]
TARGETS = {
    "test-install",
    "test-ios",
    "test-ios-perf",
    "test-ios-session-open",
    "test-mobile-chat",
    "test-mobile-chat-stress",
    "test-runtime-packaging-macos",
    "menubar-harness",
    "simlab-run",
    "test-e2e-onboarding",
    "ios-ui-shot",
    "ios-previews",
    "benchmark-ios-transcript",
}
OPTIONS = {
    "TEST",
    "MODE",
    "SCENARIOS",
    "CARGO_PROFILE",
    "VERBOSE",
    "IOS_TEST_SCHEMES",
    "IOS_TEST_FILTER",
    "PROJECT",
    "LONGHOUSE_NATIVE_SMOKE_REMOTE",
    "LONGHOUSE_NATIVE_SMOKE_EXPECTED_VERSION",
    "LONGHOUSE_NATIVE_SMOKE_EXPECTED_COMMIT",
    "LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG",
}


BENCH_LOCK = "/tmp/longhouse-bench.lock"
_bench_lock_handle = None


def hold_bench_lock() -> None:
    """Take the lock `bench.sh` runs under: wisp has 8 GB and one simulator, so one job at a time.

    The lock lasts until this process exits.
    """
    global _bench_lock_handle
    _bench_lock_handle = open(BENCH_LOCK, "a")
    fcntl.flock(_bench_lock_handle, fcntl.LOCK_EX)


# Xcode's build output for the bench runner's iOS targets, kept between jobs: a fresh one recompiled the app and both
# test bundles on every run. One directory per Xcode build (a new Xcode starts again, and the old one is removed), shared
# by every branch: the checkout path is the same for all of them, and Xcode rebuilds what a commit changed. It is started
# again above this size, and run_ios_tests.sh builds once more from nothing when a build in it fails.
DERIVED_DATA_KEEP_BYTES = 30 * 1024**3
XCODE_TARGETS = {"ios-ui-shot", "ios-previews", "benchmark-ios-transcript", "simlab-run"}


def uses_xcode(target: str) -> bool:
    return target.startswith(("test-ios", "test-mobile-chat")) or target in XCODE_TARGETS


def _disk_bytes(path: Path) -> int:
    done = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, check=False)
    return int(done.stdout.split()[0]) * 1024 if done.returncode == 0 and done.stdout else 0


def kept_derived_data(runner_temp: Path) -> Path:
    """The bench runner's derived data for this Xcode build, beside the runner's own work directories."""

    version = subprocess.run(["xcodebuild", "-version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"Build version (\S+)", version)
    if not match:
        raise RuntimeError(f"cannot read the Xcode build from: {version!r}")
    root = runner_temp.parent / "_longhouse-cache" / "ios-derived-data"
    path = root / f"xcode-{match.group(1)}"
    if root.is_dir():
        for other in root.iterdir():
            if other != path:
                print(f"native-test-bootstrap: removing derived data of another Xcode: {other.name}", flush=True)
                shutil.rmtree(other, ignore_errors=True)
    size = _disk_bytes(path) if path.is_dir() else 0
    if size > DERIVED_DATA_KEEP_BYTES:
        print(f"native-test-bootstrap: derived data is {size >> 20} MiB, over the bound; starting again", flush=True)
        shutil.rmtree(path, ignore_errors=True)
        size = 0
    print(f"native-test-bootstrap: derived data {path.name}, {size >> 20} MiB kept", flush=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


# A simulator that has been through its first boot, kept on the bench runner between jobs. A job
# clones it, so each job still starts from the same clean device, and the clone boots in seconds:
# a new device spent about 88 s on its first boot before the first unit test (run 45687712205).
# The simulators live in the runner user's own device set (CoreSimulator ignores the scratch HOME).
SIMULATOR_TEMPLATE = "Longhouse CI template"


def _simctl(*args: str, timeout: int = 600) -> str:
    return subprocess.run(
        ["xcrun", "simctl", *args], capture_output=True, text=True, check=True, timeout=timeout
    ).stdout.strip()


def cloned_simulator() -> str:
    """Clone the kept template (making it first when needed) and return the clone as a destination."""

    sys.path.insert(0, str(ROOT / "scripts" / "ci"))
    import select_ios_simulator as select  # the runtime and device type `make test-ios` would pick

    runtime = select._choose_runtime(select._load_available_runtimes())
    device_type = select._choose_device_type(runtime) if runtime else None
    if runtime is None or device_type is None:
        raise RuntimeError("no iOS simulator runtime or device type to create the template from")
    devices = json.loads(_simctl("list", "devices", "-j"))["devices"]
    template = None
    for runtime_id, entries in devices.items():
        for device in entries:
            if device.get("name") != SIMULATOR_TEMPLATE:
                continue
            if runtime_id == runtime["identifier"] and device.get("isAvailable") and template is None:
                template = device["udid"]
            else:  # an older runtime's, or a second copy
                _simctl("delete", device["udid"])
    if template is None:
        print("native-test-bootstrap: making the simulator template (first boot)", flush=True)
        template = _simctl("create", SIMULATOR_TEMPLATE, device_type["identifier"], runtime["identifier"])
        _simctl("bootstatus", template, "-b", timeout=900)
        _simctl("shutdown", template)
    clone = _simctl("clone", template, f"Longhouse CI {device_type['name']}")
    # xcodebuild boots it when the tests start. Booting it during the build made the build
    # take 108 s instead of 33 (run 45691666834).
    print(f"native-test-bootstrap: simulator cloned from the template: {clone}", flush=True)
    return f"platform=iOS Simulator,id={clone}"


def remove_simulators(environment: dict[str, str]) -> None:
    """Shut down every simulator and delete all but the kept template."""

    subprocess.run(["xcrun", "simctl", "shutdown", "all"], env=environment, check=False, timeout=120, capture_output=True)
    try:
        devices = json.loads(_simctl("list", "devices", "-j", timeout=120))["devices"]
    except (subprocess.SubprocessError, ValueError):
        subprocess.run(["xcrun", "simctl", "delete", "all"], env=environment, check=False, timeout=120, capture_output=True)
        return
    for entries in devices.values():
        for device in entries:
            if device.get("name") != SIMULATOR_TEMPLATE:
                subprocess.run(
                    ["xcrun", "simctl", "delete", device["udid"]],
                    env=environment, check=False, timeout=120, capture_output=True,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    args = parser.parse_args()
    if not native_runner() or boundary_active():
        parser.error(
            "native tests require a GitHub-hosted macOS VM or the wisp bench runner; never the developer login"
        )
    hosted = os.environ["RUNNER_ENVIRONMENT"] == "github-hosted"
    if not hosted:
        hold_bench_lock()
    options = json.loads(os.environ.get("NATIVE_OPTIONS_JSON", "{}"))
    if not isinstance(options, dict) or any(
        key not in OPTIONS or not isinstance(value, str)
        for key, value in options.items()
    ):
        parser.error("invalid native fixture options")
    if args.target == "test-install":
        if options.get("MODE") not in (None, ""):
            parser.error("test-install does not support MODE; use installer options")
        if options.get("LONGHOUSE_NATIVE_SMOKE_REMOTE") not in (
            None,
            "",
            "0",
            "1",
        ):
            parser.error("LONGHOUSE_NATIVE_SMOKE_REMOTE must be 0 or 1")
    elif options.get("MODE") not in (
        None,
        "",
        "test",
        "smoke",
        "render-fixtures",
        "render-trust-states",
        "xcuitest",
    ):
        parser.error("live native modes require a separately authorized proof")
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    output = runner_temp / "native-isolation-evidence" / args.target
    output.mkdir(mode=0o700, parents=True)
    scratch = Path(tempfile.mkdtemp(prefix="longhouse-native-", dir=runner_temp))
    home = scratch / "home"
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TMPDIR": str(scratch / "tmp"),
        "USER": "longhouse-test",
        "LOGNAME": "longhouse-test",
        "LANG": "en_US.UTF-8",
        "CI": "1",
        "TESTING": "1",
        "LONGHOUSE_TEST_ISOLATED": "1",
        "LONGHOUSE_TEST_ROOT": str(scratch),
        "LONGHOUSE_HOME": str(home / ".longhouse"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CURSOR_CONFIG_DIR": str(home / ".cursor"),
        "PI_CODING_AGENT_DIR": str(home / ".pi/agent"),
        "LONGHOUSE_OMP_CONFIG_DIR": str(home / ".omp"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        # Playwright's configs resolve their browser store from the passwd home
        # unless this is set; the install below and the tests must share one
        # store, and it must live in this run's scratch.
        "PLAYWRIGHT_BROWSERS_PATH": str(scratch / "ms-playwright"),
        "E2E_DB_DIR": str(scratch / "e2e-db"),
        "E2E_ARTIFACT_DIR": str(scratch / "artifacts/e2e"),
        "IOS_DERIVED_DATA_PATH": str(scratch / "derived-data"),
        "IOS_RESULTS_DIR": str(scratch / "artifacts/ios"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "AWS_EC2_METADATA_DISABLED": "true",
        "LLM_DISABLED": "1",
        "AUTH_DISABLED": "1",
        "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        "CARGO_HOME": str(scratch / "cargo"),
        "UV_CACHE_DIR": str(scratch / "uv-cache"),
        # Pin the interpreter for this lane. The hosted macOS image ships a
        # Python newer than the native dependency set supports (PyO3 0.22 caps
        # at 3.13), and uv would otherwise resolve the runner's default - which
        # fails the build before any fixture runs.
        "UV_PYTHON": "3.13",
        "BUN_INSTALL_CACHE_DIR": str(scratch / "bun-cache"),
        **options,
    }
    if args.target == "test-install":
        # Never honor a caller's host path; installer evidence belongs to this run.
        environment["LONGHOUSE_NATIVE_SMOKE_ARTIFACT_DIR"] = str(
            scratch / "artifacts/native-installer"
        )
    if not hosted and uses_xcode(args.target):
        environment["IOS_DERIVED_DATA_PATH"] = str(kept_derived_data(runner_temp))
        environment["IOS_DERIVED_DATA_KEPT"] = "1"
        if args.target == "test-ios":
            try:
                environment["IOS_DESTINATION"] = cloned_simulator()
            except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
                # make test-ios then creates a new simulator, as before the template.
                print(f"::warning::simulator template unavailable, using a new simulator: {exc}", flush=True)
    for key in ("HOME", "TMPDIR", "LONGHOUSE_HOME", "CARGO_HOME"):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
    boundary_path = None
    child = None
    handlers = {}
    receipt = {
        "target": args.target,
        "source_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "lane": "github-standard-macos" if hosted else "wisp-macos",
        "network": "github-hosted" if hosted else "wisp",
        "cleanup": False,
    }

    def interrupt(signum, _frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, interrupt)
        # Resolve dependencies before setting package managers offline. No provider
        # secrets or login state enter this VM; it is discarded after the job.
        if args.target in {
            "test-mobile-chat",
            "test-mobile-chat-stress",
            "simlab-run",
            "test-e2e-onboarding",
        }:
            subprocess.run(
                ["bun", "install", "--frozen-lockfile"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        if args.target in {"simlab-run", "test-install"}:
            subprocess.run(
                ["cargo", "fetch", "--manifest-path", "engine/Cargo.toml", "--locked"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        if args.target in {"simlab-run", "test-e2e-onboarding"}:
            subprocess.run(
                ["uv", "sync", "--project", "server", "--frozen", "--extra", "dev"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        if args.target == "test-e2e-onboarding":
            subprocess.run(
                ["bunx", "playwright", "install", "chromium", "firefox", "webkit"],
                cwd=ROOT / "e2e",
                env=environment,
                check=True,
            )
        if args.target in {"menubar-harness", "test-runtime-packaging-macos"}:
            subprocess.run(
                ["swift", "package", "resolve"],
                cwd=ROOT / "desktop/LonghouseMenuBarHarness",
                env=environment,
                check=True,
            )
        if uses_xcode(args.target):
            subprocess.run(
                ["make", "ios-project"],
                cwd=ROOT,
                env=environment,
                check=True,
            )
            subprocess.run(
                [
                    "xcodebuild",
                    "-resolvePackageDependencies",
                    # Fail instead of re-resolving when project.yml pins and the
                    # committed ios/XcodeHarness/Package.resolved disagree.
                    "-onlyUsePackageVersionsFromResolvedFile",
                    "-project",
                    "ios/XcodeHarness/LonghouseIOS.xcodeproj",
                    "-scheme",
                    "Longhouse",
                    "-derivedDataPath",
                    environment["IOS_DERIVED_DATA_PATH"],
                ],
                cwd=ROOT,
                env=environment,
                check=True,
            )
        environment.update({"CARGO_NET_OFFLINE": "true", "UV_OFFLINE": "1"})
        # The boundary is created only after dependency preparation, immediately
        # before make. An outer Seatbelt sandbox breaks Xcode/SwiftPM's own
        # sandbox during manifest evaluation.
        create_boundary(scratch, environment)
        boundary_path = Path(environment["LONGHOUSE_TEST_BOUNDARY"])
        child = subprocess.Popen(
            ["make", args.target],
            cwd=ROOT,
            env=environment,
            start_new_session=True,
        )
        status = child.wait()
        receipt["exit_code"] = status
        return status
    except KeyboardInterrupt:
        receipt["exit_code"] = 130
        return 130
    finally:
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        # A hosted VM is discarded after the job, which ends every descendant and simulator even
        # on SIGKILL. Wisp is not, so shut down and delete the simulators this run made, which live
        # in the runner user's device set, keeping only the template the next job clones.
        remove_simulators(environment)
        try:
            for name, source in (
                ("native", scratch / "artifacts"),
                ("project", ROOT / "artifacts"),
            ):
                if source.exists():
                    shutil.copytree(
                        source,
                        output / name,
                        symlinks=True,
                        ignore=shutil.ignore_patterns(
                            "swift-build", "xcode-project", "*.app"
                        ),
                    )
        finally:
            shutil.rmtree(scratch)
            receipt["boundary_removed"] = (
                boundary_path is None or not boundary_path.exists()
            )
            receipt["cleanup"] = not scratch.exists() and receipt["boundary_removed"]
            (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
