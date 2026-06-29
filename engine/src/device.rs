//! Native device command surface scaffold.
//!
//! This module owns the first compiled `longhouse-engine device ...` surface.
//! Phase 2A reports the native-entrypoint contract. Phase 2B adds a native
//! fast local-health snapshot from the engine-owned status file, without
//! porting repair, provider proof, or provider launch behavior yet.

use crate::config;
use anyhow::Context;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

const NATIVE_DEVICE_ENTRYPOINTS_JSON: &str =
    include_str!("../../config/native_device_entrypoints.json");
const ENGINE_FRESH_SECONDS: u64 = 30;
const ENGINE_STALE_SECONDS: u64 = 120;
const CURRENT_TRANSPORT_ERROR_DEGRADED_MIN_COUNT: u64 = 2;
const TRANSPORT_ERROR_DEGRADED_MIN_COUNT: u64 = 3;
const TRANSPORT_ERROR_DEGRADED_MIN_RATE: f64 = 0.25;
const CONSECUTIVE_FAILURES_DEGRADED_MIN_COUNT: u64 = 2;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NativeDeviceContract {
    pub schema_version: u64,
    pub native_owner: NativeOwner,
    #[serde(default)]
    pub compatibility_shims: Vec<CompatibilityShim>,
    #[serde(default)]
    pub commands: Vec<DeviceCommandPlan>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NativeOwner {
    pub binary: String,
    pub namespace: String,
    pub status: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CompatibilityShim {
    pub script: String,
    pub target: String,
    pub status: String,
    pub delegates_to: String,
    #[serde(default)]
    pub phase1_inventory_ids: Vec<String>,
    pub removal_phase: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DeviceCommandPlan {
    pub id: String,
    pub status: String,
    pub implementation_phase: String,
    #[serde(default)]
    pub legacy_commands: Vec<String>,
    pub native_target_command: String,
    #[serde(default)]
    pub phase1_inventory_ids: Vec<String>,
    pub providers: Value,
    pub provider_binary_ownership: String,
    pub token_policy: String,
    pub cwd_policy: String,
    pub notes: String,
}

#[derive(Debug, Clone, Serialize)]
struct DeviceStatus<'a> {
    schema_version: u64,
    native_owner: &'a NativeOwner,
    compatibility_shims: Vec<CompatibilityShimStatus<'a>>,
    commands: Vec<DeviceCommandStatus<'a>>,
}

#[derive(Debug, Clone, Serialize)]
struct CompatibilityShimStatus<'a> {
    script: &'a str,
    delegates_to: &'a str,
    status: &'a str,
    removal_phase: &'a str,
}

#[derive(Debug, Clone, Serialize)]
struct DeviceCommandStatus<'a> {
    id: &'a str,
    status: &'a str,
    implementation_phase: &'a str,
    native_target_command: &'a str,
    legacy_commands: &'a [String],
    providers: &'a Value,
}

#[derive(Debug, Clone, Serialize)]
struct NativeFastLocalHealth {
    schema_version: u64,
    collection_tier: &'static str,
    health_state: String,
    headline: String,
    reasons: Vec<String>,
    engine_status: NativeEngineStatus,
    transport: NativeTransportStatus,
    spool: NativeSpoolStatus,
    managed_sessions: NativeManagedSessionsStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    control_channel: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    build: Option<Value>,
}

#[derive(Debug, Clone, Serialize)]
struct NativeEngineStatus {
    path: String,
    exists: bool,
    fresh: bool,
    age_seconds: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    last_updated: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    daemon_pid: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    is_offline: Option<bool>,
}

#[derive(Debug, Clone, Serialize)]
struct NativeTransportStatus {
    status: String,
    status_reason: String,
    status_summary: String,
}

#[derive(Debug, Clone, Serialize)]
struct NativeSpoolStatus {
    pending_count: u64,
    dead_count: u64,
}

#[derive(Debug, Clone, Serialize)]
struct NativeManagedSessionsStatus {
    count: usize,
}

pub fn cmd_device_plan(json: bool) -> anyhow::Result<()> {
    let contract = embedded_contract()?;
    if json {
        println!("{}", serde_json::to_string_pretty(&contract)?);
    } else {
        print_contract_plan(&contract);
    }
    Ok(())
}

pub fn cmd_device_status(json: bool) -> anyhow::Result<()> {
    let contract = embedded_contract()?;
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&status_from_contract(&contract))?
        );
    } else {
        print_contract_status(&contract);
    }
    Ok(())
}

