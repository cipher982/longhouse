//! Frozen payload bytes, stored as files and referenced by row.
//!
//! A storage-v2 write intent has to be immutable before it crosses the network:
//! the July 2026 incident showed that persisting only range metadata loses the
//! bytes a retry must resend. That invariant is not in question. What is in
//! question is where those bytes live — and for two years the answer was "as a
//! BLOB in the same SQLite file as the durability cursors", which is how the
//! agent database reached 831 MB with 626 MB of dead space, why a required
//! identity bind lost the write lock, and why reclaiming it needed a 25-minute
//! VACUUM that a restarting daemon never got around to running.
//!
//! So: bytes to files, metadata to rows, exactly one authoritative
//! representation per unacknowledged range. A payload is sealed under its
//! content hash, so a re-seal of the same bytes is the same file; a read
//! verifies that hash, so a truncated or replaced file is a typed error rather
//! than a silent empty ship; and a payload is only removed after the row that
//! referenced it is gone, which is the one ordering that cannot lose bytes.
//!
//! Absence never means acknowledged. A row whose file is missing is a fault the
//! caller must handle loudly — the only other explanation is a bug, because
//! nothing deletes a payload before its row.

use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use sha2::{Digest, Sha256};

/// Directory holding sealed payloads, relative to the Longhouse home.
const PAYLOAD_DIR: &str = "agent/outbox-v2";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SealedPayload {
    /// Path relative to the payload root, so a moved home does not break rows.
    pub relative_path: String,
    pub sha256: String,
    pub len: u64,
}

/// Where sealed payloads live for this machine.
pub fn payload_root() -> Result<PathBuf> {
    Ok(crate::config::get_longhouse_home()?.join(PAYLOAD_DIR))
}

pub fn hash_bytes(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

/// Write payload bytes under their content hash and return the reference.
///
/// Sealing the same bytes twice is idempotent: the file already exists and is
/// left alone. The write is a temporary file, fsynced, then renamed, so a
/// reader never sees a partial payload and a crash leaves either the old file or
/// the new one.
pub fn seal(root: &Path, bytes: &[u8]) -> Result<SealedPayload> {
    let sha256 = hash_bytes(bytes);
    let relative_path = format!("{}/{}.zst", &sha256[..2], sha256);
    let target = root.join(&relative_path);
    let sealed = SealedPayload {
        relative_path,
        sha256,
        len: bytes.len() as u64,
    };
    if target.exists() {
        return Ok(sealed);
    }
    let parent = target.parent().context("payload path has no parent")?;
    std::fs::create_dir_all(parent)
        .with_context(|| format!("creating the payload directory {}", parent.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o700)).ok();
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700)).ok();
    }

    let temporary = target.with_extension("zst.tmp");
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&temporary)
        .with_context(|| format!("writing the payload {}", temporary.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    file.write_all(bytes)
        .with_context(|| format!("writing the payload {}", temporary.display()))?;
    file.sync_all()?;
    drop(file);
    std::fs::rename(&temporary, &target)
        .with_context(|| format!("publishing the payload {}", target.display()))?;
    Ok(sealed)
}

/// Read a payload back, refusing anything that is not the bytes that were sealed.
pub fn read(root: &Path, relative_path: &str, expected_sha256: &str) -> Result<Vec<u8>> {
    let path = root.join(relative_path);
    let bytes = std::fs::read(&path).with_context(|| {
        format!(
            "reading the frozen payload {} — a row whose payload is missing must be re-prepared, \
             never shipped as empty bytes",
            path.display()
        )
    })?;
    let actual = hash_bytes(&bytes);
    anyhow::ensure!(
        actual == expected_sha256,
        "frozen payload {} does not match its recorded hash (recorded {expected_sha256}, found {actual})",
        path.display()
    );
    Ok(bytes)
}

/// Remove a payload after the row that referenced it is gone.
pub fn remove(root: &Path, relative_path: &str) -> Result<()> {
    let path = root.join(relative_path);
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => {
            Err(error).with_context(|| format!("removing the payload {}", path.display()))
        }
    }
}

