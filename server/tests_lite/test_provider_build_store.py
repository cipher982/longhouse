from __future__ import annotations

import json
from pathlib import Path

import pytest

from zerg.qa.provider_build_store import ProviderBuildStoreError
from zerg.qa.provider_build_store import closure_digest
from zerg.qa.provider_build_store import materialize_generated_fake_builds
from zerg.qa.provider_build_store import materialize_staged_provider_build
from zerg.qa.provider_build_store import verify_provider_builds


def _write_executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_closure_digest_is_root_independent_and_tracks_content_and_exec_bit(tmp_path: Path) -> None:
    first = _write_executable(tmp_path / "first" / "bin" / "codex")
    second = _write_executable(tmp_path / "second" / "bin" / "codex")

    original = closure_digest(first.parents[1])
    assert closure_digest(second.parents[1]) == original

    second.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    assert closure_digest(second.parents[1]) != original
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
    second.chmod(0o644)
    assert closure_digest(second.parents[1]) != original


def test_closure_digest_hashes_symlink_target_without_following_it(tmp_path: Path) -> None:
    root = tmp_path / "closure"
    root.mkdir()
    link = root / "provider"
    link.symlink_to("target-a")
    first = closure_digest(root)

    link.unlink()
    link.symlink_to("target-b")

    assert closure_digest(root) != first


def test_generated_fake_materialization_is_idempotent_and_evidence_is_derived(tmp_path: Path) -> None:
    source = _write_executable(tmp_path / "generated" / "codex")
    store_root = tmp_path / "store"

    first = materialize_generated_fake_builds({"codex": source}, store_root=store_root)
    second = materialize_generated_fake_builds({"codex": source}, store_root=store_root)

    assert first == second
    evidence = first["codex"].to_evidence()
    assert evidence["artifact_provenance"] == "generated_fake"
    assert evidence["closure_digest"] == closure_digest(first["codex"].build_root, granularity="single_asset")
    assert evidence["closure_granularity"] == "single_asset"
    assert evidence["entrypoint"] == "bin/codex"
    lock = json.loads((store_root / "provider-builds.lock").read_text(encoding="utf-8"))
    platform_key = f"{first['codex'].platform}-{first['codex'].architecture}"
    lock_entry = lock["builds"]["codex"][first["codex"].version][platform_key]
    assert lock_entry["first_captured_at"].endswith("Z")
    assert lock_entry["closure_manifest"]["closure_manifest_version"] == 2
    assert lock_entry["closure_manifest"]["entries"][0]["path"] == "bin/codex"


def test_generated_fake_materialization_refuses_tampered_store(tmp_path: Path) -> None:
    source = _write_executable(tmp_path / "generated" / "codex")
    store_root = tmp_path / "store"
    builds = materialize_generated_fake_builds({"codex": source}, store_root=store_root)
    builds["codex"].entrypoint.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ProviderBuildStoreError, match="store is tampered"):
        materialize_generated_fake_builds({"codex": source}, store_root=store_root)
    with pytest.raises(ProviderBuildStoreError, match="build mutated"):
        verify_provider_builds(builds)


def test_provider_build_lock_is_append_only_for_existing_identity(tmp_path: Path) -> None:
    source = _write_executable(tmp_path / "generated" / "codex")
    store_root = tmp_path / "store"
    builds = materialize_generated_fake_builds({"codex": source}, store_root=store_root)
    build = builds["codex"]
    lock_path = store_root / "provider-builds.lock"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    platform_key = f"{build.platform}-{build.architecture}"
    lock["builds"]["codex"][build.version][platform_key]["closure_digest"] = "0" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(ProviderBuildStoreError, match="lock would rewrite"):
        materialize_generated_fake_builds({"codex": source}, store_root=store_root)


def test_staged_release_materializes_a_real_closure_without_acquiring_it(tmp_path: Path) -> None:
    source_root = tmp_path / "extracted"
    _write_executable(source_root / "bin" / "codex", "#!/bin/sh\necho codex-cli 1.2.3\n")
    (source_root / "resources").mkdir()
    (source_root / "resources" / "policy.json").write_text('{"mode":"proof"}\n', encoding="utf-8")

    build = materialize_staged_provider_build(
        provider="codex",
        version="1.2.3",
        source_root=source_root,
        entrypoint_relative="bin/codex",
        store_root=tmp_path / "store",
        platform_name="linux",
        architecture="amd64",
    )
    repeated = materialize_staged_provider_build(
        provider="codex",
        version="1.2.3",
        source_root=source_root,
        entrypoint_relative="bin/codex",
        store_root=tmp_path / "store",
        platform_name="linux",
        architecture="amd64",
    )

    assert repeated == build
    assert build.artifact_provenance == "staged_release"
    assert build.architecture == "x86_64"
    assert build.closure_granularity == "full_installed_tree"
    assert build.closure_digest == closure_digest(source_root) == closure_digest(build.build_root)
    assert build.entrypoint.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert (build.build_root / "resources" / "policy.json").is_file()


def test_staged_release_refuses_a_version_identity_rewrite(tmp_path: Path) -> None:
    source_root = tmp_path / "extracted"
    entrypoint = _write_executable(source_root / "codex")
    arguments = {
        "provider": "codex",
        "version": "1.2.3",
        "source_root": source_root,
        "entrypoint_relative": "codex",
        "store_root": tmp_path / "store",
        "platform_name": "linux",
        "architecture": "x86_64",
    }
    build = materialize_staged_provider_build(**arguments)
    entrypoint.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    with pytest.raises(
        ProviderBuildStoreError,
        match=rf"existing={build.closure_digest}, candidate=[0-9a-f]{{64}}",
    ):
        materialize_staged_provider_build(**arguments)


def test_staged_release_refuses_path_traversal_and_open_symlinks(tmp_path: Path) -> None:
    source_root = tmp_path / "extracted"
    _write_executable(source_root / "codex")

    with pytest.raises(ProviderBuildStoreError, match="invalid version"):
        materialize_staged_provider_build(
            provider="codex",
            version="..",
            source_root=source_root,
            entrypoint_relative="codex",
            store_root=tmp_path / "store",
        )

    (source_root / "outside").symlink_to(tmp_path / "host-provider")
    with pytest.raises(ProviderBuildStoreError, match="escapes or is dangling"):
        materialize_staged_provider_build(
            provider="codex",
            version="1.2.3",
            source_root=source_root,
            entrypoint_relative="codex",
            store_root=tmp_path / "store",
        )