pub fn cmd_device_local_health(json: bool, state_root: Option<&Path>) -> anyhow::Result<()> {
    let status_path = engine_status_path(state_root)?;
    let health = collect_native_fast_local_health(&status_path);
    if json {
        println!("{}", serde_json::to_string_pretty(&health)?);
    } else {
        print_native_fast_local_health(&health);
    }
    Ok(())
}

pub fn embedded_contract() -> anyhow::Result<NativeDeviceContract> {
    contract_from_str(NATIVE_DEVICE_ENTRYPOINTS_JSON)
}

pub fn contract_from_str(raw: &str) -> anyhow::Result<NativeDeviceContract> {
    let contract: NativeDeviceContract =
        serde_json::from_str(raw).context("parsing native device entrypoint contract")?;
    if contract.schema_version != 1 {
        anyhow::bail!(
            "native device entrypoint contract schema_version must be 1, got {}",
            contract.schema_version
        );
    }
    if contract.native_owner.binary != "longhouse-engine" {
        anyhow::bail!("native device owner binary must be longhouse-engine");
    }
    if contract.native_owner.namespace != "device" {
        anyhow::bail!("native device owner namespace must be device");
    }
    Ok(contract)
}

fn status_from_contract(contract: &NativeDeviceContract) -> DeviceStatus<'_> {
    DeviceStatus {
        schema_version: contract.schema_version,
        native_owner: &contract.native_owner,
        compatibility_shims: contract
            .compatibility_shims
            .iter()
            .map(|shim| CompatibilityShimStatus {
                script: &shim.script,
                delegates_to: &shim.delegates_to,
                status: &shim.status,
                removal_phase: &shim.removal_phase,
            })
            .collect(),
        commands: contract
            .commands
            .iter()
            .map(|command| DeviceCommandStatus {
                id: &command.id,
                status: &command.status,
                implementation_phase: &command.implementation_phase,
                native_target_command: &command.native_target_command,
                legacy_commands: &command.legacy_commands,
                providers: &command.providers,
            })
            .collect(),
    }
}

fn print_contract_plan(contract: &NativeDeviceContract) {
    println!("native device entrypoint plan");
    println!();
    print_owner(contract);
    println!("- compatibility shims:");
    for shim in &contract.compatibility_shims {
        println!(
            "  - {} -> {} ({}, removal {})",
            shim.script, shim.delegates_to, shim.status, shim.removal_phase
        );
    }
    println!("- command groups:");
    for command in &contract.commands {
        println!(
            "  - {}: {} ({}, {})",
            command.id, command.native_target_command, command.status, command.implementation_phase
        );
        println!("    legacy: {}", command.legacy_commands.join(", "));
        println!("    notes: {}", command.notes);
    }
}

fn print_contract_status(contract: &NativeDeviceContract) {
    println!("native device entrypoint status");
    println!();
    print_owner(contract);
    println!("- command groups:");
    for command in &contract.commands {
        println!(
            "  - {}: {} -> {} ({})",
            command.id, command.status, command.native_target_command, command.implementation_phase
        );
    }
}

fn print_owner(contract: &NativeDeviceContract) {
    println!(
        "- owner: {} {} ({})",
        contract.native_owner.binary, contract.native_owner.namespace, contract.native_owner.status
    );
}

fn engine_status_path(state_root: Option<&Path>) -> anyhow::Result<PathBuf> {
    if let Some(root) = state_root {
        return Ok(root.join("agent").join("engine-status.json"));
    }
    config::get_agent_status_path()
}

fn collect_native_fast_local_health(status_path: &Path) -> NativeFastLocalHealth {
    match std::fs::metadata(status_path) {
        Ok(metadata) => {
            let age_seconds = metadata.modified().ok().map(age_seconds_since);
            match std::fs::read_to_string(status_path) {
                Ok(raw) => match serde_json::from_str::<Value>(&raw) {
                    Ok(Value::Object(map)) => native_fast_health_from_parts(
                        status_path,
                        true,
                        age_seconds,
                        Some(Value::Object(map)),
                        None,
                    ),
                    Ok(_) => native_fast_health_from_parts(
                        status_path,
                        true,
                        age_seconds,
                        None,
                        Some("engine status payload must be a JSON object".to_string()),
                    ),
                    Err(err) => native_fast_health_from_parts(
                        status_path,
                        true,
                        age_seconds,
                        None,
                        Some(format!("parsing engine status JSON: {err}")),
                    ),
                },
                Err(err) => native_fast_health_from_parts(
                    status_path,
                    true,
                    age_seconds,
                    None,
                    Some(format!("reading engine status file: {err}")),
                ),
            }
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            native_fast_health_from_parts(status_path, false, None, None, None)
        }
        Err(err) => native_fast_health_from_parts(
            status_path,
            false,
            None,
            None,
            Some(format!("stat engine status file: {err}")),
        ),
    }
}

