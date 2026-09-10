//! Managed-local OMP Helm scanner.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::process_identity::{lstart_matches_recorded, ProcessFact};

pub use crate::omp_helm_control::default_omp_helm_state_dir;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OmpHelmObservation {
    pub session_id: String,
    pub native_session_id: Option<String>,
    pub run_id: Option<String>,
    pub connection_id: Option<String>,
    pub lease_generation: Option<String>,
    pub state_file: PathBuf,
    pub session_file: Option<PathBuf>,
    pub socket_path: Option<PathBuf>,
    pub cwd: Option<String>,
    pub launcher_pid: Option<u32>,
    pub launcher_process_start_time: Option<String>,
    pub provider_pid: Option<u32>,
    pub provider_process_start_time: Option<String>,
    pub started_at: String,
    pub updated_at: String,
    pub phase: Option<String>,
    pub tool_name: Option<String>,
    pub status: String,
    pub launcher_alive: bool,
    pub provider_alive: bool,
    pub live: bool,
}

#[derive(Debug, Deserialize)]
struct OmpHelmStateFile {
    session_id: Option<String>,
    native_session_id: Option<String>,
    session_file: Option<String>,
    run_id: Option<String>,
    connection_id: Option<String>,
    lease_generation: Option<String>,
    socket_path: Option<String>,
    cwd: Option<String>,
    launcher_pid: Option<u32>,
    launcher_process_start_time: Option<String>,
    provider_pid: Option<u32>,
    provider_process_start_time: Option<String>,
    started_at: Option<String>,
    updated_at: Option<String>,
    phase: Option<String>,
    tool_name: Option<String>,
    status: Option<String>,
    #[serde(default)]
    ready: bool,
}

pub(crate) fn collect_observations_from_processes(
    state_dir: &Path,
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<OmpHelmObservation> {
    let paths = crate::managed_scan::state_file_paths(state_dir);
    collect_observations_from_paths(&paths, process_facts)
}

pub(crate) fn collect_observations_from_paths(
    paths: &[PathBuf],
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<OmpHelmObservation> {
    let mut observations = Vec::new();
    for path in paths {
        let Ok(bytes) = fs::read(path) else { continue };
        let Ok(state) = serde_json::from_slice::<OmpHelmStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.unwrap_or_default().trim().to_string();
        if session_id.is_empty() {
            continue;
        }
        let launcher_alive = state.launcher_pid.is_some_and(|pid| {
            process_facts.get(&pid).is_some_and(|fact| {
                (fact.command.contains("omp_helm")
                    || fact.command.contains("omp-helm")
                    || (fact.command.contains("longhouse") && fact.command.contains("omp")))
                    && state
                        .launcher_process_start_time
                        .as_deref()
                        .is_some_and(|recorded| {
                            !recorded.is_empty() && lstart_matches_recorded(fact, recorded)
                        })
            })
        });
        let provider_alive = state.provider_pid.is_some_and(|pid| {
            process_facts.get(&pid).is_some_and(|fact| {
                state
                    .provider_process_start_time
                    .as_deref()
                    .is_some_and(|recorded| {
                        !recorded.is_empty() && lstart_matches_recorded(fact, recorded)
                    })
            })
        });
        let socket_path = state
            .socket_path
            .filter(|value| !value.trim().is_empty())
            .map(PathBuf::from);
        let socket_present = socket_path.as_ref().is_some_and(|path| path.exists());
        let status = state.status.unwrap_or_else(|| "unknown".into());
        let live =
            status == "ready" && state.ready && launcher_alive && provider_alive && socket_present;
        observations.push(OmpHelmObservation {
            session_id,
            native_session_id: state
                .native_session_id
                .filter(|value| !value.trim().is_empty()),
            run_id: state.run_id.filter(|value| !value.trim().is_empty()),
            connection_id: state.connection_id.filter(|value| !value.trim().is_empty()),
            lease_generation: state
                .lease_generation
                .filter(|value| !value.trim().is_empty()),
            state_file: path.clone(),
            session_file: state
                .session_file
                .filter(|value| !value.trim().is_empty())
                .map(PathBuf::from),
            socket_path,
            cwd: state.cwd.filter(|value| !value.trim().is_empty()),
            launcher_pid: state.launcher_pid,
            launcher_process_start_time: state.launcher_process_start_time,
            provider_pid: state.provider_pid,
            provider_process_start_time: state.provider_process_start_time,
            started_at: state.started_at.unwrap_or_default(),
            updated_at: state.updated_at.unwrap_or_default(),
            phase: state.phase,
            tool_name: state.tool_name,
            status,
            launcher_alive,
            provider_alive,
            live,
        });
    }
    observations.sort_by(|left, right| left.session_id.cmp(&right.session_id));
    observations
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dead_owner_is_retained_as_resume_evidence_but_not_live_control() {
        let dir = tempfile::tempdir().unwrap();
        let state = serde_json::json!({
            "session_id": "session",
            "native_session_id": "native",
            "run_id": "run",
            "connection_id": "connection",
            "lease_generation": "generation",
            "socket_path": dir.path().join("missing.sock"),
            "launcher_pid": 1111,
            "launcher_process_start_time": "old",
            "provider_pid": 2222,
            "provider_process_start_time": "old",
            "status": "stopped",
            "ready": false,
            "started_at": "2026-09-09T00:00:00Z",
            "updated_at": "2026-09-09T00:00:01Z"
        });
        let path = dir.path().join("session.json");
        fs::write(&path, serde_json::to_vec(&state).unwrap()).unwrap();
        let observations = collect_observations_from_paths(&[path], &HashMap::new());
        assert_eq!(observations.len(), 1);
        assert!(!observations[0].live);
        assert_eq!(observations[0].run_id.as_deref(), Some("run"));
        assert_eq!(observations[0].native_session_id.as_deref(), Some("native"));
    }
    #[cfg(unix)]
    #[test]
    fn hyphenated_omp_helm_launcher_is_live() {
        use crate::process_identity::ProcessFact;
        use std::os::unix::net::UnixListener;

        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("channel.sock");
        let _listener = UnixListener::bind(&socket).unwrap();
        let state = serde_json::json!({
            "session_id": "session",
            "native_session_id": "native",
            "run_id": "run",
            "connection_id": "connection",
            "lease_generation": "generation",
            "socket_path": socket,
            "launcher_pid": 1111,
            "launcher_process_start_time": "birth",
            "provider_pid": 2222,
            "provider_process_start_time": "birth",
            "status": "ready",
            "ready": true,
            "started_at": "2026-09-09T00:00:00Z",
            "updated_at": "2026-09-09T00:00:01Z"
        });
        let path = dir.path().join("session.json");
        fs::write(&path, serde_json::to_vec(&state).unwrap()).unwrap();
        let facts = HashMap::from([
            (
                1111,
                ProcessFact {
                    pid: 1111,
                    tty: "??".into(),
                    stat: "S".into(),
                    lstart: "birth".into(),
                    command: "longhouse-engine omp-helm launch --cwd /tmp".into(),
                    start_time: None,
                },
            ),
            (
                2222,
                ProcessFact {
                    pid: 2222,
                    tty: "??".into(),
                    stat: "S".into(),
                    lstart: "birth".into(),
                    command: "omp".into(),
                    start_time: None,
                },
            ),
        ]);
        let observations = collect_observations_from_paths(&[path], &facts);
        assert_eq!(observations.len(), 1);
        assert!(observations[0].live);
    }
}
