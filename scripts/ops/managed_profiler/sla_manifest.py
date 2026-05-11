"""Load and validate the session propagation SLA manifest."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST_PATH = ROOT / "config" / "session-propagation-sla.toml"
ALLOWED_STATUSES = {"required", "experimental", "undefined"}
ALLOWED_PROFILE_CLASSES = {
    "cold_timeline",
    "warm_realtime",
    "durable_archive",
    "honest_degradation",
    "fidelity",
}
ALLOWED_CONTROL_PATHS = {"managed", "unmanaged"}


def load_manifest(path: Path | str = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    manifest_path = Path(path)
    with manifest_path.open("rb") as fh:
        manifest = tomllib.load(fh)
    errors = validate_manifest(manifest)
    if errors:
        joined = "\n".join(f"- {error}" for error in errors)
        raise ValueError(f"invalid session propagation SLA manifest:\n{joined}")
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    metrics = manifest.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errors.append("metrics must be a non-empty list")
        metrics = []
    metric_ids = _unique_ids(metrics, "metrics", errors)
    metric_aliases: set[str] = set()
    for metric in metrics:
        if not isinstance(metric, dict):
            errors.append("metrics entries must be tables")
            continue
        metric_id = str(metric.get("id") or "")
        _validate_profile_class(metric.get("profile_class"), f"metric {metric_id}", errors)
        for field in ("target_ms", "hard_alarm_ms"):
            value = metric.get(field)
            if not isinstance(value, int) or value <= 0:
                errors.append(f"metric {metric_id} {field} must be a positive integer")
        aliases = metric.get("aliases", [])
        if aliases is None:
            aliases = []
        if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
            errors.append(f"metric {metric_id} aliases must be a list of strings")
            continue
        for alias in aliases:
            if alias in metric_aliases:
                errors.append(f"metric alias {alias} is duplicated")
            metric_aliases.add(alias)

    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty list")
        cases = []
    _unique_ids(cases, "cases", errors)
    for case in cases:
        if not isinstance(case, dict):
            errors.append("case entries must be tables")
            continue
        case_id = str(case.get("id") or "")
        status = case.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(f"case {case_id} status must be one of {sorted(ALLOWED_STATUSES)}")
        control_path = case.get("control_path")
        if control_path not in ALLOWED_CONTROL_PATHS:
            errors.append(f"case {case_id} control_path must be managed or unmanaged")
        _validate_profile_class(case.get("profile_class"), f"case {case_id}", errors)
        for field in ("provider", "profile", "launch", "shutdown", "truth_source", "notes"):
            value = case.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"case {case_id} {field} must be a non-empty string")
        for field in ("required_observers", "metrics"):
            value = case.get(field)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors.append(f"case {case_id} {field} must be a list of strings")
        for metric_id in case.get("metrics") or []:
            if metric_id not in metric_ids:
                errors.append(f"case {case_id} references unknown metric {metric_id}")

    required_cases = [case for case in cases if isinstance(case, dict) and case.get("status") == "required"]
    if not required_cases:
        errors.append("at least one case must be status=required")
    return errors


def metric_target_ms(manifest: dict[str, Any], metric_id_or_alias: str, default: int | None = None) -> int | None:
    metric = metric_by_id_or_alias(manifest, metric_id_or_alias)
    if metric is None:
        return default
    value = metric.get("target_ms")
    return value if isinstance(value, int) else default


def metric_by_id_or_alias(manifest: dict[str, Any], metric_id_or_alias: str) -> dict[str, Any] | None:
    for metric in manifest.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        if metric.get("id") == metric_id_or_alias:
            return metric
        aliases = metric.get("aliases") or []
        if metric_id_or_alias in aliases:
            return metric
    return None


def cases_by_status(manifest: dict[str, Any], status: str) -> list[dict[str, Any]]:
    return [
        case
        for case in manifest.get("cases") or []
        if isinstance(case, dict) and case.get("status") == status
    ]


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest.get("schema_version"),
        "cases": {
            status: len(cases_by_status(manifest, status))
            for status in ("required", "experimental", "undefined")
        },
        "metrics": len(manifest.get("metrics") or []),
    }


def format_case_inventory(manifest: dict[str, Any]) -> str:
    lines = ["Session propagation SLA cases:"]
    for status in ("required", "experimental", "undefined"):
        lines.append(f"{status}:")
        for case in cases_by_status(manifest, status):
            lines.append(
                "  "
                + f"{case['id']} "
                + f"provider={case['provider']} "
                + f"control_path={case['control_path']} "
                + f"profile={case['profile']}"
            )
    return "\n".join(lines)


def _validate_profile_class(value: Any, label: str, errors: list[str]) -> None:
    if value not in ALLOWED_PROFILE_CLASSES:
        errors.append(f"{label} profile_class must be one of {sorted(ALLOWED_PROFILE_CLASSES)}")


def _unique_ids(items: list[Any], label: str, errors: list[str]) -> set[str]:
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            errors.append(f"{label} entries must have non-empty string ids")
            continue
        if item_id in seen:
            errors.append(f"{label} id {item_id} is duplicated")
        seen.add(item_id)
    return seen


def main() -> int:
    manifest = load_manifest()
    print(format_case_inventory(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