/// What a sweep found. Every field is evidence, not a count to ignore.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct PayloadSweep {
    /// Files with no row: a crash between sealing and committing the row, or a
    /// row deleted without unlinking. Safe to delete.
    pub orphans_removed: usize,
    /// Rows whose payload file is missing. Never silent: these are the rows that
    /// cannot be shipped and must be re-prepared.
    pub missing_payloads: Vec<String>,
}

/// Reconcile the payload directory against the references rows hold.
///
/// Called at startup, before anything ships. The two failure shapes are not
/// symmetrical: an orphan file wastes space and is deleted, while a row without
/// its bytes is reported so the caller can re-prepare it from the source.
pub fn sweep(root: &Path, referenced: &[String]) -> Result<PayloadSweep> {
    let mut report = PayloadSweep::default();
    let referenced: std::collections::HashSet<&str> =
        referenced.iter().map(String::as_str).collect();

    if root.exists() {
        for shard in std::fs::read_dir(root)
            .with_context(|| format!("reading the payload root {}", root.display()))?
            .flatten()
        {
            let shard_path = shard.path();
            if !shard_path.is_dir() {
                // A stray file (including a temporary) is an orphan by
                // definition: nothing references a payload outside its shard.
                if std::fs::remove_file(&shard_path).is_ok() {
                    report.orphans_removed += 1;
                }
                continue;
            }
            let Some(shard_name) = shard_path.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            for entry in std::fs::read_dir(&shard_path)
                .with_context(|| format!("reading the payload shard {}", shard_path.display()))?
                .flatten()
            {
                let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
                    continue;
                };
                let relative = format!("{shard_name}/{name}");
                if referenced.contains(relative.as_str()) {
                    continue;
                }
                if std::fs::remove_file(entry.path()).is_ok() {
                    report.orphans_removed += 1;
                }
            }
        }
    }

    for reference in referenced {
        if !root.join(reference).exists() {
            report.missing_payloads.push(reference.to_string());
        }
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_sealed_payload_reads_back_byte_identical() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let bytes = vec![7u8; 4096];

        let sealed = seal(root, &bytes).expect("seal");

        assert_eq!(sealed.len, 4096);
        assert_eq!(read(root, &sealed.relative_path, &sealed.sha256).unwrap(), bytes);
    }

    #[test]
    fn sealing_the_same_bytes_twice_is_the_same_file() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();

        let first = seal(root, b"payload").expect("seal");
        let second = seal(root, b"payload").expect("seal again");

        assert_eq!(first, second);
        let files: Vec<_> = std::fs::read_dir(root.join(&first.relative_path[..2]))
            .unwrap()
            .flatten()
            .collect();
        assert_eq!(files.len(), 1, "a re-seal must not leave a second copy");
    }

    #[test]
    fn a_replaced_payload_is_a_typed_error_not_empty_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let sealed = seal(root, b"the real bytes").expect("seal");
        std::fs::write(root.join(&sealed.relative_path), b"different bytes").unwrap();

        let error = read(root, &sealed.relative_path, &sealed.sha256).unwrap_err();

        assert!(
            format!("{error:#}").contains("does not match its recorded hash"),
            "a corrupted payload must be reported: {error:#}"
        );
    }

    #[test]
    fn a_missing_payload_is_an_error_that_names_the_consequence() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();

        let error = read(root, "ab/absent.zst", "0").unwrap_err();

        assert!(
            format!("{error:#}").contains("never shipped as empty bytes"),
            "the error must say what the caller may not do: {error:#}"
        );
    }

    #[test]
    fn a_sweep_deletes_orphans_and_reports_rows_without_payloads() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let kept = seal(root, b"referenced").expect("seal");
        let orphan = seal(root, b"orphaned").expect("seal");

        let report = sweep(root, &[kept.relative_path.clone()]).expect("sweep");

        assert_eq!(report.orphans_removed, 1);
        assert!(report.missing_payloads.is_empty());
        assert!(root.join(&kept.relative_path).exists());
        assert!(!root.join(&orphan.relative_path).exists());
    }

    #[test]
    fn a_sweep_reports_a_row_whose_payload_is_gone() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let sealed = seal(root, b"payload").expect("seal");
        remove(root, &sealed.relative_path).expect("remove");

        let report = sweep(root, &[sealed.relative_path.clone()]).expect("sweep");

        assert_eq!(report.missing_payloads, vec![sealed.relative_path]);
    }
}
