//! Authenticated Machine-Agent control for a stock OMP Helm process.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::Result;
use serde::Deserialize;
use serde_json::{json, Value};

pub use crate::helm_control::CommandKind;
pub type OmpHelmControlError = crate::helm_control::HelmControlError;

pub const OMP_HELM_TRANSPORT: &str = "omp_helm_channel";

pub fn default_omp_helm_state_dir() -> Option<PathBuf> {
    crate::config::get_longhouse_home()
        .ok()
        .map(|home| home.join("managed-local").join("omp-helm"))
}

fn resolve_state_dir(state_root: Option<&Path>) -> Result<PathBuf> {
    state_root
        .map(Path::to_path_buf)
        .or_else(default_omp_helm_state_dir)
        .ok_or_else(|| anyhow::anyhow!("could not resolve Longhouse home for OMP Helm state"))
}

fn state_file_path(session_id: &str, state_root: Option<&Path>) -> Result<PathBuf> {
    Ok(resolve_state_dir(state_root)?.join(format!("{session_id}.json")))
}

#[derive(Debug, Deserialize)]
struct OmpHelmStateFile {
    session_id: Option<String>,
    run_id: Option<String>,
    native_session_id: Option<String>,
    connection_id: Option<String>,
    lease_generation: Option<String>,
    socket_path: Option<String>,
    channel_token: Option<String>,
    launcher_pid: Option<u32>,
    launcher_process_start_time: Option<String>,
    provider_pid: Option<u32>,
    provider_process_start_time: Option<String>,
    #[serde(default)]
    ready: bool,
    status: Option<String>,
}

#[derive(Debug, Clone)]
struct OmpHelmState {
    socket_path: PathBuf,
    channel_token: String,
    run_id: String,
    native_session_id: String,
    connection_id: String,
    lease_generation: String,
}

#[derive(Debug, Clone)]
pub struct OmpHelmCommandSummary {
    pub native_session_id: Option<String>,
    pub status: Option<String>,
}

fn load_state(
    session_id: &str,
    state_root: Option<&Path>,
) -> std::result::Result<OmpHelmState, OmpHelmControlError> {
    let path = state_file_path(session_id, state_root).map_err(OmpHelmControlError::failed)?;
    let bytes = fs::read(&path).map_err(|_| {
        OmpHelmControlError::not_attached(format!(
            "OMP Helm state file not found at {}",
            path.display()
        ))
    })?;
    let state: OmpHelmStateFile = serde_json::from_slice(&bytes)
        .map_err(|error| OmpHelmControlError::failed(error.to_string()))?;
    if state.session_id.as_deref() != Some(session_id)
        || !state.ready
        || state.status.as_deref() != Some("ready")
    {
        return Err(OmpHelmControlError::not_attached(
            "OMP Helm extension channel is not ready",
        ));
    }
    let launcher_pid = state.launcher_pid.ok_or_else(|| {
        OmpHelmControlError::not_attached("OMP Helm state is missing launcher identity")
    })?;
    let launcher_start = state
        .launcher_process_start_time
        .as_deref()
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            OmpHelmControlError::not_attached("OMP Helm state is missing launcher birth identity")
        })?;
    let provider_pid = state.provider_pid.ok_or_else(|| {
        OmpHelmControlError::not_attached("OMP Helm state is missing provider identity")
    })?;
    let provider_start = state
        .provider_process_start_time
        .as_deref()
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            OmpHelmControlError::not_attached("OMP Helm state is missing provider birth identity")
        })?;
    let facts = crate::process_identity::try_collect_process_facts_by_pid().ok_or_else(|| {
        OmpHelmControlError::not_attached("OMP Helm process identity could not be verified")
    })?;
    let launcher_fact = facts.get(&launcher_pid).ok_or_else(|| {
        OmpHelmControlError::not_attached("OMP Helm launcher process is not running")
    })?;
    if launcher_fact.lstart != launcher_start
        || !(launcher_fact.command.contains("omp_helm")
            || (launcher_fact.command.contains("longhouse")
                && launcher_fact.command.contains("omp")))
    {
        return Err(OmpHelmControlError::not_attached(
            "OMP Helm launcher identity changed",
        ));
    }
    let provider_fact = facts.get(&provider_pid).ok_or_else(|| {
        OmpHelmControlError::not_attached("OMP Helm provider process is not running")
    })?;
    if provider_fact.lstart != provider_start {
        return Err(OmpHelmControlError::not_attached(
            "OMP Helm provider identity changed",
        ));
    }
    let socket_path = PathBuf::from(
        state
            .socket_path
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                OmpHelmControlError::not_attached("OMP Helm state is missing its control socket")
            })?,
    );
    if !socket_path.exists() {
        return Err(OmpHelmControlError::not_attached(
            "OMP Helm control socket is missing",
        ));
    }
    Ok(OmpHelmState {
        socket_path,
        channel_token: state
            .channel_token
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                OmpHelmControlError::failed("OMP Helm state is missing channel authority")
            })?,
        run_id: state
            .run_id
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| OmpHelmControlError::failed("OMP Helm state is missing run identity"))?,
        native_session_id: state
            .native_session_id
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                OmpHelmControlError::not_attached(
                    "OMP Helm state is missing native session identity",
                )
            })?,
        connection_id: state
            .connection_id
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                OmpHelmControlError::failed("OMP Helm state is missing connection identity")
            })?,
        lease_generation: state
            .lease_generation
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                OmpHelmControlError::failed("OMP Helm state is missing lease generation")
            })?,
    })
}

