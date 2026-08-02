from __future__ import annotations

import hashlib
import io
from dataclasses import replace

import pytest

from zerg.services import embedding_artifact


def _entry(path: str, payload: bytes):
    template = embedding_artifact.EMBEDDING_ARTIFACT_FILES[0]
    return replace(template, path=path, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())


def test_provision_downloads_validates_and_reuses_atomic_files(tmp_path, monkeypatch):
    payloads = {"model.onnx": b"model", "tokenizer.json": b"tokenizer"}
    entries = tuple(_entry(name, payload) for name, payload in payloads.items())
    monkeypatch.setattr(embedding_artifact, "EMBEDDING_ARTIFACT_FILES", entries)
    opened: list[str] = []

    def opener(url: str):
        opened.append(url)
        return io.BytesIO(payloads[url.rsplit("/", 1)[-1]])

    root = embedding_artifact.provision_embedding_artifact(tmp_path / "model", opener=opener)
    assert embedding_artifact.validate_embedding_artifact(root)["ready"] is True
    assert sorted(path.name for path in root.iterdir()) == ["model.onnx", "tokenizer.json"]

    embedding_artifact.provision_embedding_artifact(root, opener=lambda _url: (_ for _ in ()).throw(AssertionError))
    assert len(opened) == 2


def test_corrupt_download_is_never_published(tmp_path, monkeypatch):
    entry = _entry("model.onnx", b"expected")
    monkeypatch.setattr(embedding_artifact, "EMBEDDING_ARTIFACT_FILES", (entry,))
    root = tmp_path / "model"

    with pytest.raises(embedding_artifact.EmbeddingArtifactError, match="checksum mismatch"):
        embedding_artifact.provision_embedding_artifact(root, opener=lambda _url: io.BytesIO(b"wrong"))

    assert not (root / "model.onnx").exists()
    assert list(root.glob("*.part")) == []


def test_corrupt_cached_file_is_replaced(tmp_path, monkeypatch):
    payload = b"correct"
    entry = _entry("model.onnx", payload)
    monkeypatch.setattr(embedding_artifact, "EMBEDDING_ARTIFACT_FILES", (entry,))
    root = tmp_path / "model"
    root.mkdir()
    (root / "model.onnx").write_bytes(b"corrupt")

    embedding_artifact.provision_embedding_artifact(root, opener=lambda _url: io.BytesIO(payload))

    assert (root / "model.onnx").read_bytes() == payload
