#!/usr/bin/env python3
"""The fixture image key ignores the release version and nothing else.

`make release` bumps the project's own version in five manifests. The fixture
image holds dependencies only, so a bump must not rebuild it (~6 min on the
critical path of every release's exact-SHA CI), while any dependency change
must.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("test_isolation", ROOT / "scripts" / "qa" / "test-isolation.py")
assert SPEC and SPEC.loader
isolation = importlib.util.module_from_spec(SPEC)
sys.modules["test_isolation"] = isolation
SPEC.loader.exec_module(isolation)


def key_of(root: Path) -> str:
    isolation.ROOT = root
    return isolation.manifest_sha256()


def stage(root: Path, edit=None) -> Path:
    for name in isolation.MANIFESTS:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (ROOT / name).read_bytes()
        target.write_bytes(edit(name, data) if edit else data)
    return root


def bump(name: str, data: bytes) -> bytes:
    """What bump-my-version does: every own-version line moves, nothing else."""
    if name in ("server/pyproject.toml", "engine/Cargo.toml"):
        return data.replace(b'\nversion = "', b'\nversion = "9.', 1)
    if name == "server/uv.lock":
        return data.replace(b'name = "longhouse"\nversion = "', b'name = "longhouse"\nversion = "9.', 1)
    if name == "engine/Cargo.lock":
        return data.replace(b'name = "longhouse-engine"\nversion = "', b'name = "longhouse-engine"\nversion = "9.', 1)
    if name == "runner/package.json":
        return data.replace(b'"version": "', b'"version": "9.', 1)
    return data


def test_release_version_bump_keeps_the_image_key() -> None:
    with tempfile.TemporaryDirectory() as before, tempfile.TemporaryDirectory() as after:
        original = stage(Path(before))
        bumped = stage(Path(after), bump)
        assert (original / "server/pyproject.toml").read_bytes() != (bumped / "server/pyproject.toml").read_bytes()
        assert (original / "engine/Cargo.lock").read_bytes() != (bumped / "engine/Cargo.lock").read_bytes()
        assert key_of(original) == key_of(bumped)


def test_a_dependency_change_still_changes_the_image_key() -> None:
    def add_dependency(name: str, data: bytes) -> bytes:
        if name == "engine/Cargo.lock":
            return data + b'\n[[package]]\nname = "surprise"\nversion = "1.0.0"\n'
        return data

    with tempfile.TemporaryDirectory() as before, tempfile.TemporaryDirectory() as after:
        assert key_of(stage(Path(before))) != key_of(stage(Path(after), add_dependency))


def test_a_dependency_pinned_at_the_project_version_is_not_normalized() -> None:
    # Only the project's own [[package]] entry is neutral; another package that
    # happens to share the version string is a real dependency.
    def retarget(name: str, data: bytes) -> bytes:
        if name == "engine/Cargo.lock":
            return data.replace(b'name = "serde"\nversion = "', b'name = "serde"\nversion = "9.', 1)
        return data

    with tempfile.TemporaryDirectory() as before, tempfile.TemporaryDirectory() as after:
        assert key_of(stage(Path(before))) != key_of(stage(Path(after), retarget))


def test_loading_the_cached_image_writes_nothing_to_stdout() -> None:
    """CI, 2026-11-13: `echo "ref=$(test-isolation.py --prepare)" >> $GITHUB_OUTPUT` broke on a cache hit, because
    `docker load` printed "Loaded image: ..." on the stdout the workflow reads as the image reference."""
    import os
    import subprocess

    label = isolation.MANIFEST_LABEL
    loaded: list[str] = []

    def fake_docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
        if args[0] == "load":
            loaded.append(args[2])
            # Like the real client: one line on whatever stdout it was given.
            return subprocess.run(["sh", "-c", "echo 'Loaded image: longhouse-test:x'"], check=check, **kwargs)
        raise AssertionError(f"unexpected docker {args}")

    def fake_inspect(ref: str):
        return isolation.ImageMetadata(image_id="sha256:abc", labels={label: "m" * 64}, repo_digests=()) if loaded else None

    saved = (isolation.docker, isolation.inspect_image, isolation.validate_image, isolation.manifest_sha256)
    with tempfile.TemporaryDirectory() as temp:
        cache = Path(temp) / "docker-images"
        cache.mkdir()
        (cache / f"longhouse-test-{'m' * 64}.tar").write_bytes(b"tar")
        captured = Path(temp) / "stdout"
        environment = {k: os.environ.get(k) for k in ("LONGHOUSE_CI_IMAGE_CACHE_DIR", "LONGHOUSE_TEST_IMAGE_REF")}
        os.environ["LONGHOUSE_CI_IMAGE_CACHE_DIR"] = str(cache)
        os.environ.pop("LONGHOUSE_TEST_IMAGE_REF", None)
        isolation.docker, isolation.inspect_image = fake_docker, fake_inspect
        isolation.validate_image = lambda ref, existing, expected: existing.image_id
        isolation.manifest_sha256 = lambda: "m" * 64
        sys.stdout.flush()
        real_stdout = os.dup(1)
        try:
            with open(captured, "wb") as sink:
                os.dup2(sink.fileno(), 1)
                try:
                    prepared = isolation.prepare_image(Path(temp))
                finally:
                    sys.stdout.flush()
                    os.dup2(real_stdout, 1)
        finally:
            os.close(real_stdout)
            isolation.docker, isolation.inspect_image, isolation.validate_image, isolation.manifest_sha256 = saved
            for key, value in environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        assert prepared.source == "ci-cache-tar" and loaded, prepared
        assert captured.read_bytes() == b"", captured.read_bytes()


if __name__ == "__main__":
    test_loading_the_cached_image_writes_nothing_to_stdout()
    test_release_version_bump_keeps_the_image_key()
    test_a_dependency_change_still_changes_the_image_key()
    test_a_dependency_pinned_at_the_project_version_is_not_normalized()
    print("test-isolation image key tests passed")
