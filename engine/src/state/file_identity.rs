//! Stable-enough source-file identity for cursor safety.
//!
//! Transcript cursors are keyed by path for lookup, but path alone is not the
//! source epoch. A rotated or replaced file can reuse the same path with new
//! bytes, so offset cursors must remember the backing file identity too.

use std::fs::Metadata;
use std::path::Path;

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
