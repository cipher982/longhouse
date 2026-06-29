//! Native device command surface scaffold.
//!
//! This module owns the first compiled `longhouse-engine device ...` surface.
//! It intentionally reports the native-entrypoint contract without porting
//! local-health, repair, provider proof, or provider launch behavior yet.

use anyhow::Context;
use serde::{Deserialize, Serialize};
use serde_json::Value;

const NATIVE_DEVICE_ENTRYPOINTS_JSON: &str =
    include_str!("../../config/native_device_entrypoints.json");

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

#[cfg(test)]
mod tests {
    use super::*;

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
}
