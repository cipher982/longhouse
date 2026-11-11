//! Codex rollouts that the provider compressed in place (`rollout-*.jsonl.zst`).
//!
//! Codex compresses older rollouts and removes the plain file. The compressed
//! file holds exactly the bytes the plain one did, so it is the same source:
//! it keeps the plain path's source id and its byte offsets are offsets into
//! the decompressed stream. Reading one means decompressing it; deciding that
//! one was already shipped must not, because a machine can hold thousands
//! (741 of 758 rollouts on cinder, 2026-11-11), so that answer comes from the
//! zstd frame header (`content_size_hint`) whenever the frame records its size.

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};

use anyhow::{Context, Result};

const ZSTD_SUFFIX: &str = ".zst";

/// Whether `path` is a compressed JSONL transcript (`*.jsonl.zst`).
pub fn is_compressed_transcript(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".jsonl.zst") && name.len() > ".jsonl.zst".len())
}

/// The plain path a compressed transcript replaced, which is its source identity.
pub fn uncompressed_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default();
    match name.strip_suffix(ZSTD_SUFFIX) {
        Some(plain) => path.with_file_name(plain),
        None => path.to_path_buf(),
    }
}

/// A reader over the decompressed bytes.
pub fn open_decompressed(path: &Path) -> Result<impl Read> {
    let file = File::open(path)
        .with_context(|| format!("opening compressed transcript: {}", path.display()))?;
    zstd::stream::read::Decoder::new(BufReader::new(file))
        .with_context(|| format!("reading zstd frame: {}", path.display()))
}

/// The decompressed size the first zstd frame records, if it records one.
///
/// Codex writes one frame with its size, so this is normally the whole
/// stream's length. A file of several frames reports only the first; callers
/// treat the hint as exact only when it equals what they already know.
pub fn content_size_hint(path: &Path) -> Option<u64> {
    // A frame header is at most 18 bytes.
    let mut header = [0u8; 18];
    let mut file = File::open(path).ok()?;
    let mut read = 0;
    while read < header.len() {
        match file.read(&mut header[read..]) {
            Ok(0) => break,
            Ok(n) => read += n,
            Err(_) => return None,
        }
    }
    zstd::zstd_safe::get_frame_content_size(&header[..read])
        .ok()
        .flatten()
}

/// File length and modification time, the key for a remembered verdict.
fn stamp(path: &Path) -> Option<(u64, i128)> {
    let metadata = path.metadata().ok()?;
    let modified = metadata
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_nanos() as i128;
    Some((metadata.len(), modified))
}

/// Decompressed lengths already measured by a full read, by path and stamp, so a
/// file whose header carries no size is decompressed once per process, not on
/// every scan.
static MEASURED: LazyLock<Mutex<HashMap<PathBuf, ((u64, i128), u64)>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// The exact decompressed length, reading the whole stream unless this file,
/// unchanged, was measured before.
pub fn content_size(path: &Path) -> Result<u64> {
    let stamp = stamp(path);
    if let (Some(stamp), Ok(measured)) = (stamp, MEASURED.lock()) {
        if let Some((known, len)) = measured.get(path) {
            if *known == stamp {
                return Ok(*len);
            }
        }
    }
    let len = std::io::copy(&mut open_decompressed(path)?, &mut std::io::sink())
        .with_context(|| format!("decompressing transcript: {}", path.display()))?;
    if let (Some(stamp), Ok(mut measured)) = (stamp, MEASURED.lock()) {
        measured.insert(path.to_path_buf(), (stamp, len));
    }
    Ok(len)
}

/// Whether a source whose durable lane stands at `position` has nothing left
/// to ship, answered from the frame header when it can be. The header is
/// trusted only when it agrees exactly: a multi-frame file reports its first
/// frame, which is shorter than what was shipped, and falls through to the
/// full measure.
pub fn fully_shipped(path: &Path, position: u64) -> Result<bool> {
    if content_size_hint(path) == Some(position) {
        return Ok(true);
    }
    Ok(position >= content_size(path)?)
}

/// Where decompressed copies are kept while their tail ships.
pub fn cache_dir() -> Result<PathBuf> {
    Ok(crate::config::get_agent_dir()?.join("compressed-source-cache"))
}

fn cache_path_in(dir: &Path, identity_path: &Path) -> PathBuf {
    let digest = <sha2::Sha256 as sha2::Digest>::digest(identity_path.to_string_lossy().as_bytes());
    dir.join(format!("{digest:x}.jsonl"))
}

