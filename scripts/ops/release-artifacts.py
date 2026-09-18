#!/usr/bin/env python3
"""Publish and restore immutable OCI runtime images.

The registry remains the distribution surface.  This helper copies the complete
OCI closure addressed by an image digest into an S3-compatible, immutable object
store.  The sealed image manifest is deliberately written last, so an interrupted
run leaves reusable blobs but never a recovery-ready receipt.

Commands:
  publish             Fetch a registry digest and seal its archive manifest.
  inspect             Read source/schema labels from one exact registry image config.
  fetch               Rebuild an OCI image layout using the archive store only.
  verify-publication  Validate a publication or post-canary verification receipt.
The object store uses ordinary AWS credentials (AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, AWS_REGION) and an explicit
S3-compatible endpoint, bucket, and prefix.  Runtime-specific environment names
are accepted for CI wiring without making the public code depend on a secret
manager.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Protocol


SCHEMA = "longhouse.runtime-oci-archive.v1"
OCI_LAYOUT_VERSION = "1.0.0"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}
_CONFLICT_CODES = {"409", "412", "PreconditionFailed", "ConditionalRequestConflict"}
_UNSUPPORTED_CONDITIONAL_CODES = {"InvalidRequest", "NotImplemented", "NotImplementedException"}


class ArtifactError(RuntimeError):
    """An archive cannot be safely published or restored."""


class ArtifactConflict(ArtifactError):
    """An immutable key already contains different bytes."""


class ArchiveStore(Protocol):
    def put_immutable(self, key: str, data: bytes | BinaryIO, *, content_type: str) -> bool: ...

    def get(self, key: str) -> bytes: ...


@dataclass(frozen=True)
class Descriptor:
    digest: str
    size: int
    media_type: str
    kind: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "size": self.size,
            "mediaType": self.media_type,
            "kind": self.kind,
            "key": blob_key(self.digest),
        }


@dataclass
class CapturedBlob:
    descriptor: Descriptor
    data: BinaryIO | None

    def close(self) -> None:
        if self.data is not None:
            self.data.close()


@dataclass
class CapturedArchive:
    image_digest: str
    root: Descriptor
    blobs: tuple[CapturedBlob, ...]
    index: dict[str, Any]

    def close(self) -> None:
        for blob in self.blobs:
            blob.close()


def normalize_digest(value: str) -> str:
    digest = value.strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ArtifactError(f"expected a sha256 image/blob digest, got {value!r}")
    return digest


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _descriptor(value: Mapping[str, Any], *, kind: str) -> Descriptor:
    try:
        digest = normalize_digest(str(value["digest"]))
        size = value["size"]
        media_type = value.get("mediaType") or value.get("media_type") or "application/octet-stream"
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError(f"invalid OCI {kind} descriptor") from exc
    if not isinstance(size, int) or size < 0:
        raise ArtifactError(f"invalid OCI {kind} descriptor size")
    if not isinstance(media_type, str) or not media_type:
        raise ArtifactError(f"invalid OCI {kind} descriptor media type")
    return Descriptor(digest=digest, size=size, media_type=media_type, kind=kind)


def blob_key(digest: str) -> str:
    digest = normalize_digest(digest)
    return f"blobs/sha256/{digest.removeprefix('sha256:')}"


def manifest_key(digest: str) -> str:
    digest = normalize_digest(digest)
    return f"images/sha256/{digest.removeprefix('sha256:')}/manifest.json"


def timing_key(digest: str, run_id: str, attempt: int) -> str:
    digest = normalize_digest(digest)
    safe_run = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
    return f"records/timing/sha256/{digest.removeprefix('sha256:')}/{safe_run}-{attempt}.json"


def qualification_key(digest: str, record_id: str) -> str:
    digest = normalize_digest(digest)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", record_id)
    if not safe_id:
        raise ArtifactError("qualification record id must not be empty")
    return f"records/qualification/sha256/{digest.removeprefix('sha256:')}/{safe_id}.json"


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return str(error.get("Code", ""))
    return str(getattr(exc, "code", "")) or str(exc).split(":", 1)[0]


class S3ArchiveStore:
    """Content-addressed immutable storage over a boto3-compatible client."""

    def __init__(self, client: Any, *, bucket: str, prefix: str = "") -> None:
        if not bucket or bucket.strip() != bucket:
            raise ArtifactError("archive bucket must be a non-empty name")
        prefix = prefix.strip("/")
        if prefix and any(part in {"", ".", ".."} for part in prefix.split("/")):
            raise ArtifactError("archive prefix must be a safe relative path")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def _key(self, key: str) -> str:
        if not key or key.startswith("/") or ".." in Path(key).parts:
            raise ArtifactError(f"unsafe archive object key: {key!r}")
        return f"{self.prefix}/{key}" if self.prefix else key

    def _head_existing(self, key: str) -> tuple[int, Mapping[str, Any]] | None:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:  # boto3-compatible clients expose ClientError lazily.
            if _error_code(exc) in _NOT_FOUND_CODES:
                return None
            raise ArtifactError(f"archive object head failed: {_error_code(exc)}") from exc
        size = response.get("ContentLength") if isinstance(response, Mapping) else None
        metadata = response.get("Metadata") if isinstance(response, Mapping) else None
        if not isinstance(size, int) or size < 0 or not isinstance(metadata, Mapping):
            raise ArtifactError(f"archive object head is malformed: {key}")
        return size, metadata

    def _read_existing(self, key: str) -> bytes | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:  # boto3-compatible clients expose ClientError lazily.
            if _error_code(exc) in _NOT_FOUND_CODES:
                return None
            raise ArtifactError(f"archive object read failed: {_error_code(exc)}") from exc
        body = response.get("Body") if isinstance(response, Mapping) else None
        if body is None or not hasattr(body, "read"):
            raise ArtifactError("archive object response has no readable body")
        data = body.read()
        close = getattr(body, "close", None)
        if callable(close):
            close()
        if not isinstance(data, bytes):
            raise ArtifactError("archive object response was not bytes")
        return data
    def get(self, key: str) -> bytes:
        data = self._read_existing(key)
        if data is None:
            raise ArtifactError(f"archive object is missing: {key}")
        return data
    def get_stream(self, key: str) -> BinaryIO:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            raise ArtifactError(f"archive object read failed: {_error_code(exc)}") from exc
        body = response.get("Body") if isinstance(response, Mapping) else None
        if body is None or not hasattr(body, "read"):
            raise ArtifactError("archive object response has no readable body")
        return body

    def has_immutable(self, key: str, *, digest: str, size: int) -> bool:
        """Check a content-addressed object using metadata without downloading it."""
        head = self._head_existing(key)
        return bool(
            head is not None
            and head[0] == size
            and head[1].get("sha256") == normalize_digest(digest).removeprefix("sha256:")
        )

    def _payload_info(self, data: bytes | BinaryIO) -> tuple[int, str]:
        if isinstance(data, bytes):
            return len(data), hashlib.sha256(data).hexdigest()
        if not hasattr(data, "seek") or not hasattr(data, "read"):
            raise ArtifactError("archive object payload must be bytes or seekable binary data")
        data.seek(0)
        hasher = hashlib.sha256()
        size = 0
        while True:
            chunk = data.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            hasher.update(chunk)
        data.seek(0)
        return size, hasher.hexdigest()

    def put_immutable(self, key: str, data: bytes | BinaryIO, *, content_type: str = "application/octet-stream") -> bool:
        """Put once, or prove that an existing object is byte-identical."""
        size, sha256 = self._payload_info(data)
        existing_head = self._head_existing(key)
        if existing_head is not None:
            existing_size, metadata = existing_head
            if existing_size == size and metadata.get("sha256") == sha256:
                return False
            raise ArtifactConflict(f"immutable archive object differs: {key}")
        request = {
            "Bucket": self.bucket,
            "Key": self._key(key),
            "Body": data,
            "ContentLength": size,
            "ContentType": content_type,
            "Metadata": {"sha256": sha256},
        }
        try:
            self.client.put_object(**request, IfNoneMatch="*")
        except Exception as exc:
            code = _error_code(exc)
            if code in _CONFLICT_CODES:
                existing_head = self._head_existing(key)
                if existing_head is None:
                    raise ArtifactError(f"archive immutable race has no readable winner: {key}") from exc
                existing_size, metadata = existing_head
                if existing_size == size and metadata.get("sha256") == sha256:
                    return False
                raise ArtifactConflict(f"immutable archive object differs after race: {key}") from exc
            if code in _UNSUPPORTED_CONDITIONAL_CODES:
                # Never fall back to an unconditional overwrite: a concurrent
                # writer could win between the preflight and this point.
                raise ArtifactError(
                    f"archive store does not support conditional immutable writes: {key}"
                ) from exc
            raise ArtifactError(f"archive object write failed: {code}") from exc
        stored_head = self._head_existing(key)
        if stored_head is None:
            raise ArtifactError(f"archive object disappeared after write: {key}")
        stored_size, metadata = stored_head
        if stored_size != size or metadata.get("sha256") != sha256:
            raise ArtifactError(f"archive object failed read-back verification: {key}")
        return True


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def store_from_environment(args: argparse.Namespace) -> S3ArchiveStore:
    endpoint = args.endpoint or _env_first("RUNTIME_ARTIFACT_S3_ENDPOINT", "ARTIFACT_STORE_S3_ENDPOINT")
    bucket = args.bucket or _env_first("RUNTIME_ARTIFACT_S3_BUCKET", "ARTIFACT_STORE_BUCKET")
    prefix = args.prefix if args.prefix is not None else (_env_first("RUNTIME_ARTIFACT_S3_PREFIX", "ARTIFACT_STORE_PREFIX") or "")
    if not endpoint:
        raise ArtifactError("archive destination is not configured: set RUNTIME_ARTIFACT_S3_ENDPOINT")
    if not bucket:
        raise ArtifactError("archive destination is not configured: set RUNTIME_ARTIFACT_S3_BUCKET")
    if not prefix:
        raise ArtifactError("archive destination is not configured: set RUNTIME_ARTIFACT_S3_PREFIX")
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArtifactError("boto3 is required for S3-compatible archive publication") from exc
    client_kwargs: dict[str, Any] = {
        "endpoint_url": endpoint,
        "region_name": args.region or _env_first("RUNTIME_ARTIFACT_S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION") or "us-east-1",
    }
    access_key = _env_first("RUNTIME_ARTIFACT_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    secret_key = _env_first("RUNTIME_ARTIFACT_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    session_token = _env_first("RUNTIME_ARTIFACT_S3_SESSION_TOKEN", "AWS_SESSION_TOKEN")
    if access_key:
        client_kwargs["aws_access_key_id"] = access_key
    if secret_key:
        client_kwargs["aws_secret_access_key"] = secret_key
    if session_token:
        client_kwargs["aws_session_token"] = session_token
    return S3ArchiveStore(boto3.client("s3", **client_kwargs), bucket=bucket, prefix=prefix)


class Registry(Protocol):
    def fetch_manifest(self, digest: str) -> tuple[bytes, str]: ...

    def fetch_blob(self, digest: str) -> bytes | BinaryIO: ...


def _parse_image_reference(value: str) -> tuple[str, str, str]:
    if "@" not in value:
        raise ArtifactError("registry image reference must be digest-qualified (image@sha256:...)")
    repository_ref, digest_value = value.rsplit("@", 1)
    digest = normalize_digest(digest_value)
    parsed = urllib.parse.urlsplit(repository_ref if "://" in repository_ref else "https://" + repository_ref)
    registry = parsed.netloc
    repository = parsed.path.strip("/")
    if not registry or not repository or "/" not in repository:
        raise ArtifactError(f"invalid registry image reference: {value!r}")
    return registry, repository, digest

class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, registry_origin: tuple[str, str]) -> None:
        super().__init__()
        self.registry_origin = registry_origin

    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.username or target.password:
            raise ArtifactError("registry redirects must remain HTTPS without URL credentials")
        target_origin = (target.scheme.lower(), target.netloc.lower())
        redirected_headers = dict(request.headers)
        redirected_headers.pop("Host", None)
        if target_origin != self.registry_origin:
            redirected_headers.pop("Authorization", None)
        return urllib.request.Request(
            newurl,
            data=request.data,
            headers=redirected_headers,
            origin_req_host=request.origin_req_host,
            unverifiable=True,
            method=request.get_method(),
        )


class RegistryClient:

    def __init__(self, image: str, *, username: str | None = None, password: str | None = None, token: str | None = None) -> None:
        self.registry, self.repository, self.image_digest = _parse_image_reference(image)
        self.scheme = "https"
        parsed = urllib.parse.urlsplit(image if "://" in image else "https://" + image)
        if parsed.scheme in {"http", "https"}:
            self.scheme = parsed.scheme
        self.username = username
        self.password = password
        self.token = token
        if self.scheme != "https" and (self.username or self.password or self.token):
            raise ArtifactError("refusing registry credentials over plaintext HTTP")
        self._bearer: str | None = token
        self._opener = urllib.request.build_opener(
            _SafeRedirectHandler(("https", self.registry.lower()))
        )
    def _url(self, suffix: str) -> str:
        return f"{self.scheme}://{self.registry}/v2/{self.repository}/{suffix}"

    def _request(self, suffix: str, *, accept: str | None = None, sink: BinaryIO | None = None) -> tuple[bytes, Mapping[str, str]]:
        headers = {"Accept": accept} if accept else {}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        request = urllib.request.Request(self._url(suffix), headers=headers)
        try:
            with self._opener.open(request) as response:
                return self._read_response(response, sink)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise ArtifactError(f"registry request failed: HTTP {exc.code}") from exc
            challenge = exc.headers.get("WWW-Authenticate", "")
            self._authenticate(challenge)
            headers["Authorization"] = f"Bearer {self._bearer}"
            request = urllib.request.Request(self._url(suffix), headers=headers)
            if sink is not None:
                sink.seek(0)
                sink.truncate(0)
            try:
                with self._opener.open(request) as response:
                    return self._read_response(response, sink)
            except urllib.error.HTTPError as retry_exc:
                raise ArtifactError(f"registry request failed after auth: HTTP {retry_exc.code}") from retry_exc
        except urllib.error.URLError as exc:
            raise ArtifactError(f"registry request failed: {exc.reason}") from exc

    @staticmethod
    def _read_response(response: Any, sink: BinaryIO | None) -> tuple[bytes, Mapping[str, str]]:
        if sink is None:
            return response.read(), response.headers
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            sink.write(chunk)
        return b"", response.headers

    def _authenticate(self, challenge: str) -> None:
        if not challenge.lower().startswith("bearer "):
            raise ArtifactError("registry requires unsupported authentication")
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge[7:]))
        realm = params.get("realm")
        if not realm:
            raise ArtifactError("registry bearer challenge has no realm")
        realm_parts = urllib.parse.urlsplit(realm)
        if realm_parts.scheme != self.scheme or realm_parts.netloc != self.registry:
            raise ArtifactError("registry auth realm must match the registry origin")
        query = {
            "service": params.get("service", self.registry),
            "scope": params.get("scope", f"repository:{self.repository}:pull"),
        }
        token_url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(query)
        request = urllib.request.Request(token_url)
        if self.username and self.password:
            raw = f"{self.username}:{self.password}".encode("utf-8")
            request.add_header("Authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
        try:
            with self._opener.open(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ArtifactError("registry bearer token request failed") from exc
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ArtifactError("registry bearer token response had no token")
        self._bearer = token

    def fetch_manifest(self, digest: str) -> tuple[bytes, str]:
        digest = normalize_digest(digest)
        data, headers = self._request(
            f"manifests/{digest}",
            accept=", ".join(
                (
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                )
            ),
        )
        if digest_bytes(data) != digest:
            raise ArtifactError(f"registry manifest digest mismatch for {digest}")
        media_type = headers.get("Content-Type", "application/vnd.oci.image.manifest.v1+json").split(";", 1)[0]
        return data, media_type

    def fetch_blob(self, digest: str) -> BinaryIO:
        digest = normalize_digest(digest)
        payload = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        hasher = hashlib.sha256()

        class HashingSink:
            def write(self, chunk: bytes) -> int:
                hasher.update(chunk)
                return payload.write(chunk)

        self._request(f"blobs/{digest}", sink=HashingSink())
        if "sha256:" + hasher.hexdigest() != digest:
            payload.close()
            raise ArtifactError(f"registry blob digest mismatch for {digest}")
        payload.seek(0)
        return payload


def _json_object(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"{what} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ArtifactError(f"{what} must be a JSON object")
    return value


def validate_publication_receipt(
    receipt: Mapping[str, Any],
    *,
    source_sha: str,
    source_workflow: str,
    source_order: int | None = None,
    build_run_id: str | None = None,
    build_attempt: int | None = None,
    require_verification: bool = False,
) -> dict[str, Any]:
    """Validate one immutable publication/verification envelope."""

    schema = receipt.get("schema")
    allowed_schemas = {
        "longhouse.runtime-publication.v1",
        "longhouse.runtime-verification.v1",
    }
    if schema not in allowed_schemas:
        raise ArtifactError("publication receipt has an unsupported schema")
    receipt_sha = receipt.get("source_sha")
    if not isinstance(receipt_sha, str) or not _GIT_SHA_RE.fullmatch(receipt_sha):
        raise ArtifactError("publication receipt has no full source revision")
    if receipt_sha != source_sha:
        raise ArtifactError("publication receipt source revision does not match the requested source")
    receipt_workflow = receipt.get("source_workflow")
    if receipt_workflow != source_workflow:
        raise ArtifactError("publication receipt workflow does not match the selected publishing workflow")
    receipt_order = receipt.get("source_order")
    if type(receipt_order) is not int or receipt_order < 1:
        raise ArtifactError("publication receipt has no positive workflow run number")
    if source_order is not None and receipt_order != source_order:
        raise ArtifactError("publication receipt workflow order does not match the selected publishing run")
    receipt_run_id = receipt.get("build_run_id")
    if not isinstance(receipt_run_id, str) or not re.fullmatch(r"[1-9][0-9]*", receipt_run_id):
        raise ArtifactError("publication receipt has no positive publishing run id")
    if build_run_id is not None and receipt_run_id != build_run_id:
        raise ArtifactError("publication receipt run id does not match the selected publishing run")
    receipt_attempt = receipt.get("build_attempt")
    if type(receipt_attempt) is not int or receipt_attempt < 1:
        raise ArtifactError("publication receipt has no positive publishing run attempt")
    if build_attempt is not None and receipt_attempt != build_attempt:
        raise ArtifactError("publication receipt attempt does not match the selected publishing run")
    qualification_id = receipt.get("qualification_id")
    expected_qualification = f"runtime-image-{receipt_run_id}-{receipt_attempt}"
    if qualification_id != expected_qualification:
        raise ArtifactError("publication receipt qualification is not tied to its publishing run")
    image_digest = receipt.get("image_digest")
    if not isinstance(image_digest, str) or not _SHA256_RE.fullmatch(image_digest):
        raise ArtifactError("publication receipt has no immutable image digest")

    canary_deployment_id: str | None = None
    if schema == "longhouse.runtime-verification.v1" or require_verification:
        verification = receipt.get("verification")
        if not isinstance(verification, Mapping):
            raise ArtifactError("publication receipt has no functional canary verification")
        if verification.get("functional_smoke") != "success":
            raise ArtifactError("publication receipt has no successful functional canary smoke")
        value = verification.get("canary_deployment_id")
        if not isinstance(value, str) or not value:
            raise ArtifactError("publication receipt has no canary deployment id")
        canary_deployment_id = value
    result = {
        "schema": schema,
        "image_digest": image_digest,
        "source_sha": receipt_sha,
        "source_workflow": receipt_workflow,
        "source_order": receipt_order,
        "build_run_id": receipt_run_id,
        "build_attempt": receipt_attempt,
        "qualification_id": qualification_id,
    }
    if canary_deployment_id is not None:
        result["canary_deployment_id"] = canary_deployment_id
    return result


def inspect_runtime_schema(*, registry: Registry, image_digest: str) -> dict[str, Any]:
    """Read source and schema labels from one exact OCI image.

    Labels are emitted from the same source checkout as the image and travel
    with the config blob into the immutable archive closure.  Missing or
    malformed labels are an error: callers must bootstrap historical images
    explicitly rather than guessing metadata from a mutable tag or checkout.
    """

    image_digest = normalize_digest(image_digest)
    manifest_data, _ = registry.fetch_manifest(image_digest)
    manifest = _json_object(manifest_data, "OCI image manifest")
    # Buildx may return an OCI index digest even for a single-platform build.
    # Resolve that exact immutable root to its linux/amd64 child before reading
    # the config; never inspect a local checkout or a tag-selected image.
    while "config" not in manifest:
        values = manifest.get("manifests")
        if not isinstance(values, list) or not values:
            raise ArtifactError(
                "selected OCI image has no platform config; bootstrap this historical image before promotion"
            )
        candidates = [
            value
            for value in values
            if isinstance(value, Mapping)
            and isinstance(value.get("platform"), Mapping)
            and value["platform"].get("os") == "linux"
            and value["platform"].get("architecture") == "amd64"
        ]
        if not candidates and len(values) == 1 and isinstance(values[0], Mapping):
            candidates = [values[0]]
        if len(candidates) != 1:
            raise ArtifactError(
                "selected OCI image index has no unique linux/amd64 config; bootstrap this image before promotion"
            )
        child = _descriptor(candidates[0], kind="manifest")
        manifest_data, _ = registry.fetch_manifest(child.digest)
        manifest = _json_object(manifest_data, "OCI image platform manifest")

    config_value = manifest.get("config")
    if not isinstance(config_value, Mapping):
        raise ArtifactError(
            "selected OCI image must be a platform manifest with a config descriptor; "
            "bootstrap this historical image before promotion"
        )
    config_descriptor = _descriptor(config_value, kind="config")
    config_blob = registry.fetch_blob(config_descriptor.digest)
    try:
        if isinstance(config_blob, bytes):
            config_data = config_blob
        else:
            config_data = config_blob.read()
    finally:
        close = getattr(config_blob, "close", None)
        if callable(close):
            close()
    if not isinstance(config_data, bytes) or len(config_data) != config_descriptor.size:
        raise ArtifactError("OCI image config size does not match its descriptor")
    if digest_bytes(config_data) != config_descriptor.digest:
        raise ArtifactError("OCI image config digest does not match its descriptor")
    config = _json_object(config_data, "OCI image config")
    config_section = config.get("config")
    labels = config_section.get("Labels") if isinstance(config_section, Mapping) else None
    if not isinstance(labels, Mapping):
        raise ArtifactError(
            "selected OCI image has no source/schema labels; bootstrap this historical image before promotion"
        )

    source_sha = labels.get("org.opencontainers.image.revision")
    if not isinstance(source_sha, str) or not _GIT_SHA_RE.fullmatch(source_sha.strip().lower()):
        raise ArtifactError(
            "selected OCI image has no full source revision label; bootstrap this historical image before promotion"
        )
    label_names = {
        "schema_version": "org.longhouse.catalog-schema.version",
        "schema_min_reader": "org.longhouse.catalog-schema.min-reader",
        "schema_max_reader": "org.longhouse.catalog-schema.max-reader",
    }
    result: dict[str, Any] = {
        "image_digest": image_digest,
        "source_sha": source_sha.strip().lower(),
    }
    for field, label in label_names.items():
        value = labels.get(label)
        if not isinstance(value, str) or not value.isdigit():
            raise ArtifactError(
                f"selected OCI image has no numeric {label} label; "
                "bootstrap this historical image before promotion"
            )
        result[field] = int(value, 10)
    return result


def capture_oci_closure(
    registry: Registry,
    image_digest: str,
    *,
    existing_blob: Callable[[Descriptor], bool] | None = None,
) -> CapturedArchive:
    image_digest = normalize_digest(image_digest)
    blobs: dict[str, CapturedBlob] = {}
    visiting: set[str] = set()
    def add_blob(descriptor: Descriptor, data: bytes | BinaryIO) -> None:
        if isinstance(data, bytes):
            payload: BinaryIO = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
            payload.write(data)
            payload.seek(0)
        else:
            payload = data
            payload.seek(0)
        hasher = hashlib.sha256()
        size = 0
        while True:
            chunk = payload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            hasher.update(chunk)
        if size != descriptor.size:
            payload.close()
            raise ArtifactError(f"registry {descriptor.kind} size mismatch for {descriptor.digest}")
        if "sha256:" + hasher.hexdigest() != descriptor.digest:
            payload.close()
            raise ArtifactError(f"registry {descriptor.kind} digest mismatch for {descriptor.digest}")
        payload.seek(0)
        previous = blobs.get(descriptor.digest)
        if previous is not None:
            if previous.descriptor.size != descriptor.size:
                payload.close()
                raise ArtifactError(f"conflicting OCI descriptors for {descriptor.digest}")
            payload.close()
            return
        blobs[descriptor.digest] = CapturedBlob(descriptor=descriptor, data=payload)

    def visit_manifest(descriptor: Descriptor) -> None:
        if descriptor.digest in visiting:
            raise ArtifactError(f"cyclic OCI manifest closure at {descriptor.digest}")
        existing = blobs.get(descriptor.digest)
        if existing is not None:
            if existing.descriptor.size != descriptor.size:
                raise ArtifactError(f"conflicting OCI descriptor sizes for {descriptor.digest}")
            return
        visiting.add(descriptor.digest)
        manifest_data, content_type = registry.fetch_manifest(descriptor.digest)
        if len(manifest_data) != descriptor.size:
            raise ArtifactError(f"registry manifest size mismatch for {descriptor.digest}")
        actual = Descriptor(descriptor.digest, len(manifest_data), content_type or descriptor.media_type, descriptor.kind)
        add_blob(actual, manifest_data)
        document = _json_object(manifest_data, f"OCI manifest {descriptor.digest}")
        index_descriptors = document.get("manifests")
        if isinstance(index_descriptors, list):
            if not index_descriptors:
                raise ArtifactError(f"OCI index has no manifests: {descriptor.digest}")
            for child in index_descriptors:
                if not isinstance(child, Mapping):
                    raise ArtifactError("OCI index contains a non-object descriptor")
                visit_manifest(_descriptor(child, kind="manifest"))
        elif "config" not in document:
            raise ArtifactError(f"OCI image manifest has no config: {descriptor.digest}")
        for field, kind in (("config", "config"), ("layers", "layer")):
            if field not in document:
                continue
            values: Iterable[Any] = [document[field]] if field == "config" else document[field]
            if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
                raise ArtifactError(f"OCI manifest field {field} is malformed")
            for child in values:
                if not isinstance(child, Mapping):
                    raise ArtifactError(f"OCI manifest field {field} contains a non-object descriptor")
                child_descriptor = _descriptor(child, kind=kind)
                if child_descriptor.digest in blobs:
                    if blobs[child_descriptor.digest].descriptor.size != child_descriptor.size:
                        raise ArtifactError(f"conflicting OCI descriptor sizes for {child_descriptor.digest}")
                    continue
                if existing_blob is not None and existing_blob(child_descriptor):
                    blobs[child_descriptor.digest] = CapturedBlob(descriptor=child_descriptor, data=None)
                else:
                    add_blob(child_descriptor, registry.fetch_blob(child_descriptor.digest))
        subject = document.get("subject")
        if isinstance(subject, Mapping):
            visit_manifest(_descriptor(subject, kind="manifest"))
        visiting.remove(descriptor.digest)

    root_data, root_media_type = registry.fetch_manifest(image_digest)
    root = Descriptor(image_digest, len(root_data), root_media_type or "application/vnd.oci.image.manifest.v1+json", "manifest")
    visit_manifest(root)
    root = blobs[image_digest].descriptor
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": root.media_type,
                "digest": root.digest,
                "size": root.size,
            }
        ],
    }
    return CapturedArchive(image_digest=image_digest, root=root, blobs=tuple(blobs[key] for key in sorted(blobs)), index=index)


def _validate_identity(identity: Mapping[str, Any], source_sha: str) -> None:
    if not isinstance(identity, Mapping) or not identity:
        raise ArtifactError("build identity must be a non-empty JSON object")
    commit = identity.get("commit") or identity.get("source_sha")
    if not isinstance(commit, str) or commit.lower() != source_sha:
        raise ArtifactError("build identity commit does not match source SHA")


def _load_json_path(path: str | None, label: str) -> dict[str, Any]:
    if not path:
        raise ArtifactError(f"{label} is required")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"unable to read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ArtifactError(f"{label} must be a JSON object")
    return value


def publish_archive(
    *,
    registry: Registry,
    store: ArchiveStore,
    image_digest: str,
    source_sha: str,
    build_run_id: str,
    build_attempt: int,
    build_identity: Mapping[str, Any],
    component: str = "longhouse-runtime",
    registry_image: str | None = None,
    timing: Mapping[str, Any] | None = None,
    qualification: Mapping[str, Any] | None = None,
    qualification_id: str | None = None,
) -> dict[str, Any]:
    image_digest = normalize_digest(image_digest)
    source_sha = source_sha.strip().lower()
    if not _GIT_SHA_RE.fullmatch(source_sha):
        raise ArtifactError("source SHA must be the full 40-character commit SHA")
    if not build_run_id or not build_run_id.strip():
        raise ArtifactError("build run ID is required")
    if not isinstance(build_attempt, int) or build_attempt < 1:
        raise ArtifactError("build attempt must be a positive integer")
    if not component or not isinstance(component, str):
        raise ArtifactError("component must be non-empty")
    if qualification is not None and (not isinstance(qualification_id, str) or not qualification_id):
        raise ArtifactError("qualification_id is required when qualification is provided")
    if timing is not None and not isinstance(timing, Mapping):
        raise ArtifactError("timing record must be a JSON object")
    if qualification is not None and not isinstance(qualification, Mapping):
        raise ArtifactError("qualification record must be a JSON object")
    _validate_identity(build_identity, source_sha)
    identity_value = json.loads(json.dumps(build_identity, sort_keys=True))
    checker = getattr(store, "has_immutable", None)
    existing_blob = (
        (lambda descriptor: descriptor.kind in {"config", "layer"} and checker(
            blob_key(descriptor.digest), digest=descriptor.digest, size=descriptor.size
        ))
        if callable(checker)
        else None
    )
    captured = capture_oci_closure(registry, image_digest, existing_blob=existing_blob)
    written_blobs = 0
    try:
        for captured_blob in captured.blobs:
            if captured_blob.data is None:
                continue
            captured_blob.data.seek(0)
            if store.put_immutable(
                blob_key(captured_blob.descriptor.digest),
                captured_blob.data,
                content_type=captured_blob.descriptor.media_type,
            ):
                written_blobs += 1
    finally:
        captured.close()
    archive_manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "component": component,
        "image_digest": image_digest,
        "source_sha": source_sha,
        "build_run_id": str(build_run_id),
        "build_attempt": build_attempt,
        "build_identity": identity_value,
        "registry_image": registry_image or None,
        "root": captured.root.as_dict(),
        "oci_layout": {"imageLayoutVersion": OCI_LAYOUT_VERSION},
        "oci_index": captured.index,
        "blobs": [blob.descriptor.as_dict() for blob in captured.blobs],
        "sealed": True,
    }
    sealed_bytes = canonical_json(archive_manifest)
    manifest_reused = not store.put_immutable(
        manifest_key(image_digest), sealed_bytes, content_type="application/json"
    )
    if timing is not None:
        timing_record = {
            "schema": "longhouse.runtime-oci-archive-timing.v1",
            "image_digest": image_digest,
            "source_sha": source_sha,
            "build_run_id": str(build_run_id),
            "build_attempt": build_attempt,
            "timing": dict(timing),
        }
        if isinstance(timing.get("recorded_at"), str):
            timing_record["recorded_at"] = timing["recorded_at"]
        store.put_immutable(
            timing_key(image_digest, str(build_run_id), build_attempt),
            canonical_json(timing_record),
            content_type="application/json",
        )
    if qualification is not None:
        if not qualification_id:
            raise ArtifactError("qualification_id is required when qualification is provided")
        qualification_record = {
            "schema": "longhouse.runtime-oci-archive-qualification.v1",
            "image_digest": image_digest,
            "source_sha": source_sha,
            "build_run_id": str(build_run_id),
            "build_attempt": build_attempt,
            "qualification": dict(qualification),
        }
        if isinstance(qualification.get("recorded_at"), str):
            qualification_record["recorded_at"] = qualification["recorded_at"]
        store.put_immutable(
            qualification_key(image_digest, qualification_id),
            canonical_json(qualification_record),
            content_type="application/json",
        )
    # stdout is the sealed receipt consumed by private builds.  Return the
    # complete manifest identity rather than a summary that would force a
    # second, mutable lookup.
    return {
        **archive_manifest,
        "manifest_key": manifest_key(image_digest),
        "blob_count": len(captured.blobs),
        "blobs_written": written_blobs,
        "manifest_reused": manifest_reused,
    }


def _check_descriptor_record(value: Mapping[str, Any]) -> Descriptor:
    descriptor = _descriptor(value, kind=str(value.get("kind") or "blob"))
    if value.get("key") != blob_key(descriptor.digest):
        raise ArtifactError(f"archive descriptor has an invalid key for {descriptor.digest}")
    return descriptor


def _validate_layout_closure(manifest: Mapping[str, Any], objects: Mapping[str, Path]) -> None:
    root_value = manifest.get("root")
    if not isinstance(root_value, Mapping):
        raise ArtifactError("archive manifest has no root descriptor")
    root = _check_descriptor_record(root_value)
    if root.digest not in objects:
        raise ArtifactError(f"archive is missing root manifest blob {root.digest}")
    seen: set[str] = set()
    visiting: set[str] = set()

    def walk(descriptor: Descriptor) -> None:
        if descriptor.digest in visiting:
            raise ArtifactError(f"archive contains cyclic manifest closure at {descriptor.digest}")
        if descriptor.digest in seen:
            return
        path = objects.get(descriptor.digest)
        if path is None:
            raise ArtifactError(f"archive is missing referenced blob {descriptor.digest}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ArtifactError(f"archive blob is unreadable: {descriptor.digest}") from exc
        if size != descriptor.size:
            raise ArtifactError(f"archive blob verification failed for {descriptor.digest}")
        seen.add(descriptor.digest)
        if descriptor.kind not in {"manifest", "index"}:
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ArtifactError(f"archive manifest is unreadable: {descriptor.digest}") from exc
        if digest_bytes(data) != descriptor.digest:
            raise ArtifactError(f"archive blob verification failed for {descriptor.digest}")
        document = _json_object(data, f"archived manifest {descriptor.digest}")
        visiting.add(descriptor.digest)
        index_descriptors = document.get("manifests")
        if isinstance(index_descriptors, list):
            if not index_descriptors:
                raise ArtifactError(f"archived index has no manifests: {descriptor.digest}")
            for child in index_descriptors:
                if not isinstance(child, Mapping):
                    raise ArtifactError("archived index contains a malformed descriptor")
                walk(_descriptor(child, kind="manifest"))
        elif not isinstance(document.get("config"), Mapping):
            raise ArtifactError(f"archived image manifest has no config: {descriptor.digest}")
        if "layers" in document and not isinstance(document["layers"], list):
            raise ArtifactError(f"archived image manifest layers are malformed: {descriptor.digest}")
        for field, kind in (("config", "config"), ("layers", "layer")):
            if field == "config" and "config" in document:
                values = [document["config"]]
            elif field == "layers" and "layers" in document:
                values = document["layers"]
            else:
                values = []
            for child in values:
                if not isinstance(child, Mapping):
                    raise ArtifactError(f"archived manifest field {field} is malformed")
                walk(_descriptor(child, kind=kind))
        subject = document.get("subject")
        if isinstance(subject, Mapping):
            walk(_descriptor(subject, kind="manifest"))
        visiting.remove(descriptor.digest)

    walk(root)


def fetch_archive(*, store: ArchiveStore, image_digest: str, output: Path, overwrite: bool = False) -> dict[str, Any]:
    image_digest = normalize_digest(image_digest)
    manifest_data = store.get(manifest_key(image_digest))
    manifest = _json_object(manifest_data, "sealed archive manifest")
    if manifest.get("schema") != SCHEMA or manifest.get("image_digest") != image_digest or manifest.get("sealed") is not True:
        raise ArtifactError("sealed archive manifest is not a valid immutable OCI archive")
    descriptors_value = manifest.get("blobs")
    if not isinstance(descriptors_value, list) or not descriptors_value:
        raise ArtifactError("sealed archive manifest has no blob closure")
    descriptors: dict[str, Descriptor] = {}
    for value in descriptors_value:
        if not isinstance(value, Mapping):
            raise ArtifactError("sealed archive manifest has malformed blob descriptor")
        descriptor = _check_descriptor_record(value)
        prior = descriptors.get(descriptor.digest)
        if prior is not None and prior != descriptor:
            raise ArtifactError(f"sealed archive has conflicting descriptors for {descriptor.digest}")
        descriptors[descriptor.digest] = descriptor
    if image_digest not in descriptors:
        raise ArtifactError("sealed archive blob closure does not include image digest")
    index = manifest.get("oci_index")
    layout = manifest.get("oci_layout")
    if not isinstance(index, Mapping) or not isinstance(layout, Mapping) or layout.get("imageLayoutVersion") != OCI_LAYOUT_VERSION:
        raise ArtifactError("sealed archive manifest has invalid OCI layout metadata")

    output = output.expanduser().resolve()
    if output.exists():
        if not overwrite:
            raise ArtifactError(f"output path already exists: {output}")
        if output.is_dir():
            shutil.rmtree(output)
        else:
            output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        blob_root = temp_path / "blobs" / "sha256"
        blob_root.mkdir(parents=True)
        objects: dict[str, Path] = {}
        stream_get = getattr(store, "get_stream", None)
        for digest, descriptor in descriptors.items():
            source = stream_get(blob_key(digest)) if callable(stream_get) else store.get(blob_key(digest))
            blob_path = blob_root / digest.removeprefix("sha256:")
            hasher = hashlib.sha256()
            size = 0
            try:
                with blob_path.open("wb") as output_blob:
                    if isinstance(source, bytes):
                        chunks = (source[offset : offset + 1024 * 1024] for offset in range(0, len(source), 1024 * 1024))
                    else:
                        chunks = iter(lambda: source.read(1024 * 1024), b"")
                    for chunk in chunks:
                        size += len(chunk)
                        hasher.update(chunk)
                        output_blob.write(chunk)
            finally:
                if not isinstance(source, bytes):
                    close = getattr(source, "close", None)
                    if callable(close):
                        close()
            if size != descriptor.size or "sha256:" + hasher.hexdigest() != digest:
                raise ArtifactError(f"archived blob verification failed for {digest}")
            objects[digest] = blob_path
        _validate_layout_closure(manifest, objects)
        (temp_path / "oci-layout").write_bytes(canonical_json(dict(layout)))
        (temp_path / "index.json").write_bytes(canonical_json(dict(index)))
        temp_path.replace(output)
    except Exception:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise
    return {
        "schema": SCHEMA,
        "image_digest": image_digest,
        "output": str(output),
        "blob_count": len(objects),
        "layout_verified": True,
    }


def _registry_from_environment(args: argparse.Namespace, image: str) -> RegistryClient:
    username = args.registry_username or _env_first("REGISTRY_USERNAME", "GITHUB_ACTOR")
    password = args.registry_password or _env_first("REGISTRY_PASSWORD", "GITHUB_TOKEN")
    token = args.registry_token or _env_first("REGISTRY_TOKEN")
    return RegistryClient(image, username=username, password=password, token=token)


def _store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", help="S3-compatible endpoint URL (or RUNTIME_ARTIFACT_S3_ENDPOINT)")
    parser.add_argument("--bucket", help="archive bucket (or RUNTIME_ARTIFACT_S3_BUCKET)")
    parser.add_argument("--prefix", default=None, help="archive object prefix (or RUNTIME_ARTIFACT_S3_PREFIX)")
    parser.add_argument("--region", help="object-store signing region")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    publish = subparsers.add_parser("publish", help="publish a digest-qualified registry image")
    publish.add_argument("--image", required=True, help="digest-qualified registry image reference")
    publish.add_argument("--source-sha", required=True)
    publish.add_argument("--build-run-id", required=True)
    publish.add_argument("--build-attempt", required=True, type=int)
    publish.add_argument("--build-identity", required=True, help="path to generated build identity JSON")
    publish.add_argument("--component", default="longhouse-runtime")
    publish.add_argument("--registry-username")
    publish.add_argument("--registry-password")
    publish.add_argument("--registry-token")
    publish.add_argument("--timing-json", help="optional append-only timing record JSON")
    publish.add_argument("--qualification-json", help="optional append-only qualification record JSON")
    publish.add_argument("--qualification-id")
    _store_arguments(publish)

    inspect = subparsers.add_parser("inspect", help="inspect source/schema metadata from a digest-qualified registry image")
    inspect.add_argument("--image", required=True, help="digest-qualified registry image reference")
    inspect.add_argument("--registry-username")
    inspect.add_argument("--registry-password")
    inspect.add_argument("--registry-token")

    verify = subparsers.add_parser("verify-publication", help="validate a publication or canary verification receipt")
    verify.add_argument("--receipt", required=True, type=Path)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--source-workflow", default="Publish Runtime Image")
    verify.add_argument("--source-order", type=int)
    verify.add_argument("--build-run-id")
    verify.add_argument("--build-attempt", type=int)
    verify.add_argument("--require-verification", action="store_true")


    fetch = subparsers.add_parser("fetch", help="restore an archived digest into an OCI layout")
    fetch.add_argument("--image-digest", required=True)
    fetch.add_argument("--output", required=True, type=Path)
    fetch.add_argument("--overwrite", action="store_true")
    _store_arguments(fetch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            registry = _registry_from_environment(args, args.image)
            result = inspect_runtime_schema(registry=registry, image_digest=registry.image_digest)
        elif args.command == "verify-publication":
            receipt = _load_json_path(str(args.receipt), "publication receipt")
            result = validate_publication_receipt(
                receipt,
                source_sha=args.source_sha,
                source_workflow=args.source_workflow,
                source_order=args.source_order,
                build_run_id=args.build_run_id,
                build_attempt=args.build_attempt,
                require_verification=args.require_verification,
            )
        else:
            store = store_from_environment(args)
            if args.command == "publish":
                registry = _registry_from_environment(args, args.image)
                registry_digest = registry.image_digest
                identity = _load_json_path(args.build_identity, "build identity")
                timing = _load_json_path(args.timing_json, "timing record") if args.timing_json else None
                qualification = _load_json_path(args.qualification_json, "qualification record") if args.qualification_json else None
                result = publish_archive(
                    registry=registry,
                    store=store,
                    image_digest=registry_digest,
                    source_sha=args.source_sha,
                    build_run_id=args.build_run_id,
                    build_attempt=args.build_attempt,
                    build_identity=identity,
                    component=args.component,
                    registry_image=args.image,
                    timing=timing,
                    qualification=qualification,
                    qualification_id=args.qualification_id,
                )
            else:
                result = fetch_archive(
                    store=store,
                    image_digest=args.image_digest,
                    output=args.output,
                    overwrite=args.overwrite,
                )
    except ArtifactError as exc:
        print(f"release-artifacts: ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
