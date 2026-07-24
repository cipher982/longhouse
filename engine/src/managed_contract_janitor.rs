//! Sweep orphaned managed-session contract files.
//!
//! Contracts under `~/.longhouse/managed-local/contracts/<provider>/` are
//! written at launch and removed at teardown. Any teardown path that exits
//! early leaves one behind, and abrupt process death always does. They
//! accumulate indefinitely and feed later liveness and reattach reasoning.
//!
//! Provider-neutral on purpose: the leak is not Codex-specific. Codex leaked
//! through an error path that skipped cleanup; Claude leaks through abrupt
//! process death. One sweep covers both without either provider needing to
//! share a teardown model.

use std::collections::HashSet;
use std::path::Path;
use std::time::{Duration, SystemTime};

/// Grace period before a contract with no live session is considered orphaned.
///
/// Guards the launch race: a contract is written before the bridge registers,
/// so a young contract with no observation yet is normal, not garbage.
const ORPHAN_GRACE: Duration = Duration::from_secs(3600);

/// Remove contracts in `contract_dir` whose session is not live and whose file
/// is older than the grace period.
///
/// `live_session_ids` must be the sessions currently observed as alive. A
/// caller that cannot enumerate them should not call this — an empty set means
/// "nothing is live", which is a real state, not a missing-evidence state.
pub fn sweep_orphan_contracts(
    contract_dir: &Path,
    live_session_ids: &HashSet<String>,
    now: SystemTime,
) -> usize {
    let entries = match std::fs::read_dir(contract_dir) {
        Ok(entries) => entries,
        Err(_) => return 0,
    };
    let mut removed = 0usize;
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
            continue;
        };
        if !file_name.ends_with(".json") || file_name.starts_with('.') {
            continue;
        }
        let session_id = file_name.trim_end_matches(".json");
        if live_session_ids.contains(session_id) {
            continue;
        }
        let old_enough = entry
            .metadata()
            .and_then(|metadata| metadata.modified())
            .ok()
            .and_then(|modified| now.duration_since(modified).ok())
            .is_some_and(|age| age >= ORPHAN_GRACE);
        if !old_enough {
            continue;
        }
        if std::fs::remove_file(&path).is_ok() {
            removed += 1;
        }
    }
    removed
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Age is simulated by advancing `now` rather than backdating mtimes, so
    /// the tests need no filesystem-time dependency.
    fn later(seconds: u64) -> SystemTime {
        SystemTime::now() + Duration::from_secs(seconds)
    }

    fn write_contract(dir: &Path, session_id: &str) {
        std::fs::write(dir.join(format!("{session_id}.json")), b"{}").unwrap();
    }

    #[test]
    fn removes_old_contract_with_no_live_session() {
        let dir = tempfile::tempdir().unwrap();
        write_contract(dir.path(), "dead");
        let removed = sweep_orphan_contracts(dir.path(), &HashSet::new(), later(7200));
        assert_eq!(removed, 1);
        assert!(!dir.path().join("dead.json").exists());
    }

    #[test]
    fn keeps_contract_for_live_session() {
        let dir = tempfile::tempdir().unwrap();
        write_contract(dir.path(), "alive");
        let live = HashSet::from(["alive".to_string()]);
        let removed = sweep_orphan_contracts(dir.path(), &live, later(7200));
        assert_eq!(removed, 0);
        assert!(dir.path().join("alive.json").exists());
    }

    #[test]
    fn keeps_young_contract_even_with_no_observation() {
        let dir = tempfile::tempdir().unwrap();
        write_contract(dir.path(), "launching");
        let removed = sweep_orphan_contracts(dir.path(), &HashSet::new(), later(5));
        assert_eq!(removed, 0);
        assert!(dir.path().join("launching.json").exists());
    }

    #[test]
    fn ignores_non_contract_files() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("notes.txt"), b"x").unwrap();
        let removed = sweep_orphan_contracts(dir.path(), &HashSet::new(), later(7200));
        assert_eq!(removed, 0);
        assert!(dir.path().join("notes.txt").exists());
    }

    #[test]
    fn missing_directory_is_not_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let removed =
            sweep_orphan_contracts(&dir.path().join("absent"), &HashSet::new(), later(7200));
        assert_eq!(removed, 0);
    }
}
