#!/usr/bin/env python3
"""Run portable Longhouse tests in a disposable container, never the host login.

No host mounts, daemon socket, published ports, inherited secrets, or network in
fixture mode. Dependency installation runs separately while building the image.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "ai.longhouse.test-isolation"
OWNER_LABEL = LABEL + ".owner"
DEADLINE_LABEL = LABEL + ".deadline"
OWNER = hashlib.sha256(str(ROOT).encode()).hexdigest()[:20]
# Test selection, public proof metadata, bounded tuning, and artifact destinations.
# Never forward arbitrary Make variables: HOME=..., SHELL=..., MAKEFLAGS=...,
# auth, service URLs, and binary overrides are authority.
OPTIONS = {
    "ARGS",
    "TEST",
    "MODE",
    "FILES",
    "SCENARIOS",
    "CARGO_PROFILE",
    "VERBOSE",
    "PYTEST_XDIST_WORKERS",
    "PYTEST_ADDOPTS",
    "LONGHOUSE_TEST_SHARD",
    "PLAYWRIGHT_WORKERS",
    "IOS_TEST_SCHEMES",
    "PROJECT",
    "UNIVERSAL_PROVIDER",
    "PROVIDER",
    "PRODUCER_CLASS",
    "INVOCATION_ID",
    "RUN_REFERENCE",
    "LONGHOUSE_GIT_SHA",
    "PROVIDER_VERSION",
    "PROVIDER_EXECUTABLE_IDENTITY",
    "STORE_ROOT",
    "BUNDLE_OUTPUT",
    "ARTIFACT",
    "EVIDENCE_ROOT",
    "LONGHOUSE_NATIVE_SMOKE_REMOTE",
    "LONGHOUSE_NATIVE_SMOKE_EXPECTED_VERSION",
    "LONGHOUSE_NATIVE_SMOKE_EXPECTED_COMMIT",
    "LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG",
}
NATIVE = {
    "test-install",
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
    "hosted-shipper-mixed-bench",
    "render-canary",
    "cohort-journey",
    "qa-live",
    "qa-unmanaged",
    "qa-landing-live",
}
ANONYMOUS_LIVE = {"qa-landing-live"}
FIXTURE_SETTINGS = {
    "ARGS",
    "UNIVERSAL_PROVIDER",
    "STORE_ROOT",
    "BUNDLE_OUTPUT",
    "ARTIFACT",
    "EVIDENCE_ROOT",
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
    ".cargo/config.toml",
)


def command(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    env = dict(kwargs.pop("env", os.environ))
    env.pop("LONGHOUSE_TEST_PULL_TOKEN", None)
    return subprocess.run(args, check=True, env=env, **kwargs)


DOCKER_ENV: dict[str, str] | None = None


def configure_docker(scratch: Path) -> None:
    global DOCKER_ENV
    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint:
        context_env = dict(os.environ)
        context_env.pop("LONGHOUSE_TEST_PULL_TOKEN", None)
        endpoint = command(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            env=context_env,
            capture_output=True,
            text=True,
        ).stdout.strip()
    if not endpoint.startswith("unix://"):
        raise ValueError(
            "isolated tests require a local Docker Unix socket, not a remote daemon"
        )
    config = scratch / "docker-config"
    config.mkdir(mode=0o700)
    config_file = config / "config.json"
    config_file.write_text("{}\n")
    config_file.chmod(0o600)
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

IMAGE_REF_PATTERN = re.compile(
    r"^ghcr\.io/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+@sha256:[0-9a-f]{64}$"
)
MANIFEST_LABEL = LABEL + ".manifest-sha256"


@dataclass(frozen=True)
class ImageMetadata:
    image_id: str
    labels: dict[str, str]
    repo_digests: tuple[str, ...]


@dataclass(frozen=True)
class ImagePreparation:
    ref: str
    image_id: str
    seconds: float
    cache: str
    source: str


def manifest_sha256() -> str:
    digest = hashlib.sha256()
    for name in MANIFESTS:
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def image_key() -> dict[str, str | list[str]]:
    manifest = manifest_sha256()
    # The producer uses this as the GHCR tag, while the consumer uses the
    # separately supplied immutable digest reference.
    return {"tag": manifest, "manifest_sha256": manifest, "manifests": list(MANIFESTS)}


def image_tag() -> str:
    return "longhouse-test:" + manifest_sha256()


def inspect_image(ref: str) -> ImageMetadata | None:
    inspected = docker(
        "image",
        "inspect",
        ref,
        check=False,
        capture_output=True,
        text=True,
    )
    if inspected.returncode:
        return None
    try:
        payload = json.loads(inspected.stdout)
        image = payload[0]
        config = image["Config"]
        labels = config.get("Labels") or {}
        repo_digests = image.get("RepoDigests") or []
        if not isinstance(labels, dict) or not isinstance(repo_digests, list):
            raise TypeError
        image_id = image["Id"]
        if not isinstance(image_id, str):
            raise TypeError
        return ImageMetadata(
            image_id=image_id,
            labels={str(key): str(value) for key, value in labels.items()},
            repo_digests=tuple(str(value) for value in repo_digests),
        )
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Docker returned malformed metadata for image {ref}") from exc


def validate_image_ref(ref: str) -> str:
    if not IMAGE_REF_PATTERN.fullmatch(ref):
        raise ValueError(
            "LONGHOUSE_TEST_IMAGE_REF must be a GHCR image with an immutable sha256 digest"
        )
    return ref


def validate_image(
    ref: str, metadata: ImageMetadata, expected_manifest: str
) -> str:
    if metadata.labels.get(MANIFEST_LABEL) != expected_manifest:
        raise RuntimeError(
            f"image {ref} has the wrong {MANIFEST_LABEL} label; refusing to run it"
        )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata.image_id):
        raise RuntimeError(f"image {ref} has no valid local image ID")
    if "@" in ref and metadata.repo_digests:
        expected_digest = ref.rsplit("@", 1)[1]
        if not any(value.rsplit("@", 1)[-1] == expected_digest for value in metadata.repo_digests):
            raise RuntimeError(f"image {ref} resolved to a different local manifest")
    return metadata.image_id


def ghcr_login(token: str) -> None:
    username = (
        os.environ.get("LONGHOUSE_TEST_PULL_USER")
        or os.environ.get("GITHUB_ACTOR")
        or "github-actions"
    )
    result = docker(
        "login",
        "ghcr.io",
        "--username",
        username,
        "--password-stdin",
        check=False,
        input=(token + "\n").encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError("GHCR authentication failed")


def prepare_image(scratch: Path) -> ImagePreparation:
    started = time.monotonic()
    expected_manifest = manifest_sha256()
    configured_ref = os.environ.get("LONGHOUSE_TEST_IMAGE_REF")
    if configured_ref is not None:
        ref = validate_image_ref(configured_ref)
        existing = inspect_image(ref)
        if existing is None:
            token = os.environ.get("LONGHOUSE_TEST_PULL_TOKEN")
            if token:
                ghcr_login(token)
            pulled = docker(
                "pull",
                ref,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if pulled.returncode:
                # Do not fall back to a local build: the digest is the
                # producer-consumer contract for public fixture jobs.
                raise RuntimeError("immutable GHCR image pull failed")
            existing = inspect_image(ref)
            if existing is None:
                raise RuntimeError("pulled GHCR image is not available locally")
            cache = "miss"
            source = "ghcr-pull"
        else:
            cache = "hit"
            source = "ghcr-cache"
        image_id = validate_image(ref, existing, expected_manifest)
        return ImagePreparation(
            ref=ref,
            image_id=image_id,
            seconds=round(time.monotonic() - started, 3),
            cache=cache,
            source=source,
        )

    ref = image_tag()
    existing = inspect_image(ref)
    if existing is not None and existing.labels.get(MANIFEST_LABEL) == expected_manifest:
        image_id = validate_image(ref, existing, expected_manifest)
        return ImagePreparation(
            ref=ref,
            image_id=image_id,
            seconds=round(time.monotonic() - started, 3),
            cache="hit",
            source="local-cache",
        )
    # A stale local tag is rebuilt, but an explicit GHCR digest above is
    # always fail-closed on a provenance mismatch.
    context = scratch / "image"
    for name in MANIFESTS:
        dest = context / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    docker(
        "build",
        "--build-arg",
        f"TEST_MANIFEST_SHA={expected_manifest}",
        "--label",
        f"{LABEL}.image=true",
        "-f",
        str(context / "docker/test.dockerfile"),
        "-t",
        ref,
        str(context),
    )
    existing = inspect_image(ref)
    if existing is None:
        raise RuntimeError("local dependency image build produced no image")
    image_id = validate_image(ref, existing, expected_manifest)
    return ImagePreparation(
        ref=ref,
        image_id=image_id,
        seconds=round(time.monotonic() - started, 3),
        cache="miss",
        source="local-build",
    )


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


ARTIFACT_OPTIONS = {
    "STORE_ROOT",
    "BUNDLE_OUTPUT",
    "ARTIFACT",
    "EVIDENCE_ROOT",
    "LONGHOUSE_JOURNEY_OUTPUT",
}


def guest_artifact_path(value: str) -> str:
    if "\x00" in value:
        raise ValueError("artifact path contains NUL")
    path = Path(value)
    guest_root = Path("/work/artifacts")
    if path.is_relative_to(guest_root):
        relative = path.relative_to(guest_root)
    elif path.is_absolute():
        relative = Path(path.name)
    else:
        relative = path
    if ".." in relative.parts or not relative.name:
        raise ValueError("artifact path must name a destination inside its guest root")
    return str(guest_root / relative)


def container_options(options: dict[str, str]) -> dict[str, str]:
    result = dict(options)
    for key in ARTIFACT_OPTIONS:
        if key in result and result[key]:
            result[key] = guest_artifact_path(result[key])
    return result


def make_command(args: argparse.Namespace, options: dict[str, str]) -> list[str]:
    if args.command is not None:
        return list(args.command)
    command = ["make", args.target]
    for key in (
        "ARGS",
        "UNIVERSAL_PROVIDER",
        "STORE_ROOT",
        "BUNDLE_OUTPUT",
        "ARTIFACT",
        "EVIDENCE_ROOT",
    ):
        value = options.get(key)
        if value:
            command.append(f"{key}={value}")
    return command


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
        "CARGO_BUILD_JOBS": CONTAINER_CPUS,
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
        "CONTROL_PLANE_ADMIN_TOKEN",
        "SMOKE_RUNTIME_TOKEN",
        "LONGHOUSE_CANARY_TOKEN",
        "LONGHOUSE_JOURNEY_LEXICAL_QUERY",
        "LONGHOUSE_JOURNEY_RECALL_QUERY",
    }
    if any(
        key not in allowed or not isinstance(value, str) or not value
        for key, value in data.items()
    ):
        raise ValueError(
            "credential JSON accepts only explicit provider, hosted-QA, and journey keys"
        )
    return data


# Backend unit tests put their catalogd databases under /tmp, and on cube that
# is btrfs shared with every other CI job: fsync tails there reached the
# catalog's 1 s RPC deadline and failed a few storage tests on most pushes.
# These lanes test logic, not disk durability, and leave no artifacts in /tmp.
# The runner that owns the Docker daemon sizes the guest; cube's shared DinD
# pods keep the historical 2 CPU / 4 GiB default.
CONTAINER_CPUS = os.environ.get("LONGHOUSE_TEST_CPUS", "2")
CONTAINER_MEMORY = os.environ.get("LONGHOUSE_TEST_MEMORY", "4g")
# The engine lanes are the same shape: ~1700 Rust tests plus the shipper E2E
# open SQLite databases in tempdirs under /tmp, and on a disk-backed /tmp the
# unit suite ran ~6x slower than the same guest locally. Their tempdirs are
# small; the cap only bounds a runaway, and unused tmpfs costs no memory.
# Provider contract tests start a real catalog daemon per test and hit the
# same fsync stalls ("catalogd deadline exceeded").
MEMORY_TMP_TARGETS = frozenset(
    {
        "test",
        "test-backend-single",
        "ci-backend",
        "test-provider-contract",
        "test-engine",
        "test-engine-single",
        "test-engine-projection-failure",
        "test-shipper-e2e",
        "test-shipper-premerge",
    }
)


def memory_backed_tmp(target: str) -> list[str]:
    if target not in MEMORY_TMP_TARGETS:
        return []
    return ["--tmpfs", "/tmp:rw,exec,nosuid,size=2g,mode=1777"]


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


def reap_stale_owned_containers(now: float | None = None) -> None:
    """Remove only this worktree's containers whose supervisor deadline passed."""
    listed = docker(
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label={LABEL}",
        "--filter",
        f"label={OWNER_LABEL}={OWNER}",
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode:
        return
    current = time.time() if now is None else now
    for container_id in listed.stdout.splitlines():
        inspected = docker(
            "inspect",
            "--format",
            "{{json .}}",
            container_id,
            check=False,
            capture_output=True,
            text=True,
        )
        if inspected.returncode:
            continue
        try:
            payload = json.loads(inspected.stdout)
            labels = payload["Config"]["Labels"] or {}
            deadline = float(labels[DEADLINE_LABEL])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if labels.get(OWNER_LABEL) != OWNER or not (0 < deadline <= current):
            continue
        name = str(payload.get("Name") or "").lstrip("/")
        run_id = str(labels.get(LABEL, ""))
        if (
            not re.fullmatch(r"[0-9a-f]{32}", run_id)
            or name != f"longhouse-test-{run_id}"
        ):
            continue
        receipt_dir = ROOT / "artifacts" / "test-isolation" / run_id
        try:
            if payload.get("State", {}).get("Running"):
                docker(
                    "stop",
                    "--time",
                    "5",
                    name,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if receipt_dir.is_dir():
                with tempfile.TemporaryDirectory(prefix="longhouse-stale-") as temp:
                    collect_artifacts(name, Path(temp), receipt_dir / "files")
        finally:
            docker(
                "rm",
                "--force",
                name,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            remaining = docker(
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"id={container_id}",
                check=False,
                capture_output=True,
                text=True,
            )
            cleaned = remaining.returncode == 0 and not remaining.stdout.strip()
            receipt_path = receipt_dir / "receipt.json"
            if receipt_path.is_file():
                stale_receipt = json.loads(receipt_path.read_text())
                scratch = Path("/tmp") / name
                if stale_receipt.get("scratch_path") == str(scratch):
                    if scratch.is_symlink():
                        raise RuntimeError(f"owned scratch became a symlink: {scratch}")
                    if scratch.exists():
                        shutil.rmtree(scratch)
                    stale_receipt["scratch_removed"] = not scratch.exists()
                stale_receipt["stale_reaped"] = cleaned
                stale_receipt["cleanup"] = cleaned
                receipt_path.write_text(json.dumps(stale_receipt, indent=2) + "\n")
            if not cleaned:
                raise RuntimeError(f"stale owned container cleanup failed: {name}")


def run_container(args: argparse.Namespace, options: dict[str, str]) -> int:
    if shutil.which("docker") is None:
        raise RuntimeError(
            "Docker is required for isolated tests; no host-execution fallback"
        )
    if args.target in LIVE and not args.live:
        raise ValueError(
            f"{args.target} is a live proof. Use --live --image IMAGE --credentials PRIVATE_JSON --target {args.target}; no host-login fallback"
        )
    if args.live and not args.image:
        raise ValueError(
            "live proofs require an explicit provider image; prepare it separately with --prepare"
        )
    if args.live and args.target not in ANONYMOUS_LIVE and not args.credentials:
        raise ValueError(
            f"{args.target} requires explicit private credential JSON; anonymous live targets may omit --credentials"
        )
    if not args.live and (args.image or args.credentials):
        raise ValueError(
            "custom images and credentials require the explicit --live lane"
        )
    credentials = load_credentials(args.credentials) if args.credentials else {}
    options = container_options(options)
    run_id = uuid.uuid4().hex
    name = "longhouse-test-" + run_id
    deadline = time.time() + args.timeout + 20
    receipt_dir = ROOT / "artifacts" / "test-isolation" / run_id
    receipt_dir.mkdir(parents=True, mode=0o700)
    configured_image_ref = os.environ.get("LONGHOUSE_TEST_IMAGE_REF")
    image_hint = (
        args.image
        or (configured_image_ref if configured_image_ref is not None else image_tag())
    )
    receipt = {
        "run_id": run_id,
        "container": name,
        "owner": OWNER,
        "deadline": deadline,
        "target": args.target,
        "lane": "live" if args.live else "fixture",
        "network": "bridge" if args.live else "none",
        "cleanup": False,
        "image": image_hint,
        "image_ref": image_hint,
        "image_id": None,
        "image_preparation_seconds": None,
        "image_cache": "explicit" if args.image else "unknown",
        "image_source": (
            "explicit"
            if args.image
            else (
                "ghcr"
                if "LONGHOUSE_TEST_IMAGE_REF" in os.environ
                else "local"
            )
        ),
    }
    options.setdefault("ARTIFACT", f"/work/artifacts/{args.target}.json")
    options.setdefault("EVIDENCE_ROOT", f"/work/artifacts/{args.target}")
    child = None
    scratch = Path("/tmp") / name
    scratch.mkdir(mode=0o700)
    receipt["scratch_path"] = str(scratch)
    previous_handlers = {}
    interrupted = 0

    def interrupt(signum, _frame):
        nonlocal interrupted
        interrupted = signum
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, interrupt)
    try:
        preparation_started = time.monotonic()
        try:
            configure_docker(scratch)
            reap_stale_owned_containers()
            preparation_started = time.monotonic()
            if args.image:
                image = args.image
                metadata = inspect_image(image)
                if metadata is None or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", metadata.image_id
                ):
                    raise RuntimeError(f"image {image} has no valid local image ID")
                receipt["image_id"] = metadata.image_id
                receipt["image_cache"] = "explicit"
                receipt["image_ref"] = image
                receipt["image"] = image
            else:
                preparation = prepare_image(scratch)
                image = preparation.ref
                receipt["image"] = preparation.ref
                receipt["image_ref"] = preparation.ref
                receipt["image_id"] = preparation.image_id
                receipt["image_cache"] = preparation.cache
                receipt["image_source"] = preparation.source
        finally:
            receipt["image_preparation_seconds"] = round(
                time.monotonic() - preparation_started, 3
            )
        archive = source_archive(scratch)
        with archive.open("rb") as source:
            receipt["source_archive_sha256"] = hashlib.file_digest(
                source, "sha256"
            ).hexdigest()
        (receipt_dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        env = test_environment(run_id, options)
        env.update(credentials)
        env["LONGHOUSE_TEST_COMMAND"] = json.dumps(make_command(args, options))
        configuration = io.BytesIO()
        with tarfile.open(fileobj=configuration, mode="w") as archive_config:
            data = json.dumps(env).encode()
            member = tarfile.TarInfo("test-env.json")
            member.size = len(data)
            member.mode = 0o600
            archive_config.addfile(member, io.BytesIO(data))
            with tarfile.open(archive) as snapshot:
                data = (
                    b"\0".join(
                        os.fsencode(member.name)
                        for member in snapshot
                        if not member.isdir()
                    )
                    + b"\0"
                )
            member = tarfile.TarInfo("test-source-files")
            member.size = len(data)
            member.mode = 0o600
            archive_config.addfile(member, io.BytesIO(data))
        docker(
            "create",
            "--init",
            "--name",
            name,
            "--label",
            f"{LABEL}={run_id}",
            "--label",
            f"{OWNER_LABEL}={OWNER}",
            "--label",
            f"{DEADLINE_LABEL}={deadline:.6f}",
            "--network",
            receipt["network"],
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "1024",
            "--cpus",
            CONTAINER_CPUS,
            "--memory",
            CONTAINER_MEMORY,
            *memory_backed_tmp(args.target),
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
        # Not /tmp: a tmpfs mounted there at start would hide it.
        docker("cp", "-", name + ":/opt", input=configuration.getvalue())
        configuration.close()
        print(
            f"[test-isolation] image={receipt['image_ref']} "
            f"id={receipt['image_id']} preparation={receipt['image_preparation_seconds']:.3f}s "
            f"cache={receipt['image_cache']} source={receipt['image_source']}",
            flush=True,
        )
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
        # The guest's status is the step's verdict. 9e4b16bd0 dropped this
        # return and every containerized CI step passed with failing tests.
        receipt["exit_code"] = code
        return code
    except KeyboardInterrupt:
        receipt["exit_code"] = 128 + (interrupted or signal.SIGINT)
        return receipt["exit_code"]
    except (
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        receipt["error"] = str(exc)
        raise
    finally:
        # Exact generated container identity, never ports/process-name matches.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if DOCKER_ENV is not None:
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
            else:
                receipt["cleanup"] = True
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
            json.dumps(
                {
                    key: value
                    for key, value in options.items()
                    if value
                    and key
                    in {
                        "TEST",
                        "MODE",
                        "SCENARIOS",
                        "CARGO_PROFILE",
                        "VERBOSE",
                        "IOS_TEST_SCHEMES",
                        "PROJECT",
                        "LONGHOUSE_NATIVE_SMOKE_REMOTE",
                        "LONGHOUSE_NATIVE_SMOKE_EXPECTED_VERSION",
                        "LONGHOUSE_NATIVE_SMOKE_EXPECTED_COMMIT",
                        "LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG",
                    }
                }
            )
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
    parser.add_argument(
        "--print-image-key",
        action="store_true",
        help="print the producer-consumer dependency image key as JSON",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="explicit proof settings; fixture mode accepts only selection/output keys",
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
            (not args.live and key not in FIXTURE_SETTINGS)
            or not separator
            or key
            not in {
                "ARGS",
                "UNIVERSAL_PROVIDER",
                "STORE_ROOT",
                "BUNDLE_OUTPUT",
                "ARTIFACT",
                "EVIDENCE_ROOT",
                "LONGHOUSE_API_URL",
                "LONGHOUSE_DEVICE_ID",
                "PLAYWRIGHT_BASE_URL",
                "PLAYWRIGHT_API_BASE_URL",
                "CONTROL_PLANE_URL",
                "QA_INSTANCE_SUBDOMAIN",
                "INSTANCE_SUBDOMAIN",
                "CONSOLE_QA_PROVIDER",
                "CONSOLE_QA_CWD",
                "LONGHOUSE_JOURNEY_TARGET_LABEL",
                "LONGHOUSE_JOURNEY_OUTPUT",
            }
        ):
            parser.error(
                "--set requires an explicit allowlisted setting; URLs and credentials require --live"
            )
        options[key] = value
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        if args.print_image_key:
            print(json.dumps(image_key(), sort_keys=True))
            return 0
        if args.target in PRIVATE_NATIVE:
            raise ValueError(
                "private-input native proofs require a separately authorized disposable macOS worker; public fixture CI never imports personal transcripts or tokens"
            )
        if args.prepare:
            with tempfile.TemporaryDirectory(prefix="longhouse-test-image-") as temp:
                scratch = Path(temp)
                configure_docker(scratch)
                prepared = prepare_image(scratch)
                print(
                    f"[test-isolation] image={prepared.ref} id={prepared.image_id} "
                    f"preparation={prepared.seconds:.3f}s cache={prepared.cache} "
                    f"source={prepared.source}",
                    file=sys.stderr,
                    flush=True,
                )
                print(prepared.ref)
                return 0
        if args.target == "launch-gate-local":
            args.target = "test-install"
            status = run_native(args, options)
            if status:
                return status
            args.target = "onboarding-funnel"
            return run_container(args, options)
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
