"""Provision and verify the pinned local embedding model artifact."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from zerg.embedding_space import EMBEDDING_ARTIFACT_DIRECTORY
from zerg.embedding_space import EMBEDDING_ARTIFACT_FILES
from zerg.embedding_space import EMBEDDING_ARTIFACT_REPOSITORY
from zerg.embedding_space import EMBEDDING_ARTIFACT_REVISION
from zerg.embedding_space import EmbeddingArtifactFile

_DOWNLOAD_TIMEOUT_SECONDS = 120
_COPY_BYTES = 1024 * 1024


class EmbeddingArtifactError(RuntimeError):
    """The pinned model is missing, corrupt, or could not be provisioned."""


def embedding_model_dir() -> Path:
    override = os.getenv("LONGHOUSE_EMBED_MODEL_DIR")
    if override:
        return Path(override).expanduser().resolve()
    from zerg.config import get_settings

    return (get_settings().data_dir / "models" / EMBEDDING_ARTIFACT_DIRECTORY).resolve()


def _digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _valid(path: Path, expected: EmbeddingArtifactFile) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    size, digest = _digest(path)
    return size == expected.bytes and digest == expected.sha256


def validate_embedding_artifact(model_dir: Path | None = None) -> dict[str, object]:
    root = (model_dir or embedding_model_dir()).resolve()
    invalid = [entry.name for entry in EMBEDDING_ARTIFACT_FILES if not _valid(root / entry.name, entry)]
    return {
        "ready": not invalid,
        "directory": str(root),
        "repository": EMBEDDING_ARTIFACT_REPOSITORY,
        "revision": EMBEDDING_ARTIFACT_REVISION,
        "invalid_files": invalid,
    }


def _download_url(entry: EmbeddingArtifactFile) -> str:
    remote_path = quote(entry.path, safe="/")
    return f"https://huggingface.co/{EMBEDDING_ARTIFACT_REPOSITORY}/resolve/" f"{EMBEDDING_ARTIFACT_REVISION}/{remote_path}"


def _open_url(url: str) -> BinaryIO:
    request = urllib.request.Request(url, headers={"User-Agent": "Longhouse embedding artifact provisioner"})
    return urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS)  # noqa: S310 - pinned HTTPS host and digest


def _download_one(
    root: Path,
    entry: EmbeddingArtifactFile,
    *,
    opener: Callable[[str], BinaryIO],
) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{entry.name}.", suffix=".part", dir=root, delete=False) as output:
            temporary = Path(output.name)
            digest = hashlib.sha256()
            size = 0
            with opener(_download_url(entry)) as source:
                while chunk := source.read(_COPY_BYTES):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != entry.bytes or digest.hexdigest() != entry.sha256:
            raise EmbeddingArtifactError(
                f"embedding artifact checksum mismatch for {entry.name}: bytes={size}, sha256={digest.hexdigest()}"
            )
        os.replace(temporary, root / entry.name)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def provision_embedding_artifact(
    model_dir: Path | None = None,
    *,
    opener: Callable[[str], BinaryIO] = _open_url,
) -> Path:
    """Return a checksum-verified model directory, downloading under a lock.

    Every file is downloaded to a temporary sibling and published with
    ``os.replace`` only after its exact byte count and SHA-256 match. A second
    Runtime Host process blocks on the same cross-process lock and observes
    either the previous complete file or the new complete file.
    """

    root = (model_dir or embedding_model_dir()).resolve()
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    lock_path = root.parent / f".{root.name}.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            for entry in EMBEDDING_ARTIFACT_FILES:
                target = root / entry.name
                if not _valid(target, entry):
                    _download_one(root, entry, opener=opener)
            result = validate_embedding_artifact(root)
            if result["ready"] is not True:
                raise EmbeddingArtifactError(f"embedding artifact is incomplete: {result['invalid_files']}")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return root