/// Decompress `path` into the cache as a plain file and return it. A copy
/// already there with the right length is reused. Written to a temporary name
/// and renamed, so a reader never sees a partial copy.
pub fn materialize(path: &Path, identity_path: &Path, dir: &Path) -> Result<PathBuf> {
    std::fs::create_dir_all(dir)
        .with_context(|| format!("creating compressed-source cache: {}", dir.display()))?;
    let target = cache_path_in(dir, identity_path);
    let expected = content_size(path)?;
    if target
        .metadata()
        .is_ok_and(|metadata| metadata.len() == expected)
    {
        return Ok(target);
    }
    let partial = target.with_extension("jsonl.partial");
    {
        let mut out = File::create(&partial)
            .with_context(|| format!("creating cache copy: {}", partial.display()))?;
        std::io::copy(&mut open_decompressed(path)?, &mut out)?;
        out.flush()?;
    }
    std::fs::rename(&partial, &target)
        .with_context(|| format!("publishing cache copy: {}", target.display()))?;
    Ok(target)
}

/// Remove the cached copy for a source that has finished shipping.
pub fn evict(identity_path: &Path, dir: &Path) {
    let _ = std::fs::remove_file(cache_path_in(dir, identity_path));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn compress(bytes: &[u8], with_size: bool) -> Vec<u8> {
        let mut encoder = zstd::stream::write::Encoder::new(Vec::new(), 3).unwrap();
        if with_size {
            encoder
                .set_pledged_src_size(Some(bytes.len() as u64))
                .unwrap();
        }
        encoder.include_contentsize(with_size).unwrap();
        encoder.write_all(bytes).unwrap();
        encoder.finish().unwrap()
    }

    #[test]
    fn recognises_and_strips_compressed_transcripts() {
        let path = Path::new("/x/2026/10/07/rollout-2026-10-07T00-20-11-abc.jsonl.zst");
        assert!(is_compressed_transcript(path));
        assert_eq!(
            uncompressed_path(path),
            Path::new("/x/2026/10/07/rollout-2026-10-07T00-20-11-abc.jsonl")
        );
        assert!(!is_compressed_transcript(Path::new("/x/a.jsonl")));
        assert!(!is_compressed_transcript(Path::new("/x/a.zst")));
        assert!(!is_compressed_transcript(Path::new("/x/.jsonl.zst")));
    }

    #[test]
    fn header_size_decides_shipped_without_decompressing() {
        let dir = tempfile::tempdir().unwrap();
        let body = b"{\"a\":1}\n{\"b\":2}\n".repeat(50);
        let path = dir.path().join("r.jsonl.zst");
        std::fs::write(&path, compress(&body, true)).unwrap();
        assert_eq!(content_size_hint(&path), Some(body.len() as u64));
        assert!(fully_shipped(&path, body.len() as u64).unwrap());
        assert!(!fully_shipped(&path, 10).unwrap());
    }

    #[test]
    fn frame_without_size_is_measured_by_reading() {
        let dir = tempfile::tempdir().unwrap();
        let body = b"{\"a\":1}\n".repeat(20);
        let path = dir.path().join("r.jsonl.zst");
        std::fs::write(&path, compress(&body, false)).unwrap();
        assert_eq!(content_size_hint(&path), None);
        assert_eq!(content_size(&path).unwrap(), body.len() as u64);
        assert!(fully_shipped(&path, body.len() as u64).unwrap());
    }

    #[test]
    fn multi_frame_file_is_not_judged_by_its_first_frame() {
        let dir = tempfile::tempdir().unwrap();
        let first = b"{\"a\":1}\n".repeat(5);
        let second = b"{\"b\":2}\n".repeat(5);
        let path = dir.path().join("r.jsonl.zst");
        std::fs::write(
            &path,
            [compress(&first, true), compress(&second, true)].concat(),
        )
        .unwrap();
        let total = (first.len() + second.len()) as u64;
        assert_eq!(content_size_hint(&path), Some(first.len() as u64));
        assert!(!fully_shipped(&path, first.len() as u64 - 1).unwrap());
        assert!(fully_shipped(&path, total).unwrap());
        assert!(!fully_shipped(&path, total - 1).unwrap());
    }

    #[test]
    fn materialize_writes_the_plain_bytes_once() {
        let dir = tempfile::tempdir().unwrap();
        let body = b"{\"a\":1}\n".repeat(30);
        let path = dir.path().join("r.jsonl.zst");
        std::fs::write(&path, compress(&body, true)).unwrap();
        let cache = dir.path().join("cache");
        let identity = dir.path().join("r.jsonl");
        let copy = materialize(&path, &identity, &cache).unwrap();
        assert_eq!(std::fs::read(&copy).unwrap(), body);
        let again = materialize(&path, &identity, &cache).unwrap();
        assert_eq!(copy, again);
        evict(&identity, &cache);
        assert!(!copy.exists());
    }
}
