//! Strict, probe-produced Cursor Helm launch binding claims.
//!
//! Cursor does not document a launch-to-chat API.  A claim is therefore valid
//! only when the interactive probe observed the launch's exact session token as
//! `meta['0'].agentId` at every required lifecycle point.  Paths, timestamps,
//! and newest-store selection are intentionally not considered evidence.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::Result;
use chrono::{DateTime, Utc};
use serde::Deserialize;

const REQUIRED_PHASES: [&str; 4] = [
    "before_launch",
    "after_prompt",
    "after_tool_turn",
    "at_exit",
];

#[derive(Debug, Deserialize)]
struct LaunchBindingClaim {
    schema_version: u32,
    provider: String,
    status: String,
    session_id: String,
    #[serde(default)]
    thread_id: Option<String>,
    #[serde(default)]
    turn_id: Option<String>,
    #[serde(default)]
    run_id: Option<String>,
    #[serde(default)]
    client_request_id: Option<String>,
    conversation_uuid: String,
    #[serde(default)]
    previous_conversation_uuids: Vec<String>,
    #[serde(default)]
    agent_id: Option<String>,
    #[serde(default)]
    launch_token: Option<String>,
    #[serde(default)]
    expires_at: Option<DateTime<Utc>>,
    #[serde(default)]
    hook_observed_at: Option<DateTime<Utc>>,
    #[serde(default)]
    observations: Vec<ProbeObservation>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedCursorBinding {
    pub session_id: String,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
    pub run_id: Option<String>,
    pub client_request_id: Option<String>,
    pub previous_provider_session_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CursorLaunchBindingState {
    Managed(ManagedCursorBinding),
    Pending,
    Unclaimed,
}

#[derive(Debug, Deserialize)]
struct ProbeObservation {
    phase: String,
    agent_id: Option<String>,
    launcher_pid: Option<u64>,
    cursor_pid: Option<u64>,
}

pub fn launch_binding_state_for_conversation(
    conversation_uuid: &str,
) -> Result<CursorLaunchBindingState> {
    launch_binding_state_for_conversation_in(&claim_dir(), conversation_uuid)
}

fn launch_binding_state_for_conversation_in(
    dir: &Path,
    conversation_uuid: &str,
) -> Result<CursorLaunchBindingState> {
    let mut managed = Vec::new();
    let mut pending = false;
    for path in claim_paths(dir)? {
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(claim) = serde_json::from_slice::<LaunchBindingClaim>(&bytes) else {
            continue;
        };
        if valid_claim(&claim, conversation_uuid) {
            let previous_provider_session_id = active_predecessor(&claim, conversation_uuid);
            managed.push(ManagedCursorBinding {
                session_id: claim.session_id,
                thread_id: claim.thread_id,
                turn_id: claim.turn_id,
                run_id: claim.run_id,
                client_request_id: claim.client_request_id,
                previous_provider_session_id,
            });
        } else if claim.schema_version == 2
            && claim.provider == "cursor"
            && claim.status == "pending"
            && claim.conversation_uuid == conversation_uuid
            && claim.expires_at.is_some_and(|value| value > Utc::now())
        {
            pending = true;
        }
    }
    managed.sort_by(|left, right| left.session_id.cmp(&right.session_id));
    managed.dedup_by(|left, right| left.session_id == right.session_id);
    Ok(if managed.len() == 1 {
        CursorLaunchBindingState::Managed(managed.remove(0))
    } else if pending || !managed.is_empty() {
        // More than one observed binding is not enough evidence to select a
        // destination. Hold the source just like a pending claim rather than
        // materializing a duplicate unmanaged session.
        CursorLaunchBindingState::Pending
    } else {
        CursorLaunchBindingState::Unclaimed
    })
}

#[cfg(test)]
fn managed_session_id_for_conversation_in(
    dir: &Path,
    conversation_uuid: &str,
) -> Result<Option<String>> {
    Ok(managed_binding_for_conversation_in(dir, conversation_uuid)?
        .map(|binding| binding.session_id))
}

#[cfg(test)]
fn managed_binding_for_conversation_in(
    dir: &Path,
    conversation_uuid: &str,
) -> Result<Option<ManagedCursorBinding>> {
    let mut matches = Vec::new();
    for path in claim_paths(dir)? {
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(claim) = serde_json::from_slice::<LaunchBindingClaim>(&bytes) else {
            continue;
        };
        if valid_claim(&claim, conversation_uuid) {
            let previous_provider_session_id = active_predecessor(&claim, conversation_uuid);
            matches.push(ManagedCursorBinding {
                session_id: claim.session_id,
                thread_id: claim.thread_id,
                turn_id: claim.turn_id,
                run_id: claim.run_id,
                client_request_id: claim.client_request_id,
                previous_provider_session_id,
            });
        }
    }
    matches.sort_by(|left, right| left.session_id.cmp(&right.session_id));
    matches.dedup_by(|left, right| left.session_id == right.session_id);
    Ok((matches.len() == 1).then(|| matches.remove(0)))
}

/// A prelaunch reservation prevents the empty Cursor store from racing ahead
/// of the first provider hook and materializing as a duplicate Shadow session.
#[cfg(test)]
fn pending_claim_for_conversation_in(dir: &Path, conversation_uuid: &str) -> Result<bool> {
    for path in claim_paths(dir)? {
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(claim) = serde_json::from_slice::<LaunchBindingClaim>(&bytes) else {
            continue;
        };
        if claim.schema_version == 2
            && claim.provider == "cursor"
            && claim.status == "pending"
            && claim.conversation_uuid == conversation_uuid
            && claim.expires_at.is_some_and(|value| value > Utc::now())
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn valid_claim(claim: &LaunchBindingClaim, conversation_uuid: &str) -> bool {
    if claim.provider != "cursor"
        || (claim.conversation_uuid != conversation_uuid
            && !claim
                .previous_conversation_uuids
                .iter()
                .any(|value| value == conversation_uuid))
    {
        return false;
    }
    if claim.schema_version == 2 {
        return claim.status == "observed"
            && !claim.session_id.trim().is_empty()
            && claim.hook_observed_at.is_some();
    }
    claim.schema_version == 1
        && claim.provider == "cursor"
        && claim.status == "passed"
        && claim.expires_at.is_some_and(|value| value > Utc::now())
        && claim.conversation_uuid == conversation_uuid
        && claim.agent_id.as_deref() == Some(conversation_uuid)
        // This equality is the capability gate. It is direct provider-native
        // evidence, unlike matching a workspace, launch time, or fresh file.
        && claim.launch_token.as_deref() == Some(claim.session_id.as_str())
        && claim.agent_id.as_deref() == claim.launch_token.as_deref()
        && REQUIRED_PHASES.iter().all(|phase| {
            claim.observations.iter().any(|observation| {
                observation.phase == *phase
                    && (*phase == "before_launch" || observation.agent_id.as_deref() == Some(conversation_uuid))
            })
        })
        && claim.observations.iter().any(|observation| {
            observation.launcher_pid.unwrap_or(0) > 0 && observation.cursor_pid.unwrap_or(0) > 0
        })
}

fn active_predecessor(claim: &LaunchBindingClaim, conversation_uuid: &str) -> Option<String> {
    (claim.conversation_uuid == conversation_uuid)
        .then(|| claim.previous_conversation_uuids.last().cloned())
        .flatten()
}

/// A fresh Cursor store can become visible just before its top-level
/// `sessionStart` hook rotates the durable managed claim. Hold that store for
/// the shipper's short freshness window when exactly one live managed launch
/// could own the transition. The hook remains the identity authority.
pub fn reset_binding_may_be_pending(conversation_uuid: &str) -> Result<bool> {
    reset_binding_may_be_pending_in(&claim_dir(), conversation_uuid)
}

/// A native Helm launch creates Cursor's store before it can write the
/// conversation-specific binding claim.  Keep a fresh source from being
/// materialized as Shadow during that small window.
pub fn launch_reservation_may_be_pending() -> Result<bool> {
    let claims = claim_dir();
    let Some(state_root) = claims.parent() else {
        return Ok(false);
    };
    launch_reservation_may_be_pending_in(state_root)
}

fn launch_reservation_may_be_pending_in(state_root: &Path) -> Result<bool> {
    let reservations = state_root.join("launch-reservations");
    let mut active = 0usize;
    for path in claim_paths(&reservations)? {
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
            continue;
        };
        let expires_at = value
            .get("expires_at")
            .and_then(serde_json::Value::as_str)
            .and_then(|value| value.parse::<DateTime<Utc>>().ok());
        if value
            .get("schema_version")
            .and_then(serde_json::Value::as_u64)
            == Some(1)
            && value.get("provider").and_then(serde_json::Value::as_str) == Some("cursor")
            && value.get("status").and_then(serde_json::Value::as_str) == Some("pending")
            && value
                .get("session_id")
                .and_then(serde_json::Value::as_str)
                .is_some_and(|value| !value.trim().is_empty())
            && value
                .get("launch_id")
                .and_then(serde_json::Value::as_str)
                .is_some_and(|value| !value.trim().is_empty())
            && expires_at.is_some_and(|value| value > Utc::now())
        {
            active += 1;
        }
    }
    Ok(active > 0)
}

fn reset_binding_may_be_pending_in(dir: &Path, conversation_uuid: &str) -> Result<bool> {
    let Some(state_root) = dir.parent() else {
        return Ok(false);
    };
    let mut candidate_sessions = Vec::new();
    for path in claim_paths(dir)? {
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(claim) = serde_json::from_slice::<LaunchBindingClaim>(&bytes) else {
            continue;
        };
        if claim.schema_version != 2
            || claim.provider != "cursor"
            || claim.status != "observed"
            || claim.conversation_uuid == conversation_uuid
            || claim
                .previous_conversation_uuids
                .iter()
                .any(|value| value == conversation_uuid)
            || claim.hook_observed_at.is_none()
            || claim.session_id.trim().is_empty()
        {
            continue;
        }
        let state_path = state_root.join(format!("{}.json", claim.session_id));
        let Ok(state_bytes) = fs::read(state_path) else {
            continue;
        };
        let Ok(state) = serde_json::from_slice::<serde_json::Value>(&state_bytes) else {
            continue;
        };
        if state.get("session_id").and_then(serde_json::Value::as_str)
            == Some(claim.session_id.as_str())
            && state
                .get("provider_session_id")
                .and_then(serde_json::Value::as_str)
                == Some(claim.conversation_uuid.as_str())
            && state.get("ready").and_then(serde_json::Value::as_bool) == Some(true)
            && state
                .get("launcher_pid")
                .and_then(serde_json::Value::as_u64)
                .is_some()
            && state
                .get("cursor_pid")
                .and_then(serde_json::Value::as_u64)
                .is_some()
        {
            candidate_sessions.push(claim.session_id);
        }
    }
    candidate_sessions.sort();
    candidate_sessions.dedup();
    Ok(candidate_sessions.len() == 1)
}

fn claim_dir() -> PathBuf {
    if let Ok(path) = std::env::var("LONGHOUSE_CURSOR_HELM_BINDING_DIR") {
        return PathBuf::from(path);
    }
    let home = std::env::var("LONGHOUSE_HOME")
        .or_else(|_| std::env::var("HOME").map(|home| format!("{home}/.longhouse")))
        .unwrap_or_else(|_| "/tmp/.longhouse".to_string());
    PathBuf::from(home).join("managed-local/cursor-helm/binding-probes")
}

fn claim_paths(dir: &Path) -> Result<Vec<PathBuf>> {
    let Ok(entries) = fs::read_dir(dir) else {
        return Ok(Vec::new());
    };
    let mut paths = Vec::new();
    for entry in entries {
        let entry = entry?;
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) == Some("json") {
            paths.push(path);
        }
    }
    Ok(paths)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn claim(session_id: &str, agent_id: &str, expires_at: &str) -> String {
        format!(
            r#"{{"schema_version":1,"provider":"cursor","status":"passed","session_id":"{session_id}","conversation_uuid":"{agent_id}","agent_id":"{agent_id}","launch_token":"{session_id}","expires_at":"{expires_at}","observations":[{{"phase":"before_launch","agent_id":null,"launcher_pid":null,"cursor_pid":null}},{{"phase":"after_prompt","agent_id":"{agent_id}","launcher_pid":1,"cursor_pid":2}},{{"phase":"after_tool_turn","agent_id":"{agent_id}","launcher_pid":1,"cursor_pid":2}},{{"phase":"at_exit","agent_id":"{agent_id}","launcher_pid":null,"cursor_pid":null}}]}}"#
        )
    }

    #[test]
    fn accepts_only_direct_token_equals_agent_id_proof() {
        let future = "2099-01-01T00:00:00Z";
        let raw: LaunchBindingClaim =
            serde_json::from_str(&claim("launch-1", "launch-1", future)).unwrap();
        assert!(valid_claim(&raw, "launch-1"));
        let raw: LaunchBindingClaim =
            serde_json::from_str(&claim("launch-1", "cursor-generated-id", future)).unwrap();
        assert!(!valid_claim(&raw, "cursor-generated-id"));
    }

    #[test]
    fn accepts_hook_observed_managed_to_native_identity_mapping() {
        let raw: LaunchBindingClaim = serde_json::from_str(
            r#"{"schema_version":2,"provider":"cursor","status":"observed","session_id":"longhouse-id","thread_id":"thread-id","turn_id":"turn-id","run_id":"run-id","client_request_id":"request-id","conversation_uuid":"cursor-id","hook_observed_at":"2026-07-17T00:00:00Z"}"#,
        )
        .unwrap();
        assert!(valid_claim(&raw, "cursor-id"));
        assert!(!valid_claim(&raw, "different-cursor-id"));
        let dir = tempdir().unwrap();
        fs::write(
            dir.path().join("claim.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "longhouse-id",
                "thread_id": "thread-id",
                "turn_id": "turn-id",
                "run_id": "run-id",
                "client_request_id": "request-id",
                "conversation_uuid": "cursor-id",
                "hook_observed_at": "2026-07-17T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();
        let binding = managed_binding_for_conversation_in(dir.path(), "cursor-id")
            .unwrap()
            .unwrap();
        assert_eq!(binding.session_id, "longhouse-id");
        assert_eq!(binding.thread_id.as_deref(), Some("thread-id"));
        assert_eq!(binding.turn_id.as_deref(), Some("turn-id"));
        assert_eq!(binding.run_id.as_deref(), Some("run-id"));
        assert_eq!(binding.client_request_id.as_deref(), Some("request-id"));
    }

    #[test]
    fn rotated_claim_keeps_old_store_bound_and_exposes_active_predecessor() {
        let dir = tempdir().unwrap();
        fs::write(
            dir.path().join("claim.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "longhouse-id",
                "conversation_uuid": "cursor-new",
                "previous_conversation_uuids": ["cursor-old"],
                "hook_observed_at": "2026-07-17T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();

        let active = managed_binding_for_conversation_in(dir.path(), "cursor-new")
            .unwrap()
            .unwrap();
        assert_eq!(
            active.previous_provider_session_id.as_deref(),
            Some("cursor-old")
        );
        let historical = managed_binding_for_conversation_in(dir.path(), "cursor-old")
            .unwrap()
            .unwrap();
        assert_eq!(historical.session_id, "longhouse-id");
        assert_eq!(historical.previous_provider_session_id, None);
    }

    #[test]
    fn fresh_unknown_store_waits_for_one_live_managed_claim_to_rotate() {
        let root = tempdir().unwrap();
        let claims = root.path().join("binding-probes");
        fs::create_dir_all(&claims).unwrap();
        fs::write(
            claims.join("claim.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "longhouse-id",
                "conversation_uuid": "cursor-old",
                "hook_observed_at": "2026-07-17T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();
        fs::write(
            root.path().join("longhouse-id.json"),
            serde_json::to_vec(&serde_json::json!({
                "session_id": "longhouse-id",
                "provider_session_id": "cursor-old",
                "ready": true,
                "launcher_pid": 10,
                "cursor_pid": 11
            }))
            .unwrap(),
        )
        .unwrap();

        assert!(reset_binding_may_be_pending_in(&claims, "cursor-new").unwrap());
        assert!(!reset_binding_may_be_pending_in(&claims, "cursor-old").unwrap());
    }

    #[test]
    fn launch_binding_state_distinguishes_pending_observed_and_unclaimed() {
        let dir = tempdir().unwrap();
        assert_eq!(
            launch_binding_state_for_conversation_in(dir.path(), "cursor-id").unwrap(),
            CursorLaunchBindingState::Unclaimed
        );
        fs::write(
            dir.path().join("claim.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 2,
                "provider": "cursor",
                "status": "pending",
                "session_id": "longhouse-id",
                "conversation_uuid": "cursor-id",
                "expires_at": "2099-01-01T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            launch_binding_state_for_conversation_in(dir.path(), "cursor-id").unwrap(),
            CursorLaunchBindingState::Pending
        );

        fs::write(
            dir.path().join("claim.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": "longhouse-id",
                "conversation_uuid": "cursor-id",
                "hook_observed_at": "2026-07-17T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();
        assert!(matches!(
            launch_binding_state_for_conversation_in(dir.path(), "cursor-id").unwrap(),
            CursorLaunchBindingState::Managed(ManagedCursorBinding { session_id, .. })
                if session_id == "longhouse-id"
        ));
    }

    #[test]
    fn malformed_or_expired_claims_do_not_bind() {
        let dir = tempdir().unwrap();
        fs::write(
            dir.path().join("one.json"),
            claim("same", "same", "2099-01-01T00:00:00Z"),
        )
        .unwrap();
        assert_eq!(
            managed_session_id_for_conversation_in(dir.path(), "same")
                .unwrap()
                .as_deref(),
            Some("same")
        );
        fs::write(
            dir.path().join("two.json"),
            claim("other", "same", "2099-01-01T00:00:00Z"),
        )
        .unwrap();
        assert_eq!(
            managed_session_id_for_conversation_in(dir.path(), "same")
                .unwrap()
                .as_deref(),
            Some("same")
        );
        fs::write(
            dir.path().join("one.json"),
            claim("same", "same", "2000-01-01T00:00:00Z"),
        )
        .unwrap();
        assert!(managed_session_id_for_conversation_in(dir.path(), "same")
            .unwrap()
            .is_none());
    }

    #[test]
    fn pending_reservation_defers_only_its_unexpired_native_conversation() {
        let dir = tempdir().unwrap();
        fs::write(
            dir.path().join("pending.json"),
            r#"{"schema_version":2,"provider":"cursor","status":"pending","session_id":"longhouse-id","conversation_uuid":"cursor-id","expires_at":"2099-01-01T00:00:00Z"}"#,
        )
        .unwrap();
        assert!(pending_claim_for_conversation_in(dir.path(), "cursor-id").unwrap());
        assert!(!pending_claim_for_conversation_in(dir.path(), "different-cursor-id").unwrap());

        fs::write(
            dir.path().join("pending.json"),
            r#"{"schema_version":2,"provider":"cursor","status":"pending","session_id":"longhouse-id","conversation_uuid":"cursor-id","expires_at":"2000-01-01T00:00:00Z"}"#,
        )
        .unwrap();
        assert!(!pending_claim_for_conversation_in(dir.path(), "cursor-id").unwrap());
    }

    #[test]
    fn fresh_source_waits_while_native_launch_reservation_is_active() {
        let root = tempdir().unwrap();
        let reservations = root.path().join("launch-reservations");
        fs::create_dir_all(&reservations).unwrap();
        fs::write(
            reservations.join("launch.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 1,
                "provider": "cursor",
                "status": "pending",
                "session_id": "longhouse-id",
                "launch_id": "launch-id",
                "expires_at": "2099-01-01T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();
        assert!(launch_reservation_may_be_pending_in(root.path()).unwrap());

        fs::write(
            reservations.join("launch.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 1,
                "provider": "cursor",
                "status": "pending",
                "session_id": "longhouse-id",
                "launch_id": "launch-id",
                "expires_at": "2000-01-01T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();
        assert!(!launch_reservation_may_be_pending_in(root.path()).unwrap());
    }
}