fn native_fast_health_from_parts(
    status_path: &Path,
    exists: bool,
    age_seconds: Option<u64>,
    payload: Option<Value>,
    error: Option<String>,
) -> NativeFastLocalHealth {
    let object = payload.as_ref().and_then(Value::as_object);
    let is_offline = object
        .and_then(|value| value.get("is_offline"))
        .and_then(Value::as_bool);
    let pending_count = object
        .and_then(|value| value.get("spool_pending_count"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let dead_count = object
        .and_then(|value| value.get("spool_dead_count"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let transport = native_transport_status(object);
    let managed_session_count = object
        .and_then(|value| value.get("managed_sessions"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);

    let mut reasons = Vec::new();
    if error.is_some() {
        reasons.push("engine_status_unreadable".to_string());
    } else if !exists {
        reasons.push("engine_status_missing".to_string());
    } else if age_seconds
        .map(|age| age > ENGINE_STALE_SECONDS)
        .unwrap_or(false)
    {
        reasons.push("engine_status_stale".to_string());
    } else if age_seconds
        .map(|age| age > ENGINE_FRESH_SECONDS)
        .unwrap_or(false)
    {
        reasons.push("engine_status_aging".to_string());
    } else if exists && error.is_none() && age_seconds.is_none() {
        reasons.push("engine_status_age_unknown".to_string());
    }
    if is_offline == Some(true) {
        reasons.push("engine_offline".to_string());
    }
    if dead_count > 0 {
        reasons.push("spool_dead_letters".to_string());
    }
    if !matches!(
        transport.status_reason.as_str(),
        "healthy" | "transport_unavailable"
    ) && !reasons.contains(&transport.status_reason)
    {
        reasons.push(transport.status_reason.clone());
    }

    let health_state = if reasons.iter().any(|reason| {
        matches!(
            reason.as_str(),
            "engine_status_unreadable"
                | "engine_status_missing"
                | "engine_status_stale"
                | "payload_rejected"
                | "payload_too_large"
        )
    }) {
        "broken"
    } else if reasons.is_empty() {
        "healthy"
    } else {
        "degraded"
    }
    .to_string();

    let headline = match health_state.as_str() {
        "healthy" => "Longhouse native fast health is healthy",
        "degraded" => "Longhouse native fast health needs attention",
        _ => "Longhouse native fast health is broken",
    }
    .to_string();

    NativeFastLocalHealth {
        schema_version: 1,
        collection_tier: "native_fast",
        health_state,
        headline,
        reasons,
        engine_status: NativeEngineStatus {
            path: status_path.display().to_string(),
            exists,
            fresh: exists
                && error.is_none()
                && age_seconds
                    .map(|age| age <= ENGINE_FRESH_SECONDS)
                    .unwrap_or(false),
            age_seconds,
            error,
            last_updated: object
                .and_then(|value| value.get("last_updated"))
                .and_then(Value::as_str)
                .map(str::to_string),
            daemon_pid: object.and_then(|value| value.get("daemon_pid")).cloned(),
            is_offline,
        },
        transport,
        spool: NativeSpoolStatus {
            pending_count,
            dead_count,
        },
        managed_sessions: NativeManagedSessionsStatus {
            count: managed_session_count,
        },
        control_channel: object
            .and_then(|value| value.get("control_channel"))
            .cloned(),
        build: object.and_then(|value| value.get("build")).cloned(),
    }
}

fn native_transport_status(
    object: Option<&serde_json::Map<String, Value>>,
) -> NativeTransportStatus {
    let Some(object) = object else {
        return transport_status(
            "unknown",
            "transport_unavailable",
            "Shipping transport fields unavailable.",
        );
    };

    let is_offline = object
        .get("is_offline")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let spool_dead = get_u64(object, "spool_dead_count");
    let parse_errors = get_u64(object, "parse_error_count_1h");
    let consecutive_failures = get_u64(object, "consecutive_ship_failures");
    let payload_rejections = get_u64(object, "ship_payload_rejections_1h");
    let payload_too_large = get_u64(object, "ship_payload_too_large_1h");
    let attempts_active = get_optional_u64(object, "ship_attempts_10m")
        .unwrap_or_else(|| get_u64(object, "ship_attempts_1h"));
    let connect_errors = get_optional_u64(object, "ship_connect_errors_10m")
        .unwrap_or_else(|| get_u64(object, "ship_connect_errors_1h"));
    let server_errors = get_optional_u64(object, "ship_server_errors_10m")
        .unwrap_or_else(|| get_u64(object, "ship_server_errors_1h"));
    let rate_limited = get_optional_u64(object, "ship_rate_limited_10m")
        .unwrap_or_else(|| get_u64(object, "ship_rate_limited_1h"));
    let retryable_client_errors = get_optional_u64(object, "ship_retryable_client_errors_10m")
        .unwrap_or_else(|| get_u64(object, "ship_retryable_client_errors_1h"));
    let last_ship_result = object.get("last_ship_result").and_then(Value::as_str);

    let connect_error_burst = is_transport_error_burst(
        connect_errors,
        attempts_active,
        last_ship_result,
        "connect_error",
    );
    let server_error_burst = is_transport_error_burst(
        server_errors,
        attempts_active,
        last_ship_result,
        "server_error",
    );
    let rate_limited_burst = is_transport_error_burst(
        rate_limited,
        attempts_active,
        last_ship_result,
        "rate_limited",
    );
    let retryable_client_error_burst = is_transport_error_burst(
        retryable_client_errors,
        attempts_active,
        last_ship_result,
        "retryable_client_error",
    );

    if payload_rejections > 0 {
        transport_status(
            "broken",
            "payload_rejected",
            &format!("{payload_rejections} ship payload rejection(s) in the last hour."),
        )
    } else if payload_too_large > 0 {
        transport_status(
            "broken",
            "payload_too_large",
            &format!("{payload_too_large} ship payload too-large rejection(s) in the last hour."),
        )
    } else if is_offline {
        transport_status("offline", "reported_offline", "Engine reported offline.")
    } else if spool_dead > 0 {
        transport_status(
            "degraded",
            "spool_dead",
            &format!("{spool_dead} dead-letter archive range(s) need attention."),
        )
    } else if parse_errors > 0 {
        transport_status(
            "degraded",
            "parse_errors",
            &format!("{parse_errors} parse error(s) in the last hour."),
        )
    } else if consecutive_failures >= CONSECUTIVE_FAILURES_DEGRADED_MIN_COUNT {
        transport_status(
            "degraded",
            "consecutive_failures",
            &format!("{consecutive_failures} consecutive ship failure(s)."),
        )
    } else if connect_error_burst {
        transport_status(
            "degraded",
            "connect_errors",
            &format!("{connect_errors} ship connect error(s) in the active window."),
        )
    } else if server_error_burst {
        transport_status(
            "degraded",
            "server_errors",
            &format!("{server_errors} ship server error(s) in the active window."),
        )
    } else if rate_limited_burst {
        transport_status(
            "degraded",
            "rate_limited",
            &format!("{rate_limited} rate-limit response(s) in the active window."),
        )
    } else if retryable_client_error_burst {
        transport_status(
            "degraded",
            "retryable_client_errors",
            &format!("{retryable_client_errors} retryable client error(s) in the active window."),
        )
    } else {
        transport_status("healthy", "healthy", "Shipping healthy.")
    }
}

fn transport_status(status: &str, reason: &str, summary: &str) -> NativeTransportStatus {
    NativeTransportStatus {
        status: status.to_string(),
        status_reason: reason.to_string(),
        status_summary: summary.to_string(),
    }
}

fn is_transport_error_burst(
    error_count: u64,
    ship_attempts: u64,
    last_ship_result: Option<&str>,
    result_kind: &str,
) -> bool {
    if error_count == 0 {
        return false;
    }
    if last_ship_result == Some(result_kind)
        && error_count >= CURRENT_TRANSPORT_ERROR_DEGRADED_MIN_COUNT
    {
        return true;
    }
    if result_kind != "connect_error" {
        return false;
    }
    if ship_attempts == 0 || error_count < TRANSPORT_ERROR_DEGRADED_MIN_COUNT {
        return false;
    }
    (error_count as f64 / ship_attempts as f64) >= TRANSPORT_ERROR_DEGRADED_MIN_RATE
}

fn get_u64(object: &serde_json::Map<String, Value>, key: &str) -> u64 {
    object.get(key).and_then(Value::as_u64).unwrap_or(0)
}

fn get_optional_u64(object: &serde_json::Map<String, Value>, key: &str) -> Option<u64> {
    object.get(key).and_then(Value::as_u64)
}

fn age_seconds_since(modified: SystemTime) -> u64 {
    SystemTime::now()
        .duration_since(modified)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn print_native_fast_local_health(health: &NativeFastLocalHealth) {
    println!("{} ({})", health.headline, health.health_state);
    println!("Engine");
    println!("  status file: {}", health.engine_status.path);
    println!(
        "  exists: {}",
        if health.engine_status.exists {
            "yes"
        } else {
            "no"
        }
    );
    println!(
        "  age: {}",
        health
            .engine_status
            .age_seconds
            .map(|age| format!("{age}s"))
            .unwrap_or_else(|| "-".to_string())
    );
    if let Some(error) = &health.engine_status.error {
        println!("  error: {error}");
    }
    println!("Spool");
    println!("  pending: {}", health.spool.pending_count);
    println!("  dead: {}", health.spool.dead_count);
    println!("Transport");
    println!("  status: {}", health.transport.status);
    println!("  summary: {}", health.transport.status_summary);
    if let Some(control_channel) = &health.control_channel {
        if let Some(status) = control_channel.get("status").and_then(Value::as_str) {
            println!("Control Channel");
            println!("  status: {status}");
        }
    }
    if !health.reasons.is_empty() {
        println!("Reasons");
        for reason in &health.reasons {
            println!("  - {reason}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn embedded_contract_marks_native_owner_but_not_command_groups() {
        let contract = embedded_contract().unwrap();
        assert_eq!(contract.native_owner.binary, "longhouse-engine");
        assert_eq!(contract.native_owner.namespace, "device");
        assert_eq!(contract.native_owner.status, "native");
        assert!(contract.commands.len() >= 8);
        assert!(contract
            .commands
            .iter()
            .all(|command| command.status == "planned"));
    }

    #[test]
    fn status_projection_keeps_core_fields() {
        let contract = embedded_contract().unwrap();
        let status = status_from_contract(&contract);
        let value = serde_json::to_value(status).unwrap();
        assert_eq!(value["schema_version"].as_u64(), Some(1));
        assert_eq!(value["native_owner"]["status"].as_str(), Some("native"));
        assert!(value["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command["id"] == "device-root"
                && command["native_target_command"] == "longhouse-engine device"));
    }

    #[test]
    fn contract_rejects_wrong_schema_version() {
        let err = contract_from_str(
            r#"{
                "schema_version": 2,
                "native_owner": {"binary": "longhouse-engine", "namespace": "device", "status": "native"},
                "commands": []
            }"#,
        )
        .unwrap_err()
        .to_string();

        assert!(err.contains("schema_version must be 1"));
    }

    #[test]
    fn native_fast_local_health_reports_fresh_status_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let health = native_fast_health_from_parts(
            &path,
            true,
            Some(2),
            Some(json!({
                "last_updated": "2026-06-29T00:00:00Z",
                "daemon_pid": 1234,
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false,
                "managed_sessions": [{"session_id": "s1"}],
                "control_channel": {"status": "connected"},
                "build": {"commit_short": "abc123"}
            })),
            None,
        );

        assert_eq!(health.schema_version, 1);
        assert_eq!(health.collection_tier, "native_fast");
        assert_eq!(health.health_state, "healthy");
        assert_eq!(health.transport.status, "healthy");
        assert!(health.engine_status.fresh);
        assert_eq!(health.managed_sessions.count, 1);
        assert_eq!(health.spool.pending_count, 0);
        assert_eq!(
            health
                .control_channel
                .unwrap()
                .get("status")
                .and_then(Value::as_str),
            Some("connected")
        );
    }

    #[test]
    fn native_fast_local_health_reports_missing_status_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let health = native_fast_health_from_parts(&path, false, None, None, None);

        assert_eq!(health.health_state, "broken");
        assert_eq!(health.engine_status.exists, false);
        assert!(health
            .reasons
            .contains(&"engine_status_missing".to_string()));
    }

    #[test]
    fn native_fast_local_health_reports_stale_status_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let health = native_fast_health_from_parts(
            &path,
            true,
            Some(ENGINE_STALE_SECONDS + 1),
            Some(json!({})),
            None,
        );

        assert_eq!(health.health_state, "broken");
        assert!(!health.engine_status.fresh);
        assert!(health.reasons.contains(&"engine_status_stale".to_string()));
    }

    #[test]
    fn native_fast_local_health_reports_unreadable_status_payload() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let health = native_fast_health_from_parts(
            &path,
            true,
            Some(1),
            None,
            Some("parsing engine status JSON: expected value".to_string()),
        );

        assert_eq!(health.health_state, "broken");
        assert_eq!(
            health.engine_status.error.as_deref(),
            Some("parsing engine status JSON: expected value")
        );
        assert!(health
            .reasons
            .contains(&"engine_status_unreadable".to_string()));
    }

    #[test]
    fn native_fast_local_health_reports_transport_payload_rejection() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let health = native_fast_health_from_parts(
            &path,
            true,
            Some(1),
            Some(json!({
                "ship_payload_rejections_1h": 1,
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            })),
            None,
        );

        assert_eq!(health.health_state, "broken");
        assert_eq!(health.transport.status, "broken");
        assert_eq!(health.transport.status_reason, "payload_rejected");
        assert!(health.reasons.contains(&"payload_rejected".to_string()));
    }

    #[test]
    fn native_fast_local_health_reports_transport_error_burst() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let health = native_fast_health_from_parts(
            &path,
            true,
            Some(1),
            Some(json!({
                "ship_attempts_10m": 4,
                "ship_server_errors_10m": 3,
                "last_ship_result": "server_error",
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            })),
            None,
        );

        assert_eq!(health.health_state, "degraded");
        assert_eq!(health.transport.status_reason, "server_errors");
        assert!(health.reasons.contains(&"server_errors".to_string()));
    }

    #[test]
    fn native_fast_local_health_ignores_recovered_server_error_rate() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let health = native_fast_health_from_parts(
            &path,
            true,
            Some(1),
            Some(json!({
                "ship_attempts_10m": 674,
                "ship_server_errors_10m": 201,
                "last_ship_result": "ok",
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            })),
            None,
        );

        assert_eq!(health.health_state, "healthy");
        assert_eq!(health.transport.status_reason, "healthy");
        assert!(health.reasons.is_empty());
    }

    #[test]
    fn native_fast_local_health_clamps_future_mtime_to_fresh() {
        let future = SystemTime::now() + std::time::Duration::from_secs(60);
        assert_eq!(age_seconds_since(future), 0);
    }

    #[test]
    fn native_fast_local_health_reports_unknown_mtime_as_degraded() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let health = native_fast_health_from_parts(
            &path,
            true,
            None,
            Some(json!({
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            })),
            None,
        );

        assert_eq!(health.health_state, "degraded");
        assert!(!health.engine_status.fresh);
        assert!(health
            .reasons
            .contains(&"engine_status_age_unknown".to_string()));
    }

    #[test]
    fn native_fast_local_health_collects_malformed_status_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "{not-json").unwrap();

        let health = collect_native_fast_local_health(&path);

        assert_eq!(health.health_state, "broken");
        assert!(health
            .reasons
            .contains(&"engine_status_unreadable".to_string()));
        assert!(health
            .engine_status
            .error
            .as_deref()
            .unwrap()
            .contains("parsing engine status JSON"));
        assert_eq!(health.transport.status, "unknown");
        assert_eq!(health.transport.status_reason, "transport_unavailable");
    }

    #[test]
    fn native_fast_local_health_collects_state_root_status_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = engine_status_path(Some(dir.path())).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            serde_json::to_string(&json!({
                "last_updated": "2026-06-29T00:00:00Z",
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            }))
            .unwrap(),
        )
        .unwrap();

        let health = collect_native_fast_local_health(&path);

        assert_eq!(health.health_state, "healthy");
        assert_eq!(health.engine_status.path, path.display().to_string());
        assert!(health.engine_status.exists);
    }

    #[test]
    fn native_fast_local_health_collects_transport_failure_from_status_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            serde_json::to_string(&json!({
                "last_updated": "2026-06-29T00:00:00Z",
                "ship_payload_rejections_1h": 2,
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            }))
            .unwrap(),
        )
        .unwrap();

        let health = collect_native_fast_local_health(&path);

        assert_eq!(health.health_state, "broken");
        assert_eq!(health.transport.status_reason, "payload_rejected");
        assert!(health.reasons.contains(&"payload_rejected".to_string()));
    }

    #[test]
    fn native_fast_local_health_state_root_resolves_agent_status_path() {
        let root = PathBuf::from("/tmp/longhouse-state");
        assert_eq!(
            engine_status_path(Some(&root)).unwrap(),
            PathBuf::from("/tmp/longhouse-state/agent/engine-status.json")
        );
    }
}
