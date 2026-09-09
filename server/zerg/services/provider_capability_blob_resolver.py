"""Read-only resolver for provider-factory capability evidence."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError

FACTORY_BLOB_NAMESPACE = "longhouse/provider-factory/v1/blobs/sha256"
MAX_FACTORY_BLOB_BYTES = 50 * 1024 * 1024
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_ROLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProviderCapabilityBlobResolverError(RuntimeError):
    """Base class for a failed factory CAS read."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ProviderCapabilityBlobResolverConfigurationError(ProviderCapabilityBlobResolverError):
    """The explicit read-only factory configuration is incomplete or invalid."""


class ProviderCapabilityBlobMissing(ProviderCapabilityBlobResolverError):
    """The exact immutable object does not exist."""


class ProviderCapabilityBlobUnavailable(ProviderCapabilityBlobResolverError):
    """The configured object store could not answer the read."""


class ProviderCapabilityBlobTampered(ProviderCapabilityBlobResolverError):
    """The object store returned bytes or headers that do not match the ref."""


@dataclass(frozen=True, slots=True)
class FactoryBlobReference:
    digest: str
    byte_length: int
    media_type: str
    logical_role: str
    key: str
    compression: object | None = None
    encryption: object | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "FactoryBlobReference":
        required = {"digest", "byte_length", "media_type", "logical_role", "key"}
        optional = {"compression", "encryption"}
        if not isinstance(payload, Mapping) or set(payload) - required - optional or not required <= set(payload):
            raise ValueError("v4 blob reference has an unexpected schema")
        if payload.get("compression") is not None or payload.get("encryption") is not None:
            raise ValueError("v4 blob reference encodings are unsupported")
        digest = payload["digest"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("v4 blob reference digest is invalid")
        byte_length = payload["byte_length"]
        if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 0:
            raise ValueError("v4 blob reference byte_length must be a non-negative integer")
        if byte_length > MAX_FACTORY_BLOB_BYTES:
            raise ValueError("v4 blob reference exceeds the 50 MiB blob cap")
        media_type = payload["media_type"]
        logical_role = payload["logical_role"]
        if not isinstance(media_type, str) or not media_type.strip():
            raise ValueError("v4 blob reference media_type must be a non-empty string")
        if not isinstance(logical_role, str) or _ROLE.fullmatch(logical_role) is None:
            raise ValueError("v4 blob reference logical_role is invalid")
        expected_key = factory_blob_key(digest)
        if payload["key"] != expected_key:
            raise ValueError("v4 blob reference key is not derived from its digest")
        return cls(
            digest=digest,
            byte_length=byte_length,
            media_type=media_type,
            logical_role=logical_role,
            key=expected_key,
        )

    @property
    def identity_payload(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "logical_role": self.logical_role,
            "key": self.key,
        }


@dataclass(frozen=True, slots=True)
class VerifiedFactoryBlob:
    reference: FactoryBlobReference
    content: bytes | None
    verification: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderCapabilityBlobResolverConfig:
    endpoint: str
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str

    @classmethod
    def from_settings(cls, settings: object) -> "ProviderCapabilityBlobResolverConfig":
        endpoint = str(getattr(settings, "provider_capability_blob_s3_endpoint", "") or "").strip()
        bucket = str(getattr(settings, "provider_capability_blob_s3_bucket", "") or "").strip()
        region = str(getattr(settings, "provider_capability_blob_s3_region", "us-east-1") or "us-east-1").strip()
        access_key_id = str(getattr(settings, "provider_capability_blob_s3_access_key_id", "") or "").strip()
        secret_access_key = str(getattr(settings, "provider_capability_blob_s3_secret_access_key", "") or "").strip()
        if not all((endpoint, bucket, region, access_key_id, secret_access_key)):
            raise ProviderCapabilityBlobResolverConfigurationError(
                "not_configured", "provider capability blob resolver requires explicit endpoint, bucket, and credentials"
            )
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ProviderCapabilityBlobResolverConfigurationError(
                "invalid_endpoint", "provider capability blob resolver endpoint must be a credential-free HTTP(S) origin"
            )
        if _BUCKET.fullmatch(bucket) is None:
            raise ProviderCapabilityBlobResolverConfigurationError("invalid_bucket", "provider capability blob bucket is invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", region):
            raise ProviderCapabilityBlobResolverConfigurationError("invalid_region", "provider capability blob region is invalid")
        return cls(endpoint, bucket, region, access_key_id, secret_access_key)


def factory_blob_key(digest: str) -> str:
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ValueError("digest must be a lowercase sha256 digest")
    raw = digest.removeprefix("sha256:")
    return f"{FACTORY_BLOB_NAMESPACE}/{raw[:2]}/{raw}"


class ProviderCapabilityBlobResolver:
    """Resolve one exact factory CAS ref with read-only, explicit credentials."""

    def __init__(self, config: ProviderCapabilityBlobResolverConfig, *, client: Any | None = None) -> None:
        self.config = config
        # Supplying both credentials is deliberate: this client never consults
        # the ambient AWS credential chain and has no write/delete methods here.
        self.client = client or boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            region_name=config.region,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 0},
            ),
        )

    def resolve(self, reference: FactoryBlobReference, *, collect: bool = False, deadline: float | None = None) -> VerifiedFactoryBlob:
        deadline = time.monotonic() + 90 if deadline is None else deadline
        if time.monotonic() >= deadline:
            raise ProviderCapabilityBlobUnavailable("deadline", "factory evidence read deadline exceeded")
        if reference.key != factory_blob_key(reference.digest):
            raise ProviderCapabilityBlobTampered("key_mismatch", "factory blob key is not digest-derived")
        if reference.byte_length > MAX_FACTORY_BLOB_BYTES:
            raise ProviderCapabilityBlobTampered("size_cap", "factory blob exceeds the 50 MiB blob cap")
        try:
            response = self.client.get_object(
                Bucket=self.config.bucket,
                Key=reference.key,
                ChecksumMode="ENABLED",
            )
        except ClientError as exc:
            code, http_status = _client_error_code(exc)
            if code in {"NoSuchKey", "NotFound", "404"} or http_status == 404:
                raise ProviderCapabilityBlobMissing("missing", "factory blob is missing") from exc
            raise ProviderCapabilityBlobUnavailable("unavailable", "factory blob store GET failed") from exc
        except (BotoCoreError, OSError, TimeoutError) as exc:
            raise ProviderCapabilityBlobUnavailable("unavailable", "factory blob store GET failed") from exc

        body = response.get("Body") if isinstance(response, Mapping) else None
        try:
            verification = _verify_headers(response, reference)
            content = _consume_body(body, reference, collect=collect, deadline=deadline)
        except ProviderCapabilityBlobResolverError:
            _close_body(body)
            raise
        except (BotoCoreError, OSError, TimeoutError) as exc:
            _close_body(body)
            raise ProviderCapabilityBlobUnavailable("unavailable", "factory blob body read failed") from exc
        return VerifiedFactoryBlob(reference=reference, content=content, verification=verification)