async fn terminate_owned(
    session_id: &str,
    state_root: Option<&Path>,
    expected_grant: Option<&Value>,
) -> std::result::Result<OmpHelmCommandSummary, OmpHelmControlError> {
    let path = state_file_path(session_id, state_root).map_err(OmpHelmControlError::failed)?;
    let bytes = fs::read(&path).map_err(|_| {
        OmpHelmControlError::not_attached(format!(
            "OMP Helm state file not found at {}",
            path.display()
        ))
    })?;
    let state: OmpHelmStateFile = serde_json::from_slice(&bytes)
        .map_err(|error| OmpHelmControlError::failed(error.to_string()))?;
    if state.session_id.as_deref() != Some(session_id) {
        return Err(OmpHelmControlError::not_attached(
            "OMP Helm state belongs to a different session",
        ));
    }
    if let Some(grant) = expected_grant {
        for (field, actual) in [
            ("run_id", state.run_id.as_deref()),
            ("connection_id", state.connection_id.as_deref()),
            ("lease_generation", state.lease_generation.as_deref()),
        ] {
            if grant.get(field).and_then(Value::as_str) != actual {
                return Err(OmpHelmControlError::stale_channel("OMP"));
            }
        }
    }
    let provider_pid = state.provider_pid.ok_or_else(|| {
        OmpHelmControlError::not_attached("OMP Helm state is missing provider identity")
    })?;
    let provider_start = state
        .provider_process_start_time
        .as_deref()
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            OmpHelmControlError::not_attached("OMP Helm state is missing provider birth identity")
        })?;
    let facts = crate::process_identity::try_collect_process_facts_by_pid().ok_or_else(|| {
        OmpHelmControlError::not_attached("OMP Helm process identity could not be verified")
    })?;
    let provider_fact = facts.get(&provider_pid).ok_or_else(|| {
        OmpHelmControlError::not_attached("OMP Helm provider process is not running")
    })?;
    if provider_fact.lstart != provider_start {
        return Err(OmpHelmControlError::not_attached(
            "OMP Helm provider identity changed",
        ));
    }
    let pgid = crate::process_group::leader_group_for(provider_pid).ok_or_else(|| {
        OmpHelmControlError::not_attached("OMP Helm provider process group is not owned")
    })?;
    let outcome =
        crate::process_group::shutdown_group(pgid, crate::process_group::DEFAULT_GRACE).await;
    if !outcome.is_gone() {
        return Err(OmpHelmControlError::failed(format!(
            "OMP Helm provider process group survived {} cleanup",
            outcome.as_str()
        )));
    }
    Ok(OmpHelmCommandSummary {
        native_session_id: state.native_session_id,
        status: Some(outcome.as_str().into()),
    })
}

