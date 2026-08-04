"""Dependency-free contract for the one embedding space Longhouse serves.

``config/models.json`` owns the identity, artifact, prefixes, and output
semantics.  Both the API process and the deliberately lean searchd process
import this module, so it uses only the standard library and validates the
configuration eagerly.  A malformed or mixed space is a startup error, never a
smaller corpus.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True, slots=True)
class EmbeddingArtifactFile:
    path: str
    bytes: int
    sha256: str

    @property
    def name(self) -> str:
        return Path(self.path).name


def _models_config_path() -> Path:
    override = os.getenv("MODELS_CONFIG_PATH")
    if override:
        return Path(override)
    packaged = Path(__file__).resolve().parent / "_config" / "models.json"
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parent.parent.parent / "config" / "models.json"


def _exact_keys(value: object, expected: set[str], *, subject: str) -> dict:
    if not isinstance(value, dict):
        raise RuntimeError(f"{subject} must be an object")
    actual = set(value)
    if actual != expected:
        raise RuntimeError(f"invalid {subject} fields: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")
    return value


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{field} must be a nonempty string")
    return value


def _load_contract() -> tuple[dict, dict]:
    try:
        root = json.loads(_models_config_path().read_text(encoding="utf-8"))
        embedding = root["embedding"]
        default = embedding["default"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("config/models.json has no valid embedding.default contract") from exc

    contract = _exact_keys(
        default,
        {
            "provider",
            "model",
            "dims",
            "dtype",
            "normalization",
            "truncation",
            "outputName",
            "queryPrefix",
            "documentPrefix",
            "legacyModels",
            "artifact",
        },
        subject="embedding.default",
    )
    artifact = _exact_keys(
        contract["artifact"],
        {"repository", "revision", "directory", "files"},
        subject="embedding.default.artifact",
    )
    return contract, artifact


_CONTRACT, _ARTIFACT = _load_contract()

if _CONTRACT["provider"] != "local-onnx":
    raise RuntimeError("embedding.default.provider must be local-onnx")
if type(_CONTRACT["dims"]) is not int or int(_CONTRACT["dims"]) <= 0:
    raise RuntimeError("embedding.default.dims must be a positive integer")
if (_CONTRACT["dtype"], _CONTRACT["normalization"], _CONTRACT["truncation"]) != ("float32", "l2", "prefix"):
    raise RuntimeError("embedding.default must use float32, l2 normalization, and prefix truncation")

_legacy = _CONTRACT["legacyModels"]
if not isinstance(_legacy, list) or not _legacy or not all(isinstance(value, str) and value for value in _legacy):
    raise RuntimeError("embedding.default.legacyModels must contain model ids")

_revision = _text(_ARTIFACT["revision"], field="embedding.default.artifact.revision")
if _REVISION.fullmatch(_revision) is None:
    raise RuntimeError("embedding.default.artifact.revision must be a full git SHA")
_directory = _text(_ARTIFACT["directory"], field="embedding.default.artifact.directory")
if Path(_directory).name != _directory:
    raise RuntimeError("embedding.default.artifact.directory must be one path segment")

_files = _ARTIFACT["files"]
if not isinstance(_files, list) or not _files:
    raise RuntimeError("embedding.default.artifact.files must be nonempty")
_parsed_files: list[EmbeddingArtifactFile] = []
for index, raw_file in enumerate(_files):
    entry = _exact_keys(raw_file, {"path", "bytes", "sha256"}, subject=f"embedding artifact file {index}")
    path = _text(entry["path"], field=f"embedding artifact file {index}.path")
    size = entry["bytes"]
    digest = entry["sha256"]
    if Path(path).is_absolute() or ".." in Path(path).parts or Path(path).name != Path(path).parts[-1]:
        raise RuntimeError(f"embedding artifact file {index}.path is unsafe")
    if type(size) is not int or size <= 0:
        raise RuntimeError(f"embedding artifact file {index}.bytes must be positive")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise RuntimeError(f"embedding artifact file {index}.sha256 is invalid")
    _parsed_files.append(EmbeddingArtifactFile(path=path, bytes=size, sha256=digest))
if len({entry.name for entry in _parsed_files}) != len(_parsed_files):
    raise RuntimeError("embedding artifact file basenames must be unique")

ACTIVE_EMBEDDING_MODEL: Final = _text(_CONTRACT["model"], field="embedding.default.model")
ACTIVE_EMBEDDING_DIMS: Final = int(_CONTRACT["dims"])
EMBEDDING_DTYPE: Final = "float32"
EMBEDDING_OUTPUT_NAME: Final = _text(_CONTRACT["outputName"], field="embedding.default.outputName")
QUERY_PREFIX: Final = _text(_CONTRACT["queryPrefix"], field="embedding.default.queryPrefix")
DOCUMENT_PREFIX: Final = _text(_CONTRACT["documentPrefix"], field="embedding.default.documentPrefix")
LEGACY_EMBEDDING_MODELS: Final = tuple(_legacy)
LEGACY_EMBEDDING_MODEL: Final = LEGACY_EMBEDDING_MODELS[0]

EMBEDDING_ARTIFACT_REPOSITORY: Final = _text(_ARTIFACT["repository"], field="embedding.default.artifact.repository")
EMBEDDING_ARTIFACT_REVISION: Final = _revision
EMBEDDING_ARTIFACT_DIRECTORY: Final = _directory
EMBEDDING_ARTIFACT_FILES: Final = tuple(_parsed_files)
# The projector identity is a projection-schema version, not only a model-space
# name. Bump it when provenance semantics change so catalogd reclaims every
# session instead of treating rows written under the old contract as complete.
# The trailing generation marker identifies one projected corpus. Bumping it
# re-seeds every session and forces a full re-projection, which is how a chunker
# change reaches episodes that are already complete (their hashes only get
# re-compared when the projector re-claims them).
#
# Do NOT bump it casually. `certify_projector_cutover` issues a new generation's
# certificate only from a transaction that observes zero lag, and a live instance
# with active sessions may never reach zero lag -- so a bumped generation can sit
# uncertified indefinitely while `_require_complete_projection_coverage` refuses
# every dense query for want of proof. Verified on hosted david010: bumping to p3
# put dense recall into `cutover_not_certified` immediately.
#
# The 2048-token model-tokenizer budgeting therefore applies to episodes as the
# projector re-claims them, rather than through a forced sweep. Re-embedding the
# historical corpus needs certification to accept a watermark instead of demanding
# a single zero-lag moment; that is separate work.
EMBEDDING_PROJECTOR_ID: Final = f"embeddings-{EMBEDDING_ARTIFACT_REVISION[:12]}-{ACTIVE_EMBEDDING_DIMS}d-p2"
