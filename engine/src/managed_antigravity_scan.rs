//! Managed Antigravity hook observation scanner.
//!
//! Antigravity has no bridge process. Its durable session state records hook,
//! claim, and response receipts; this scanner preserves those raw facts so the
//! heartbeat can explain readiness without inventing a generic control lease.

use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AntigravityHookObservation {
    pub state_file: PathBuf,
    pub schema_version: u32,
    pub session_id: String,
    /// Control identity, seeded by the Helm launcher and preserved by the hook.
    ///
    /// Absent for Shadow sessions, which is the point: a session Longhouse did
    /// not launch has no owned control path, so it must not be able to produce
    /// a control fact that would authorize commands against it.
    pub connection_id: Option<String>,
    pub lease_generation: Option<String>,
    pub run_id: Option<String>,
    pub provider_session_id: Option<String>,
    pub cwd: Option<String>,
    pub transcript_path: Option<String>,
    pub state: Option<String>,
    pub updated_at: String,
    pub last_hook_event: Option<String>,
    pub last_hook_observed_at: Option<String>,
    pub last_claimed_message_id: Option<String>,
    pub last_claimed_at: Option<String>,
    pub last_claim_event: Option<String>,
    pub last_response_event: Option<String>,
    pub last_response_at: Option<String>,
    pub last_response_status: Option<String>,
    pub last_response_claimed_message_ids: Vec<String>,
    pub last_continuation_requested: bool,
}

#[derive(Debug, Deserialize)]
struct AntigravityStateFile {
    #[serde(default)]
    schema_version: u32,
    session_id: Option<String>,
    connection_id: Option<String>,
    lease_generation: Option<String>,
    run_id: Option<String>,
    provider_session_id: Option<String>,
    cwd: Option<String>,
    transcript_path: Option<String>,
    state: Option<String>,
    updated_at: Option<String>,
    last_hook_event: Option<String>,
    last_hook_observed_at: Option<String>,
    last_claimed_message_id: Option<String>,
    last_claimed_at: Option<String>,
    last_claim_event: Option<String>,
    last_response_event: Option<String>,
    last_response_at: Option<String>,
    last_response_status: Option<String>,
    #[serde(default)]
    last_response_claimed_message_ids: Vec<String>,
    #[serde(default)]
    last_continuation_requested: bool,
}

pub fn default_antigravity_state_dir() -> Option<PathBuf> {
    crate::config::get_longhouse_home().ok().map(|home| {
        home.join("managed-local")
            .join("antigravity")
            .join("sessions")
    })
}

pub fn collect_observations_from(state_dir: &Path) -> Vec<AntigravityHookObservation> {
    collect_observations_from_paths(&crate::managed_scan::state_file_paths(state_dir))
}

pub(crate) fn collect_observations_from_paths(
    paths: &[PathBuf],
) -> Vec<AntigravityHookObservation> {
    let mut observations = Vec::new();
    for path in paths {
        if !private_regular_file(path) {
            continue;
        }
        let Ok(bytes) = fs::read(path) else {
            continue;
        };
        let Ok(state) = serde_json::from_slice::<AntigravityStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.unwrap_or_default().trim().to_string();
        if session_id.is_empty() {
            continue;
        }
        observations.push(AntigravityHookObservation {
            state_file: path.clone(),
            schema_version: state.schema_version,
            session_id,
            connection_id: normalize(state.connection_id),
            lease_generation: normalize(state.lease_generation),
            run_id: normalize(state.run_id),
            provider_session_id: normalize(state.provider_session_id),
            cwd: normalize(state.cwd),
            transcript_path: normalize(state.transcript_path),
            state: normalize(state.state),
            updated_at: state.updated_at.unwrap_or_default(),
            last_hook_event: normalize(state.last_hook_event),
            last_hook_observed_at: normalize(state.last_hook_observed_at),
            last_claimed_message_id: normalize(state.last_claimed_message_id),
            last_claimed_at: normalize(state.last_claimed_at),
            last_claim_event: normalize(state.last_claim_event),
            last_response_event: normalize(state.last_response_event),
            last_response_at: normalize(state.last_response_at),
            last_response_status: normalize(state.last_response_status),
            last_response_claimed_message_ids: state
                .last_response_claimed_message_ids
                .into_iter()
                .filter_map(|value| normalize(Some(value)))
                .collect(),
            last_continuation_requested: state.last_continuation_requested,
        });
    }
    observations.sort_by(|left, right| left.session_id.cmp(&right.session_id));
    observations
}

