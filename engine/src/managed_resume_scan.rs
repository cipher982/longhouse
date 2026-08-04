//! Local proof that an ended Helm session still has enough retained provider
//! state to run its native cold-continuation command.

use std::fs;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Duration, Utc};
use serde::Serialize;
use serde_json::Value;
use uuid::Uuid;

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct ResumeContractObservation {
    pub provider: String,
    pub session_id: String,
    pub provider_session_id: Option<String>,
    pub cwd: Option<String>,
    pub contract_state: String,
    pub unavailable_reason: Option<String>,
    pub source: String,
    pub raw_locator: String,
    pub observed_at: String,
    pub valid_until: String,
}

/// Retained provider contracts are machine evidence, not provider transcript
/// state. The daemon watches these directories so a contract created or
/// removed after startup triggers a fresh evidence projection.
pub fn resume_contract_dirs(longhouse_home: &Path) -> [PathBuf; 4] {
    [
        longhouse_home.join("managed-local/contracts/codex"),
        longhouse_home.join("managed-local/contracts/claude"),
        longhouse_home.join("managed-local/cursor-helm/binding-probes"),
        longhouse_home.join("managed-local/opencode/bridge/sessions"),
    ]
}

pub fn scan_resume_contracts(
    longhouse_home: &Path,
    now: DateTime<Utc>,
) -> Vec<ResumeContractObservation> {
    let mut observations = Vec::new();
    let validators: [(&str, Validator); 4] = [
        ("codex", validate_codex),
        ("claude", validate_claude),
        ("cursor", validate_cursor),
        ("opencode", validate_opencode),
    ];
    for ((provider, validate), dir) in validators
        .into_iter()
        .zip(resume_contract_dirs(longhouse_home))
    {
        scan_dir(&dir, provider, now, &mut observations, validate);
    }
    observations.sort_by(|left, right| {
        (&left.provider, &left.session_id).cmp(&(&right.provider, &right.session_id))
    });
    observations
}

type Validator = fn(&Path, &str, &Value) -> Result<(String, String), &'static str>;

fn scan_dir(
    dir: &Path,
    provider: &str,
    now: DateTime<Utc>,
    observations: &mut Vec<ResumeContractObservation>,
    validate: Validator,
) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(session_id) = path
            .file_name()
            .and_then(|value| value.to_str())
            .and_then(|value| value.strip_suffix(".json"))
        else {
            continue;
        };
        if session_id.ends_with(".observed-backup") || Uuid::parse_str(session_id).is_err() {
            continue;
        }
        let parsed = fs::read(&path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
        let validation = parsed
            .as_ref()
            .ok_or("contract_invalid")
            .and_then(|value| validate(&path, session_id, value));
        let (provider_session_id, cwd, contract_state, unavailable_reason) = match validation {
            Ok((provider_session_id, cwd)) => (
                Some(provider_session_id),
                Some(cwd),
                "valid".to_string(),
                None,
            ),
            Err(reason) => (None, None, "invalid".to_string(), Some(reason.to_string())),
        };
        observations.push(ResumeContractObservation {
            provider: provider.to_string(),
            session_id: session_id.to_string(),
            provider_session_id,
            cwd,
            contract_state,
            unavailable_reason,
            source: "managed_resume_contract_scan".to_string(),
            raw_locator: format!("{provider}/{session_id}"),
            observed_at: now.to_rfc3339(),
            valid_until: (now + Duration::minutes(20)).to_rfc3339(),
        });
    }
}

fn validate_codex(
    path: &Path,
    session_id: &str,
    value: &Value,
) -> Result<(String, String), &'static str> {
    validate_common(value, session_id, "codex", 2)?;
    let cwd = valid_directory(value.pointer("/workspace/canonical_cwd"))?;
    valid_file(value.pointer("/provider_binary/path"))?;
    let state_path = value
        .pointer("/control/state_path")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .ok_or("provider_state_missing")?;
    let longhouse_home = path.ancestors().nth(4).ok_or("contract_invalid")?;
    if !canonical_path_is_within(&state_path, longhouse_home) {
        return Err("contract_invalid");
    }
    let state: Value = fs::read(&state_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .ok_or("provider_state_missing")?;
    let provider_session_id = nonempty(state.get("thread_id")).ok_or("provider_state_missing")?;
    let thread_path = nonempty(state.get("thread_path")).ok_or("provider_state_missing")?;
    if !Path::new(&thread_path).is_file() {
        return Err("provider_state_missing");
    }
    Ok((provider_session_id, cwd))
}

