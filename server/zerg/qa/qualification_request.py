"""Validation and canonical identity for factory qualification requests."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from typing import Mapping

SCHEMA_VERSION = 2
KIND = "provider_qualification"
EVIDENCE_CLASSES = frozenset({"hermetic", "live_no_token", "live_token", "observed_install"})
AUTH_MODES = frozenset({"none", "isolated_profile", "factory_token"})
EVIDENCE_PRIORITY = {
    "hermetic": 0,
    "observed_install": 1,
    "live_no_token": 2,
    "live_token": 3,
}
SEMANTIC_KEYS = (
    "auth_mode",
    "evidence_class",
    "expected_executable_identity",
    "expected_provider_build_granularity",
    "expected_provider_build_identity",
    "expected_provider_version",
    "factory_source_sha",
    "kind",
    "longhouse_git_sha",
    "profile",
    "provider",
    "release_identity",
    "release_tag",
    "scenario_evidence",
    "scenario_ids",
    "selection_input_digests",
    "schema_version",
)
REQUEST_KEYS = frozenset(
    {
        *SEMANTIC_KEYS,
        "provider_bin",
        "invocation_id",
        "producer_class",
        "producer_version",
        "run_reference",
        "trigger",
        "semantic_digest",
    }
)


class QualificationRequestError(ValueError):
    pass


def semantic_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = {key: request[key] for key in SEMANTIC_KEYS}
    except KeyError as exc:
        raise QualificationRequestError(f"qualification request is missing {exc.args[0]}") from exc
    payload["scenario_ids"] = list(payload["scenario_ids"])
    payload["scenario_evidence"] = dict(sorted(payload["scenario_evidence"].items()))
    payload["selection_input_digests"] = dict(sorted(payload["selection_input_digests"].items()))
    return payload


def semantic_digest(request: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        semantic_payload(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def policy_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return the non-secret policy facts that the proof must repeat."""

    return {
        "auth_mode": request["auth_mode"],
        "evidence_class": request["evidence_class"],
        "scenario_ids": list(request["scenario_ids"]),
        "scenario_evidence": dict(sorted(request["scenario_evidence"].items())),
    }


def metadata_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return attempt/provenance fields that are excluded from the semantic digest."""

    return {
        key: request[key]
        for key in (
            "provider_bin",
            "invocation_id",
            "producer_class",
            "producer_version",
            "run_reference",
            "trigger",
        )
    }


def evidence_class_for_scenarios(scenario_evidence: Mapping[str, str]) -> str:
    if not scenario_evidence:
        raise QualificationRequestError("qualification request must declare scenario evidence")
    unsupported = set(scenario_evidence.values()) - EVIDENCE_CLASSES
    if unsupported:
        raise QualificationRequestError(f"unsupported evidence classes: {sorted(unsupported)}")
    return max(scenario_evidence.values(), key=EVIDENCE_PRIORITY.__getitem__)


def validate(
    request: Mapping[str, Any],
    *,
    provider: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise QualificationRequestError("qualification request must be an object")
    if request.get("schema_version") != SCHEMA_VERSION:
        raise QualificationRequestError(f"qualification request schema_version must be {SCHEMA_VERSION}")
    if request.get("kind") != KIND:
        raise QualificationRequestError(f"qualification request kind must be {KIND!r}")
    if provider is not None and request.get("provider") != provider:
        raise QualificationRequestError("unsupported provider")
    if profile is not None and request.get("profile") != profile:
        raise QualificationRequestError("unsupported profile")
    unknown = set(request) - REQUEST_KEYS
    if unknown:
        raise QualificationRequestError(f"qualification request has unknown keys: {sorted(unknown)}")
    for key in (
        "provider",
        "release_identity",
        "release_tag",
        "profile",
        "evidence_class",
        "auth_mode",
        "expected_provider_version",
        "expected_executable_identity",
        "expected_provider_build_identity",
        "expected_provider_build_granularity",
        "factory_source_sha",
        "provider_bin",
        "invocation_id",
        "producer_class",
        "producer_version",
        "run_reference",
        "longhouse_git_sha",
        "trigger",
        "semantic_digest",
    ):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise QualificationRequestError(f"{key} must be a non-empty string")
    if re.fullmatch(r"[0-9a-f]{40}", request["factory_source_sha"]) is None:
        raise QualificationRequestError("factory_source_sha must be a full 40-character lowercase Git SHA")
    selection_input_digests = request.get("selection_input_digests")
    if (
        not isinstance(selection_input_digests, dict)
        or not selection_input_digests
        or any(not isinstance(key, str) or not key.strip() for key in selection_input_digests)
        or any(
            not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71 for value in selection_input_digests.values()
        )
    ):
        raise QualificationRequestError("selection_input_digests must be a non-empty sha256 digest mapping")
    scenario_ids = request.get("scenario_ids")
    if (
        not isinstance(scenario_ids, list)
        or not scenario_ids
        or any(not isinstance(item, str) or not item.strip() for item in scenario_ids)
    ):
        raise QualificationRequestError("scenario_ids must be a non-empty list of strings")
    if len(scenario_ids) != len(set(scenario_ids)):
        raise QualificationRequestError("scenario_ids must not contain duplicates")
    scenario_evidence = request.get("scenario_evidence")
    if not isinstance(scenario_evidence, dict) or set(scenario_evidence) != set(scenario_ids):
        raise QualificationRequestError("scenario_evidence must cover every declared scenario_id")
    if any(not isinstance(value, str) or value not in EVIDENCE_CLASSES for value in scenario_evidence.values()):
        raise QualificationRequestError("scenario_evidence contains an unsupported evidence class")
    if request["evidence_class"] not in EVIDENCE_CLASSES:
        raise QualificationRequestError("unsupported evidence_class")
    if request["auth_mode"] not in AUTH_MODES:
        raise QualificationRequestError("unsupported auth_mode")
    if request["auth_mode"] == "factory_token" and "live_no_token" in scenario_evidence.values():
        raise QualificationRequestError("live_no_token evidence cannot run with factory credentials")
    if "live_token" in scenario_evidence.values() and request["auth_mode"] != "factory_token":
        raise QualificationRequestError("live_token evidence requires factory credentials")
    if request["evidence_class"] != evidence_class_for_scenarios(scenario_evidence):
        raise QualificationRequestError("evidence_class must match the strongest declared scenario evidence")
    if request["expected_provider_build_granularity"] not in {"full_installed_tree", "single_asset"}:
        raise QualificationRequestError("unsupported provider build granularity")
    if request["semantic_digest"] != semantic_digest(request):
        raise QualificationRequestError("qualification request semantic_digest does not match its contents")
    return dict(request)


def load(
    path: Path,
    *,
    provider: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationRequestError(f"invalid qualification request JSON: {exc}") from exc
    return validate(request, provider=provider, profile=profile)
