//! What this machine can control: the managed-provider contract manifest,
//! provider binary readiness and the advertised control operations.

use super::*;

/// Every launchable provider takes Console turns; the manifest check below
/// fails if the schema ever declares one that does not.
pub(super) fn console_turn_provider_supported(provider: &str) -> bool {
    crate::managed_identity_contract::ALL_MANAGED_PROVIDERS
        .iter()
        .any(|managed| managed.as_str() == provider)
}

pub(super) fn console_provider_binary_with_env(
    provider: &str,
    get_env: &impl Fn(&str) -> Option<OsString>,
) -> String {
    let contract = managed_provider_contract_items()
        .iter()
        .find(|item| item.get("provider").and_then(Value::as_str) == Some(provider))
        .unwrap_or_else(|| panic!("missing managed provider contract for {provider}"));
    provider_binary_value(contract, get_env)
        .unwrap_or_else(|| panic!("managed provider contract for {provider} has no CLI binary"))
        .to_string_lossy()
        .into_owned()
}

pub(super) fn is_executable(path: &Path) -> bool {
    let Ok(metadata) = std::fs::metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

pub(super) fn command_value_exists_in_path(command: &OsStr, path_value: Option<&OsStr>) -> bool {
    let command_path = Path::new(command);
    if command_path.is_absolute() || command_path.components().count() > 1 {
        return is_executable(command_path);
    }
    let Some(path_value) = path_value else {
        return false;
    };
    std::env::split_paths(path_value)
        .map(|dir| dir.join(command_path))
        .any(|candidate| is_executable(&candidate))
}

pub(super) fn command_exists_in_path(command: &str, path_value: Option<&OsStr>) -> bool {
    command_value_exists_in_path(OsStr::new(command), path_value)
}

pub(super) fn managed_provider_contract_items() -> &'static Vec<Value> {
    let payload = MANAGED_PROVIDER_CONTRACTS.get_or_init(|| {
        let payload: Value = serde_json::from_str(MANAGED_PROVIDER_CONTRACTS_JSON)
            .expect("managed provider contract manifest must be valid JSON");
        validate_managed_provider_contract_manifest(&payload)
            .expect("managed provider contract manifest must satisfy the engine contract");
        payload
    });
    payload
        .get("providers")
        .and_then(Value::as_array)
        .expect("managed provider contract manifest must contain providers[]")
}

pub(crate) fn granted_control_operations(provider: &str, attached: bool) -> Vec<String> {
    if !attached {
        return Vec::new();
    }
    let Some(contract) = managed_provider_contract_items()
        .iter()
        .find(|contract| contract.get("provider").and_then(Value::as_str) == Some(provider))
    else {
        return Vec::new();
    };
    let supports = contract
        .get("machine_control_supports")
        .and_then(Value::as_array);
    let supports_operation = |operation: &str| {
        let expected = format!("{provider}.{operation}");
        supports.is_some_and(|items| items.iter().any(|item| item.as_str() == Some(&expected)))
    };
    let mut granted = Vec::new();
    if supports_operation("interrupt") {
        granted.push("interrupt".to_string());
    }
    if supports_operation("send") {
        granted.push("send_input".to_string());
    }
    if supports_operation("terminate") {
        granted.push("terminate".to_string());
    }
    granted
}

pub(super) fn validate_managed_provider_contract_manifest(payload: &Value) -> Result<(), String> {
    if payload.get("schema_version").and_then(Value::as_u64) != Some(1) {
        return Err("schema_version must be 1".to_string());
    }
    let providers = payload
        .get("providers")
        .and_then(Value::as_array)
        .ok_or_else(|| "providers[] missing".to_string())?;
    // Generated from the server's CONTRACT_OPERATIONS and evidence levels.
    let vocabulary = |key: &str| -> Result<Vec<&str>, String> {
        payload
            .get(key)
            .and_then(Value::as_array)
            .and_then(|items| items.iter().map(Value::as_str).collect::<Option<Vec<_>>>())
            .ok_or_else(|| format!("{key}[] missing"))
    };
    let operations = vocabulary("operations")?;
    let evidence_levels = vocabulary("evidence_levels")?;
    for provider in providers {
        let provider_name = provider
            .get("provider")
            .and_then(Value::as_str)
            .unwrap_or("<unknown>");
        let evidence = provider
            .get("operation_evidence")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("{provider_name}: operation_evidence must be an object"))?;
        for key in evidence.keys() {
            if !operations.contains(&key.as_str()) {
                return Err(format!(
                    "{provider_name}: unknown operation_evidence key {key}"
                ));
            }
        }
        for &operation in &operations {
            let supported = provider
                .get(operation)
                .and_then(Value::as_bool)
                .ok_or_else(|| {
                    format!("{provider_name}.{operation}: support flag must be boolean")
                })?;
            let entry = evidence
                .get(operation)
                .and_then(Value::as_object)
                .ok_or_else(|| format!("{provider_name}.{operation}: evidence missing"))?;
            let level = entry
                .get("level")
                .and_then(Value::as_str)
                .ok_or_else(|| format!("{provider_name}.{operation}: evidence level missing"))?;
            if !evidence_levels.contains(&level) {
                return Err(format!(
                    "{provider_name}.{operation}: unknown evidence level {level}"
                ));
            }
            let source = entry
                .get("source")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .trim();
            if source.is_empty() {
                return Err(format!(
                    "{provider_name}.{operation}: evidence source missing"
                ));
            }
            if supported == (level == "none") {
                return Err(format!(
                    "{provider_name}.{operation}: support flag and evidence level diverge"
                ));
            }
        }
        let turn_start = provider
            .get("turn_start")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if turn_start != console_turn_provider_supported(provider_name) {
            return Err(format!(
                "{provider_name}.turn_start: manifest support and real Console admission diverge"
            ));
        }
        let close_support = format!("{provider_name}.invocation_close");
        let supports_invocation_close = provider
            .get("machine_control_supports")
            .and_then(Value::as_array)
            .is_some_and(|items| {
                items
                    .iter()
                    .any(|item| item.as_str() == Some(close_support.as_str()))
            });
        if supports_invocation_close && !matches!(provider_name, "claude" | "codex" | "omp") {
            return Err(format!(
                "{provider_name}: invocation_close is only admitted for claude, codex, and omp"
            ));
        }
        if provider.get("support_tier").and_then(Value::as_str) == Some("maintenance") {
            let console_adapter = provider
                .get("console_adapter")
                .and_then(Value::as_str)
                .is_some_and(|value| !value.trim().is_empty());
            let console_support = provider
                .get("machine_control_supports")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .any(|support| {
                    support.ends_with(".turn_start")
                        || support.ends_with(".turn_interrupt")
                        || support.ends_with(".turn_steer")
                        || support.ends_with(".invocation_close")
                });
            if console_adapter || turn_start || console_support {
                return Err(format!(
                    "{provider_name}: maintenance providers cannot advertise Console control"
                ));
            }
        }
    }
    Ok(())
}

