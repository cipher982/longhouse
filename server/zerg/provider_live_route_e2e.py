"""Local hosted provider-live route E2E artifacts consumed by local health.

The provider live-proof sidecars prove local provider behavior. This sidecar
proves the hosted Runtime Host -> Machine Agent route can ask this machine to
run that proof and gets typed version-mismatch rejection on the negative leg.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.provider_release_status import PROVIDER_STATUS_SCHEMA_VERSION
from zerg.provider_release_status import _max_artifact_age_seconds
from zerg.provider_release_status import _parse_rfc3339
from zerg.services.longhouse_paths import get_provider_live_route_e2e_dir

ROUTE_E2E_ARTIFACT_KIND = "provider_live_route_e2e"
SUPPORTED_ROUTE_PROOF_PROVIDERS = frozenset({"claude", "opencode", "antigravity"})


def _read_json_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, "artifact root is not an object"
    return payload, None


def configured_provider_live_route_e2e_path(base_dir: Path | None = None) -> Path:
    return get_provider_live_route_e2e_dir(base_dir) / "latest.json"


def _freshness(generated_at: Any) -> tuple[str, int | None]:
    generated_at_dt = _parse_rfc3339(generated_at)
    if generated_at_dt is None:
        return "missing", None
    age_seconds = int((datetime.now(UTC) - generated_at_dt).total_seconds())
    if age_seconds > _max_artifact_age_seconds():
        return "stale", age_seconds
    return "fresh", age_seconds


def _detail_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if not isinstance(detail, dict):
        return None
    code = detail.get("code")
    return str(code) if code is not None else None


def _version_match_status(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    version_match = result.get("provider_version_match")
    if not isinstance(version_match, dict):
        return None
    status = version_match.get("status")
    return str(status) if status is not None else None


def _summarize_result(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"status": "malformed_result"}

    match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
    mismatch = raw.get("mismatch") if isinstance(raw.get("mismatch"), dict) else {}
    version_match_info = raw.get("version_match")
    version_match = version_match_info.get("status") if isinstance(version_match_info, dict) else None
    return {
        "provider": raw.get("provider"),
        "status": raw.get("status"),
        "verdict": raw.get("verdict"),
        "expected_provider_version": raw.get("expected_provider_version"),
        "version_match": version_match,
        "match_status_code": match.get("status_code"),
        "match_attempt_count": raw.get("match_attempt_count"),
        "match_version_match": _version_match_status(match.get("payload")),
        "mismatch_status_code": mismatch.get("status_code"),
        "mismatch_attempt_count": raw.get("mismatch_attempt_count"),
        "mismatch_code": _detail_code(mismatch.get("payload")),
        "failure_code": raw.get("failure_code"),
        "message": raw.get("message"),
    }


def _summarize_results(results: Any) -> list[dict[str, Any]]:
    if not isinstance(results, list):
        return []
    return [_summarize_result(result) for result in results]


def expected_route_providers_from_live_proof(provider_live_proof: Mapping[str, Any] | None) -> list[str]:
    statuses = dict((provider_live_proof or {}).get("statuses") or {})
    providers: list[str] = []
    for provider, raw_info in sorted(statuses.items()):
        if provider not in SUPPORTED_ROUTE_PROOF_PROVIDERS:
            continue
        info = dict(raw_info or {})
        if info.get("applies") and info.get("status") == "ok":
            providers.append(str(provider))
    return providers


def _failure_count(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _coverage(
    *,
    expected_providers: Iterable[str] | None,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    if expected_providers is None:
        return {
            "coverage_status": "not_evaluated",
            "expected_providers": [],
            "covered_providers": [],
            "missing_providers": [],
            "unexpected_providers": [],
        }

    expected = sorted({str(provider) for provider in expected_providers if provider})
    covered_set: set[str] = set()
    for result in results:
        provider = result.get("provider")
        if provider and result.get("status") == "pass":
            covered_set.add(str(provider))
    covered = sorted(covered_set)
    missing = [provider for provider in expected if provider not in covered]
    unexpected = [provider for provider in covered if provider not in expected]
    if not expected:
        coverage_status = "none_expected"
    elif missing:
        coverage_status = "missing"
    else:
        coverage_status = "complete"
    return {
        "coverage_status": coverage_status,
        "expected_providers": expected,
        "covered_providers": covered,
        "missing_providers": missing,
        "unexpected_providers": unexpected,
    }


def _status_for_artifact(payload: dict[str, Any], *, freshness_status: str) -> str:
    schema_version = payload.get("schema_version")
    artifact_kind = str(payload.get("artifact_kind") or "").strip()
    if schema_version != PROVIDER_STATUS_SCHEMA_VERSION:
        return "schema_mismatch"
    if artifact_kind != ROUTE_E2E_ARTIFACT_KIND:
        return "artifact_kind_mismatch"
    if freshness_status != "fresh":
        return "stale"
    if str(payload.get("verdict") or "").lower() != "green":
        return "failed"
    failure_count = _failure_count(payload.get("failure_count"))
    if failure_count is None:
        return "malformed_results"
    if failure_count != 0:
        return "failed"
    results = payload.get("results")
    if not isinstance(results, list):
        return "malformed_results"
    if any(not isinstance(result, dict) or result.get("status") != "pass" for result in results):
        return "failed"
    return "ok"


def collect_provider_live_route_e2e(
    *,
    fast: bool = False,
    base_dir: Path | None = None,
    expected_providers: Iterable[str] | None = None,
) -> dict[str, Any]:
    if fast:
        return {
            "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
            "enabled": False,
            "configured": False,
            "status": "skipped",
            "applies": False,
            "skipped_reason": "fast_local_health",
            "coverage_status": "not_evaluated",
            "expected_providers": [],
            "covered_providers": [],
            "missing_providers": [],
            "unexpected_providers": [],
        }

    path = configured_provider_live_route_e2e_path(base_dir)
    payload, error = _read_json_file(path)
    source = {"source": "file", "path": str(path), "error": error}
    if payload is None:
        configured = error != "missing"
        return {
            "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
            "enabled": configured,
            "configured": configured,
            "status": "unavailable" if configured else "not_configured",
            "applies": False,
            "source": source,
            **_coverage(expected_providers=expected_providers, results=[]),
        }

    generated_at = payload.get("generated_at")
    freshness_status, generated_at_age_seconds = _freshness(generated_at)
    status = _status_for_artifact(payload, freshness_status=freshness_status)
    summarized_results = _summarize_results(payload.get("results"))
    raw_providers = payload.get("providers")
    providers = [str(item) for item in raw_providers if item] if isinstance(raw_providers, list) else []
    if not providers:
        providers = [str(result.get("provider")) for result in summarized_results if result.get("provider")]
    coverage = _coverage(expected_providers=expected_providers, results=summarized_results)

    return {
        "schema_version": PROVIDER_STATUS_SCHEMA_VERSION,
        "enabled": True,
        "configured": True,
        "status": status,
        "applies": status == "ok" and coverage["coverage_status"] in {"complete", "none_expected", "not_evaluated"},
        "artifact_schema_version": payload.get("schema_version"),
        "artifact_kind": payload.get("artifact_kind"),
        "generated_at": generated_at,
        "generated_at_age_seconds": generated_at_age_seconds,
        "freshness_status": freshness_status,
        "verdict": payload.get("verdict"),
        "failure_count": payload.get("failure_count"),
        "failure_code": payload.get("failure_code"),
        "message": payload.get("message"),
        "api_url": payload.get("api_url"),
        "device_id": payload.get("device_id"),
        "engine_build": payload.get("engine_build"),
        "providers": providers,
        "require_verdict": payload.get("require_verdict"),
        "mismatch_checked": payload.get("mismatch_checked"),
        "results": summarized_results,
        **coverage,
        "source": source,
    }


__all__ = [
    "ROUTE_E2E_ARTIFACT_KIND",
    "collect_provider_live_route_e2e",
    "configured_provider_live_route_e2e_path",
    "expected_route_providers_from_live_proof",
]
