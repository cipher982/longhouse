#!/usr/bin/env python3
"""Focused regressions for immutable OCI archive publication and restore."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.request import Request


MODULE_PATH = Path(__file__).resolve().parents[1] / "ops" / "release-artifacts.py"
_spec = importlib.util.spec_from_file_location("release_artifacts", MODULE_PATH)
assert _spec and _spec.loader
release_artifacts = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = release_artifacts
_spec.loader.exec_module(release_artifacts)


SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _descriptor(data: bytes, media_type: str) -> dict[str, Any]:
    return {"mediaType": media_type, "digest": _digest(data), "size": len(data)}


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_key: str | None = None

    def put_immutable(self, key: str, data: Any, *, content_type: str) -> bool:
        del content_type
        if hasattr(data, "read"):
            data.seek(0)
            data = data.read()
        if key == self.fail_key:
            raise release_artifacts.ArtifactError("simulated interrupted publication")
        previous = self.objects.get(key)
        if previous is not None:
            if previous != data:
                raise release_artifacts.ArtifactConflict(key)
            return False
        self.objects[key] = data
        return True

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise release_artifacts.ArtifactError(f"missing {key}") from exc


class FakeRegistry:
    def __init__(self, manifests: dict[str, tuple[bytes, str]], blobs: dict[str, bytes]) -> None:
        self.manifests = manifests
        self.blobs = blobs
        self.fetches = 0

    def fetch_manifest(self, digest: str) -> tuple[bytes, str]:
        self.fetches += 1
        return self.manifests[digest]

    def fetch_blob(self, digest: str) -> bytes:
        self.fetches += 1
        return self.blobs[digest]


def _fixture() -> tuple[FakeRegistry, str, dict[str, Any]]:
    config = (
        b'{"architecture":"amd64","config":{"Labels":{"org.opencontainers.image.revision":"0123456789abcdef0123456789abcdef01234567",'
        b'"org.longhouse.catalog-schema.version":"5","org.longhouse.catalog-schema.min-reader":"5","org.longhouse.catalog-schema.max-reader":"5"}},'
        b'"os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'
    )
    layer = b"layer bytes"
    config_ref = _descriptor(config, "application/vnd.oci.image.config.v1+json")
    layer_ref = _descriptor(layer, "application/vnd.oci.image.layer.v1.tar+gzip")
    manifest_value = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": config_ref,
        "layers": [layer_ref],
    }
    manifest = json.dumps(manifest_value, sort_keys=True, separators=(",", ":")).encode()
    root = _digest(manifest)
    registry = FakeRegistry(
        manifests={root: (manifest, manifest_value["mediaType"])},
        blobs={config_ref["digest"]: config, layer_ref["digest"]: layer},
    )
    identity = {"version": "0.1.0", "commit": SOURCE_SHA, "dirty": False, "channel": "main"}
    return registry, root, identity


def test_publish_and_fetch_complete_closure_without_registry() -> None:
    registry, root, identity = _fixture()
    store = MemoryStore()
    receipt = release_artifacts.publish_archive(
        registry=registry,
        store=store,
        image_digest=root,
        source_sha=SOURCE_SHA,
        build_run_id="1001",
        build_attempt=2,
        build_identity=identity,
        registry_image=f"ghcr.io/cipher982/longhouse-runtime@{root}",
    )
    assert receipt["sealed"] is True
    assert receipt["blob_count"] == 3
    fetch_count = registry.fetches
    with tempfile.TemporaryDirectory() as directory:
        restored = release_artifacts.fetch_archive(store=store, image_digest=root, output=Path(directory) / "layout")
        assert restored["layout_verified"] is True
        layout = json.loads((Path(directory) / "layout" / "index.json").read_text())
        assert layout["manifests"][0]["digest"] == root
        assert registry.fetches == fetch_count

def test_inspect_runtime_schema_reads_exact_image_config_labels() -> None:
    registry, root, _identity = _fixture()
    assert release_artifacts.inspect_runtime_schema(registry=registry, image_digest=root) == {
        "image_digest": root,
        "source_sha": SOURCE_SHA,
        "schema_version": 5,
        "schema_min_reader": 5,
        "schema_max_reader": 5,
    }


def test_inspect_runtime_schema_rejects_missing_labels_with_bootstrap_error() -> None:
    registry, root, _identity = _fixture()
    manifest_data, _ = registry.manifests[root]
    manifest = json.loads(manifest_data)
    config = json.loads(registry.blobs[manifest["config"]["digest"]])
    config["config"]["Labels"].pop("org.longhouse.catalog-schema.version")
    missing_schema = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_ref = _descriptor(missing_schema, "application/vnd.oci.image.config.v1+json")
    manifest["config"] = config_ref
    missing_manifest = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    missing_root = _digest(missing_manifest)
    missing_registry = FakeRegistry(
        manifests={missing_root: (missing_manifest, manifest["mediaType"])},
        blobs={config_ref["digest"]: missing_schema},
    )
    try:
        release_artifacts.inspect_runtime_schema(
            registry=missing_registry, image_digest=missing_root
        )
    except release_artifacts.ArtifactError as exc:
        assert "bootstrap" in str(exc)
    else:
        raise AssertionError("image without schema labels was accepted")


def test_fetch_accepts_oci_index_root_without_layers_field() -> None:
    config = b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'
    layer = b"index-root layer bytes"
    config_ref = _descriptor(config, "application/vnd.oci.image.config.v1+json")
    layer_ref = _descriptor(layer, "application/vnd.oci.image.layer.v1.tar+gzip")
    child_value = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": config_ref,
        "layers": [layer_ref],
    }
    child = json.dumps(child_value, sort_keys=True, separators=(",", ":")).encode()
    child_ref = _descriptor(child, child_value["mediaType"])
    root_value = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [child_ref],
    }
    root = json.dumps(root_value, sort_keys=True, separators=(",", ":")).encode()
    root_digest = _digest(root)
    registry = FakeRegistry(
        manifests={
            root_digest: (root, root_value["mediaType"]),
            child_ref["digest"]: (child, child_value["mediaType"]),
        },
        blobs={config_ref["digest"]: config, layer_ref["digest"]: layer},
    )
    store = MemoryStore()
    release_artifacts.publish_archive(
        registry=registry,
        store=store,
        image_digest=root_digest,
        source_sha=SOURCE_SHA,
        build_run_id="1002",
        build_attempt=1,
        build_identity={"version": "0.1.0", "commit": SOURCE_SHA},
    )
    with tempfile.TemporaryDirectory() as directory:
        restored = release_artifacts.fetch_archive(
            store=store,
            image_digest=root_digest,
            output=Path(directory) / "layout",
        )
        assert restored["layout_verified"] is True


def test_fetch_rejects_corrupt_or_missing_referenced_blob() -> None:
    registry, root, identity = _fixture()
    store = MemoryStore()
    release_artifacts.publish_archive(
        registry=registry,
        store=store,
        image_digest=root,
        source_sha=SOURCE_SHA,
        build_run_id="1001",
        build_attempt=1,
        build_identity=identity,
    )
    manifest = json.loads(store.objects[release_artifacts.manifest_key(root)])
    layer = next(item for item in manifest["blobs"] if item["kind"] == "layer")
    store.objects[layer["key"]] = b"corrupt"
    with tempfile.TemporaryDirectory() as directory:
        try:
            release_artifacts.fetch_archive(store=store, image_digest=root, output=Path(directory) / "corrupt")
        except release_artifacts.ArtifactError as exc:
            assert "verification failed" in str(exc)
        else:
            raise AssertionError("corrupt blob was accepted")
    del store.objects[layer["key"]]
    with tempfile.TemporaryDirectory() as directory:
        try:
            release_artifacts.fetch_archive(store=store, image_digest=root, output=Path(directory) / "missing")
        except release_artifacts.ArtifactError as exc:
            assert "missing" in str(exc)
        else:
            raise AssertionError("missing referenced blob was accepted")


def test_interrupted_publication_leaves_reusable_blobs_but_no_sealed_receipt() -> None:
    registry, root, identity = _fixture()
    store = MemoryStore()
    store.fail_key = release_artifacts.manifest_key(root)
    try:
        release_artifacts.publish_archive(
            registry=registry,
            store=store,
            image_digest=root,
            source_sha=SOURCE_SHA,
            build_run_id="1001",
            build_attempt=1,
            build_identity=identity,
        )
    except release_artifacts.ArtifactError:
        pass
    else:
        raise AssertionError("interrupted publication unexpectedly sealed")
    assert release_artifacts.manifest_key(root) not in store.objects
    assert len(store.objects) == 3
    store.fail_key = None
    receipt = release_artifacts.publish_archive(
        registry=registry,
        store=store,
        image_digest=root,
        source_sha=SOURCE_SHA,
        build_run_id="1001",
        build_attempt=1,
        build_identity=identity,
    )
    assert receipt["blobs_written"] == 0
    assert receipt["sealed"] is True


def test_digest_qualification_and_immutable_conflict() -> None:
    registry, root, identity = _fixture()
    store = MemoryStore()
    try:
        release_artifacts.RegistryClient("ghcr.io/cipher982/longhouse-runtime:latest")
    except release_artifacts.ArtifactError:
        pass
    else:
        raise AssertionError("tag-only registry reference was accepted")
    release_artifacts.publish_archive(
        registry=registry,
        store=store,
        image_digest=root,
        source_sha=SOURCE_SHA,
        build_run_id="1001",
        build_attempt=1,
        build_identity=identity,
    )
    other_identity = dict(identity, version="0.1.1")
    try:
        release_artifacts.publish_archive(
            registry=registry,
            store=store,
            image_digest=root,
            source_sha=SOURCE_SHA,
            build_run_id="1001",
            build_attempt=1,
            build_identity=other_identity,
        )
    except release_artifacts.ArtifactConflict:
        pass
    else:
        raise AssertionError("conflicting sealed metadata was accepted")


def test_registry_redirect_uses_destination_authority_without_credentials() -> None:
    request = Request(
        "https://ghcr.io/v2/owner/image/blobs/sha256:example",
        headers={"Authorization": "Bearer intentionally-not-a-credential"},
    )
    request.add_unredirected_header("Host", "ghcr.io")
    redirected = release_artifacts._SafeRedirectHandler(("https", "ghcr.io")).redirect_request(
        request, None, 307, "Temporary Redirect", {}, "https://pkg-containers.githubusercontent.com/blob"
    )
    assert redirected.host == "pkg-containers.githubusercontent.com"
    assert redirected.get_header("Host") is None
    assert redirected.get_header("Authorization") is None


if __name__ == "__main__":
    test_publish_and_fetch_complete_closure_without_registry()
    test_inspect_runtime_schema_reads_exact_image_config_labels()
    test_inspect_runtime_schema_rejects_missing_labels_with_bootstrap_error()
    test_fetch_accepts_oci_index_root_without_layers_field()
    test_fetch_rejects_corrupt_or_missing_referenced_blob()
    test_interrupted_publication_leaves_reusable_blobs_but_no_sealed_receipt()
    test_digest_qualification_and_immutable_conflict()
    test_registry_redirect_uses_destination_authority_without_credentials()
    print("release artifact tests passed")
