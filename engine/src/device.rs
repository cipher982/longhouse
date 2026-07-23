//! Native device command surface.

use crate::config;
use anyhow::Context;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::SystemTime;

const NATIVE_DEVICE_ENTRYPOINTS_JSON: &str =
    include_str!("../../config/native_device_entrypoints.json");
const ENGINE_FRESH_SECONDS: u64 = 30;
const ENGINE_STALE_SECONDS: u64 = 120;
const CURRENT_TRANSPORT_ERROR_DEGRADED_MIN_COUNT: u64 = 2;
const TRANSPORT_ERROR_DEGRADED_MIN_COUNT: u64 = 3;
const TRANSPORT_ERROR_DEGRADED_MIN_RATE: f64 = 0.25;
const CONSECUTIVE_FAILURES_DEGRADED_MIN_COUNT: u64 = 2;
const DEFAULT_FALLBACK_SCAN_SECS: u64 = 300;
const DEFAULT_SPOOL_REPLAY_SECS: u64 = 30;
const DEFAULT_COMPRESSION: &str = "zstd";
const LAUNCHD_LABEL: &str = "com.longhouse.shipper";
const SYSTEMD_UNIT: &str = "longhouse-shipper";
const COMMON_SERVICE_PATH_SUFFIXES: &[&str] = &[
    ".local/bin",
    "bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/home/linuxbrew/.linuxbrew/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
];

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NativeDeviceContract {
    pub schema_version: u64,
    pub native_owner: NativeOwner,
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
pub struct DeviceCommandPlan {
    pub id: String,
    pub status: String,
    pub native_target_command: String,
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
    commands: Vec<DeviceCommandStatus<'a>>,
}

#[derive(Debug, Clone, Serialize)]
struct DeviceCommandStatus<'a> {
    id: &'a str,
    status: &'a str,
    native_target_command: &'a str,
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
    file_age_seconds: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    evidence_age_seconds: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    last_updated: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    daemon_pid: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    is_offline: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    reconciliation: Option<Value>,
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

#[derive(Debug, Clone, Serialize)]
struct NativeRepairPlan {
    schema_version: u64,
    collection_tier: &'static str,
    read_only: bool,
    recommendation: String,
    headline: String,
    reasons: Vec<String>,
    machine_state: NativeMachineStateStatus,
    engine_health: NativeFastLocalHealth,
    suggested_actions: Vec<NativeRepairAction>,
    notes: Vec<&'static str>,
}

#[derive(Debug, Clone, Serialize)]
struct NativeMachineStateStatus {
    path: String,
    exists: bool,
    readable: bool,
    configured: bool,
    runtime_url_present: bool,
    machine_name_present: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct NativeRepairAction {
    id: &'static str,
    label: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    command: Option<String>,
    status: &'static str,
}

#[derive(Debug, Clone, Serialize)]
struct NativeRepairExecution {
    schema_version: u64,
    collection_tier: &'static str,
    repair_mode: &'static str,
    dry_run: bool,
    state: String,
    headline: String,
    actions: Vec<NativeRepairExecutionAction>,
    machine_state: NativeMachineStateStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    service: Option<NativeRepairServiceStatus>,
    before_health: NativeFastLocalHealth,
    #[serde(skip_serializing_if = "Option::is_none")]
    after_health: Option<NativeFastLocalHealth>,
    notes: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
struct NativeRepairExecutionAction {
    id: &'static str,
    label: &'static str,
    status: &'static str,
    platform: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    command: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct NativeRepairServiceStatus {
    path: String,
    exists: bool,
    platform: &'static str,
    longhouse_home_present: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    longhouse_home_matches: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_engine_matches: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(dead_code)]
enum NativeServicePlatform {
    Macos,
    Linux,
    Unsupported,
}

#[derive(Debug, Clone)]
struct NativeRestartCommand {
    program: &'static str,
    args: Vec<String>,
    display: String,
}

#[derive(Debug, Clone)]
struct NativeServiceManagerCommand {
    id: &'static str,
    label: &'static str,
    program: &'static str,
    args: Vec<String>,
    display: String,
}

#[derive(Debug, Clone)]
struct NativeMachineStateDetail {
    status: NativeMachineStateStatus,
    runtime_url: String,
    machine_name: String,
    config_generation: Option<String>,
    state_hash: String,
}

#[derive(Debug, Clone)]
struct NativeServiceArtifactPlan {
    service_path: PathBuf,
    log_dir: PathBuf,
    content: String,
    platform: NativeServicePlatform,
    redactions: Vec<String>,
}

#[derive(Debug, Clone)]
struct NativeEngineExecutable {
    path: PathBuf,
}

#[derive(Debug, Clone)]
struct NativeServiceRepairOptions {
    allow_scratch_home: bool,
    engine_executable_override: Option<PathBuf>,
}

impl Default for NativeServiceRepairOptions {
    fn default() -> Self {
        Self {
            allow_scratch_home: false,
            engine_executable_override: None,
        }
    }
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

pub fn cmd_device_repair_plan(json: bool, state_root: Option<&Path>) -> anyhow::Result<()> {
    let plan = collect_native_repair_plan(state_root)?;
    if json {
        println!("{}", serde_json::to_string_pretty(&plan)?);
    } else {
        print_native_repair_plan(&plan);
    }
    Ok(())
}

pub fn cmd_device_repair(
    json: bool,
    dry_run: bool,
    repair_service: bool,
    state_root: Option<&Path>,
) -> anyhow::Result<()> {
    let execution = collect_native_repair_execution(state_root, dry_run, repair_service)?;
    if json {
        println!("{}", serde_json::to_string_pretty(&execution)?);
    } else {
        print_native_repair_execution(&execution);
    }
    Ok(())
}

pub fn embedded_contract() -> anyhow::Result<NativeDeviceContract> {
    contract_from_str(NATIVE_DEVICE_ENTRYPOINTS_JSON)
}

pub fn contract_from_str(raw: &str) -> anyhow::Result<NativeDeviceContract> {
    let contract: NativeDeviceContract =
        serde_json::from_str(raw).context("parsing native device entrypoint contract")?;
    if contract.schema_version != 2 {
        anyhow::bail!(
            "native device command contract schema_version must be 2, got {}",
            contract.schema_version
        );
    }
    if contract.native_owner.binary != "longhouse" {
        anyhow::bail!("native device owner binary must be longhouse");
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
        commands: contract
            .commands
            .iter()
            .map(|command| DeviceCommandStatus {
                id: &command.id,
                status: &command.status,
                native_target_command: &command.native_target_command,
                providers: &command.providers,
            })
            .collect(),
    }
}

fn print_contract_plan(contract: &NativeDeviceContract) {
    println!("native device commands");
    println!();
    print_owner(contract);
    println!("- command groups:");
    for command in &contract.commands {
        println!(
            "  - {}: {} ({})",
            command.id, command.native_target_command, command.status
        );
        println!("    notes: {}", command.notes);
    }
}

fn print_contract_status(contract: &NativeDeviceContract) {
    println!("native device status");
    println!();
    print_owner(contract);
    println!("- command groups:");
    for command in &contract.commands {
        println!(
            "  - {}: {} -> {}",
            command.id, command.status, command.native_target_command
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

fn machine_state_path(state_root: Option<&Path>) -> anyhow::Result<PathBuf> {
    if let Some(root) = state_root {
        return Ok(root.join("machine").join("state.json"));
    }
    Ok(config::get_machine_dir()?.join("state.json"))
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
    let local_projection = object
        .and_then(|value| value.get("local_projection"))
        .and_then(Value::as_object);
    let pulse_age_seconds = local_projection
        .and_then(|value| value.get("engine_pulse_at"))
        .and_then(Value::as_str)
        .and_then(rfc3339_age_seconds);
    let evidence_age_seconds = local_projection
        .and_then(|value| value.get("generated_at"))
        .and_then(Value::as_str)
        .and_then(rfc3339_age_seconds);
    let effective_age_seconds = pulse_age_seconds.or(age_seconds);
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
    } else if effective_age_seconds
        .map(|age| age > ENGINE_STALE_SECONDS)
        .unwrap_or(false)
    {
        reasons.push("engine_status_stale".to_string());
    } else if effective_age_seconds
        .map(|age| age > ENGINE_FRESH_SECONDS)
        .unwrap_or(false)
    {
        reasons.push("engine_status_aging".to_string());
    } else if exists && error.is_none() && effective_age_seconds.is_none() {
        reasons.push("engine_status_age_unknown".to_string());
    }
    let reconciliation_state = local_projection
        .and_then(|value| value.get("reconciliation"))
        .and_then(Value::as_object)
        .and_then(|value| value.get("state"))
        .and_then(Value::as_str);
    if evidence_age_seconds
        .map(|age| age > ENGINE_FRESH_SECONDS)
        .unwrap_or(false)
    {
        reasons.push("engine_evidence_stale".to_string());
    }
    if reconciliation_state == Some("failed") {
        reasons.push("engine_reconciliation_failed".to_string());
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
                && effective_age_seconds
                    .map(|age| age <= ENGINE_FRESH_SECONDS)
                    .unwrap_or(false),
            age_seconds: effective_age_seconds,
            file_age_seconds: age_seconds,
            evidence_age_seconds,
            error,
            last_updated: object
                .and_then(|value| value.get("last_updated"))
                .and_then(Value::as_str)
                .map(str::to_string),
            daemon_pid: object.and_then(|value| value.get("daemon_pid")).cloned(),
            is_offline,
            reconciliation: local_projection
                .and_then(|value| value.get("reconciliation"))
                .cloned(),
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

fn rfc3339_age_seconds(value: &str) -> Option<u64> {
    let observed = chrono::DateTime::parse_from_rfc3339(value)
        .ok()?
        .with_timezone(&chrono::Utc);
    Some(
        chrono::Utc::now()
            .signed_duration_since(observed)
            .num_seconds()
            .max(0) as u64,
    )
}

fn collect_native_repair_plan(state_root: Option<&Path>) -> anyhow::Result<NativeRepairPlan> {
    let status_path = engine_status_path(state_root)?;
    let machine_path = machine_state_path(state_root)?;
    let engine_health = collect_native_fast_local_health(&status_path);
    let machine_state = collect_native_machine_state(&machine_path);
    Ok(native_repair_plan_from_parts(
        engine_health,
        machine_state,
        state_root.map(|path| path.display().to_string()),
    ))
}

fn collect_native_repair_execution(
    state_root: Option<&Path>,
    dry_run: bool,
    repair_service: bool,
) -> anyhow::Result<NativeRepairExecution> {
    let home = home_dir()?;
    if repair_service {
        collect_native_service_artifact_repair_execution_with_runner(
            state_root,
            dry_run,
            NativeServicePlatform::current(),
            &home,
            NativeServiceRepairOptions::default(),
            run_service_manager_command,
        )
    } else {
        collect_native_repair_execution_with_runner(
            state_root,
            dry_run,
            NativeServicePlatform::current(),
            &home,
            run_restart_command,
        )
    }
}

fn collect_native_repair_execution_with_runner<F>(
    state_root: Option<&Path>,
    dry_run: bool,
    platform: NativeServicePlatform,
    home: &Path,
    restart_runner: F,
) -> anyhow::Result<NativeRepairExecution>
where
    F: FnOnce(&NativeRestartCommand) -> Result<(), String>,
{
    let status_path = engine_status_path(state_root)?;
    let machine_path = machine_state_path(state_root)?;
    let before_health = collect_native_fast_local_health(&status_path);
    let machine_state = collect_native_machine_state(&machine_path);

    if !machine_state.configured {
        return Ok(native_repair_execution_result(
            dry_run,
            "rejected_native_setup",
            "Longhouse needs machine setup before native repair can run",
            Vec::new(),
            machine_state,
            None,
            before_health,
            None,
            vec![
                "Authenticate first with LONGHOUSE_DEVICE_TOKEN=... longhouse auth --url <runtime-url>.",
                "Then run longhouse machine repair --repair-service to install the native Machine Agent service.",
            ],
        ));
    }

    let service = collect_native_repair_service_status(platform, home, state_root);
    if platform == NativeServicePlatform::Unsupported {
        return Ok(native_repair_execution_result(
            dry_run,
            "rejected_unsupported_platform",
            "Native repair is not available on this platform yet",
            Vec::new(),
            machine_state,
            Some(service),
            before_health,
            None,
            vec!["Repair supports existing launchd and systemd user services only."],
        ));
    }

    if !service.exists {
        return Ok(native_repair_execution_result(
            dry_run,
            "rejected_no_service",
            "Longhouse has no existing Machine Agent service to restart",
            Vec::new(),
            machine_state,
            Some(service),
            before_health,
            None,
            vec![
                "Run longhouse machine repair --repair-service to create the native Machine Agent service.",
            ],
        ));
    }

    if service.error.is_some() {
        return Ok(native_repair_execution_result(
            dry_run,
            "failed",
            "Longhouse could not inspect the existing Machine Agent service",
            Vec::new(),
            machine_state,
            Some(service),
            before_health,
            None,
            vec!["Native repair refused to restart a service it could not inspect."],
        ));
    }

    if service.longhouse_home_matches != Some(true) {
        return Ok(native_repair_execution_result(
            dry_run,
            "rejected_service_mismatch",
            "Longhouse refused to restart a service for a different state root",
            Vec::new(),
            machine_state,
            Some(service),
            before_health,
            None,
            vec![
                "The existing service must declare the same LONGHOUSE_HOME as the requested state root.",
                "Native repair avoids touching ambiguous or unrelated installs.",
            ],
        ));
    }

    if service.native_engine_matches != Some(true) {
        return Ok(native_repair_execution_result(
            dry_run,
            "rejected_service_mismatch",
            "Longhouse refused to restart an unrecognized Machine Agent service",
            Vec::new(),
            machine_state,
            Some(service),
            before_health,
            None,
            vec!["The service must use the paired longhouse-engine executable."],
        ));
    }

    let Some(command) = restart_command(platform) else {
        return Ok(native_repair_execution_result(
            dry_run,
            "rejected_unsupported_platform",
            "Native repair is not available on this platform yet",
            Vec::new(),
            machine_state,
            Some(service),
            before_health,
            None,
            vec!["Repair supports existing launchd and systemd user services only."],
        ));
    };

    if dry_run {
        return Ok(native_repair_execution_result(
            true,
            "dry_run_planned",
            "Longhouse can restart the existing Machine Agent service",
            vec![NativeRepairExecutionAction {
                id: "restart_machine_agent_service",
                label: "Restart existing Machine Agent service",
                status: "planned",
                platform: platform.as_str(),
                command: Some(command.display),
                error: None,
            }],
            machine_state,
            Some(service),
            before_health,
            None,
            vec![
                "Dry run only; no service restart was attempted.",
                "Native repair does not regenerate service files, hooks, desktop artifacts, or tokens.",
            ],
        ));
    }

    match restart_runner(&command) {
        Ok(()) => {
            let after_health = collect_native_fast_local_health(&status_path);
            Ok(native_repair_execution_result(
                false,
                "completed",
                "Longhouse restarted the existing Machine Agent service",
                vec![NativeRepairExecutionAction {
                    id: "restart_machine_agent_service",
                    label: "Restart existing Machine Agent service",
                    status: "completed",
                    platform: platform.as_str(),
                    command: Some(command.display),
                    error: None,
                }],
                machine_state,
                Some(service),
                before_health,
                Some(after_health),
                vec![
                    "Native repair restarted only the existing service manager entry.",
                    "Fast health is sampled immediately after restart and may still be warming up.",
                ],
            ))
        }
        Err(error) => Ok(native_repair_execution_result(
            false,
            "failed",
            "Longhouse failed to restart the existing Machine Agent service",
            vec![NativeRepairExecutionAction {
                id: "restart_machine_agent_service",
                label: "Restart existing Machine Agent service",
                status: "failed",
                platform: platform.as_str(),
                command: Some(command.display),
                error: Some(error),
            }],
            machine_state,
            Some(service),
            before_health,
            None,
            vec![
                "Native repair did not attempt any fallback process killing or file regeneration.",
            ],
        )),
    }
}

fn native_repair_execution_result(
    dry_run: bool,
    state: &str,
    headline: &str,
    actions: Vec<NativeRepairExecutionAction>,
    machine_state: NativeMachineStateStatus,
    service: Option<NativeRepairServiceStatus>,
    before_health: NativeFastLocalHealth,
    after_health: Option<NativeFastLocalHealth>,
    notes: Vec<&'static str>,
) -> NativeRepairExecution {
    NativeRepairExecution {
        schema_version: 1,
        collection_tier: "native_fast_write",
        repair_mode: "existing_service_restart",
        dry_run,
        state: state.to_string(),
        headline: headline.to_string(),
        actions,
        machine_state,
        service,
        before_health,
        after_health,
        notes: notes.into_iter().map(str::to_string).collect(),
    }
}

fn collect_native_service_artifact_repair_execution_with_runner<F>(
    state_root: Option<&Path>,
    dry_run: bool,
    platform: NativeServicePlatform,
    home: &Path,
    options: NativeServiceRepairOptions,
    mut command_runner: F,
) -> anyhow::Result<NativeRepairExecution>
where
    F: FnMut(&NativeServiceManagerCommand) -> Result<(), String>,
{
    let status_path = engine_status_path(state_root)?;
    let machine_path = machine_state_path(state_root)?;
    let before_health = collect_native_fast_local_health(&status_path);
    let machine_status = collect_native_machine_state(&machine_path);

    if platform == NativeServicePlatform::Unsupported {
        return Ok(native_service_repair_execution_result(
            dry_run,
            "rejected_unsupported_platform",
            "Native service repair is not available on this platform yet",
            Vec::new(),
            machine_status,
            None,
            before_health,
            None,
            vec!["Service repair supports launchd and systemd user services only."],
        ));
    }

    if !options.allow_scratch_home {
        if let Some(reason) = stable_home_rejection(state_root, home) {
            return Ok(native_service_repair_execution_result(
                dry_run,
                "rejected_scratch_home",
                "Longhouse refused to install a global service for scratch state",
                Vec::new(),
                machine_status,
                None,
                before_health,
                None,
                vec![reason],
            ));
        }
    }

    let machine_detail = match collect_native_machine_state_detail(&machine_path) {
        Ok(detail) => detail,
        Err((state, status, note)) => {
            return Ok(native_service_repair_execution_result(
                dry_run,
                state,
                "Longhouse needs complete machine state before native service repair can run",
                Vec::new(),
                status,
                None,
                before_health,
                None,
                vec![note],
            ));
        }
    };

    let service = collect_native_repair_service_status(platform, home, state_root);
    if service.exists {
        if service.error.is_some() {
            return Ok(native_service_repair_execution_result(
                dry_run,
                "rejected_existing_service_ambiguous",
                "Longhouse could not safely inspect the existing Machine Agent service",
                Vec::new(),
                machine_detail.status,
                Some(service),
                before_health,
                None,
                vec![
                    "Native service repair refused to rewrite a service file it could not inspect.",
                ],
            ));
        }
        if service.longhouse_home_matches == Some(false) {
            return Ok(native_service_repair_execution_result(
                dry_run,
                "rejected_existing_service_mismatch",
                "Longhouse refused to rewrite a service for a different state root",
                Vec::new(),
                machine_detail.status,
                Some(service),
                before_health,
                None,
                vec!["The existing service must declare the target LONGHOUSE_HOME before native service repair can rewrite it."],
            ));
        }
        if service.longhouse_home_matches != Some(true) {
            return Ok(native_service_repair_execution_result(
                dry_run,
                "rejected_existing_service_ambiguous",
                "Longhouse refused to rewrite an ambiguous Machine Agent service",
                Vec::new(),
                machine_detail.status,
                Some(service),
                before_health,
                None,
                vec!["The existing service must positively identify its LONGHOUSE_HOME before native service repair can rewrite it."],
            ));
        }
        if let Some(reason) = existing_service_rewrite_rejection(platform, home) {
            return Ok(native_service_repair_execution_result(
                dry_run,
                "rejected_existing_service_ambiguous",
                "Longhouse refused to rewrite an unsafe service artifact",
                Vec::new(),
                machine_detail.status,
                Some(service),
                before_health,
                None,
                vec![reason],
            ));
        }
    }

    let artifact = match build_native_service_artifact_plan(
        platform,
        home,
        state_root,
        &machine_detail,
        options.engine_executable_override.as_deref(),
    ) {
        Ok(plan) => plan,
        Err(note) => {
            return Ok(native_service_repair_execution_result(
                dry_run,
                "rejected_engine_executable_unavailable",
                "Longhouse could not resolve an installed longhouse-engine binary",
                Vec::new(),
                machine_detail.status,
                Some(service),
                before_health,
                None,
                vec![note],
            ));
        }
    };

    if dry_run {
        return Ok(native_service_repair_execution_result(
            true,
                "dry_run_planned",
                "Longhouse can repair the stable Machine Agent service artifact",
            service_artifact_actions(&artifact, true, service.exists, None),
            machine_detail.status,
            Some(service),
            before_health,
            None,
            vec![
                "Dry run only; no service file was written and no service manager command was run.",
                "Native service repair does not touch tokens, hooks, Desktop App artifacts, backlog, or machine state.",
            ],
        ));
    }

    let mut actions = Vec::new();
    if let Err(error) = write_service_artifact(&artifact) {
        actions.push(NativeRepairExecutionAction {
            id: "write_service_file",
            label: "Write Machine Agent service file",
            status: "failed",
            platform: artifact.platform.as_str(),
            command: Some(format!("write {}", artifact.service_path.display())),
            error: Some(redact_service_error(&error, &artifact.redactions)),
        });
        return Ok(native_service_repair_execution_result(
            false,
            "failed",
            "Longhouse failed to write the Machine Agent service artifact",
            actions,
            machine_detail.status,
            Some(service),
            before_health,
            None,
            vec!["Native service repair stopped before running service-manager commands."],
        ));
    }
    actions.push(NativeRepairExecutionAction {
        id: "write_service_file",
        label: "Write Machine Agent service file",
        status: "completed",
        platform: artifact.platform.as_str(),
        command: Some(format!("write {}", artifact.service_path.display())),
        error: None,
    });

    for command in service_manager_commands(&artifact, service.exists) {
        match command_runner(&command) {
            Ok(()) => actions.push(NativeRepairExecutionAction {
                id: command.id,
                label: command.label,
                status: "completed",
                platform: artifact.platform.as_str(),
                command: Some(command.display),
                error: None,
            }),
            Err(error) => {
                actions.push(NativeRepairExecutionAction {
                    id: command.id,
                    label: command.label,
                    status: "failed",
                    platform: artifact.platform.as_str(),
                    command: Some(command.display),
                    error: Some(redact_service_error(&error, &artifact.redactions)),
                });
                return Ok(native_service_repair_execution_result(
                    false,
                    "failed",
                    "Longhouse failed to activate the Machine Agent service artifact",
                    actions,
                    machine_detail.status,
                    Some(service),
                    before_health,
                    None,
                    vec!["Native service repair does not kill fallback processes."],
                ));
            }
        }
    }

    let after_health = collect_native_fast_local_health(&status_path);
    Ok(native_service_repair_execution_result(
        false,
        "completed",
        "Longhouse repaired and activated the Machine Agent service artifact",
        actions,
        machine_detail.status,
        Some(collect_native_repair_service_status(platform, home, state_root)),
        before_health,
        Some(after_health),
        vec![
            "Native service repair wrote only the service artifact and log directory.",
            "Fast health is sampled immediately after service activation and may still be warming up.",
        ],
    ))
}

fn native_service_repair_execution_result<S: Into<String>>(
    dry_run: bool,
    state: &str,
    headline: &str,
    actions: Vec<NativeRepairExecutionAction>,
    machine_state: NativeMachineStateStatus,
    service: Option<NativeRepairServiceStatus>,
    before_health: NativeFastLocalHealth,
    after_health: Option<NativeFastLocalHealth>,
    notes: Vec<S>,
) -> NativeRepairExecution {
    NativeRepairExecution {
        schema_version: 1,
        collection_tier: "native_fast_write",
        repair_mode: "service_artifact",
        dry_run,
        state: state.to_string(),
        headline: headline.to_string(),
        actions,
        machine_state,
        service,
        before_health,
        after_health,
        notes: notes.into_iter().map(Into::into).collect(),
    }
}

fn collect_native_machine_state(path: &Path) -> NativeMachineStateStatus {
    match std::fs::read_to_string(path) {
        Ok(raw) => match serde_json::from_str::<Value>(&raw) {
            Ok(Value::Object(object)) => {
                let runtime_url_present = object
                    .get("runtime_url")
                    .and_then(Value::as_str)
                    .map(runtime_url_looks_configured)
                    .unwrap_or(false);
                let machine_name_present = object
                    .get("machine_name")
                    .and_then(Value::as_str)
                    .map(machine_name_looks_configured)
                    .unwrap_or(false);
                NativeMachineStateStatus {
                    path: path.display().to_string(),
                    exists: true,
                    readable: true,
                    configured: runtime_url_present && machine_name_present,
                    runtime_url_present,
                    machine_name_present,
                    error: None,
                }
            }
            Ok(_) => machine_state_error(path, true, "machine state payload must be a JSON object"),
            Err(err) => {
                machine_state_error(path, true, &format!("parsing machine state JSON: {err}"))
            }
        },
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => NativeMachineStateStatus {
            path: path.display().to_string(),
            exists: false,
            readable: false,
            configured: false,
            runtime_url_present: false,
            machine_name_present: false,
            error: None,
        },
        Err(err) => machine_state_error(path, true, &format!("reading machine state file: {err}")),
    }
}

fn collect_native_machine_state_detail(
    path: &Path,
) -> Result<NativeMachineStateDetail, (&'static str, NativeMachineStateStatus, &'static str)> {
    let status = collect_native_machine_state(path);
    if status.error.is_some() {
        return Err((
            "rejected_machine_state_unreadable",
            status,
            "Native service repair requires readable canonical machine state.",
        ));
    }
    if !status.exists {
        return Err((
            "rejected_machine_state_incomplete",
            status,
            "Authenticate with LONGHOUSE_DEVICE_TOKEN=... longhouse auth --url <runtime-url> to create canonical machine state.",
        ));
    }

    let raw = fs::read_to_string(path).map_err(|_| {
        (
            "rejected_machine_state_unreadable",
            status.clone(),
            "Native service repair requires readable canonical machine state.",
        )
    })?;
    let value: Value = serde_json::from_str(&raw).map_err(|_| {
        (
            "rejected_machine_state_unreadable",
            status.clone(),
            "Native service repair requires parseable canonical machine state.",
        )
    })?;
    let Some(object) = value.as_object() else {
        return Err((
            "rejected_machine_state_unreadable",
            status,
            "Canonical machine state must be a JSON object.",
        ));
    };

    let runtime_url = object
        .get("runtime_url")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| runtime_url_looks_configured(value))
        .map(str::to_string);
    let machine_name = object
        .get("machine_name")
        .and_then(Value::as_str)
        .and_then(sanitize_machine_name);

    let (Some(runtime_url), Some(machine_name)) = (runtime_url, machine_name) else {
        return Err((
            "rejected_machine_state_incomplete",
            status,
            "Canonical machine state must include runtime_url and machine_name before native service repair can run.",
        ));
    };

    let schema_version = object
        .get("schema_version")
        .and_then(Value::as_u64)
        .unwrap_or(1);
    let desktop_app_enabled = object.get("desktop_app_enabled").and_then(Value::as_bool);
    let desired_bundle_version = object
        .get("desired_bundle_version")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    let config_generation = object
        .get("config_generation")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    let state_hash = machine_state_hash(
        schema_version,
        &runtime_url,
        &machine_name,
        desktop_app_enabled,
        desired_bundle_version.as_deref(),
    );

    Ok(NativeMachineStateDetail {
        status,
        runtime_url,
        machine_name,
        config_generation,
        state_hash,
    })
}

fn machine_state_error(path: &Path, exists: bool, error: &str) -> NativeMachineStateStatus {
    NativeMachineStateStatus {
        path: path.display().to_string(),
        exists,
        readable: false,
        configured: false,
        runtime_url_present: false,
        machine_name_present: false,
        error: Some(error.to_string()),
    }
}

fn runtime_url_looks_configured(value: &str) -> bool {
    let trimmed = value.trim();
    if trimmed.is_empty()
        || trimmed.contains("typer.models.OptionInfo")
        || trimmed.contains('<')
        || trimmed.contains('>')
    {
        return false;
    }
    let Some(rest) = trimmed
        .strip_prefix("http://")
        .or_else(|| trimmed.strip_prefix("https://"))
    else {
        return false;
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("").trim();
    !authority.is_empty()
        && !authority.starts_with('/')
        && !authority.contains(char::is_whitespace)
        && !authority.starts_with(':')
}

fn machine_name_looks_configured(value: &str) -> bool {
    let normalized = value
        .trim()
        .chars()
        .filter(|ch| !matches!(ch, '&' | '<' | '>' | '"' | '\''))
        .collect::<String>();
    !normalized.trim_matches('-').trim().is_empty()
}

fn sanitize_machine_name(value: &str) -> Option<String> {
    let mut normalized = value.split_whitespace().collect::<Vec<_>>().join("-");
    normalized = normalized
        .chars()
        .filter(|ch| !matches!(ch, '&' | '<' | '>' | '"' | '\'') && !ch.is_control())
        .collect::<String>();
    while normalized.contains("--") {
        normalized = normalized.replace("--", "-");
    }
    let normalized = normalized
        .trim_matches('-')
        .chars()
        .take(64)
        .collect::<String>();
    if normalized.is_empty() {
        None
    } else {
        Some(normalized)
    }
}

fn machine_state_hash(
    schema_version: u64,
    runtime_url: &str,
    machine_name: &str,
    desktop_app_enabled: Option<bool>,
    desired_bundle_version: Option<&str>,
) -> String {
    let mut payload = BTreeMap::new();
    payload.insert("schema_version", Value::from(schema_version));
    payload.insert("runtime_url", Value::from(runtime_url.to_string()));
    payload.insert("machine_name", Value::from(machine_name.to_string()));
    payload.insert(
        "desktop_app_enabled",
        desktop_app_enabled.map(Value::from).unwrap_or(Value::Null),
    );
    payload.insert(
        "desired_bundle_version",
        desired_bundle_version
            .map(|value| Value::from(value.to_string()))
            .unwrap_or(Value::Null),
    );
    let encoded = serde_json::to_string(&payload).unwrap_or_else(|_| "{}".to_string());
    format!("{:x}", Sha256::digest(encoded.as_bytes()))
}

fn native_repair_plan_from_parts(
    engine_health: NativeFastLocalHealth,
    machine_state: NativeMachineStateStatus,
    state_root: Option<String>,
) -> NativeRepairPlan {
    let mut reasons = Vec::new();
    if machine_state.error.is_some() {
        reasons.push("machine_state_unreadable".to_string());
    } else if !machine_state.exists {
        reasons.push("machine_state_missing".to_string());
    } else if !machine_state.runtime_url_present {
        reasons.push("machine_state_missing_runtime_url".to_string());
    } else if !machine_state.machine_name_present {
        reasons.push("machine_state_missing_machine_name".to_string());
    }
    for reason in &engine_health.reasons {
        if !reasons.contains(reason) {
            reasons.push(reason.clone());
        }
    }

    let (recommendation, headline, suggested_actions) = if !machine_state.configured {
        (
            "native_setup",
            "Longhouse needs machine setup",
            vec![
                NativeRepairAction {
                    id: "native_auth",
                    label: "Authenticate this machine",
                    command: Some(
                        "LONGHOUSE_DEVICE_TOKEN=... longhouse auth --url <runtime-url>".to_string(),
                    ),
                    status: "native",
                },
                NativeRepairAction {
                    id: "native_service_install",
                    label: "Install the native Machine Agent service",
                    command: Some("longhouse machine repair --repair-service".to_string()),
                    status: "native",
                },
            ],
        )
    } else if engine_health.health_state == "healthy" {
        ("healthy", "Longhouse is healthy and configured", Vec::new())
    } else if engine_health_needs_repair(&engine_health) {
        (
            "machine_repair",
            "Longhouse local shipping needs native repair planning",
            repair_actions_with_inspection(&engine_health, state_root.as_deref()),
        )
    } else {
        (
            "inspect_logs",
            "Longhouse local shipping needs inspection",
            inspect_actions(&engine_health, state_root.as_deref()),
        )
    };

    NativeRepairPlan {
        schema_version: 1,
        collection_tier: "native_fast",
        read_only: true,
        recommendation: recommendation.to_string(),
        headline: headline.to_string(),
        reasons,
        machine_state,
        engine_health,
        suggested_actions,
        notes: vec![
            "This command only reports native repair recommendations; use the listed native commands to act.",
            "Normal installed-device commands use the paired native binaries.",
        ],
    }
}

fn engine_health_needs_repair(health: &NativeFastLocalHealth) -> bool {
    health.reasons.iter().any(|reason| {
        matches!(
            reason.as_str(),
            "engine_status_missing" | "engine_status_unreadable" | "engine_status_stale"
        )
    })
}

fn repair_actions_with_inspection(
    health: &NativeFastLocalHealth,
    state_root: Option<&str>,
) -> Vec<NativeRepairAction> {
    let mut actions = vec![NativeRepairAction {
        id: "machine_repair",
        label: "Repair the configured Longhouse machine",
        command: Some("longhouse machine repair".to_string()),
        status: "available",
    }];
    if !health.reasons.is_empty() {
        actions.push(NativeRepairAction {
            id: "inspect_native_status",
            label: "Inspect native local health details",
            command: Some(native_local_health_command(state_root)),
            status: "native",
        });
    }
    actions
}

fn inspect_actions(
    health: &NativeFastLocalHealth,
    state_root: Option<&str>,
) -> Vec<NativeRepairAction> {
    let mut actions = vec![NativeRepairAction {
        id: "inspect_native_status",
        label: "Inspect native local health details",
        command: Some(native_local_health_command(state_root)),
        status: "native",
    }];
    if health.transport.status != "healthy" && health.transport.status != "unknown" {
        actions.push(NativeRepairAction {
            id: "inspect_engine_logs",
            label: "Inspect Machine Agent logs",
            command: None,
            status: "operator_action",
        });
    }
    actions
}

fn native_local_health_command(state_root: Option<&str>) -> String {
    match state_root {
        Some(path) => format!(
            "longhouse-engine device local-health --json --state-root {}",
            shell_quote(path)
        ),
        None => "longhouse-engine device local-health --json".to_string(),
    }
}

fn shell_quote(value: &str) -> String {
    if value.is_empty() {
        return "''".to_string();
    }
    let safe = value
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '/' | '.' | '_' | '-' | ':'));
    if safe {
        return value.to_string();
    }
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn stable_home_rejection(state_root: Option<&Path>, home: &Path) -> Option<&'static str> {
    let canonical = canonical_longhouse_home(home);
    if let Some(root) = state_root {
        if !paths_match(root, &canonical) {
            return Some("Native service repair only writes the stable ~/.longhouse service.");
        }
    }
    if let Ok(value) = env::var("LONGHOUSE_HOME") {
        if !value.trim().is_empty() && !paths_match(Path::new(value.trim()), &canonical) {
            return Some("LONGHOUSE_HOME targets scratch state; native service repair refused to install a global service.");
        }
    }
    if let Ok(value) = env::var("CLAUDE_CONFIG_DIR") {
        if !value.trim().is_empty() {
            let mapped = provider_home_to_longhouse_home(Path::new(value.trim()));
            if !paths_match(&mapped, &canonical) {
                return Some("CLAUDE_CONFIG_DIR maps to scratch state; native service repair refused to install a global service.");
            }
        }
    }
    let effective = state_root
        .map(Path::to_path_buf)
        .or_else(|| config::get_longhouse_home().ok())
        .unwrap_or_else(|| canonical.clone());
    if !paths_match(&effective, &canonical) {
        return Some("Effective Longhouse home is scratch state; native service repair refused to install a global service.");
    }
    None
}

fn canonical_longhouse_home(home: &Path) -> PathBuf {
    home.join(".longhouse")
}

fn provider_home_to_longhouse_home(path: &Path) -> PathBuf {
    if matches!(
        path.file_name().and_then(|value| value.to_str()),
        Some(".longhouse")
    ) {
        return path.to_path_buf();
    }
    path.parent()
        .map(|parent| parent.join(".longhouse"))
        .unwrap_or_else(|| path.join(".longhouse"))
}

impl NativeServicePlatform {
    fn current() -> Self {
        #[cfg(target_os = "macos")]
        {
            Self::Macos
        }
        #[cfg(target_os = "linux")]
        {
            Self::Linux
        }
        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        {
            Self::Unsupported
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Macos => "macos",
            Self::Linux => "linux",
            Self::Unsupported => "unsupported",
        }
    }
}

fn home_dir() -> anyhow::Result<PathBuf> {
    env::var("HOME").map(PathBuf::from).context("HOME not set")
}

fn collect_native_repair_service_status(
    platform: NativeServicePlatform,
    home: &Path,
    state_root: Option<&Path>,
) -> NativeRepairServiceStatus {
    let Some(path) = service_path(platform, home) else {
        return NativeRepairServiceStatus {
            path: String::new(),
            exists: false,
            platform: platform.as_str(),
            longhouse_home_present: false,
            longhouse_home_matches: None,
            native_engine_matches: None,
            error: Some("unsupported service manager platform".to_string()),
        };
    };

    match std::fs::read_to_string(&path) {
        Ok(raw) => {
            let service_home = extract_service_longhouse_home(platform, &raw);
            let expected_home = state_root
                .map(Path::to_path_buf)
                .or_else(|| config::get_longhouse_home().ok());
            let longhouse_home_matches = match (service_home.as_deref(), expected_home.as_deref()) {
                (Some(actual), Some(expected)) => Some(paths_match(Path::new(actual), expected)),
                (None, Some(_)) if state_root.is_some() => Some(false),
                (Some(_), None) => None,
                (None, _) => None,
            };
            let native_engine_matches =
                extract_service_engine_executable(platform, &raw).map(|actual| {
                    resolve_native_service_engine_executable(home, None)
                        .map(|expected| paths_match(Path::new(&actual), &expected.path))
                        .unwrap_or(false)
                });
            NativeRepairServiceStatus {
                path: path.display().to_string(),
                exists: true,
                platform: platform.as_str(),
                longhouse_home_present: service_home.is_some(),
                longhouse_home_matches,
                native_engine_matches,
                error: None,
            }
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => NativeRepairServiceStatus {
            path: path.display().to_string(),
            exists: false,
            platform: platform.as_str(),
            longhouse_home_present: false,
            longhouse_home_matches: None,
            native_engine_matches: None,
            error: None,
        },
        Err(err) => NativeRepairServiceStatus {
            path: path.display().to_string(),
            exists: true,
            platform: platform.as_str(),
            longhouse_home_present: false,
            longhouse_home_matches: None,
            native_engine_matches: None,
            error: Some(format!("reading service file: {err}")),
        },
    }
}

fn service_path(platform: NativeServicePlatform, home: &Path) -> Option<PathBuf> {
    match platform {
        NativeServicePlatform::Macos => Some(
            home.join("Library")
                .join("LaunchAgents")
                .join(format!("{LAUNCHD_LABEL}.plist")),
        ),
        NativeServicePlatform::Linux => Some(
            home.join(".config")
                .join("systemd")
                .join("user")
                .join(format!("{SYSTEMD_UNIT}.service")),
        ),
        NativeServicePlatform::Unsupported => None,
    }
}

fn build_native_service_artifact_plan(
    platform: NativeServicePlatform,
    home: &Path,
    state_root: Option<&Path>,
    machine: &NativeMachineStateDetail,
    engine_executable_override: Option<&Path>,
) -> Result<NativeServiceArtifactPlan, String> {
    let Some(service_path) = service_path(platform, home) else {
        return Err("Unsupported service manager platform.".to_string());
    };
    let longhouse_home = state_root
        .map(Path::to_path_buf)
        .or_else(|| config::get_longhouse_home().ok())
        .unwrap_or_else(|| canonical_longhouse_home(home));
    let log_dir = longhouse_home.join("agent").join("logs");
    let engine = resolve_native_service_engine_executable(home, engine_executable_override)?;
    let archive_mode = default_archive_repair_mode_for_url(&machine.runtime_url);

    let content = match platform {
        NativeServicePlatform::Macos => generate_launchd_plist(
            &engine.path,
            &longhouse_home,
            &log_dir,
            machine,
            archive_mode,
            home,
        ),
        NativeServicePlatform::Linux => generate_systemd_unit(
            &engine.path,
            &longhouse_home,
            &log_dir,
            machine,
            archive_mode,
            home,
        ),
        NativeServicePlatform::Unsupported => {
            return Err("Unsupported service manager platform.".to_string())
        }
    };

    Ok(NativeServiceArtifactPlan {
        service_path,
        log_dir,
        content,
        platform,
        redactions: vec![machine.runtime_url.clone(), machine.machine_name.clone()],
    })
}

fn resolve_native_service_engine_executable(
    home: &Path,
    override_path: Option<&Path>,
) -> Result<NativeEngineExecutable, String> {
    if let Some(path) = override_path {
        let path = path.to_path_buf();
        if is_executable_file(&path) {
            return Ok(NativeEngineExecutable { path });
        }
        return Err(format!(
            "Injected engine executable does not exist or is not executable: {}",
            path.display()
        ));
    }

    let candidate = home.join(".local").join("bin").join("longhouse-engine");
    if is_executable_file(&candidate) {
        return Ok(NativeEngineExecutable { path: candidate });
    }

    if let Ok(current) = env::current_exe() {
        if is_executable_file(&current)
            && matches!(
                current.file_name().and_then(|value| value.to_str()),
                Some("longhouse-engine")
            )
        {
            return Ok(NativeEngineExecutable { path: current });
        }
    }

    if let Some(path) = find_executable_on_path("longhouse-engine") {
        return Ok(NativeEngineExecutable { path });
    }

    Err(format!(
        "Installed longhouse-engine not found at {}, current executable, or PATH.",
        candidate.display()
    ))
}

fn find_executable_on_path(name: &str) -> Option<PathBuf> {
    let path_env = env::var_os("PATH")?;
    env::split_paths(&path_env)
        .map(|dir| dir.join(name))
        .find(|candidate| is_executable_file(candidate))
}

fn is_executable_file(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        return path
            .metadata()
            .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
            .unwrap_or(false);
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn default_archive_repair_mode_for_url(url: &str) -> &'static str {
    let lower = url.to_ascii_lowercase();
    let host = lower
        .strip_prefix("https://")
        .or_else(|| lower.strip_prefix("http://"))
        .unwrap_or(&lower)
        .split(['/', '?', '#', ':'])
        .next()
        .unwrap_or("")
        .trim_end_matches('.');
    if host == "longhouse.ai" || host.ends_with(".longhouse.ai") {
        "paused"
    } else {
        "drain"
    }
}

fn generate_launchd_plist(
    engine_path: &Path,
    longhouse_home: &Path,
    log_dir: &Path,
    machine: &NativeMachineStateDetail,
    archive_mode: &str,
    home: &Path,
) -> String {
    let mut args = vec![
        engine_path.display().to_string(),
        "connect".to_string(),
        "--fallback-scan-secs".to_string(),
        DEFAULT_FALLBACK_SCAN_SECS.to_string(),
        "--spool-replay-secs".to_string(),
        DEFAULT_SPOOL_REPLAY_SECS.to_string(),
        "--archive-repair-mode".to_string(),
        archive_mode.to_string(),
        "--compression".to_string(),
        DEFAULT_COMPRESSION.to_string(),
        "--machine-name".to_string(),
        machine.machine_name.clone(),
    ];
    let program_args = args
        .drain(..)
        .map(|arg| format!("        <string>{}</string>", xml_escape(&arg)))
        .collect::<Vec<_>>()
        .join("\n");

    let environment_xml = service_environment(longhouse_home, log_dir, machine, home)
        .into_iter()
        .map(|(key, value)| {
            format!(
                "        <key>{}</key>\n        <string>{}</string>",
                xml_escape(&key),
                xml_escape(&value)
            )
        })
        .collect::<Vec<_>>()
        .join("\n");

    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
{environment_xml}
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/engine.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/engine.stdout.log</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>Nice</key>
    <integer>10</integer>
    <key>LowPriorityIO</key>
    <true/>
</dict>
</plist>
"#,
        label = LAUNCHD_LABEL,
        log_dir = xml_escape(&log_dir.display().to_string())
    )
}

fn generate_systemd_unit(
    engine_path: &Path,
    longhouse_home: &Path,
    log_dir: &Path,
    machine: &NativeMachineStateDetail,
    archive_mode: &str,
    home: &Path,
) -> String {
    let exec_args = [
        engine_path.display().to_string(),
        "connect".to_string(),
        "--fallback-scan-secs".to_string(),
        DEFAULT_FALLBACK_SCAN_SECS.to_string(),
        "--spool-replay-secs".to_string(),
        DEFAULT_SPOOL_REPLAY_SECS.to_string(),
        "--archive-repair-mode".to_string(),
        archive_mode.to_string(),
        "--compression".to_string(),
        DEFAULT_COMPRESSION.to_string(),
        "--machine-name".to_string(),
        machine.machine_name.clone(),
    ];
    let exec_start = exec_args
        .iter()
        .map(|arg| systemd_quote_arg(arg))
        .collect::<Vec<_>>()
        .join(" ");
    let environment_block = service_environment(longhouse_home, log_dir, machine, home)
        .into_iter()
        .map(|(key, value)| format!("Environment=\"{}={}\"", key, systemd_escape_value(&value)))
        .collect::<Vec<_>>()
        .join("\n");

    format!(
        r#"[Unit]
Description=Longhouse Engine - Session Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
{environment_block}

[Install]
WantedBy=default.target
"#
    )
}

fn service_environment(
    longhouse_home: &Path,
    log_dir: &Path,
    machine: &NativeMachineStateDetail,
    home: &Path,
) -> Vec<(String, String)> {
    let mut env = vec![
        (
            "CLAUDE_CONFIG_DIR".to_string(),
            home.join(".claude").display().to_string(),
        ),
        (
            "LONGHOUSE_HOME".to_string(),
            longhouse_home.display().to_string(),
        ),
        (
            "LONGHOUSE_LOG_DIR".to_string(),
            log_dir.display().to_string(),
        ),
        ("PATH".to_string(), common_service_path(home)),
    ];
    if let Some(generation) = &machine.config_generation {
        env.push((
            "LONGHOUSE_MACHINE_GENERATION".to_string(),
            generation.clone(),
        ));
    }
    env.push((
        "LONGHOUSE_MACHINE_STATE_HASH".to_string(),
        machine.state_hash.clone(),
    ));
    env
}

fn common_service_path(home: &Path) -> String {
    COMMON_SERVICE_PATH_SUFFIXES
        .iter()
        .map(|suffix| {
            if suffix.starts_with('/') {
                (*suffix).to_string()
            } else {
                home.join(suffix).display().to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(":")
}

fn extract_service_longhouse_home(platform: NativeServicePlatform, raw: &str) -> Option<String> {
    match platform {
        NativeServicePlatform::Macos => extract_plist_key_value(raw, "LONGHOUSE_HOME"),
        NativeServicePlatform::Linux => extract_systemd_environment_value(raw, "LONGHOUSE_HOME"),
        NativeServicePlatform::Unsupported => None,
    }
}

fn extract_service_engine_executable(platform: NativeServicePlatform, raw: &str) -> Option<String> {
    match platform {
        NativeServicePlatform::Macos => {
            let rest = raw.split_once("<key>ProgramArguments</key>")?.1;
            let array = rest.split_once("<array>")?.1.split_once("</array>")?.0;
            array
                .split_once("<string>")?
                .1
                .split_once("</string>")
                .map(|(value, _)| xml_unescape(value))
        }
        NativeServicePlatform::Linux => raw.lines().find_map(|line| {
            line.trim()
                .strip_prefix("ExecStart=")?
                .split_whitespace()
                .next()
                .map(str::to_string)
        }),
        NativeServicePlatform::Unsupported => None,
    }
}

fn extract_plist_key_value(raw: &str, key: &str) -> Option<String> {
    let key_tag = format!("<key>{key}</key>");
    let rest = raw.split_once(&key_tag)?.1;
    let rest = rest.split_once("<string>")?.1;
    let value = rest.split_once("</string>")?.0;
    Some(xml_unescape(value))
}

fn xml_unescape(value: &str) -> String {
    value
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn systemd_quote_arg(value: &str) -> String {
    let escaped = systemd_escape_value(value);
    let safe = escaped
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '/' | '.' | '_' | '-' | ':' | '='));
    if safe {
        escaped
    } else {
        format!("\"{escaped}\"")
    }
}

fn systemd_escape_value(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('%', "%%")
}

fn extract_systemd_environment_value(raw: &str, key: &str) -> Option<String> {
    for line in raw.lines() {
        let line = line.trim();
        let Some(rest) = line.strip_prefix("Environment=") else {
            continue;
        };
        for entry in systemd_environment_entries(rest) {
            if let Some(value) = entry.strip_prefix(&format!("{key}=")) {
                return Some(value.to_string());
            }
        }
    }
    None
}

fn systemd_environment_entries(rest: &str) -> Vec<String> {
    let mut entries = Vec::new();
    let mut current = String::new();
    let mut chars = rest.chars().peekable();
    let mut quote: Option<char> = None;
    while let Some(ch) = chars.next() {
        match quote {
            Some(q) if ch == q => {
                quote = None;
            }
            Some(_) => current.push(ch),
            None if ch == '"' || ch == '\'' => {
                quote = Some(ch);
            }
            None if ch.is_whitespace() => {
                if !current.is_empty() {
                    entries.push(std::mem::take(&mut current));
                }
            }
            None if ch == '\\' => {
                if let Some(next) = chars.next() {
                    current.push(next);
                }
            }
            None => current.push(ch),
        }
    }
    if !current.is_empty() {
        entries.push(current);
    }
    entries
}

fn existing_service_rewrite_rejection(
    platform: NativeServicePlatform,
    home: &Path,
) -> Option<String> {
    let path = service_path(platform, home)?;
    let metadata = fs::symlink_metadata(&path).ok()?;
    let file_type = metadata.file_type();
    if file_type.is_symlink() {
        return Some(format!(
            "Refusing to rewrite symlinked service file at {}.",
            path.display()
        ));
    }
    if !file_type.is_file() {
        return Some(format!(
            "Refusing to rewrite non-regular service file at {}.",
            path.display()
        ));
    }
    let raw = fs::read_to_string(&path).ok()?;
    if !looks_like_longhouse_service(platform, &raw) {
        return Some(format!(
            "Refusing to rewrite service file at {} because it does not look like Longhouse's Machine Agent service.",
            path.display()
        ));
    }
    None
}

fn looks_like_longhouse_service(platform: NativeServicePlatform, raw: &str) -> bool {
    match platform {
        NativeServicePlatform::Macos => {
            let has_label = raw.contains(&format!("<string>{LAUNCHD_LABEL}</string>"));
            let native_engine =
                raw.contains("longhouse-engine") && raw.contains("<string>connect</string>");
            has_label && native_engine
        }
        NativeServicePlatform::Linux => {
            let has_description = raw.contains("Description=Longhouse Engine - Session Sync");
            let native_engine = raw.contains("longhouse-engine") && raw.contains(" connect");
            has_description && native_engine
        }
        NativeServicePlatform::Unsupported => false,
    }
}

fn service_artifact_actions(
    artifact: &NativeServiceArtifactPlan,
    planned: bool,
    existing_service: bool,
    error: Option<String>,
) -> Vec<NativeRepairExecutionAction> {
    let status = if error.is_some() {
        "failed"
    } else if planned {
        "planned"
    } else {
        "completed"
    };
    let mut actions = vec![NativeRepairExecutionAction {
        id: "write_service_file",
        label: "Write Machine Agent service file",
        status,
        platform: artifact.platform.as_str(),
        command: Some(format!("write {}", artifact.service_path.display())),
        error,
    }];
    for command in service_manager_commands(artifact, existing_service) {
        actions.push(NativeRepairExecutionAction {
            id: command.id,
            label: command.label,
            status: if planned { "planned" } else { "pending" },
            platform: artifact.platform.as_str(),
            command: Some(command.display),
            error: None,
        });
    }
    actions
}

fn service_manager_commands(
    artifact: &NativeServiceArtifactPlan,
    existing_service: bool,
) -> Vec<NativeServiceManagerCommand> {
    match artifact.platform {
        NativeServicePlatform::Macos => {
            let mut commands = Vec::new();
            if existing_service {
                commands.push(NativeServiceManagerCommand {
                    id: "unload_launchd_service",
                    label: "Unload existing launchd service",
                    program: "launchctl",
                    args: vec![
                        "unload".to_string(),
                        artifact.service_path.display().to_string(),
                    ],
                    display: format!(
                        "launchctl unload {}",
                        shell_quote(&artifact.service_path.display().to_string())
                    ),
                });
            }
            commands.push(NativeServiceManagerCommand {
                id: "load_launchd_service",
                label: "Load launchd service",
                program: "launchctl",
                args: vec![
                    "load".to_string(),
                    artifact.service_path.display().to_string(),
                ],
                display: format!(
                    "launchctl load {}",
                    shell_quote(&artifact.service_path.display().to_string())
                ),
            });
            commands
        }
        NativeServicePlatform::Linux => vec![
            NativeServiceManagerCommand {
                id: "systemd_daemon_reload",
                label: "Reload systemd user manager",
                program: "systemctl",
                args: vec!["--user".to_string(), "daemon-reload".to_string()],
                display: "systemctl --user daemon-reload".to_string(),
            },
            NativeServiceManagerCommand {
                id: "systemd_enable_service",
                label: "Enable systemd user service",
                program: "systemctl",
                args: vec![
                    "--user".to_string(),
                    "enable".to_string(),
                    SYSTEMD_UNIT.to_string(),
                ],
                display: format!("systemctl --user enable {SYSTEMD_UNIT}"),
            },
            NativeServiceManagerCommand {
                id: if existing_service {
                    "systemd_restart_service"
                } else {
                    "systemd_start_service"
                },
                label: if existing_service {
                    "Restart systemd user service"
                } else {
                    "Start systemd user service"
                },
                program: "systemctl",
                args: vec![
                    "--user".to_string(),
                    if existing_service { "restart" } else { "start" }.to_string(),
                    SYSTEMD_UNIT.to_string(),
                ],
                display: format!(
                    "systemctl --user {} {SYSTEMD_UNIT}",
                    if existing_service { "restart" } else { "start" }
                ),
            },
        ],
        NativeServicePlatform::Unsupported => Vec::new(),
    }
}

fn write_service_artifact(artifact: &NativeServiceArtifactPlan) -> Result<(), String> {
    if let Some(parent) = artifact.service_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("creating service directory {}: {err}", parent.display()))?;
    }
    fs::create_dir_all(&artifact.log_dir).map_err(|err| {
        format!(
            "creating log directory {}: {err}",
            artifact.log_dir.display()
        )
    })?;
    write_text_atomic(&artifact.service_path, &artifact.content)
}

fn write_text_atomic(path: &Path, content: &str) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("service path has no parent: {}", path.display()))?;
    let tmp = parent.join(format!(
        ".{}.tmp-{}",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("longhouse-service"),
        std::process::id()
    ));
    fs::write(&tmp, content)
        .map_err(|err| format!("writing temp service file {}: {err}", tmp.display()))?;
    fs::rename(&tmp, path).map_err(|err| {
        let _ = fs::remove_file(&tmp);
        format!("installing service file {}: {err}", path.display())
    })
}

fn run_service_manager_command(command: &NativeServiceManagerCommand) -> Result<(), String> {
    let output = Command::new(command.program)
        .args(&command.args)
        .output()
        .map_err(|err| format!("starting {}: {err}", command.program))?;
    if output.status.success() {
        return Ok(());
    }
    Err(format_process_failure(
        output.status.code(),
        &output.stdout,
        &output.stderr,
    ))
}

fn redact_service_error(error: &str, redactions: &[String]) -> String {
    let mut redacted = error.to_string();
    for value in redactions {
        if !value.is_empty() {
            redacted = redacted.replace(value, "<redacted>");
        }
    }
    redacted
}

fn paths_match(actual: &Path, expected: &Path) -> bool {
    normalize_path(actual) == normalize_path(expected)
}

fn normalize_path(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}

fn restart_command(platform: NativeServicePlatform) -> Option<NativeRestartCommand> {
    match platform {
        NativeServicePlatform::Macos => {
            let target = format!("gui/{}/com.longhouse.shipper", current_uid());
            Some(NativeRestartCommand {
                program: "launchctl",
                args: vec!["kickstart".to_string(), "-k".to_string(), target.clone()],
                display: format!("launchctl kickstart -k {}", shell_quote(&target)),
            })
        }
        NativeServicePlatform::Linux => Some(NativeRestartCommand {
            program: "systemctl",
            args: vec![
                "--user".to_string(),
                "restart".to_string(),
                "longhouse-shipper".to_string(),
            ],
            display: "systemctl --user restart longhouse-shipper".to_string(),
        }),
        NativeServicePlatform::Unsupported => None,
    }
}

fn current_uid() -> u32 {
    #[cfg(unix)]
    {
        unsafe { libc::getuid() as u32 }
    }
    #[cfg(not(unix))]
    {
        0
    }
}

fn run_restart_command(command: &NativeRestartCommand) -> Result<(), String> {
    let output = Command::new(command.program)
        .args(&command.args)
        .output()
        .map_err(|err| format!("starting {}: {err}", command.program))?;
    if output.status.success() {
        return Ok(());
    }
    Err(format_process_failure(
        output.status.code(),
        &output.stdout,
        &output.stderr,
    ))
}

fn format_process_failure(status_code: Option<i32>, stdout: &[u8], stderr: &[u8]) -> String {
    let mut parts = vec![format!(
        "exit status {}",
        status_code
            .map(|code| code.to_string())
            .unwrap_or_else(|| "unknown".to_string())
    )];
    let stderr = truncate_output(stderr);
    if !stderr.is_empty() {
        parts.push(format!("stderr: {stderr}"));
    }
    let stdout = truncate_output(stdout);
    if !stdout.is_empty() {
        parts.push(format!("stdout: {stdout}"));
    }
    parts.join("; ")
}

fn truncate_output(output: &[u8]) -> String {
    let text = String::from_utf8_lossy(output)
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .take(3)
        .collect::<Vec<_>>()
        .join(" ");
    if text.len() > 240 {
        format!("{}...", text.chars().take(240).collect::<String>())
    } else {
        text
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

fn print_native_repair_plan(plan: &NativeRepairPlan) {
    println!("{} ({})", plan.headline, plan.recommendation);
    println!("Machine State");
    println!("  path: {}", plan.machine_state.path);
    println!(
        "  configured: {}",
        if plan.machine_state.configured {
            "yes"
        } else {
            "no"
        }
    );
    if let Some(error) = &plan.machine_state.error {
        println!("  error: {error}");
    }
    println!("Engine");
    println!("  health: {}", plan.engine_health.health_state);
    println!("  status file: {}", plan.engine_health.engine_status.path);
    if !plan.reasons.is_empty() {
        println!("Reasons");
        for reason in &plan.reasons {
            println!("  - {reason}");
        }
    }
    if !plan.suggested_actions.is_empty() {
        println!("Suggested Actions");
        for action in &plan.suggested_actions {
            match &action.command {
                Some(command) => println!("  - {}: {}", action.label, command),
                None => println!("  - {}", action.label),
            }
        }
    }
    println!("Note");
    println!("  {}", plan.notes[0]);
}

fn print_native_repair_execution(execution: &NativeRepairExecution) {
    println!("{} ({})", execution.headline, execution.state);
    println!("Machine State");
    println!("  path: {}", execution.machine_state.path);
    println!(
        "  configured: {}",
        if execution.machine_state.configured {
            "yes"
        } else {
            "no"
        }
    );
    if let Some(service) = &execution.service {
        println!("Service");
        println!("  path: {}", service.path);
        println!("  exists: {}", if service.exists { "yes" } else { "no" });
        println!("  platform: {}", service.platform);
        if let Some(matches) = service.longhouse_home_matches {
            println!(
                "  LONGHOUSE_HOME matches: {}",
                if matches { "yes" } else { "no" }
            );
        }
        if let Some(error) = &service.error {
            println!("  error: {error}");
        }
    }
    println!("Before");
    println!("  health: {}", execution.before_health.health_state);
    if let Some(after) = &execution.after_health {
        println!("After");
        println!("  health: {}", after.health_state);
    }
    if !execution.actions.is_empty() {
        println!("Actions");
        for action in &execution.actions {
            match (&action.command, &action.error) {
                (Some(command), Some(error)) => {
                    println!(
                        "  - {} ({}): {} [{error}]",
                        action.label, action.status, command
                    )
                }
                (Some(command), None) => {
                    println!("  - {} ({}): {}", action.label, action.status, command)
                }
                (None, Some(error)) => {
                    println!("  - {} ({}): {error}", action.label, action.status)
                }
                (None, None) => println!("  - {} ({})", action.label, action.status),
            }
        }
    }
    if let Some(note) = execution.notes.first() {
        println!("Note");
        println!("  {note}");
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
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn embedded_contract_describes_available_native_commands() {
        let contract = embedded_contract().unwrap();
        assert_eq!(contract.native_owner.binary, "longhouse");
        assert_eq!(contract.native_owner.namespace, "device");
        assert_eq!(contract.native_owner.status, "available");
        assert!(contract.commands.len() >= 6);
        assert!(contract
            .commands
            .iter()
            .all(|command| matches!(command.status.as_str(), "available" | "excluded")));
    }

    #[test]
    fn status_projection_keeps_core_fields() {
        let contract = embedded_contract().unwrap();
        let status = status_from_contract(&contract);
        let value = serde_json::to_value(status).unwrap();
        assert_eq!(value["schema_version"].as_u64(), Some(2));
        assert_eq!(value["native_owner"]["status"].as_str(), Some("available"));
        assert!(value["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command["id"] == "codex-managed"
                && command["native_target_command"] == "longhouse codex"));
    }

    #[test]
    fn contract_rejects_wrong_schema_version() {
        let err = contract_from_str(
            r#"{
                "schema_version": 1,
                "native_owner": {"binary": "longhouse", "namespace": "device", "status": "available"},
                "commands": []
            }"#,
        )
        .unwrap_err()
        .to_string();

        assert!(err.contains("schema_version must be 2"));
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
    fn native_fast_local_health_prefers_projection_pulse_over_file_age() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let now = chrono::Utc::now().to_rfc3339();
        let health = native_fast_health_from_parts(
            &path,
            true,
            Some(ENGINE_STALE_SECONDS + 1),
            Some(json!({
                "local_projection": {
                    "generated_at": "2026-01-01T00:00:00Z",
                    "engine_pulse_at": now,
                    "reconciliation": {"state": "reconciling", "reason": "local_status"}
                }
            })),
            None,
        );

        assert_eq!(health.health_state, "degraded");
        assert!(health.engine_status.fresh);
        assert!(health
            .reasons
            .contains(&"engine_evidence_stale".to_string()));
        assert!(health.engine_status.age_seconds.unwrap_or_default() <= 1);
        assert_eq!(
            health
                .engine_status
                .reconciliation
                .as_ref()
                .and_then(|value| value.get("state"))
                .and_then(Value::as_str),
            Some("reconciling")
        );
    }

    #[test]
    fn native_fast_local_health_surfaces_failed_reconciliation() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let now = chrono::Utc::now().to_rfc3339();
        let health = native_fast_health_from_parts(
            &path,
            true,
            Some(1),
            Some(json!({
                "local_projection": {
                    "generated_at": now.clone(),
                    "engine_pulse_at": now,
                    "reconciliation": {"state": "failed", "reason": "process_inventory"}
                }
            })),
            None,
        );

        assert_eq!(health.health_state, "degraded");
        assert!(health
            .reasons
            .contains(&"engine_reconciliation_failed".to_string()));
        assert!(health.engine_status.fresh);
    }

    #[test]
    fn native_fast_local_health_rejects_stale_projection_pulse_on_fresh_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent").join("engine-status.json");
        let stale = (chrono::Utc::now() - chrono::Duration::seconds(180)).to_rfc3339();
        let health = native_fast_health_from_parts(
            &path,
            true,
            Some(1),
            Some(json!({
                "local_projection": {
                    "generated_at": stale.clone(),
                    "engine_pulse_at": stale,
                    "reconciliation": {"state": "idle"}
                }
            })),
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

    #[test]
    fn native_repair_plan_reports_healthy_when_configured_and_fresh() {
        let dir = tempfile::tempdir().unwrap();
        let status_path = dir.path().join("agent").join("engine-status.json");
        let machine_path = dir.path().join("machine").join("state.json");
        let plan = native_repair_plan_from_parts(
            native_fast_health_from_parts(
                &status_path,
                true,
                Some(2),
                Some(json!({
                    "spool_pending_count": 0,
                    "spool_dead_count": 0,
                    "is_offline": false
                })),
                None,
            ),
            NativeMachineStateStatus {
                path: machine_path.display().to_string(),
                exists: true,
                readable: true,
                configured: true,
                runtime_url_present: true,
                machine_name_present: true,
                error: None,
            },
            None,
        );

        assert_eq!(plan.recommendation, "healthy");
        assert_eq!(plan.suggested_actions.len(), 0);
        assert!(plan.read_only);
    }

    #[test]
    fn native_repair_plan_prefers_machine_repair_for_configured_missing_engine_status() {
        let dir = tempfile::tempdir().unwrap();
        let status_path = dir.path().join("agent").join("engine-status.json");
        let machine_path = dir.path().join("machine").join("state.json");
        let plan = native_repair_plan_from_parts(
            native_fast_health_from_parts(&status_path, false, None, None, None),
            NativeMachineStateStatus {
                path: machine_path.display().to_string(),
                exists: true,
                readable: true,
                configured: true,
                runtime_url_present: true,
                machine_name_present: true,
                error: None,
            },
            None,
        );

        assert_eq!(plan.recommendation, "machine_repair");
        assert!(plan.reasons.contains(&"engine_status_missing".to_string()));
        assert!(plan
            .suggested_actions
            .iter()
            .any(|action| action.command.as_deref() == Some("longhouse machine repair")));
    }

    #[test]
    fn native_repair_plan_prefers_machine_repair_for_configured_stale_engine_status() {
        let dir = tempfile::tempdir().unwrap();
        let status_path = dir.path().join("agent").join("engine-status.json");
        let machine_path = dir.path().join("machine").join("state.json");
        let plan = native_repair_plan_from_parts(
            native_fast_health_from_parts(
                &status_path,
                true,
                Some(ENGINE_STALE_SECONDS + 1),
                Some(json!({
                    "spool_pending_count": 0,
                    "spool_dead_count": 0,
                    "is_offline": false
                })),
                None,
            ),
            NativeMachineStateStatus {
                path: machine_path.display().to_string(),
                exists: true,
                readable: true,
                configured: true,
                runtime_url_present: true,
                machine_name_present: true,
                error: None,
            },
            None,
        );

        assert_eq!(plan.recommendation, "machine_repair");
        assert!(plan.reasons.contains(&"engine_status_stale".to_string()));
    }

    #[test]
    fn native_repair_plan_prefers_native_setup_when_machine_state_missing() {
        let dir = tempfile::tempdir().unwrap();
        let status_path = dir.path().join("agent").join("engine-status.json");
        let machine_path = dir.path().join("machine").join("state.json");
        let plan = native_repair_plan_from_parts(
            native_fast_health_from_parts(
                &status_path,
                true,
                Some(2),
                Some(json!({
                    "spool_pending_count": 0,
                    "spool_dead_count": 0,
                    "is_offline": false
                })),
                None,
            ),
            collect_native_machine_state(&machine_path),
            None,
        );

        assert_eq!(plan.recommendation, "native_setup");
        assert!(plan.reasons.contains(&"machine_state_missing".to_string()));
        assert_eq!(
            plan.suggested_actions[0].command.as_deref(),
            Some("LONGHOUSE_DEVICE_TOKEN=... longhouse auth --url <runtime-url>")
        );
    }

    #[test]
    fn native_repair_plan_prefers_native_setup_when_machine_state_incomplete() {
        let dir = tempfile::tempdir().unwrap();
        let machine_path = dir.path().join("machine").join("state.json");
        std::fs::create_dir_all(machine_path.parent().unwrap()).unwrap();
        std::fs::write(
            &machine_path,
            serde_json::to_string(&json!({"runtime_url": "https://demo.longhouse.test"})).unwrap(),
        )
        .unwrap();

        let state = collect_native_machine_state(&machine_path);

        assert!(state.exists);
        assert!(state.readable);
        assert!(!state.configured);
        assert!(state.runtime_url_present);
        assert!(!state.machine_name_present);

        let status_path = dir.path().join("agent").join("engine-status.json");
        let plan = native_repair_plan_from_parts(
            native_fast_health_from_parts(
                &status_path,
                true,
                Some(2),
                Some(json!({
                    "spool_pending_count": 0,
                    "spool_dead_count": 0,
                    "is_offline": false
                })),
                None,
            ),
            state,
            None,
        );

        assert_eq!(plan.recommendation, "native_setup");
        assert!(plan
            .reasons
            .contains(&"machine_state_missing_machine_name".to_string()));
    }

    #[test]
    fn native_repair_plan_prefers_native_setup_when_machine_state_unreadable() {
        let dir = tempfile::tempdir().unwrap();
        let machine_path = dir.path().join("machine").join("state.json");
        std::fs::create_dir_all(machine_path.parent().unwrap()).unwrap();
        std::fs::write(&machine_path, "{not-json").unwrap();

        let state = collect_native_machine_state(&machine_path);

        assert!(state.exists);
        assert!(!state.readable);
        assert!(!state.configured);
        assert!(state
            .error
            .as_deref()
            .unwrap()
            .contains("parsing machine state JSON"));

        let status_path = dir.path().join("agent").join("engine-status.json");
        let plan = native_repair_plan_from_parts(
            native_fast_health_from_parts(
                &status_path,
                true,
                Some(2),
                Some(json!({
                    "spool_pending_count": 0,
                    "spool_dead_count": 0,
                    "is_offline": false
                })),
                None,
            ),
            state,
            None,
        );

        assert_eq!(plan.recommendation, "native_setup");
        assert!(plan
            .reasons
            .contains(&"machine_state_unreadable".to_string()));
    }

    #[test]
    fn native_repair_plan_matches_canonical_machine_state_completeness() {
        assert!(!runtime_url_looks_configured("https://?x"));
        assert!(!runtime_url_looks_configured("http:///path"));
        assert!(!runtime_url_looks_configured("ftp://demo.longhouse.test"));
        assert!(!runtime_url_looks_configured(
            "https://<typer.models.OptionInfo object>"
        ));
        assert!(runtime_url_looks_configured("http://127.0.0.1:8080"));
        assert!(runtime_url_looks_configured("https://demo.longhouse.test"));

        assert!(!machine_name_looks_configured("<>"));
        assert!(!machine_name_looks_configured("   "));
        assert!(machine_name_looks_configured("work macbook"));
    }

    #[test]
    fn native_repair_plan_uses_inspection_for_transport_only_failures() {
        let dir = tempfile::tempdir().unwrap();
        let status_path = dir.path().join("agent").join("engine-status.json");
        let machine_path = dir.path().join("machine").join("state.json");
        let plan = native_repair_plan_from_parts(
            native_fast_health_from_parts(
                &status_path,
                true,
                Some(2),
                Some(json!({
                    "ship_attempts_10m": 4,
                    "ship_server_errors_10m": 3,
                    "last_ship_result": "server_error",
                    "spool_pending_count": 0,
                    "spool_dead_count": 0,
                    "is_offline": false
                })),
                None,
            ),
            NativeMachineStateStatus {
                path: machine_path.display().to_string(),
                exists: true,
                readable: true,
                configured: true,
                runtime_url_present: true,
                machine_name_present: true,
                error: None,
            },
            None,
        );

        assert_eq!(plan.recommendation, "inspect_logs");
        assert!(plan.reasons.contains(&"server_errors".to_string()));
        assert!(!plan
            .suggested_actions
            .iter()
            .any(|action| action.command.as_deref() == Some("longhouse machine repair")));
    }

    #[test]
    fn native_repair_plan_collects_from_state_root() {
        let dir = tempfile::tempdir().unwrap();
        let status_path = engine_status_path(Some(dir.path())).unwrap();
        let machine_path = machine_state_path(Some(dir.path())).unwrap();
        std::fs::create_dir_all(status_path.parent().unwrap()).unwrap();
        std::fs::create_dir_all(machine_path.parent().unwrap()).unwrap();
        std::fs::write(
            &status_path,
            serde_json::to_string(&json!({
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            }))
            .unwrap(),
        )
        .unwrap();
        std::fs::write(
            &machine_path,
            serde_json::to_string(&json!({
                "runtime_url": "https://demo.longhouse.test",
                "machine_name": "cinder"
            }))
            .unwrap(),
        )
        .unwrap();

        let plan = collect_native_repair_plan(Some(dir.path())).unwrap();

        assert_eq!(plan.recommendation, "healthy");
        assert_eq!(
            plan.engine_health.engine_status.path,
            status_path.display().to_string()
        );
        assert_eq!(plan.machine_state.path, machine_path.display().to_string());
    }

    #[test]
    fn native_repair_plan_state_root_preserves_native_inspection_command_context() {
        let dir = tempfile::tempdir().unwrap();
        let status_path = engine_status_path(Some(dir.path())).unwrap();
        let machine_path = machine_state_path(Some(dir.path())).unwrap();
        std::fs::create_dir_all(status_path.parent().unwrap()).unwrap();
        std::fs::create_dir_all(machine_path.parent().unwrap()).unwrap();
        std::fs::write(
            &status_path,
            serde_json::to_string(&json!({
                "ship_attempts_10m": 4,
                "ship_server_errors_10m": 3,
                "last_ship_result": "server_error",
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            }))
            .unwrap(),
        )
        .unwrap();
        std::fs::write(
            &machine_path,
            serde_json::to_string(&json!({
                "runtime_url": "https://demo.longhouse.test",
                "machine_name": "cinder"
            }))
            .unwrap(),
        )
        .unwrap();

        let plan = collect_native_repair_plan(Some(dir.path())).unwrap();
        let expected = format!(
            "longhouse-engine device local-health --json --state-root {}",
            dir.path().display()
        );

        assert!(plan
            .suggested_actions
            .iter()
            .any(|action| action.command.as_deref() == Some(expected.as_str())));
    }

    #[test]
    fn native_repair_plan_quotes_state_root_in_suggested_commands() {
        assert_eq!(
            native_local_health_command(Some("/tmp/longhouse state;rm")),
            "longhouse-engine device local-health --json --state-root '/tmp/longhouse state;rm'"
        );
        assert_eq!(
            native_local_health_command(Some("/tmp/longhouse'root")),
            "longhouse-engine device local-health --json --state-root '/tmp/longhouse'\\''root'"
        );
    }

    #[test]
    fn native_repair_plan_json_does_not_include_machine_state_values_or_tokens() {
        let dir = tempfile::tempdir().unwrap();
        let status_path = engine_status_path(Some(dir.path())).unwrap();
        let machine_path = machine_state_path(Some(dir.path())).unwrap();
        std::fs::create_dir_all(status_path.parent().unwrap()).unwrap();
        std::fs::create_dir_all(machine_path.parent().unwrap()).unwrap();
        std::fs::write(
            &status_path,
            serde_json::to_string(&json!({
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            }))
            .unwrap(),
        )
        .unwrap();
        std::fs::write(
            &machine_path,
            serde_json::to_string(&json!({
                "runtime_url": "https://demo.longhouse.test",
                "machine_name": "cinder",
                "device_token": "zdt_secret"
            }))
            .unwrap(),
        )
        .unwrap();

        let plan = collect_native_repair_plan(Some(dir.path())).unwrap();

        let raw = serde_json::to_string(&plan).unwrap();

        assert!(!raw.contains("demo.longhouse.test"));
        assert!(!raw.contains("cinder"));
        assert!(!raw.contains("zdt_secret"));
        assert!(!raw.contains("zdt_"));
        assert!(raw.contains("\"read_only\":true"));
    }

    #[test]
    fn native_repair_plan_state_root_resolves_machine_state_path() {
        let root = PathBuf::from("/tmp/longhouse-state");
        assert_eq!(
            machine_state_path(Some(&root)).unwrap(),
            PathBuf::from("/tmp/longhouse-state/machine/state.json")
        );
    }

    fn write_configured_machine_state(root: &Path) {
        let machine_path = machine_state_path(Some(root)).unwrap();
        std::fs::create_dir_all(machine_path.parent().unwrap()).unwrap();
        std::fs::write(
            &machine_path,
            serde_json::to_string(&json!({
                "runtime_url": "https://demo.longhouse.test",
                "machine_name": "cinder",
                "device_token": "zdt_secret"
            }))
            .unwrap(),
        )
        .unwrap();
    }

    fn write_healthy_engine_status(root: &Path) {
        let status_path = engine_status_path(Some(root)).unwrap();
        std::fs::create_dir_all(status_path.parent().unwrap()).unwrap();
        std::fs::write(
            &status_path,
            serde_json::to_string(&json!({
                "last_updated": "2026-06-29T00:00:00Z",
                "spool_pending_count": 0,
                "spool_dead_count": 0,
                "is_offline": false
            }))
            .unwrap(),
        )
        .unwrap();
    }

    fn write_macos_service(home: &Path, longhouse_home: &Path) {
        let path = service_path(NativeServicePlatform::Macos, home).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let engine = home.join(".local/bin/longhouse-engine");
        std::fs::create_dir_all(engine.parent().unwrap()).unwrap();
        std::fs::write(&engine, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        std::fs::set_permissions(&engine, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::write(
            path,
            format!(
                r#"<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>ProgramArguments</key>
  <array><string>{}</string><string>connect</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LONGHOUSE_HOME</key>
    <string>{}</string>
  </dict>
</dict>
</plist>"#,
                engine.display(),
                longhouse_home.display()
            ),
        )
        .unwrap();
    }

    fn write_linux_service(home: &Path, longhouse_home: &Path) {
        let path = service_path(NativeServicePlatform::Linux, home).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let engine = home.join(".local/bin/longhouse-engine");
        std::fs::create_dir_all(engine.parent().unwrap()).unwrap();
        std::fs::write(&engine, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        std::fs::set_permissions(&engine, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::write(
            path,
            format!(
                r#"[Service]
ExecStart={} connect
Environment="CLAUDE_CONFIG_DIR=/tmp/claude" "LONGHOUSE_HOME={}" "PATH=/bin"
"#,
                engine.display(),
                longhouse_home.display()
            ),
        )
        .unwrap();
    }

    #[test]
    fn native_repair_execution_rejects_unconfigured_machine_before_service_touch() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            |_| panic!("restart must not run for an unconfigured machine"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_native_setup");
        assert!(execution.service.is_none());
        assert!(execution.actions.is_empty());
        assert!(execution.after_health.is_none());
    }

    #[test]
    fn native_repair_execution_rejects_missing_existing_service() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            |_| panic!("restart must not run without a service file"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_no_service");
        assert_eq!(execution.service.unwrap().exists, false);
        assert!(execution.actions.is_empty());
    }

    #[test]
    fn native_repair_execution_dry_run_plans_macos_restart_without_running_it() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        write_healthy_engine_status(state.path());
        write_macos_service(home.path(), state.path());

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            true,
            NativeServicePlatform::Macos,
            home.path(),
            |_| panic!("dry run must not restart the service"),
        )
        .unwrap();

        assert_eq!(execution.state, "dry_run_planned");
        assert!(execution.dry_run);
        assert_eq!(execution.actions[0].status, "planned");
        assert_eq!(execution.actions[0].platform, "macos");
        assert!(execution.actions[0]
            .command
            .as_deref()
            .unwrap()
            .starts_with("launchctl kickstart -k gui/"));
        assert_eq!(
            execution.service.unwrap().longhouse_home_matches,
            Some(true)
        );
        assert!(execution.after_health.is_none());
    }

    #[test]
    fn native_repair_execution_completes_existing_service_restart() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        write_healthy_engine_status(state.path());
        write_macos_service(home.path(), state.path());
        let mut called = false;

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            |command| {
                called = true;
                assert_eq!(command.program, "launchctl");
                Ok(())
            },
        )
        .unwrap();

        assert!(called);
        assert_eq!(execution.state, "completed");
        assert_eq!(execution.actions[0].status, "completed");
        assert!(execution.after_health.is_some());
    }

    #[test]
    fn native_repair_execution_reports_restart_failure_without_fallbacks() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        write_macos_service(home.path(), state.path());

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            |_| Err("launchctl bootstrap failed".to_string()),
        )
        .unwrap();

        assert_eq!(execution.state, "failed");
        assert_eq!(execution.actions[0].status, "failed");
        assert_eq!(
            execution.actions[0].error.as_deref(),
            Some("launchctl bootstrap failed")
        );
        assert!(execution.after_health.is_none());
        assert!(execution.notes[0].contains("fallback"));
    }

    #[test]
    fn native_repair_execution_rejects_state_root_service_mismatch() {
        let state = tempfile::tempdir().unwrap();
        let other_state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        write_macos_service(home.path(), other_state.path());

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            |_| panic!("restart must not run for a mismatched service"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_service_mismatch");
        assert_eq!(
            execution.service.unwrap().longhouse_home_matches,
            Some(false)
        );
        assert!(execution.actions.is_empty());
    }

    #[test]
    fn native_repair_refuses_to_restart_an_unrecognized_service() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        let path = service_path(NativeServicePlatform::Macos, home.path()).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            path,
            format!(
                r#"<plist><dict><key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>-m</string><string>zerg</string></array><key>EnvironmentVariables</key><dict><key>LONGHOUSE_HOME</key><string>{}</string></dict></dict></plist>"#,
                state.path().display()
            ),
        )
        .unwrap();

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            |_| panic!("unrecognized service must not be restarted"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_service_mismatch");
        assert_eq!(
            execution.service.unwrap().native_engine_matches,
            Some(false)
        );
    }

    #[test]
    fn native_repair_execution_rejects_state_root_service_without_longhouse_home() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        let path = service_path(NativeServicePlatform::Macos, home.path()).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "<plist><dict></dict></plist>").unwrap();

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            |_| panic!("restart must not run for an ambiguous service"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_service_mismatch");
        assert_eq!(
            execution.service.unwrap().longhouse_home_matches,
            Some(false)
        );
    }

    #[test]
    fn native_repair_execution_rejects_default_service_without_longhouse_home() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        let path = service_path(NativeServicePlatform::Macos, home.path()).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "<plist><dict></dict></plist>").unwrap();

        temp_env::with_vars(
            [
                ("LONGHOUSE_HOME", Some(state.path().display().to_string())),
                ("HOME", Some(home.path().display().to_string())),
                ("CLAUDE_CONFIG_DIR", None::<String>),
            ],
            || {
                let execution = collect_native_repair_execution_with_runner(
                    None,
                    false,
                    NativeServicePlatform::Macos,
                    home.path(),
                    |_| panic!("restart must not run for an ambiguous default service"),
                )
                .unwrap();

                assert_eq!(execution.state, "rejected_service_mismatch");
                assert_eq!(execution.service.unwrap().longhouse_home_matches, None);
            },
        );
    }

    #[test]
    fn native_repair_execution_dry_run_supports_linux_user_service() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        write_linux_service(home.path(), state.path());

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            true,
            NativeServicePlatform::Linux,
            home.path(),
            |_| panic!("dry run must not restart the service"),
        )
        .unwrap();

        assert_eq!(execution.state, "dry_run_planned");
        assert_eq!(execution.actions[0].platform, "linux");
        assert_eq!(
            execution.actions[0].command.as_deref(),
            Some("systemctl --user restart longhouse-shipper")
        );
        assert_eq!(
            execution.service.unwrap().longhouse_home_matches,
            Some(true)
        );
    }

    #[test]
    fn native_repair_execution_reports_unsupported_platform() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Unsupported,
            home.path(),
            |_| panic!("restart must not run for unsupported platforms"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_unsupported_platform");
        assert!(execution.service.unwrap().error.is_some());
    }

    #[test]
    fn native_repair_service_parsers_extract_longhouse_home() {
        assert_eq!(
            extract_service_longhouse_home(
                NativeServicePlatform::Macos,
                "<key>LONGHOUSE_HOME</key><string>/tmp/longhouse&amp;state</string>"
            )
            .as_deref(),
            Some("/tmp/longhouse&state")
        );
        assert_eq!(
            extract_service_longhouse_home(
                NativeServicePlatform::Linux,
                r#"Environment="CLAUDE_CONFIG_DIR=/tmp/claude" "LONGHOUSE_HOME=/tmp/longhouse state" PATH=/bin"#
            )
            .as_deref(),
            Some("/tmp/longhouse state")
        );
    }

    #[test]
    fn native_repair_execution_json_does_not_echo_machine_state_values_or_tokens() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        write_macos_service(home.path(), state.path());

        let execution = collect_native_repair_execution_with_runner(
            Some(state.path()),
            true,
            NativeServicePlatform::Macos,
            home.path(),
            |_| panic!("dry run must not restart the service"),
        )
        .unwrap();
        let raw = serde_json::to_string(&execution).unwrap();

        assert!(!raw.contains("demo.longhouse.test"));
        assert!(!raw.contains("cinder"));
        assert!(!raw.contains("zdt_secret"));
        assert!(!raw.contains("zdt_"));
        assert!(raw.contains("dry_run_planned"));
    }

    fn write_machine_state_payload(root: &Path, payload: Value) -> String {
        let machine_path = machine_state_path(Some(root)).unwrap();
        std::fs::create_dir_all(machine_path.parent().unwrap()).unwrap();
        let raw = serde_json::to_string_pretty(&payload).unwrap() + "\n";
        std::fs::write(&machine_path, &raw).unwrap();
        raw
    }

    fn write_fake_engine(home: &Path) -> PathBuf {
        let path = home.join(".local").join("bin").join("longhouse-engine");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = std::fs::metadata(&path).unwrap().permissions();
            permissions.set_mode(0o755);
            std::fs::set_permissions(&path, permissions).unwrap();
        }
        path
    }

    fn service_repair_options(engine: &Path) -> NativeServiceRepairOptions {
        NativeServiceRepairOptions {
            allow_scratch_home: true,
            engine_executable_override: Some(engine.to_path_buf()),
        }
    }

    #[test]
    fn native_service_repair_public_stable_home_dry_run_is_allowed() {
        let home = tempfile::tempdir().unwrap();
        let state = home.path().join(".longhouse");
        write_configured_machine_state(&state);
        write_fake_engine(home.path());

        temp_env::with_vars(
            [
                ("HOME", Some(home.path().display().to_string())),
                ("LONGHOUSE_HOME", None::<String>),
                ("CLAUDE_CONFIG_DIR", None::<String>),
            ],
            || {
                let execution = collect_native_service_artifact_repair_execution_with_runner(
                    Some(&state),
                    true,
                    NativeServicePlatform::Macos,
                    home.path(),
                    NativeServiceRepairOptions::default(),
                    |_| panic!("dry run must not run service-manager commands"),
                )
                .unwrap();

                assert_eq!(execution.repair_mode, "service_artifact");
                assert_eq!(execution.state, "dry_run_planned");
                assert_eq!(execution.actions[0].id, "write_service_file");
                assert_eq!(execution.actions[1].id, "load_launchd_service");
                assert!(!service_path(NativeServicePlatform::Macos, home.path())
                    .unwrap()
                    .exists());
            },
        );
    }

    #[test]
    fn native_service_repair_public_rejects_scratch_state_root() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        write_fake_engine(home.path());

        temp_env::with_vars(
            [
                ("HOME", Some(home.path().display().to_string())),
                ("LONGHOUSE_HOME", None::<String>),
                ("CLAUDE_CONFIG_DIR", None::<String>),
            ],
            || {
                let execution = collect_native_service_artifact_repair_execution_with_runner(
                    Some(state.path()),
                    true,
                    NativeServicePlatform::Macos,
                    home.path(),
                    NativeServiceRepairOptions::default(),
                    |_| panic!("scratch rejection must not run service-manager commands"),
                )
                .unwrap();

                assert_eq!(execution.state, "rejected_scratch_home");
                assert!(execution.actions.is_empty());
            },
        );
    }

    #[test]
    fn native_service_repair_public_rejects_scratch_longhouse_home_env() {
        let home = tempfile::tempdir().unwrap();
        let scratch = tempfile::tempdir().unwrap();
        let state = home.path().join(".longhouse");
        write_configured_machine_state(&state);
        write_fake_engine(home.path());

        temp_env::with_vars(
            [
                ("HOME", Some(home.path().display().to_string())),
                ("LONGHOUSE_HOME", Some(scratch.path().display().to_string())),
                ("CLAUDE_CONFIG_DIR", None::<String>),
            ],
            || {
                let execution = collect_native_service_artifact_repair_execution_with_runner(
                    Some(&state),
                    true,
                    NativeServicePlatform::Macos,
                    home.path(),
                    NativeServiceRepairOptions::default(),
                    |_| panic!("scratch env rejection must not run service-manager commands"),
                )
                .unwrap();

                assert_eq!(execution.state, "rejected_scratch_home");
                assert!(execution.actions.is_empty());
            },
        );
    }

    #[test]
    fn native_service_repair_public_rejects_scratch_claude_config_dir_env() {
        let home = tempfile::tempdir().unwrap();
        let scratch = tempfile::tempdir().unwrap();
        let state = home.path().join(".longhouse");
        write_configured_machine_state(&state);
        write_fake_engine(home.path());

        temp_env::with_vars(
            [
                ("HOME", Some(home.path().display().to_string())),
                ("LONGHOUSE_HOME", None::<String>),
                (
                    "CLAUDE_CONFIG_DIR",
                    Some(scratch.path().join(".claude").display().to_string()),
                ),
            ],
            || {
                let execution = collect_native_service_artifact_repair_execution_with_runner(
                    Some(&state),
                    true,
                    NativeServicePlatform::Macos,
                    home.path(),
                    NativeServiceRepairOptions::default(),
                    |_| panic!("scratch env rejection must not run service-manager commands"),
                )
                .unwrap();

                assert_eq!(execution.state, "rejected_scratch_home");
                assert!(execution.actions.is_empty());
            },
        );
    }

    #[test]
    fn native_service_repair_rejects_missing_machine_state() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());

        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            true,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |_| panic!("missing machine state must not run service-manager commands"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_machine_state_incomplete");
        assert!(execution.actions.is_empty());
    }

    #[test]
    fn native_service_repair_rejects_unreadable_machine_state_json() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        let machine_path = machine_state_path(Some(state.path())).unwrap();
        std::fs::create_dir_all(machine_path.parent().unwrap()).unwrap();
        std::fs::write(&machine_path, "{not-json").unwrap();

        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            true,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |_| panic!("unreadable machine state must not run service-manager commands"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_machine_state_unreadable");
        assert!(execution.actions.is_empty());
    }

    #[test]
    fn native_service_repair_rejects_incomplete_machine_state() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        write_machine_state_payload(
            state.path(),
            json!({
                "runtime_url": "https://demo.longhouse.test"
            }),
        );

        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            true,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |_| panic!("incomplete machine state must not run service-manager commands"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_machine_state_incomplete");
        assert!(execution.actions.is_empty());
    }

    #[test]
    fn native_service_repair_rejects_unsupported_platform() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());

        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            true,
            NativeServicePlatform::Unsupported,
            home.path(),
            NativeServiceRepairOptions {
                allow_scratch_home: true,
                engine_executable_override: None,
            },
            |_| panic!("unsupported platform must not run service-manager commands"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_unsupported_platform");
        assert!(execution.actions.is_empty());
    }

    #[test]
    fn native_service_repair_rejects_unavailable_engine_executable() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let path_dir = tempfile::tempdir().unwrap();
        write_configured_machine_state(state.path());
        std::fs::write(path_dir.path().join("longhouse-engine"), "not executable").unwrap();

        temp_env::with_var("PATH", Some(path_dir.path().display().to_string()), || {
            let execution = collect_native_service_artifact_repair_execution_with_runner(
                Some(state.path()),
                true,
                NativeServicePlatform::Macos,
                home.path(),
                NativeServiceRepairOptions {
                    allow_scratch_home: true,
                    engine_executable_override: None,
                },
                |_| panic!("missing engine must not run service-manager commands"),
            )
            .unwrap();

            assert_eq!(execution.state, "rejected_engine_executable_unavailable");
            assert!(execution.actions.is_empty());
        });
    }

    #[test]
    fn native_service_repair_writes_macos_plist_and_loads_service() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        write_machine_state_payload(
            state.path(),
            json!({
                "schema_version": 1,
                "config_generation": "gen-123",
                "runtime_url": "https://david010.longhouse.ai",
                "machine_name": "work macbook & <dev>",
                "desktop_app_enabled": true,
                "desired_bundle_version": "0.1.26",
                "device_token": "zdt_secret"
            }),
        );
        let mut commands = Vec::new();
        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |command| {
                commands.push(command.id);
                Ok(())
            },
        )
        .unwrap();

        let service_path = service_path(NativeServicePlatform::Macos, home.path()).unwrap();
        let content = std::fs::read_to_string(service_path).unwrap();

        assert_eq!(execution.state, "completed");
        assert_eq!(commands, vec!["load_launchd_service"]);
        assert!(content.contains("<string>com.longhouse.shipper</string>"));
        assert!(content.contains("<string>connect</string>"));
        assert!(content.contains("<string>--archive-repair-mode</string>"));
        assert!(content.contains("<string>paused</string>"));
        assert!(content.contains("<string>work-macbook-dev</string>"));
        assert!(content.contains("<key>LONGHOUSE_MACHINE_GENERATION</key>"));
        assert!(content.contains("<key>LONGHOUSE_MACHINE_STATE_HASH</key>"));
        assert!(state.path().join("agent").join("logs").exists());
    }

    #[test]
    fn native_service_repair_writes_linux_unit_with_expected_order() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        write_machine_state_payload(
            state.path(),
            json!({
                "runtime_url": "https://selfhost.example.test",
                "machine_name": "linux box"
            }),
        );
        let mut commands = Vec::new();
        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Linux,
            home.path(),
            service_repair_options(&engine),
            |command| {
                commands.push(command.id);
                Ok(())
            },
        )
        .unwrap();

        let service_path = service_path(NativeServicePlatform::Linux, home.path()).unwrap();
        let content = std::fs::read_to_string(service_path).unwrap();

        assert_eq!(execution.state, "completed");
        assert_eq!(
            commands,
            vec![
                "systemd_daemon_reload",
                "systemd_enable_service",
                "systemd_start_service"
            ]
        );
        assert!(content.contains("ExecStart="));
        assert!(content.contains("--archive-repair-mode drain"));
        assert!(content.contains("--machine-name linux-box"));
        assert!(content.contains("Environment=\"LONGHOUSE_HOME="));
    }

    #[test]
    fn native_service_repair_rewrites_existing_matching_service() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        write_configured_machine_state(state.path());
        let machine_detail =
            collect_native_machine_state_detail(&machine_state_path(Some(state.path())).unwrap())
                .unwrap();
        let plan = build_native_service_artifact_plan(
            NativeServicePlatform::Macos,
            home.path(),
            Some(state.path()),
            &machine_detail,
            Some(&engine),
        )
        .unwrap();
        write_service_artifact(&plan).unwrap();
        let mut commands = Vec::new();
        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |command| {
                commands.push(command.id);
                Ok(())
            },
        )
        .unwrap();

        assert_eq!(execution.state, "completed");
        assert_eq!(
            commands,
            vec!["unload_launchd_service", "load_launchd_service"]
        );
    }

    #[test]
    fn native_service_repair_rejects_an_unrecognized_service() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        write_configured_machine_state(state.path());
        let path = service_path(NativeServicePlatform::Macos, home.path()).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            format!(
                r#"<plist><dict><key>Label</key><string>{LAUNCHD_LABEL}</string><key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>-m</string><string>zerg</string></array><key>EnvironmentVariables</key><dict><key>LONGHOUSE_HOME</key><string>{}</string></dict></dict></plist>"#,
                state.path().display()
            ),
        )
        .unwrap();

        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |_| panic!("unrecognized service must not be rewritten"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_existing_service_ambiguous");
    }

    #[test]
    fn native_service_repair_rejects_existing_service_mismatch() {
        let state = tempfile::tempdir().unwrap();
        let other_state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        write_configured_machine_state(state.path());
        write_macos_service(home.path(), other_state.path());

        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |_| panic!("mismatched service must not be rewritten"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_existing_service_mismatch");
    }

    #[cfg(unix)]
    #[test]
    fn native_service_repair_rejects_symlink_service_file() {
        use std::os::unix::fs::symlink;

        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        write_configured_machine_state(state.path());
        let service_path = service_path(NativeServicePlatform::Macos, home.path()).unwrap();
        std::fs::create_dir_all(service_path.parent().unwrap()).unwrap();
        let target = home.path().join("target.plist");
        let machine_detail =
            collect_native_machine_state_detail(&machine_state_path(Some(state.path())).unwrap())
                .unwrap();
        let plan = build_native_service_artifact_plan(
            NativeServicePlatform::Macos,
            home.path(),
            Some(state.path()),
            &machine_detail,
            Some(&engine),
        )
        .unwrap();
        std::fs::write(&target, plan.content).unwrap();
        symlink(&target, &service_path).unwrap();

        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |_| panic!("symlink service must not be rewritten"),
        )
        .unwrap();

        assert_eq!(execution.state, "rejected_existing_service_ambiguous");
    }

    #[test]
    fn native_service_repair_reports_service_manager_failure_with_redaction() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        write_machine_state_payload(
            state.path(),
            json!({
                "runtime_url": "https://david010.longhouse.ai",
                "machine_name": "secret machine"
            }),
        );

        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |_| Err("failed for https://david010.longhouse.ai secret-machine".to_string()),
        )
        .unwrap();
        let raw = serde_json::to_string(&execution).unwrap();

        assert_eq!(execution.state, "failed");
        assert!(!raw.contains("david010.longhouse.ai"));
        assert!(!raw.contains("secret-machine"));
        assert!(raw.contains("<redacted>"));
    }

    #[test]
    fn native_service_repair_does_not_mutate_machine_state_or_echo_secrets() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let engine = write_fake_engine(home.path());
        let original = write_machine_state_payload(
            state.path(),
            json!({
                "runtime_url": "https://demo.longhouse.test",
                "machine_name": "cinder",
                "device_token": "zdt_secret"
            }),
        );

        let execution = collect_native_service_artifact_repair_execution_with_runner(
            Some(state.path()),
            false,
            NativeServicePlatform::Macos,
            home.path(),
            service_repair_options(&engine),
            |_| Ok(()),
        )
        .unwrap();
        let raw = serde_json::to_string(&execution).unwrap();
        let after =
            std::fs::read_to_string(machine_state_path(Some(state.path())).unwrap()).unwrap();

        assert_eq!(after, original);
        assert!(!state
            .path()
            .join("machine")
            .join("state-journal.jsonl")
            .exists());
        assert!(!raw.contains("demo.longhouse.test"));
        assert!(!raw.contains("cinder"));
        assert!(!raw.contains("zdt_secret"));
        assert!(!raw.contains("zdt_"));
    }

    #[test]
    fn native_service_repair_hash_matches_python_contract_vector() {
        let hash = machine_state_hash(
            1,
            "https://demo.longhouse.test",
            "cinder",
            Some(true),
            Some("0.1.26"),
        );

        assert_eq!(
            hash,
            "323c324778672b567522d29687b14f1e273951dbba28ff1dc10f3bd8c5d2c09f"
        );
    }
}