fn normalize(value: Option<String>) -> Option<String> {
    value.and_then(|value| {
        let trimmed = value.trim();
        (!trimmed.is_empty()).then(|| trimmed.to_string())
    })
}

fn private_regular_file(path: &Path) -> bool {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    if !metadata.file_type().is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.mode() & 0o077 != 0 {
            return false;
        }
        if metadata.uid() != unsafe { libc::geteuid() } {
            return false;
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scanner_reads_the_control_identity_a_helm_launch_actually_writes() {
        // Byte shape taken from a real `longhouse antigravity` launch followed
        // by one hook invocation: the launcher seeds identity, the hook merges
        // liveness over it, and both must survive into the observation. If the
        // hook ever clobbered the identity instead of merging, control would
        // silently stop being authorizable and only a live session would show
        // it.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("f57b62ad-2aee-4e12-9dd2-af7aac643799.json");
        fs::write(
            &path,
            r#"{
                "schema_version":3,
                "session_id":"f57b62ad-2aee-4e12-9dd2-af7aac643799",
                "connection_id":"101cc5f7-2c32-4dc2-ad80-1c67a24b3019",
                "lease_generation":"b88dfaf4-07c0-4d0d-b88a-97736a04a172",
                "run_id":"8c5f2301-13f1-5783-a3fc-24ea4d18d43f",
                "provider":"antigravity",
                "control_plane":"antigravity_hook_inbox",
                "provider_session_id":"conv-live-1",
                "state":"thinking",
                "updated_at":"2026-08-21T01:06:20.501701Z",
                "last_hook_event":"PreInvocation",
                "last_hook_observed_at":"2026-08-21T01:06:20.501701Z"
            }"#,
        )
        .unwrap();
        fs::set_permissions(&path, std::os::unix::fs::PermissionsExt::from_mode(0o600)).unwrap();

        let observations = collect_observations_from_paths(&[path]);
        assert_eq!(observations.len(), 1);
        let observation = &observations[0];
        assert_eq!(
            observation.connection_id.as_deref(),
            Some("101cc5f7-2c32-4dc2-ad80-1c67a24b3019")
        );
        assert_eq!(
            observation.lease_generation.as_deref(),
            Some("b88dfaf4-07c0-4d0d-b88a-97736a04a172")
        );
        assert_eq!(
            observation.run_id.as_deref(),
            Some("8c5f2301-13f1-5783-a3fc-24ea4d18d43f")
        );
        assert_eq!(observation.provider_session_id.as_deref(), Some("conv-live-1"));
        assert_eq!(observation.state.as_deref(), Some("thinking"));
    }

    #[test]
    fn scanner_preserves_hook_claim_and_response_receipts() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session-1.json");
        fs::write(
            &path,
            r#"{
                "schema_version":2,
                "session_id":"session-1",
                "provider_session_id":"conversation-1",
                "updated_at":"2026-05-08T12:00:00Z",
                "last_hook_event":"PostInvocation",
                "last_hook_observed_at":"2026-05-08T12:00:00Z",
                "last_claimed_message_id":"message-1",
                "last_claimed_at":"2026-05-08T12:00:01Z",
                "last_claim_event":"PostInvocation",
                "last_response_event":"PostInvocation",
                "last_response_at":"2026-05-08T12:00:02Z",
                "last_response_status":"ok",
                "last_response_claimed_message_ids":["message-1"],
                "last_continuation_requested":true
            }"#,
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        }

        let rows = collect_observations_from(dir.path());
        assert_eq!(rows.len(), 1);
        assert_eq!(
            rows[0].last_claimed_message_id.as_deref(),
            Some("message-1")
        );
        assert_eq!(rows[0].last_response_status.as_deref(), Some("ok"));
        assert!(rows[0].last_continuation_requested);
    }

    #[test]
    fn scanner_rejects_world_readable_state() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("unsafe.json");
        fs::write(&path, r#"{"schema_version":2,"session_id":"unsafe"}"#).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
            assert!(collect_observations_from(dir.path()).is_empty());
        }
    }
}