def resolver_from_settings(settings: object) -> ProviderCapabilityBlobResolver:
    return ProviderCapabilityBlobResolver(ProviderCapabilityBlobResolverConfig.from_settings(settings))


def _verify_headers(response: object, reference: FactoryBlobReference) -> dict[str, Any]:
    if not isinstance(response, Mapping) or "Body" not in response:
        raise ProviderCapabilityBlobTampered("invalid_get", "factory blob GET response has no body")
    content_length = response.get("ContentLength")
    if content_length != reference.byte_length or isinstance(content_length, bool) or not isinstance(content_length, int):
        raise ProviderCapabilityBlobTampered("length_mismatch", "factory blob ContentLength does not match its ref")
    # Blob keys identify bytes only. Media type belongs to the authenticated
    # reference; a previous writer's S3 type label need not match.
    metadata = response.get("Metadata")
    raw_digest = metadata.get("sha256") if isinstance(metadata, Mapping) else None
    expected_raw = reference.digest.removeprefix("sha256:")
    if raw_digest != expected_raw:
        raise ProviderCapabilityBlobTampered("metadata_mismatch", "factory blob sha256 metadata does not match its ref")
    checksum = response.get("ChecksumSHA256")
    try:
        decoded = base64.b64decode(checksum, validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise ProviderCapabilityBlobTampered("checksum_mismatch", "factory blob ChecksumSHA256 is invalid") from exc
    if decoded != bytes.fromhex(expected_raw):
        raise ProviderCapabilityBlobTampered("checksum_mismatch", "factory blob ChecksumSHA256 does not match its ref")
    return {
        "digest": reference.digest,
        "key": reference.key,
        "content_length": content_length,
        # Effective representation type, not an integrity claim about S3 metadata.
        "content_type": reference.media_type,
        "metadata_sha256": raw_digest,
        "checksum_sha256": checksum,
        "body_sha256": expected_raw,
        "complete": True,
    }


def _consume_body(body: object, reference: FactoryBlobReference, *, collect: bool, deadline: float) -> bytes | None:
    if body is None or not callable(getattr(body, "read", None)):
        raise ProviderCapabilityBlobTampered("invalid_body", "factory blob GET response body is not readable")
    digest = hashlib.sha256()
    content = bytearray() if collect else None
    size = 0
    try:
        while True:
            if time.monotonic() >= deadline:
                raise ProviderCapabilityBlobUnavailable("deadline", "factory evidence read deadline exceeded")
            chunk = body.read(min(1024 * 1024, reference.byte_length - size + 1))
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ProviderCapabilityBlobTampered("invalid_body", "factory blob body yielded non-bytes data")
            size += len(chunk)
            if size > reference.byte_length:
                raise ProviderCapabilityBlobTampered("length_mismatch", "factory blob body exceeds its ref")
            digest.update(chunk)
            if content is not None:
                content.extend(chunk)
    except ProviderCapabilityBlobResolverError:
        raise
    except (BotoCoreError, OSError, TimeoutError) as exc:
        raise ProviderCapabilityBlobUnavailable("unavailable", "factory blob body read failed") from exc
    finally:
        _close_body(body)
    actual = f"sha256:{digest.hexdigest()}"
    if size != reference.byte_length:
        raise ProviderCapabilityBlobTampered("length_mismatch", "factory blob body is incomplete")
    if actual != reference.digest:
        raise ProviderCapabilityBlobTampered("digest_mismatch", "factory blob body digest does not match its ref")
    return None if content is None else bytes(content)


def _close_body(body: object) -> None:
    close = getattr(body, "close", None)
    if callable(close):
        try:
            close()
        except (BotoCoreError, OSError):
            pass


def _client_error_code(error: ClientError) -> tuple[str, int | None]:
    response = error.response if isinstance(error.response, Mapping) else {}
    error_payload = response.get("Error", {}) if isinstance(response, Mapping) else {}
    metadata = response.get("ResponseMetadata", {}) if isinstance(response, Mapping) else {}
    code = str(error_payload.get("Code", "")) if isinstance(error_payload, Mapping) else ""
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return code, status if isinstance(status, int) and not isinstance(status, bool) else None


__all__ = [
    "FACTORY_BLOB_NAMESPACE",
    "FactoryBlobReference",
    "MAX_FACTORY_BLOB_BYTES",
    "ProviderCapabilityBlobMissing",
    "ProviderCapabilityBlobResolver",
    "ProviderCapabilityBlobResolverConfig",
    "ProviderCapabilityBlobResolverConfigurationError",
    "ProviderCapabilityBlobResolverError",
    "ProviderCapabilityBlobTampered",
    "ProviderCapabilityBlobUnavailable",
    "VerifiedFactoryBlob",
    "factory_blob_key",
    "resolver_from_settings",
]