pub async fn dispatch(
    session_id: &str,
    kind: CommandKind,
    text: Option<&str>,
    state_root: Option<&Path>,
    expected_grant: Option<&Value>,
    request_id: Option<&str>,
) -> std::result::Result<OmpHelmCommandSummary, OmpHelmControlError> {
    dispatch_with_attachments(
        session_id,
        kind,
        text,
        state_root,
        expected_grant,
        request_id,
        &[],
    )
    .await
}

/// `dispatch` plus staged image files. Only their path and MIME type cross
/// the socket; the extension reads and base64-encodes them inside the
/// provider process, so the 512 KiB frame cap is never met by image bytes.
pub async fn dispatch_with_attachments(
    session_id: &str,
    kind: CommandKind,
    text: Option<&str>,
    state_root: Option<&Path>,
    expected_grant: Option<&Value>,
    request_id: Option<&str>,
    attachments: &[crate::input_attachments::StagedAttachment],
) -> std::result::Result<OmpHelmCommandSummary, OmpHelmControlError> {
    let state = match load_state(session_id, state_root) {
        Ok(state) => state,
        Err(error) if matches!(kind, CommandKind::Terminate) => {
            return terminate_owned(session_id, state_root, expected_grant).await;
        }
        Err(error) => return Err(error),
    };
    crate::helm_control::check_grant(
        "OMP",
        expected_grant,
        &state.run_id,
        &state.connection_id,
        &state.lease_generation,
    )?;
    let mut request = json!({
        "kind": kind.as_str(),
        "auth_token": state.channel_token,
        "session_id": session_id,
        "native_session_id": state.native_session_id,
        "connection_id": state.connection_id,
        "lease_generation": state.lease_generation,
    });
    if let Some(request_id) = request_id.filter(|value| !value.trim().is_empty()) {
        request["request_id"] = json!(request_id);
    }
    if let Some(text) = text {
        request["text"] = json!(text);
    }
    if !attachments.is_empty() {
        request["attachments"] = Value::Array(attachments.iter().map(|a| a.to_json()).collect());
    }
    let value = crate::helm_control::round_trip("OMP Helm", &state.socket_path, &request).await?;
    Ok(OmpHelmCommandSummary {
        native_session_id: value
            .get("native_session_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
        status: value
            .get("status")
            .and_then(Value::as_str)
            .map(str::to_owned),
    })
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::process::CommandExt;
    use std::process::Command;
    use std::thread;
    use std::time::Duration;

    #[tokio::test]
    async fn terminate_kills_owned_provider_group_without_extension_channel() {
        // Spawns by name and reads the process table: hold the shared agent-state
        // lock so a concurrent test cannot empty PATH under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let root = tempfile::tempdir().unwrap();
        let session_id = "omp-terminate-without-channel";
        let mut child = Command::new("sleep")
            .arg("60")
            .process_group(0)
            .spawn()
            .unwrap();
        let provider_pid = child.id();
        let provider_start = loop {
            if let Some(fact) = crate::process_identity::try_collect_process_facts_by_pid()
                .and_then(|facts| facts.get(&provider_pid).cloned())
            {
                break fact.lstart;
            }
            thread::sleep(Duration::from_millis(10));
        };
        let run_id = "00000000-0000-0000-0000-000000000001";
        let connection_id = "connection";
        let lease_generation = "generation";
        let state = json!({
            "session_id": session_id,
            "run_id": run_id,
            "native_session_id": "native",
            "connection_id": connection_id,
            "lease_generation": lease_generation,
            "provider_pid": provider_pid,
            "provider_process_start_time": provider_start,
            "ready": false,
            "status": "degraded",
        });
        fs::write(
            root.path().join(format!("{session_id}.json")),
            serde_json::to_vec(&state).unwrap(),
        )
        .unwrap();

        let reaper = thread::spawn(move || child.wait().unwrap());
        let result = dispatch(
            session_id,
            CommandKind::Terminate,
            None,
            Some(root.path()),
            Some(&json!({
                "run_id": run_id,
                "connection_id": connection_id,
                "lease_generation": lease_generation,
            })),
            None,
        )
        .await;
        let summary = result.unwrap();
        reaper.join().unwrap();
        assert!(matches!(
            summary.status.as_deref(),
            Some("terminated" | "killed" | "absent")
        ));
    }
}