fn validate_claude(
    _path: &Path,
    session_id: &str,
    value: &Value,
) -> Result<(String, String), &'static str> {
    validate_common(value, session_id, "claude", 1)?;
    let cwd = valid_directory(value.pointer("/workspace/canonical_cwd"))?;
    valid_file(value.pointer("/provider_binary/path"))?;
    let provider_session_id = nonempty(value.get("provider_session_id"))
        .filter(|value| Uuid::parse_str(value).is_ok())
        .ok_or("provider_state_missing")?;
    match nonempty(value.get("permission_mode")).as_deref() {
        Some("bypass" | "provider_local" | "remote_approve") => {}
        _ => return Err("contract_invalid"),
    }
    Ok((provider_session_id, cwd))
}

fn validate_cursor(
    _path: &Path,
    session_id: &str,
    value: &Value,
) -> Result<(String, String), &'static str> {
    if value.get("schema_version").and_then(Value::as_u64) != Some(2)
        || value.get("provider").and_then(Value::as_str) != Some("cursor")
        || value.get("session_id").and_then(Value::as_str) != Some(session_id)
        || value.get("status").and_then(Value::as_str) != Some("observed")
    {
        return Err("contract_invalid");
    }
    let cwd = valid_directory(value.get("cwd"))?;
    valid_file(value.get("provider_binary"))?;
    let provider_session_id = nonempty(value.get("conversation_uuid"))
        .filter(|value| Uuid::parse_str(value).is_ok())
        .ok_or("provider_state_missing")?;
    Ok((provider_session_id, cwd))
}

fn validate_opencode(
    _path: &Path,
    session_id: &str,
    value: &Value,
) -> Result<(String, String), &'static str> {
    if value.get("schema_version").and_then(Value::as_u64) != Some(1)
        || value.get("provider").and_then(Value::as_str) != Some("opencode")
        || value.get("longhouse_session_id").and_then(Value::as_str) != Some(session_id)
    {
        return Err("contract_invalid");
    }
    let cwd = valid_directory(value.get("cwd"))?;
    valid_file(value.get("provider_binary"))?;
    let provider_session_id =
        nonempty(value.get("provider_session_id")).ok_or("provider_state_missing")?;
    Ok((provider_session_id, cwd))
}

fn validate_common(
    value: &Value,
    session_id: &str,
    provider: &str,
    schema_version: u64,
) -> Result<(), &'static str> {
    if value.get("schema_version").and_then(Value::as_u64) != Some(schema_version)
        || value.get("provider").and_then(Value::as_str) != Some(provider)
        || value.get("session_id").and_then(Value::as_str) != Some(session_id)
    {
        return Err("contract_invalid");
    }
    Ok(())
}

fn valid_directory(value: Option<&Value>) -> Result<String, &'static str> {
    let value = nonempty(value).ok_or("workspace_missing")?;
    let canonical = fs::canonicalize(&value).map_err(|_| "workspace_missing")?;
    if !canonical.is_dir() {
        return Err("workspace_missing");
    }
    Ok(value)
}

fn canonical_path_is_within(path: &Path, root: &Path) -> bool {
    let Ok(path) = fs::canonicalize(path) else {
        return false;
    };
    let Ok(root) = fs::canonicalize(root) else {
        return false;
    };
    path.starts_with(root)
}

fn valid_file(value: Option<&Value>) -> Result<(), &'static str> {
    let value = nonempty(value).ok_or("provider_incompatible")?;
    fs::canonicalize(value)
        .ok()
        .filter(|path| path.is_file())
        .map(|_| ())
        .ok_or("provider_incompatible")
}

