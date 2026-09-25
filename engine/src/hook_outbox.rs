//! Shared local outbox writer for provider lifecycle hooks.
//!
//! Provider callbacks are latency-sensitive and must not open SQLite or make a
//! network request. They hand a complete presence observation to the daemon by
//! atomically renaming a small file into the shared outbox instead.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

use serde_json::Value;
use uuid::Uuid;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

/// Enqueue one presence observation for the daemon without touching SQLite or
/// the network. The rename is the publication boundary: the daemon only sees
/// complete JSON files. The payload is persisted as supplied: producer
/// timestamps such as `occurred_at` are never replaced with delivery time.
pub(crate) fn enqueue_presence(home: &Path, payload: &Value) -> std::io::Result<()> {
    enqueue_presence_in_outbox(&home.join("agent").join("outbox"), payload)
}

/// Enqueue a local-health phase using the agent outbox next to its state DB.
///
/// Provider adapters run outside the engine process. They must not write the
/// shared SQLite database: a phase callback can arrive while the daemon is
/// reducing a heartbeat, and a short client timeout turns that race into lost
/// local state. The daemon already owns this outbox and is the single durable
/// writer.
pub(crate) fn enqueue_local_phase(
    db_path: &Path,
    session_id: &str,
    provider: &str,
    phase: &str,
    tool_name: Option<&str>,
    source: &str,
    occurred_at: &str,
    run_id: Option<&str>,
) -> std::io::Result<()> {
    let payload = serde_json::json!({
        "session_id": session_id,
        "state": phase,
        "tool_name": tool_name,
        "provider": provider,
        "occurred_at": occurred_at,
        "local_only": true,
        "phase_source": source,
        // The run the provider itself was launched into. Carrying it here is what
        // lets the status projection attribute the phase by identity instead of
        // guessing from a timestamp.
        "run_id": run_id,
    });
    let agent_dir = db_path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "local phase DB path has no agent directory",
        )
    })?;
    enqueue_presence_in_outbox(&agent_dir.join("outbox"), &payload)
}

fn enqueue_presence_in_outbox(outbox: &Path, payload: &Value) -> std::io::Result<()> {
    std::fs::create_dir_all(outbox)?;
    #[cfg(unix)]
    std::fs::set_permissions(outbox, std::fs::Permissions::from_mode(0o700))?;

    let temporary = outbox.join(format!(".prs.{}.tmp", Uuid::new_v4()));
    let ready = outbox.join(format!("prs.{}.json", Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    #[cfg(unix)]
    {
        file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    let mut bytes = serde_json::to_vec(payload).map_err(std::io::Error::other)?;
    bytes.push(b'\n');
    file.write_all(&bytes)?;
    file.sync_all()?;
    drop(file);
    if let Err(error) = std::fs::rename(&temporary, &ready) {
        let _ = std::fs::remove_file(&temporary);
        return Err(error);
    }
    // The file is visible only after the rename, and the directory entry is
    // durable before the provider callback returns. A crash must not create a
    // binding-intent filename whose contents never reached disk.
    if let Ok(directory) = OpenOptions::new().read(true).open(outbox) {
        directory.sync_all()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn publishes_one_private_complete_presence_file() {
        let home = tempfile::tempdir().unwrap();
        enqueue_presence(
            home.path(),
            &serde_json::json!({
                "session_id": "session",
                "state": "thinking",
                "occurred_at": "2026-09-25T16:39:00Z",
                "delegation": {
                    "count": 0,
                    "kinds": {},
                    "items": [],
                    "observed_at": "2026-09-25T16:39:00Z",
                },
            }),
        )
        .unwrap();

        let outbox = home.path().join("agent/outbox");
        let files: Vec<_> = std::fs::read_dir(outbox).unwrap().flatten().collect();
        assert_eq!(files.len(), 1);
        assert!(files[0].file_name().to_string_lossy().starts_with("prs."));
        let written =
            serde_json::from_slice::<Value>(&std::fs::read(files[0].path()).unwrap()).unwrap();
        assert_eq!(written["session_id"], "session");
        assert_eq!(written["occurred_at"], "2026-09-25T16:39:00Z");
        assert_eq!(written["delegation"]["observed_at"], "2026-09-25T16:39:00Z");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                files[0].metadata().unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }
}
