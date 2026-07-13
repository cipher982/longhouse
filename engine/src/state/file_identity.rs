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
    #[cfg(unix)]
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

pub fn current_file_identity(path: &str) -> Option<String> {
    std::fs::metadata(Path::new(path))
        .ok()
        .and_then(|metadata| identity_from_metadata(&metadata))
}

/// Hash the bytes immediately before an acknowledged cursor.
///
/// Device/inode identity detects replacement, while this boundary proof also
/// detects truncate-and-regrow of the same inode before storage-v2 adopts a
/// legacy cursor.
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
