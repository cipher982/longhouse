"""Load and validate the session propagation SLA manifest."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST_PATH = ROOT / "config" / "session-propagation-sla.toml"
ALLOWED_STATUSES = {"required", "experimental", "undefined"}
ALLOWED_CI_MODES = {"blocked", "gate", "report"}
ALLOWED_PROVIDERS = {"all", "claude", "codex", "opencode"}
IMPLEMENTED_PROFILER_DRIVERS = {
    "managed_codex_cold_timeline",
    "managed_codex_warm_live",
    "unmanaged_codex_baseline",
}
ALLOWED_PROFILE_CLASSES = {
    "cold_timeline",
    "warm_realtime",
    "durable_archive",
    "honest_degradation",
    "fidelity",
}
ALLOWED_CONTROL_PATHS = {"managed", "unmanaged"}
ALLOWED_TOPOLOGIES = {"hosted_runtime_host", "local_runtime_host", "self_hosted_runtime_host"}
ALLOWED_LAYERS = {
    "browser_card",
    "hosted_api",
    "hosted_db",
    "machine_agent",
    "provider_process",
    "provider_transcript",
    "timeline_sse",
}
ALLOWED_OBSERVERS = {
    "browser_card",
    "claude_channel_state",
    "hosted_db",
    "machine_heartbeat",
    "managed_sessions_snapshot",
    "process_scan_snapshot",
    "provider_transcript",
    "pty_and_codex_bridge",
    "timeline_api",
    "timeline_sse",
    "unmanaged_session_bindings",
}


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
        diagnostic = metric.get("diagnostic", False)
        if not isinstance(diagnostic, bool):
            errors.append(f"metric {metric_id} diagnostic must be a boolean")
        for field in ("target_ms", "hard_alarm_ms"):
            value = metric.get(field)
            if not isinstance(value, int) or value <= 0:
                errors.append(f"metric {metric_id} {field} must be a positive integer")
        layer = metric.get("layer")
        if layer not in ALLOWED_LAYERS:
            errors.append(f"metric {metric_id} layer must be one of {sorted(ALLOWED_LAYERS)}")
        legacy_aliases = metric.get("legacy_aliases", [])
        if legacy_aliases is None:
            legacy_aliases = []
        if not isinstance(legacy_aliases, list) or not all(isinstance(alias, str) for alias in legacy_aliases):
            errors.append(f"metric {metric_id} legacy_aliases must be a list of strings")
            continue
        for alias in legacy_aliases:
            if alias in metric_aliases:
                errors.append(f"metric legacy_alias {alias} is duplicated")
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
        ci_mode = case.get("ci_mode")
        profiler_driver = case.get("profiler_driver")
        if status != "undefined":
            if ci_mode not in ALLOWED_CI_MODES:
                errors.append(f"case {case_id} ci_mode must be one of {sorted(ALLOWED_CI_MODES)}")
            if not isinstance(profiler_driver, str) or not profiler_driver.strip():
                errors.append(f"case {case_id} profiler_driver must be a non-empty string")
            elif ci_mode in {"gate", "report"} and profiler_driver not in IMPLEMENTED_PROFILER_DRIVERS:
                errors.append(f"case {case_id} profiler_driver {profiler_driver!r} is not implemented for ci_mode={ci_mode}")
            if ci_mode == "blocked":
                blocked_reason = case.get("blocked_reason")
                if not isinstance(blocked_reason, str) or not blocked_reason.strip():
                    errors.append(f"case {case_id} ci_mode=blocked requires blocked_reason")
        provider = case.get("provider")
        if provider not in ALLOWED_PROVIDERS:
            errors.append(f"case {case_id} provider must be one of {sorted(ALLOWED_PROVIDERS)}")
        control_path = case.get("control_path")
        if control_path not in ALLOWED_CONTROL_PATHS:
            errors.append(f"case {case_id} control_path must be managed or unmanaged")
        topology = case.get("topology")
        if topology not in ALLOWED_TOPOLOGIES:
            errors.append(f"case {case_id} topology must be one of {sorted(ALLOWED_TOPOLOGIES)}")
        _validate_profile_class(case.get("profile_class"), f"case {case_id}", errors)
        for field in ("provider", "profile", "launch", "shutdown", "truth_source", "notes"):
            value = case.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"case {case_id} {field} must be a non-empty string")
        for field in ("required_observers", "metrics"):
            value = case.get(field)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors.append(f"case {case_id} {field} must be a list of strings")
        for observer in case.get("required_observers") or []:
            if observer not in ALLOWED_OBSERVERS:
                errors.append(f"case {case_id} references unknown observer {observer}")
        for metric_id in case.get("metrics") or []:
            if metric_id not in metric_ids:
                errors.append(f"case {case_id} references unknown metric {metric_id}")
        if status == "required":
            if len(case.get("metrics") or []) < 1:
                errors.append(f"case {case_id} is required but has no metrics")
            if len(case.get("required_observers") or []) < 3:
                errors.append(f"case {case_id} is required but has fewer than 3 observers")
            if case.get("truth_source") == "none":
                errors.append(f"case {case_id} is required but has truth_source=none")
        if status == "undefined":
            if case.get("metrics"):
                errors.append(f"case {case_id} is undefined but declares metrics")
            if case.get("required_observers"):
                errors.append(f"case {case_id} is undefined but declares observers")
            if case.get("truth_source") != "none":
                errors.append(f"case {case_id} is undefined but truth_source is not none")
            if case.get("ci_mode") and case.get("ci_mode") != "blocked":
                errors.append(f"case {case_id} is undefined but ci_mode is not blocked")

    required_cases = [case for case in cases if isinstance(case, dict) and case.get("status") == "required"]
    if not required_cases:
        errors.append("at least one case must be status=required")
    return errors


def metric_target_ms(manifest: dict[str, Any], metric_id_or_alias: str, default: int | None = None) -> int | None:
    metric = metric_by_id_or_legacy_alias(manifest, metric_id_or_alias)
    if metric is None:
        return default
    value = metric.get("target_ms")
    return value if isinstance(value, int) else default


def metric_is_diagnostic(manifest: dict[str, Any], metric_id_or_alias: str) -> bool:
    metric = metric_by_id_or_legacy_alias(manifest, metric_id_or_alias)
    return bool(metric and metric.get("diagnostic") is True)


def metric_by_id_or_legacy_alias(manifest: dict[str, Any], metric_id_or_alias: str) -> dict[str, Any] | None:
    for metric in manifest.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        if metric.get("id") == metric_id_or_alias:
            return metric
        legacy_aliases = metric.get("legacy_aliases") or []
        if metric_id_or_alias in legacy_aliases:
            return metric
    return None


def case_by_id(manifest: dict[str, Any], case_id: str) -> dict[str, Any] | None:
    for case in manifest.get("cases") or []:
        if isinstance(case, dict) and case.get("id") == case_id:
            return case
    return None


def cases_by_status(manifest: dict[str, Any], status: str) -> list[dict[str, Any]]:
    return [
        case
        for case in manifest.get("cases") or []
        if isinstance(case, dict) and case.get("status") == status
    ]


def cases_by_ci_mode(manifest: dict[str, Any], ci_mode: str) -> list[dict[str, Any]]:
    return [
        case
        for case in manifest.get("cases") or []
        if isinstance(case, dict) and case.get("ci_mode") == ci_mode
    ]


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest.get("schema_version"),
        "cases": {
            status: len(cases_by_status(manifest, status))
            for status in ("required", "experimental", "undefined")
        },
        "ci_modes": {
            ci_mode: len(cases_by_ci_mode(manifest, ci_mode))
            for ci_mode in ("gate", "report", "blocked")
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
