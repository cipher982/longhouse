//! Managed-local Pi Helm scanner.
//!
//! State files are private launch contracts, not authority by themselves. A
//! live observation requires exact launcher/provider pid start identities plus
//! the launch-scoped channel socket and ready marker.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::process_identity::{lstart_matches_recorded, ProcessFact};

pub use crate::pi_helm_control::default_pi_helm_state_dir;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PiHelmObservation {
    pub session_id: String,
    pub provider_session_id: Option<String>,
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
struct PiHelmStateFile {
    session_id: Option<String>,
    #[serde(default)]
    provider_session_id: Option<String>,
    session_file: Option<String>,
    #[serde(default)]
    run_id: Option<String>,
    #[serde(default)]
    connection_id: Option<String>,
    #[serde(default)]
    lease_generation: Option<String>,
    #[serde(default)]
    socket_path: Option<String>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default)]
    launcher_pid: Option<u32>,
    #[serde(default)]
    launcher_process_start_time: Option<String>,
    #[serde(default)]
    provider_pid: Option<u32>,
    #[serde(default)]
    provider_process_start_time: Option<String>,
    #[serde(default)]
    started_at: Option<String>,
    #[serde(default)]
    updated_at: Option<String>,
    #[serde(default)]
    phase: Option<String>,
    #[serde(default)]
    tool_name: Option<String>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    ready: bool,
}

pub(crate) fn collect_observations_from_processes(
    state_dir: &Path,
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<PiHelmObservation> {
    let paths = crate::managed_scan::state_file_paths(state_dir);
    collect_observations_from_paths(&paths, process_facts)
}

pub(crate) fn collect_observations_from_paths(
    paths: &[PathBuf],
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<PiHelmObservation> {
    let mut observations = Vec::new();
    for path in paths {
        let Ok(bytes) = fs::read(path) else { continue };
        let Ok(state) = serde_json::from_slice::<PiHelmStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.unwrap_or_default().trim().to_string();
        if session_id.is_empty() {
            continue;
        }

        let launcher_alive = state.launcher_pid.is_some_and(|pid| {
            process_facts.get(&pid).is_some_and(|fact| {
                (fact.command.contains("pi_helm")
                    || (fact.command.contains("longhouse") && fact.command.contains("pi-helm")))
                    && state
                        .launcher_process_start_time
                        .as_deref()
                        .is_some_and(|recorded| {
                            !recorded.trim().is_empty() && lstart_matches_recorded(fact, recorded)
                        })
            })
        });
        let provider_alive = state.provider_pid.is_some_and(|pid| {
            process_facts.get(&pid).is_some_and(|fact| {
                state
                    .provider_process_start_time
                    .as_deref()
                    .is_some_and(|recorded| {
                        !recorded.trim().is_empty() && lstart_matches_recorded(fact, recorded)
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
        observations.push(PiHelmObservation {
            session_id,
            provider_session_id: state
                .provider_session_id
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