pub(super) fn provider_binary_value(
    contract: &Value,
    env_lookup: &dyn Fn(&str) -> Option<OsString>,
) -> Option<OsString> {
    if let Some(env_name) = contract.get("provider_cli_env").and_then(Value::as_str) {
        if !env_name.trim().is_empty() {
            if let Some(env_value) = env_lookup(env_name) {
                return (!env_value.as_os_str().is_empty()).then_some(env_value);
            }
        }
    }

    contract
        .get("provider_cli_binary")
        .and_then(Value::as_str)
        .filter(|binary| !binary.is_empty())
        .map(OsString::from)
}

pub(super) fn provider_binary_available(
    contract: &Value,
    path_value: Option<&OsStr>,
    env_lookup: &dyn Fn(&str) -> Option<OsString>,
) -> bool {
    provider_binary_value(contract, env_lookup)
        .is_some_and(|binary| command_value_exists_in_path(binary.as_os_str(), path_value))
}

pub(super) fn control_supports_for_path_with_env(
    path_value: Option<&OsStr>,
    env_lookup: &dyn Fn(&str) -> Option<OsString>,
    claude_turn_start_ready: bool,
) -> Vec<String> {
    let mut supports = Vec::new();
    supports.push(COMMAND_ARCHIVE_BACKLOG_CONTROL.to_string());
    supports.push(COMMAND_ARCHIVE_BACKLOG_CONTROL_V2.to_string());
    let longhouse_available = command_exists_in_path(DEFAULT_LONGHOUSE_BIN, path_value);
    for contract in managed_provider_contract_items() {
        let requires_longhouse = contract
            .get("requires_longhouse_cli")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if requires_longhouse && !longhouse_available {
            continue;
        }
        if !provider_binary_available(contract, path_value, env_lookup) {
            continue;
        }
        if let Some(items) = contract
            .get("machine_control_supports")
            .and_then(Value::as_array)
        {
            supports.extend(
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .filter(|support| *support != "claude.turn_start" || claude_turn_start_ready)
                    .map(str::to_string),
            );
        }
        // The CLI is present (checked above), so its own login can be relayed.
        if crate::sign_in::declared_sign_in(contract).is_some() {
            if let Some(provider) = contract.get("provider").and_then(Value::as_str) {
                supports.push(format!("{provider}.sign_in"));
            }
        }
    }
    supports
}

pub(super) fn control_supports_for_path(path_value: Option<&OsStr>) -> Vec<String> {
    control_supports_for_path_with_env(
        path_value,
        &|name| std::env::var_os(name),
        crate::claude_print::require_claude_lifecycle_hook().is_ok(),
    )
}

/// Readiness for every managed provider, as the hello frame reports it.
///
/// Probes run concurrently: only providers whose binary is actually present
/// and whose manifest declares a runnable probe ever spawn a process, so a
/// machine with nothing installed adds no subprocesses to connect at all.
/// Concurrency matters for the pathological case rather than the normal one --
/// the real probes take well under a second, but one hung CLI must not stack
/// its timeout on top of another's.
pub(super) async fn provider_readiness_snapshot() -> Value {
    let path_value = std::env::var_os("PATH");
    let env_lookup = |name: &str| std::env::var_os(name);
    let pending = managed_provider_contract_items().iter().map(|contract| {
        let binary = provider_binary_value(contract, &env_lookup);
        let on_path = binary.as_ref().is_some_and(|value| {
            command_value_exists_in_path(value.as_os_str(), path_value.as_deref())
        });
        crate::provider_readiness::readiness_for_contract(contract, binary, on_path)
    });
    crate::provider_readiness::readiness_map(futures_util::future::join_all(pending).await)
}

pub(super) fn control_supports() -> Vec<String> {
    control_supports_for_path(std::env::var_os("PATH").as_deref())
}
