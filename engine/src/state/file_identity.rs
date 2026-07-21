//! Stable-enough source-file identity for cursor safety.
//!
//! Transcript cursors are keyed by path for lookup, but path alone is not the
//! source epoch. A rotated or replaced file can reuse the same path with new
//! bytes, so offset cursors must remember the backing file identity too.

use std::fs::{File, Metadata};
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use sha2::{Digest, Sha256};

const CURSOR_FINGERPRINT_WINDOW: u64 = 4096;

#[cfg(unix)]
use std::os::unix::fs::MetadataExt;

pub fn identity_from_metadata(metadata: &Metadata) -> Option<String> {
    #[cfg(target_os = "macos")]
    {
        // `st_dev` is a kernel mount identifier on macOS and can change after
        // reboot even when the APFS file is unchanged.  APFS inode + birth
        // time is stable across that remount and still distinguishes inode
        // reuse after delete/recreate.
        let birth_nanos = metadata
            .created()
            .ok()
            .and_then(|created| created.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|duration| duration.as_nanos());
        return Some(match birth_nanos {
            Some(birth_nanos) => {
                format!("macos-file-v1:{}:{birth_nanos}", metadata.ino())
            }
            None => format!("macos-inode-v1:{}", metadata.ino()),
        });
    }

    #[cfg(all(unix, not(target_os = "macos")))]
    {
        return Some(format!("unix:{}:{}", metadata.dev(), metadata.ino()));
    }

    #[cfg(not(unix))]
    {
        let modified = metadata.modified().ok()?;
        let modified_ms = modified
            .duration_since(std::time::SystemTime::UNIX_EPOCH)
            .ok()?
            .as_millis();
        Some(format!("generic:{}:{}", metadata.len(), modified_ms))
    }
}

/// Compare persisted file identities using the platform's durable semantics.
///
/// Old macOS builds persisted `unix:{st_dev}:{st_ino}`.  During the one-way
/// upgrade to `macos-file-v1`, matching the inode is the only evidence those
/// rows contain; the source path/provider scope and the storage-v2 boundary
/// fingerprint provide the remaining continuity proof where a cursor is
/// adopted.  Once both sides use v1, birth time also detects inode reuse.
pub fn file_identities_match(stored: Option<&str>, current: Option<&str>) -> bool {
    let (Some(stored), Some(current)) = (stored, current) else {
        return false;
    };
    if stored == current {
        return true;
    }

    #[cfg(target_os = "macos")]
    {
        let Some((stored_inode, stored_birth)) = parse_macos_identity(stored) else {
            return false;
        };
        let Some((current_inode, current_birth)) = parse_macos_identity(current) else {
            return false;
        };
        if stored_inode != current_inode {
            return false;
        }
        return match (stored_birth, current_birth) {
            (Some(stored_birth), Some(current_birth)) => stored_birth == current_birth,
            // A legacy identity has no durable birth-time component.  Accept
            // the scoped inode match once, then callers rewrite the canonical
            // v1 identity as normal progress is persisted.
            _ => true,
        };
    }

    #[cfg(not(target_os = "macos"))]
    false
}

/// Return the strongest representation when two identities describe the same
/// backing file.  A transient failure to read macOS birth time must not erase
/// a previously persisted birth-time proof.
pub fn strongest_matching_file_identity<'a>(stored: &'a str, current: &'a str) -> Option<&'a str> {
    if !file_identities_match(Some(stored), Some(current)) {
        return None;
    }
    if file_identity_strength(stored) > file_identity_strength(current) {
        Some(stored)
    } else {
        Some(current)
    }
}

fn file_identity_strength(value: &str) -> u8 {
    if value.starts_with("macos-file-v1:") {
        2
    } else {
        1
    }
}

#[cfg(target_os = "macos")]
fn parse_macos_identity(value: &str) -> Option<(u64, Option<u128>)> {
    if let Some(rest) = value.strip_prefix("macos-file-v1:") {
        let (inode, birth) = rest.split_once(':')?;
        return Some((inode.parse().ok()?, Some(birth.parse().ok()?)));
    }
    if let Some(inode) = value.strip_prefix("macos-inode-v1:") {
        return Some((inode.parse().ok()?, None));
    }
    let rest = value.strip_prefix("unix:")?;
    let (_device, inode) = rest.split_once(':')?;
    Some((inode.parse().ok()?, None))
}

pub fn current_file_identity(path: &str) -> Option<String> {
    std::fs::metadata(Path::new(path))
        .ok()
        .and_then(|metadata| identity_from_metadata(&metadata))
}

/// Hash the bytes immediately before an acknowledged cursor.
///
/// Backing-file identity detects replacement, while this boundary proof also
/// detects truncate-and-regrow before storage-v2 adopts a legacy cursor.
pub fn cursor_fingerprint(path: &Path, offset: u64) -> Option<String> {
    let mut file = File::open(path).ok()?;
    if file.metadata().ok()?.len() < offset {
        return None;
    }
    let start = offset.saturating_sub(CURSOR_FINGERPRINT_WINDOW);
    file.seek(SeekFrom::Start(start)).ok()?;
    let length = usize::try_from(offset - start).ok()?;
    let mut bytes = vec![0_u8; length];
    file.read_exact(&mut bytes).ok()?;
    let digest: [u8; 32] = Sha256::digest(&bytes).into();
    let hash: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    Some(format!("sha256:{start}:{offset}:{hash}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "macos")]
    #[test]
    fn legacy_macos_device_remap_preserves_identity() {
        assert!(file_identities_match(
            Some("unix:16777230:12345"),
            Some("unix:16777229:12345")
        ));
        assert!(file_identities_match(
            Some("unix:16777230:12345"),
            Some("macos-file-v1:12345:999")
        ));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_birth_time_detects_inode_reuse() {
        assert!(!file_identities_match(
            Some("macos-file-v1:12345:999"),
            Some("macos-file-v1:12345:1000")
        ));
        assert!(!file_identities_match(
            Some("macos-file-v1:12345:999"),
            Some("macos-file-v1:54321:999")
        ));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn transient_birth_time_failure_cannot_downgrade_identity() {
        assert_eq!(
            strongest_matching_file_identity("macos-file-v1:12345:999", "macos-inode-v1:12345"),
            Some("macos-file-v1:12345:999")
        );
        assert_eq!(
            strongest_matching_file_identity("unix:16777230:12345", "macos-file-v1:12345:999"),
            Some("macos-file-v1:12345:999")
        );
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn unix_identity_remains_device_scoped() {
        assert!(!file_identities_match(
            Some("unix:10:12345"),
            Some("unix:11:12345")
        ));
    }
}
