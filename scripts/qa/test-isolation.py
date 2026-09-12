#!/usr/bin/env python3
"""Run portable Longhouse tests in a disposable container, never the host login.

No host mounts, daemon socket, published ports, inherited secrets, or network in
fixture mode. Dependency installation runs separately while building the image.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import runpy
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "ai.longhouse.test-isolation"
# Test selection and bounded tuning only. Never forward arbitrary Make variables:
# HOME=..., SHELL=..., MAKEFLAGS=..., auth, URLs and binary overrides are authority.
OPTIONS = {
    "TEST",
    "MODE",
    "FILES",
    "SCENARIOS",
    "CARGO_PROFILE",
    "VERBOSE",
    "PYTEST_XDIST_WORKERS",
    "PLAYWRIGHT_WORKERS",
    "IOS_TEST_SCHEMES",
    "PROJECT",
}
NATIVE = {
    "test-ios",
    "test-ios-perf",
    "test-ios-session-open",
    "test-mobile-chat",
    "test-mobile-chat-stress",
    "simlab-run",
    "test-runtime-packaging-macos",
    "menubar-harness",
    "ios-ui-shot",
    "ios-previews",
    "benchmark-ios-transcript",
}
PRIVATE_NATIVE = {"test-mobile-chat-replay", "test-terminal-fidelity-ios"}
LIVE = {
    "test-storage-v2-b2",
    "test-codex-console-warm-canary",
    "test-claude-console-live-canary",
    "test-cursor-console-live-canary",
    "test-opencode-console-live-canary",
    "test-codex-conversation-reset",
    "test-claude-conversation-reset",
    "test-cursor-conversation-reset",
    "test-opencode-conversation-reset",
    "test-antigravity-conversation-reset",
    "test-opencode-console-product-e2e",
    "test-cursor-console-product-e2e",
    "test-console-served-state-e2e",
    "test-cursor-helm-gate0",
    "test-cursor-helm-product-e2e",
    "test-codex-bridge-e2e",
    "test-hooks",
    "test-terminal-fidelity-web",
    "test-terminal-fidelity-gate",
    "provider-release-proof-universal-live-smoke",
    "provider-live-route-e2e",
    "provider-live-route-e2e-opencode-transcript",
    "qa-live",
    "qa-unmanaged",
    "qa-landing-live",
}
MANIFESTS = (
    "docker/test.dockerfile",
    "package.json",
    "bun.lock",
    "web/package.json",
    "e2e/package.json",
    "runner/package.json",
    "video/package.json",
    "server/pyproject.toml",
    "server/uv.lock",
    "engine/Cargo.toml",
    "engine/Cargo.lock",
)


def command(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


DOCKER_ENV: dict[str, str] | None = None


def configure_docker(scratch: Path) -> None:
    global DOCKER_ENV
    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint:
        endpoint = command(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    if not endpoint.startswith("unix://"):
        raise ValueError(
            "isolated tests require a local Docker Unix socket, not a remote daemon"
        )
    config = scratch / "docker-config"
    config.mkdir()
    (config / "config.json").write_text("{}\n")
    # Public image pulls must not invoke the developer's Keychain credential helper.
    DOCKER_ENV = {
        "PATH": os.environ["PATH"],
        "HOME": str(scratch),
        "DOCKER_CONFIG": str(config),
        "DOCKER_HOST": endpoint,
    }


def docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    if DOCKER_ENV is None:
        raise RuntimeError("Docker supervisor configuration has not been isolated")
    return subprocess.run(["docker", *args], check=check, env=DOCKER_ENV, **kwargs)


def image_tag() -> str:
    digest = hashlib.sha256()
    for name in MANIFESTS:
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    return "longhouse-test:" + digest.hexdigest()[:20]


def prepare_image(scratch: Path) -> str:
    image = image_tag()
    exists = docker(
        "image",
        "inspect",
        image,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists.returncode == 0:
        return image
    context = scratch / "image"
    for name in MANIFESTS:
        dest = context / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    docker(
        "build",
        "--label",
        f"{LABEL}.image=true",
        "-f",
        str(context / "docker/test.dockerfile"),
        "-t",
        image,
        str(context),
    )
    return image


def source_archive(scratch: Path) -> Path:
    """Current tracked source plus nonignored additions, not git/config or caches."""
    names = command(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
    ).stdout
    archive = scratch / "source.tar"

    def container_owner(member: tarfile.TarInfo) -> tarfile.TarInfo:
        member.uid = member.gid = 0
        member.uname = member.gname = "root"
        return member

    with tarfile.open(archive, "w") as output:
        for name in sorted(set(os.fsdecode(n) for n in names.split(b"\0") if n)):
            relative = Path(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or ".git" in relative.parts
            ):
                raise ValueError(f"unsafe source path: {name}")
            # Even accidentally tracked private dotenv files are not test inputs.
            if relative.name == ".env" or (
                relative.name.startswith(".env.")
                and relative.name not in {".env.example", ".env.sample"}
            ):
                continue
            source = ROOT / relative
            if not source.exists() and not source.is_symlink():
                continue
            if source.is_symlink() and not source.resolve().is_relative_to(ROOT):
                raise ValueError(f"source symlink escapes checkout: {name}")
            output.add(source, arcname=name, recursive=False, filter=container_owner)
    return archive


def test_environment(run_id: str, options: dict[str, str]) -> dict[str, str]:
    home = "/tmp/longhouse-test/home"
    env = {
        "HOME": home,
        "USER": "longhouse-test",
        "LOGNAME": "longhouse-test",
        "XDG_CONFIG_HOME": home + "/.config",
        "XDG_DATA_HOME": home + "/.local/share",
        "XDG_CACHE_HOME": home + "/.cache",
        "XDG_STATE_HOME": home + "/.local/state",
        "TMPDIR": "/tmp/longhouse-test/tmp",
        "LONGHOUSE_TEST_ROOT": "/tmp/longhouse-test",
        "LONGHOUSE_TEST_ISOLATED": "1",
        "LONGHOUSE_HOME": home + "/.longhouse",
        "CODEX_HOME": home + "/.codex",
        "CLAUDE_CONFIG_DIR": home + "/.claude",
        "CURSOR_CONFIG_DIR": home + "/.cursor",
        "PI_CODING_AGENT_DIR": home + "/.pi/agent",
        "LONGHOUSE_OMP_CONFIG_DIR": home + "/.omp",
        "TESTING": "1",
        "E2E_DB_DIR": "/tmp/longhouse-test/e2e-db",
        "E2E_ARTIFACT_DIR": "/tmp/longhouse-test/artifacts/e2e",
        "PYTHONPATH": "/work/server",
        "AWS_EC2_METADATA_DISABLED": "true",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "CI": "1",
        "LONGHOUSE_HISTORICAL_MIN_FREE_BYTES": "0",
        "LONGHOUSE_HISTORICAL_MIN_FREE_RATIO": "0",
        "CARGO_BUILD_JOBS": "2",
        "LONGHOUSE_DEVICE_ID": f"longhouse-test-{run_id}",
    }
    env.update(options)
    return env


def load_credentials(path: Path) -> dict[str, str]:
    if path.stat().st_mode & 0o077:
        raise ValueError("live credential JSON must be private (chmod 600)")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not data:
        raise ValueError("live credential JSON must be a nonempty object")
    allowed = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "CURSOR_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "LONGHOUSE_MACHINE_TOKEN",
    }
    if any(
        key not in allowed or not isinstance(value, str) or not value
        for key, value in data.items()
    ):
        raise ValueError(
            "credential JSON accepts only explicit provider keys and a Longhouse machine token"
        )
    return data


def collect_artifacts(name: str, scratch: Path, destination: Path) -> None:
    # Never let a guest-created symlink redirect a host receipt write.
    for index, source in enumerate(
        ("/tmp/longhouse-test/artifacts", "/work/artifacts")
    ):
        packed = scratch / f"artifacts-{index}.tar"
        with packed.open("wb") as stream:
            result = docker(
                "cp",
                name + ":" + source + "/.",
                "-",
                check=False,
                stdout=stream,
                stderr=subprocess.DEVNULL,
            )
        if result.returncode:
            continue  # Most unit tests produce no artifacts.
        with tarfile.open(packed) as archive:
            for member in archive:
                relative = Path(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe artifact path returned by test")
                if not member.isfile():
                    continue  # Directories are created below; never export links/devices.
                target = destination / str(index) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as data, target.open("wb") as output:
                    shutil.copyfileobj(data, output)


def run_container(args: argparse.Namespace, options: dict[str, str]) -> int:
    if shutil.which("docker") is None:
        raise RuntimeError(
            "Docker is required for isolated tests; no host-execution fallback"
        )
    if args.target in LIVE and not args.live:
        raise ValueError(
            f"{args.target} is a live proof. Use --live --image IMAGE --credentials PRIVATE_JSON --target {args.target}; no host-login fallback"
        )
    if args.live and (not args.image or not args.credentials):
        raise ValueError(
            "live proofs require both an explicit provider image and private credential JSON"
        )
    if not args.live and (args.image or args.credentials):
        raise ValueError(
            "custom images and credentials require the explicit --live lane"
        )
    credentials = load_credentials(args.credentials) if args.live else {}
    run_id = uuid.uuid4().hex
    name = "longhouse-test-" + run_id
    receipt_dir = ROOT / "artifacts" / "test-isolation" / run_id
    receipt_dir.mkdir(parents=True, mode=0o700)
    receipt = {
        "run_id": run_id,
        "container": name,
        "target": args.target,
        "lane": "live" if args.live else "fixture",
        "network": "bridge" if args.live else "none",
        "cleanup": False,
    }
    child = None
    scratch = Path(tempfile.mkdtemp(prefix="longhouse-test-"))
    previous_handlers = {}
    interrupted = 0

    def interrupt(signum, _frame):
        nonlocal interrupted
        interrupted = signum
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, interrupt)
    try:
        configure_docker(scratch)
        image = args.image or prepare_image(scratch)
        receipt["image"] = image
        archive = source_archive(scratch)
        with archive.open("rb") as source:
            receipt["source_archive_sha256"] = hashlib.file_digest(
                source, "sha256"
            ).hexdigest()
        (receipt_dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        env = test_environment(run_id, options)
        # The image entrypoint is ignored. Only this explicit configuration enters
        # the child; credentials never appear in Docker argv or container config.
        env.update(credentials)
        env["LONGHOUSE_TEST_COMMAND"] = json.dumps(
            args.command or ["make", args.target]
        )
        env_file = scratch / "configuration.tar"
        with tarfile.open(env_file, "w") as archive_config:
            data = json.dumps(env).encode()
            member = tarfile.TarInfo("test-env.json")
            member.size = len(data)
            member.mode = 0o600
            archive_config.addfile(member, io.BytesIO(data))
        env_file.chmod(0o600)
        docker(
            "create",
            "--name",
            name,
            "--label",
            f"{LABEL}={run_id}",
            "--init",
            "--network",
            receipt["network"],
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "1024",
            "--cpus",
            "2",
            "--memory",
            "4g",
            "--workdir",
            "/work",
            "--entrypoint",
            "/usr/bin/timeout",
            image,
            "--kill-after=10",
            str(args.timeout),
            "/usr/local/bin/python",
            "/work/scripts/qa/test-container-entry.py",
            stdout=subprocess.DEVNULL,
        )
        # No bind mounts: even symlinks or absolute writes remain in the container.
        with archive.open("rb") as source:
            docker("cp", "-", name + ":/work", stdin=source)
        with env_file.open("rb") as configuration:
            docker("cp", "-", name + ":/tmp", stdin=configuration)
        print(
            f"[test-isolation] {run_id} target={args.target} network={receipt['network']}",
            flush=True,
        )
        child = subprocess.Popen(
            ["docker", "start", "--attach", name],
            env=DOCKER_ENV,
            start_new_session=True,
        )
        try:
            code = child.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            receipt["error"] = "test timeout"
            code = 124
        receipt["exit_code"] = code
        return code
    except KeyboardInterrupt:
        receipt["exit_code"] = 128 + (interrupted or signal.SIGINT)
        return receipt["exit_code"]
    finally:
        # Exact generated container identity, never ports/process-name matches.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            exists = docker(
                "inspect",
                "--format",
                "{{.Id}}",
                name,
                check=False,
                capture_output=True,
                text=True,
            )
            if exists.returncode == 0:
                docker(
                    "stop",
                    "--time",
                    "5",
                    name,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    collect_artifacts(name, scratch, receipt_dir / "files")
                finally:
                    docker("rm", "--force", name, stdout=subprocess.DEVNULL)
            if child is not None:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            remaining = docker(
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label={LABEL}={run_id}",
                capture_output=True,
                text=True,
            )
            receipt["cleanup"] = not bool(remaining.stdout.strip())
        finally:
            shutil.rmtree(scratch)
            receipt["scratch_removed"] = not scratch.exists()
            (receipt_dir / "receipt.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            print(
                f"[test-isolation] receipt: {receipt_dir / 'receipt.json'}", flush=True
            )
        if not receipt["cleanup"]:
            raise RuntimeError(f"owned container cleanup failed: {name}")


def run_native(args: argparse.Namespace, options: dict[str, str]) -> int:
    if args.live or args.credentials or args.image or args.command:
        raise ValueError(
            "native fixture tests cannot receive provider credentials or ad-hoc commands"
        )
    with tempfile.TemporaryDirectory(prefix="longhouse-native-dispatch-") as temp:
        scratch = Path(temp)
        config = scratch / "options.json"
        config.write_text(
            json.dumps({key: value for key, value in options.items() if value})
        )
        artifacts = ROOT / "artifacts" / "test-isolation" / uuid.uuid4().hex
        native = runpy.run_path(str(ROOT / "scripts/qa/native-test-isolation.py"))
        return native["run"](
            argparse.Namespace(
                target=args.target,
                artifact_dir=artifacts,
                options_json=config,
                timeout=args.timeout,
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="help")
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="prepare only the credential-free portable dependency image",
    )
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="explicit live-proof settings; never inherited from the host",
    )
    parser.add_argument(
        "--command",
        action="store_true",
        help="remaining arguments are an ad-hoc portable fixture command",
    )
    argv = sys.argv[1:]
    command_index = argv.index("--command") if "--command" in argv else len(argv)
    args = parser.parse_args(argv[:command_index])
    args.command = argv[command_index + 1 :] if command_index < len(argv) else None
    if args.command == []:
        parser.error("--command requires an executable")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.target):
        parser.error("target must be a Make target name")
    options = {key: os.environ[key] for key in OPTIONS if key in os.environ}
    for setting in args.set:
        key, separator, value = setting.partition("=")
        if (
            not args.live
            or not separator
            or key
            not in {
                "ARGS",
                "LONGHOUSE_API_URL",
                "LONGHOUSE_DEVICE_ID",
                "PLAYWRIGHT_BASE_URL",
                "PLAYWRIGHT_API_BASE_URL",
            }
        ):
            parser.error(
                "--set requires --live and an explicit proof setting (ARGS, runtime URL/device, browser/API URL)"
            )
        options[key] = value
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        if args.target in PRIVATE_NATIVE:
            raise ValueError(
                "private-input native proofs require a separately authorized disposable macOS worker; public fixture CI never imports personal transcripts or tokens"
            )
        if args.prepare:
            with tempfile.TemporaryDirectory(prefix="longhouse-test-image-") as temp:
                scratch = Path(temp)
                configure_docker(scratch)
                print(prepare_image(scratch))
                return 0
        if args.target in NATIVE or (
            args.target == "test-e2e-onboarding" and sys.platform == "darwin"
        ):
            return run_native(args, options)
        return run_container(args, options)
    except (
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"test-isolation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
