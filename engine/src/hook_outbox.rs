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
/// complete JSON files.
pub(crate) fn enqueue_presence(home: &Path, payload: &Value) -> std::io::Result<()> {
    let outbox = home.join("agent").join("outbox");
    std::fs::create_dir_all(&outbox)?;
    #[cfg(unix)]
    std::fs::set_permissions(&outbox, std::fs::Permissions::from_mode(0o700))?;

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
    drop(file);
    if let Err(error) = std::fs::rename(&temporary, &ready) {
        let _ = std::fs::remove_file(&temporary);
        return Err(error);
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
            &serde_json::json!({"session_id":"session","state":"thinking"}),
        )
        .unwrap();

        let outbox = home.path().join("agent/outbox");
        let files: Vec<_> = std::fs::read_dir(outbox).unwrap().flatten().collect();
        assert_eq!(files.len(), 1);
        assert!(files[0].file_name().to_string_lossy().starts_with("prs."));
        assert_eq!(
            serde_json::from_slice::<Value>(&std::fs::read(files[0].path()).unwrap()).unwrap()
                ["session_id"],
            "session"
        );
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