fn nonempty(value: Option<&Value>) -> Option<String> {
    value
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn claude_contract_becomes_bounded_resume_proof() {
        let root = tempfile::tempdir().unwrap();
        let cwd = root.path().join("workspace");
        let binary = root.path().join("claude");
        fs::create_dir_all(&cwd).unwrap();
        fs::write(&binary, b"binary").unwrap();
        let session_id = "11111111-1111-4111-8111-111111111111";
        let dir = root.path().join("managed-local/contracts/claude");
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join(format!("{session_id}.json")),
            serde_json::to_vec(&json!({
                "schema_version": 1,
                "provider": "claude",
                "session_id": session_id,
                "provider_session_id": "22222222-2222-4222-8222-222222222222",
                "permission_mode": "bypass",
                "workspace": {"canonical_cwd": cwd},
                "provider_binary": {"path": binary}
            }))
            .unwrap(),
        )
        .unwrap();
        let observations = scan_resume_contracts(root.path(), Utc::now());
        assert_eq!(observations.len(), 1);
        assert_eq!(observations[0].contract_state, "valid");
        assert_eq!(observations[0].provider, "claude");
        assert_eq!(observations[0].raw_locator, format!("claude/{session_id}"));
    }

    #[test]
    fn every_launch_tier_provider_emits_valid_resume_evidence() {
        let root = tempfile::tempdir().unwrap();
        let cwd = root.path().join("workspace-link");
        let binary = root.path().join("provider");
        fs::create_dir_all(&cwd).unwrap();
        fs::write(&binary, b"binary").unwrap();

        let codex_id = "11111111-1111-4111-8111-111111111111";
        let codex_thread = root.path().join("codex-thread.jsonl");
        fs::write(&codex_thread, b"{}").unwrap();
        let codex_state_dir = root.path().join("managed-local/codex-bridge/sessions");
        fs::create_dir_all(&codex_state_dir).unwrap();
        let codex_state = codex_state_dir.join(format!("{codex_id}.json"));
        fs::write(
            &codex_state,
            serde_json::to_vec(&json!({"thread_id": "thread-codex", "thread_path": codex_thread}))
                .unwrap(),
        )
        .unwrap();
        write_contract(
            root.path(),
            "managed-local/contracts/codex",
            codex_id,
            json!({
                "schema_version": 2,
                "provider": "codex",
                "session_id": codex_id,
                "workspace": {"canonical_cwd": cwd},
                "provider_binary": {"path": binary},
                "control": {"state_path": codex_state},
            }),
        );

        let cursor_id = "22222222-2222-4222-8222-222222222222";
        write_contract(
            root.path(),
            "managed-local/cursor-helm/binding-probes",
            cursor_id,
            json!({
                "schema_version": 2,
                "provider": "cursor",
                "session_id": cursor_id,
                "status": "observed",
                "cwd": cwd,
                "provider_binary": binary,
                "conversation_uuid": "33333333-3333-4333-8333-333333333333",
            }),
        );

        let opencode_id = "44444444-4444-4444-8444-444444444444";
        write_contract(
            root.path(),
            "managed-local/opencode/bridge/sessions",
            opencode_id,
            json!({
                "schema_version": 1,
                "provider": "opencode",
                "longhouse_session_id": opencode_id,
                "cwd": cwd,
                "provider_binary": binary,
                "provider_session_id": "ses_opencode",
            }),
        );

        let observations = scan_resume_contracts(root.path(), Utc::now());
        assert_eq!(observations.len(), 3);
        assert!(observations
            .iter()
            .all(|item| item.contract_state == "valid"));
        assert_eq!(
            observations
                .iter()
                .map(|item| item.provider.as_str())
                .collect::<Vec<_>>(),
            vec!["codex", "cursor", "opencode"]
        );
    }

    #[test]
    fn invalid_and_unconfined_contracts_emit_typed_unavailability() {
        let root = tempfile::tempdir().unwrap();
        let cwd = root.path().join("workspace");
        let binary = root.path().join("provider");
        fs::create_dir_all(&cwd).unwrap();
        fs::write(&binary, b"binary").unwrap();
        let outside_state = tempfile::NamedTempFile::new().unwrap();
        fs::write(
            outside_state.path(),
            serde_json::to_vec(
                &json!({"thread_id": "secret", "thread_path": outside_state.path()}),
            )
            .unwrap(),
        )
        .unwrap();
        let session_id = "55555555-5555-4555-8555-555555555555";
        write_contract(
            root.path(),
            "managed-local/contracts/codex",
            session_id,
            json!({
                "schema_version": 2,
                "provider": "codex",
                "session_id": session_id,
                "workspace": {"canonical_cwd": cwd},
                "provider_binary": {"path": binary},
                "control": {"state_path": outside_state.path()},
            }),
        );
        let observations = scan_resume_contracts(root.path(), Utc::now());
        assert_eq!(observations[0].contract_state, "invalid");
        assert_eq!(
            observations[0].unavailable_reason.as_deref(),
            Some("contract_invalid")
        );
        assert!(observations[0].provider_session_id.is_none());
    }

    fn write_contract(root: &Path, relative_dir: &str, session_id: &str, value: Value) {
        let dir = root.join(relative_dir);
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join(format!("{session_id}.json")),
            serde_json::to_vec(&value).unwrap(),
        )
        .unwrap();
    }
}
