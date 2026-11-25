//! Managed Antigravity Helm and hook observation scanner.
//!
//! `longhouse antigravity` runs `agy` under a launcher-owned PTY, records
//! `provider_pid` and `provider_process_start_time` in the session state file,
//! and merges hook, claim, and response receipts into that same file.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::process_identity::{lstart_matches_recorded, ProcessFact};

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
    pub provider_pid: Option<u32>,
    pub provider_process_group_id: Option<i32>,
    pub provider_process_start_time: Option<String>,
    pub provider_alive: bool,
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
    #[serde(default)]
    provider_pid: Option<u32>,
    #[serde(default)]
    provider_process_group_id: Option<i32>,
    #[serde(default)]
    provider_process_start_time: Option<String>,
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
    let mut raw = collect_observations_from_processes(state_dir, &HashMap::new());
    hydrate_provider_liveness(&mut raw);
    raw
}

pub(crate) fn collect_observations_from_processes(
    state_dir: &Path,
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<AntigravityHookObservation> {
    fn parse_state(
        path: &Path,
        bytes: std::io::Result<Vec<u8>>,
    ) -> Option<(PathBuf, AntigravityStateFile)> {
        let state = serde_json::from_slice::<AntigravityStateFile>(&bytes.ok()?).ok()?;
        Some((path.to_path_buf(), state))
    }
    let Ok(cached) = crate::dir_cache::parsed_json_dir(state_dir, parse_state, |left, right| {
        left.0.cmp(&right.0)
    }) else {
        return Vec::new();
    };
    collect_observations_from_entries(
        cached.iter().map(|entry| (entry.0.as_path(), &entry.1)),
        process_facts,
    )
}

pub(crate) fn collect_observations_from_paths(
    paths: &[PathBuf],
) -> Vec<AntigravityHookObservation> {
    let mut raw = collect_observations_from_paths_with_processes(paths, &HashMap::new());
    hydrate_provider_liveness(&mut raw);
    raw
}

fn hydrate_provider_liveness(raw: &mut [AntigravityHookObservation]) {
    let pids: Vec<u32> = raw
        .iter()
        .filter_map(|obs| obs.provider_pid.filter(|pid| *pid > 1))
        .collect();
    if pids.is_empty() {
        return;
    }
    let Some(facts) = crate::process_identity::try_collect_process_facts_for_pids(&pids) else {
        return;
    };
    for obs in raw {
        obs.provider_alive = provider_pid_is_alive(
            obs.provider_pid,
            obs.provider_process_start_time.as_deref(),
            &facts,
        );
    }
}

fn provider_pid_is_alive(
    provider_pid: Option<u32>,
    recorded_start_time: Option<&str>,
    process_facts: &HashMap<u32, ProcessFact>,
) -> bool {
    provider_pid
        .filter(|pid| *pid > 1)
        .and_then(|pid| process_facts.get(&pid))
        .is_some_and(|fact| {
            recorded_start_time
                .map(str::trim)
                .filter(|recorded| !recorded.is_empty())
                .is_some_and(|recorded| lstart_matches_recorded(fact, recorded))
        })
}

pub(crate) fn collect_observations_from_paths_with_processes(
    paths: &[PathBuf],
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<AntigravityHookObservation> {
    let parsed: Vec<(PathBuf, AntigravityStateFile)> = paths
        .iter()
        .filter(|path| private_regular_file(path))
        .filter_map(|path| {
            let bytes = fs::read(path).ok()?;
            let state = serde_json::from_slice::<AntigravityStateFile>(&bytes).ok()?;
            Some((path.clone(), state))
        })
        .collect();
    collect_observations_from_entries(
        parsed.iter().map(|(path, state)| (path.as_path(), state)),
        process_facts,
    )
}

fn collect_observations_from_entries<'a>(
    entries: impl IntoIterator<Item = (&'a Path, &'a AntigravityStateFile)>,
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<AntigravityHookObservation> {
    let mut observations = Vec::new();
    for (path, state) in entries {
        if !private_regular_file(path) {
            continue;
        }
        let session_id = state
            .session_id
            .as_deref()
            .unwrap_or_default()
            .trim()
            .to_string();
        if session_id.is_empty() {
            continue;
        }
        let provider_process_start_time = normalize(state.provider_process_start_time.clone());
        let provider_alive = provider_pid_is_alive(
            state.provider_pid,
            provider_process_start_time.as_deref(),
            process_facts,
        );
        observations.push(AntigravityHookObservation {
            state_file: path.to_path_buf(),
            schema_version: state.schema_version,
            session_id,
            connection_id: normalize(state.connection_id.clone()),
            lease_generation: normalize(state.lease_generation.clone()),
            run_id: normalize(state.run_id.clone()),
            provider_pid: state.provider_pid,
            provider_process_group_id: state.provider_process_group_id,
            provider_process_start_time,
            provider_alive,
            provider_session_id: normalize(state.provider_session_id.clone()),
            cwd: normalize(state.cwd.clone()),
            transcript_path: normalize(state.transcript_path.clone()),
            state: normalize(state.state.clone()),
            updated_at: state.updated_at.clone().unwrap_or_default(),
            last_hook_event: normalize(state.last_hook_event.clone()),
            last_hook_observed_at: normalize(state.last_hook_observed_at.clone()),
            last_claimed_message_id: normalize(state.last_claimed_message_id.clone()),
            last_claimed_at: normalize(state.last_claimed_at.clone()),
            last_claim_event: normalize(state.last_claim_event.clone()),
            last_response_event: normalize(state.last_response_event.clone()),
            last_response_at: normalize(state.last_response_at.clone()),
            last_response_status: normalize(state.last_response_status.clone()),
            last_response_claimed_message_ids: state
                .last_response_claimed_message_ids
                .iter()
                .filter_map(|value| normalize(Some(value.clone())))
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
        assert_eq!(
            observation.provider_session_id.as_deref(),
            Some("conv-live-1")
        );
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
    fn scanner_verifies_recorded_provider_process_identity() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("pty-owned.json");
        fs::write(
            &path,
            r#"{
                "schema_version":3,
                "session_id":"pty-owned",
                "connection_id":"conn-1",
                "lease_generation":"gen-1",
                "run_id":"run-1",
                "provider_pid":4242,
                "provider_process_group_id":4242,
                "provider_process_start_time":"Mon Nov 16 14:56:00 2026",
                "state":"idle",
                "updated_at":"2026-11-16T22:49:18Z"
            }"#,
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        }

        let live_fact = ProcessFact {
            pid: 4242,
            tty: "ttys001".to_string(),
            stat: "S+".to_string(),
            lstart: "Mon Nov 16 14:56:00 2026".to_string(),
            command: "agy --dangerously-skip-permissions".to_string(),
            start_time: None,
        };
        let live = collect_observations_from_paths_with_processes(
            std::slice::from_ref(&path),
            &HashMap::from([(4242, live_fact)]),
        );
        assert_eq!(live.len(), 1);
        assert_eq!(live[0].provider_pid, Some(4242));
        assert!(live[0].provider_alive);

        let recycled_fact = ProcessFact {
            pid: 4242,
            tty: "ttys001".to_string(),
            stat: "S+".to_string(),
            lstart: "Mon Nov 16 18:00:00 2026".to_string(),
            command: "other-binary".to_string(),
            start_time: None,
        };
        let recycled = collect_observations_from_paths_with_processes(
            std::slice::from_ref(&path),
            &HashMap::from([(4242, recycled_fact)]),
        );
        assert!(!recycled[0].provider_alive);
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
