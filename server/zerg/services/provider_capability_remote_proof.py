"""Fetch and cache Runtime Host-authenticated provider capability proofs."""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.services.longhouse_paths import resolve_longhouse_home
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping

_REMOTE_BUNDLE_KIND = "trusted_provider_capability_proof_bundle"
_CACHE_KIND = "trusted_provider_capability_proof_cache"
_CACHE_NAME = "trusted-runtime-cache.json"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CACHE_BYTES = 3 * 1024 * 1024
_MAX_RECORDS = 512
_FETCH_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True)
class TrustedProviderProofs:
    records_by_provider: dict[str, tuple[ProviderCapabilityProofRecord, ...]]
    trusted_artifact_ids: frozenset[str]
    summary: dict[str, Any]


def _cache_path(base_dir: Path | None) -> Path:
    return resolve_longhouse_home(base_dir) / "provider-capability-proofs" / _CACHE_NAME


def _runtime_origin(runtime_url: str) -> str:
    parsed = urllib.parse.urlsplit(str(runtime_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise ValueError("provider proof Runtime URL must be an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider proof Runtime URL must not contain credentials")
    hostname = parsed.hostname.rstrip(".").lower()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme != "https" and not loopback:
        raise ValueError("provider proof Runtime URL must use HTTPS except on localhost")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _proof_url(runtime_origin: str) -> str:
    return urllib.parse.urlunsplit((*urllib.parse.urlsplit(runtime_origin)[:2], "/api/agents/provider-capability-proofs", "", ""))


def _parse_remote_bundle(payload: Any) -> tuple[dict[str, tuple[ProviderCapabilityProofRecord, ...]], frozenset[str]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("trusted provider proof response schema is invalid")
    if payload.get("artifact_kind") != _REMOTE_BUNDLE_KIND:
        raise ValueError("trusted provider proof response artifact kind is invalid")
    raw_records = payload.get("records")
    raw_trusted_ids = payload.get("trusted_artifact_ids")
    if not isinstance(raw_records, list) or len(raw_records) > _MAX_RECORDS:
        raise ValueError("trusted provider proof response records are invalid")
    if (
        not isinstance(raw_trusted_ids, list)
        or len(raw_trusted_ids) > _MAX_RECORDS
        or not all(isinstance(item, str) and item for item in raw_trusted_ids)
        or len(raw_trusted_ids) != len(set(raw_trusted_ids))
    ):
        raise ValueError("trusted provider proof response trusted IDs are invalid")
    records = tuple(proof_record_from_mapping(record) for record in raw_records if isinstance(record, dict))
    if len(records) != len(raw_records):
        raise ValueError("trusted provider proof response records must be objects")
    records_by_id = {record.artifact_id: record for record in records}
    if len(records_by_id) != len(records):
        raise ValueError("trusted provider proof response has duplicate records")
    trusted_ids = frozenset(raw_trusted_ids)
    if not trusted_ids.issubset(records_by_id):
        raise ValueError("trusted provider proof response names an absent trusted record")
    by_provider: dict[str, list[ProviderCapabilityProofRecord]] = {}
    for artifact_id in raw_trusted_ids:
        record = records_by_id[artifact_id]
        by_provider.setdefault(record.provider, []).append(record)
    return (
        {
            provider: tuple(sorted(provider_records, key=lambda record: (record.generated_at, record.artifact_id)))
            for provider, provider_records in by_provider.items()
        },
        trusted_ids,
    )


def _empty(*, path: Path, cache_state: str, refresh_state: str, error: str | None = None) -> TrustedProviderProofs:
    summary: dict[str, Any] = {
        "path": str(path),
        "cache_state": cache_state,
        "refresh_state": refresh_state,
        "record_count": 0,
        "trusted_artifact_count": 0,
    }
    if error:
        summary["error"] = error[:500]
    return TrustedProviderProofs(records_by_provider={}, trusted_artifact_ids=frozenset(), summary=summary)


def load_cached_provider_capability_proofs(
    base_dir: Path | None = None,
    *,
    runtime_url: str | None = None,
) -> TrustedProviderProofs:
    path = _cache_path(base_dir)
    if not path.is_file():
        return _empty(path=path, cache_state="missing", refresh_state="cache_only")
    try:
        if path.stat().st_size > _MAX_CACHE_BYTES:
            raise ValueError("trusted provider proof cache exceeds its size limit")
        payload = json.loads(path.read_bytes())
        if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("artifact_kind") != _CACHE_KIND:
            raise ValueError("trusted provider proof cache schema is invalid")
        cached_origin = payload.get("runtime_origin")
        if not isinstance(cached_origin, str) or not cached_origin:
            raise ValueError("trusted provider proof cache has no Runtime origin")
        if runtime_url is not None and cached_origin != _runtime_origin(runtime_url):
            return _empty(path=path, cache_state="origin_mismatch", refresh_state="cache_only")
        records_by_provider, trusted_ids = _parse_remote_bundle(payload.get("bundle"))
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _empty(path=path, cache_state="invalid", refresh_state="cache_only", error=str(exc))
    return TrustedProviderProofs(
        records_by_provider=records_by_provider,
        trusted_artifact_ids=trusted_ids,
        summary={
            "path": str(path),
            "cache_state": "present",
            "refresh_state": "cache_only",
            "runtime_origin": cached_origin,
            "fetched_at": payload.get("fetched_at"),
            "record_count": sum(len(records) for records in records_by_provider.values()),
            "trusted_artifact_count": len(trusted_ids),
        },
    )


def _read_bounded_response(response: Any) -> dict[str, Any]:
    content_encoding = str(response.headers.get("Content-Encoding") or "identity").lower()
    if content_encoding != "identity":
        raise ValueError("trusted provider proof response must not be content-encoded")
    raw_length = response.headers.get("Content-Length")
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("trusted provider proof response has invalid Content-Length") from exc
        if content_length < 0:
            raise ValueError("trusted provider proof response has invalid Content-Length")
        if content_length > _MAX_RESPONSE_BYTES:
            raise ValueError("trusted provider proof response exceeds its size limit")
    encoded = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise ValueError("trusted provider proof response exceeds its size limit")
    try:
        payload = json.loads(encoded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("trusted provider proof response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("trusted provider proof response must be an object")
    return payload


def _atomic_write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    if len(encoded) > _MAX_CACHE_BYTES:
        raise ValueError("trusted provider proof cache exceeds its size limit")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".trusted-proofs-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_cached_provider_capability_proofs(
    base_dir: Path | None = None,
    *,
    runtime_url: str | None,
    token: str | None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TrustedProviderProofs:
    if not runtime_url or not token:
        cached = load_cached_provider_capability_proofs(base_dir, runtime_url=runtime_url)
        return TrustedProviderProofs(
            records_by_provider=cached.records_by_provider,
            trusted_artifact_ids=cached.trusted_artifact_ids,
            summary={**cached.summary, "refresh_state": "not_configured"},
        )
    try:
        origin = _runtime_origin(runtime_url)
    except ValueError as exc:
        cached = load_cached_provider_capability_proofs(base_dir, runtime_url=runtime_url)
        return TrustedProviderProofs(
            records_by_provider=cached.records_by_provider,
            trusted_artifact_ids=cached.trusted_artifact_ids,
            summary={**cached.summary, "refresh_state": "invalid_runtime_url", "error": str(exc)},
        )
    cached = load_cached_provider_capability_proofs(base_dir, runtime_url=runtime_url)
    request = urllib.request.Request(
        _proof_url(origin),
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "LonghouseProviderProofCache/1.0",
            "X-Agents-Token": token,
        },
    )
    try:
        with opener(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise ValueError(f"trusted provider proof request returned HTTP {status}")
            bundle = _read_bounded_response(response)
        records_by_provider, trusted_ids = _parse_remote_bundle(bundle)
        fetched_at = now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        _atomic_write_cache(
            _cache_path(base_dir),
            {
                "schema_version": 1,
                "artifact_kind": _CACHE_KIND,
                "runtime_origin": origin,
                "fetched_at": fetched_at,
                "bundle": bundle,
            },
        )
    except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        return TrustedProviderProofs(
            records_by_provider=cached.records_by_provider,
            trusted_artifact_ids=cached.trusted_artifact_ids,
            summary={**cached.summary, "refresh_state": "failed", "error": str(exc)[:500]},
        )
    return TrustedProviderProofs(
        records_by_provider=records_by_provider,
        trusted_artifact_ids=trusted_ids,
        summary={
            "path": str(_cache_path(base_dir)),
            "cache_state": "present",
            "refresh_state": "refreshed",
            "runtime_origin": origin,
            "fetched_at": fetched_at,
            "record_count": sum(len(records) for records in records_by_provider.values()),
            "trusted_artifact_count": len(trusted_ids),
        },
    )
