//! Codex rollouts that the provider compressed in place (`rollout-*.jsonl.zst`).
//!
//! Codex compresses older rollouts and removes the plain file. The compressed
//! file holds exactly the bytes the plain one did, so it is the same source:
//! it keeps the plain path's source id and its byte offsets are offsets into
//! the decompressed stream. Reading one means decompressing it; deciding that
//! one was already shipped must not, because a machine can hold thousands
//! (741 of 758 rollouts on cinder, 2026-11-11), so that answer comes from the
//! zstd frame and block headers (`declared_content_size`), read by seeking past
//! each block, whenever every frame records its size.

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

const ZSTD_MAGIC: u32 = 0xFD2F_B528;
const SKIPPABLE_MAGIC_MASK: u32 = 0xFFFF_FFF0;
const SKIPPABLE_MAGIC: u32 = 0x184D_2A50;

/// The decompressed length the file's frames declare, summed over every frame,
/// read from frame and block headers without decompressing (RFC 8878 section
/// 3.1). `None` when any frame leaves its size out, so the caller has to
/// measure. Each block costs a 3-byte read and a seek; a file of several frames
/// is summed exactly, not judged by its first frame.
pub fn declared_content_size(path: &Path) -> Result<Option<u64>> {
    // Headers are a few bytes between long seeks: a larger buffer would be
    // refilled, and thrown away, at every block.
    let mut file = BufReader::with_capacity(
        32,
        File::open(path)
            .with_context(|| format!("opening compressed transcript: {}", path.display()))?,
    );
    let len = file.get_ref().metadata()?.len();
    let mut offset = 0u64;
    let mut total = 0u64;
    let mut frames = 0u32;
    while offset < len {
        let magic = read_u32(&mut file)?;
        if magic & SKIPPABLE_MAGIC_MASK == SKIPPABLE_MAGIC {
            let skip = read_u32(&mut file)?;
            file.seek_relative(i64::from(skip))?;
            offset += 8 + u64::from(skip);
            continue;
        }
        anyhow::ensure!(magic == ZSTD_MAGIC, "not a zstd frame at byte {offset}");
        let mut descriptor = [0u8; 1];
        file.read_exact(&mut descriptor)?;
        let descriptor = descriptor[0];
        let single_segment = descriptor & 0x20 != 0;
        let checksum = descriptor & 0x04 != 0;
        let dictionary_bytes = [0u64, 1, 2, 4][usize::from(descriptor & 0x03)];
        let size_bytes = match descriptor >> 6 {
            0 if single_segment => 1,
            0 => 0,
            1 => 2,
            2 => 4,
            _ => 8,
        };
        let window_bytes = u64::from(!single_segment);
        file.seek_relative((window_bytes + dictionary_bytes) as i64)?;
        if size_bytes == 0 {
            return Ok(None);
        }
        let mut size = [0u8; 8];
        file.read_exact(&mut size[..size_bytes])?;
        let mut content = u64::from_le_bytes(size);
        if size_bytes == 2 {
            content += 256;
        }
        total = total.saturating_add(content);
        offset += 5 + window_bytes + dictionary_bytes + size_bytes as u64;
        loop {
            let mut header = [0u8; 3];
            file.read_exact(&mut header)?;
            let header =
                u32::from(header[0]) | u32::from(header[1]) << 8 | u32::from(header[2]) << 16;
            let last = header & 1 != 0;
            let block_type = (header >> 1) & 0x03;
            let block_size = u64::from(header >> 3);
            let payload = match block_type {
                0 | 2 => block_size,
                1 => 1,
                _ => anyhow::bail!("reserved zstd block type at byte {offset}"),
            };
            file.seek_relative(payload as i64)?;
            offset += 3 + payload;
            if last {
                break;
            }
        }
        if checksum {
            file.seek_relative(4)?;
            offset += 4;
        }
        frames += 1;
    }
    anyhow::ensure!(offset == len, "zstd frames overrun the file");
    anyhow::ensure!(frames > 0, "no zstd frame");
    Ok(Some(total))
}

fn read_u32(reader: &mut impl Read) -> Result<u32> {
    let mut bytes = [0u8; 4];
    reader.read_exact(&mut bytes)?;
    Ok(u32::from_le_bytes(bytes))
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

/// Decompressed lengths already measured, by path and stamp, so a file is
/// measured once per process, not on every scan.
static MEASURED: LazyLock<Mutex<HashMap<PathBuf, ((u64, i128), u64)>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// The exact decompressed length: from the frame headers when they declare it,
/// otherwise by decompressing the whole stream; once per unchanged file.
pub fn content_size(path: &Path) -> Result<u64> {
    let stamp = stamp(path);
    if let (Some(stamp), Ok(measured)) = (stamp, MEASURED.lock()) {
        if let Some((known, len)) = measured.get(path) {
            if *known == stamp {
                return Ok(*len);
            }
        }
    }
    let len = match declared_content_size(path) {
        Ok(Some(len)) => len,
        _ => std::io::copy(&mut open_decompressed(path)?, &mut std::io::sink())
            .with_context(|| format!("decompressing transcript: {}", path.display()))?,
    };
    if let (Some(stamp), Ok(mut measured)) = (stamp, MEASURED.lock()) {
        measured.insert(path.to_path_buf(), (stamp, len));
    }
    Ok(len)
}

/// Whether a source whose durable lane stands at `position` has nothing left
/// to ship. Answered without decompressing whenever the frames declare their
/// sizes (every Codex rollout sampled on cinder does, 2026-11-11).
pub fn fully_shipped(path: &Path, position: u64) -> Result<bool> {
    Ok(position >= content_size(path)?)
}

fn cache_prefix(identity_path: &Path) -> String {
    let digest = <sha2::Sha256 as sha2::Digest>::digest(identity_path.to_string_lossy().as_bytes());
    format!("{digest:x}")
}

/// The cache copy's name carries the compressed file's length and mtime, so a
/// replaced or recompressed file never reuses a copy made from another one.
fn cache_path_in(dir: &Path, identity_path: &Path, stamp: (u64, i128)) -> PathBuf {
    dir.join(format!(
        "{}-{}-{}.jsonl",
        cache_prefix(identity_path),
        stamp.0,
        stamp.1
    ))
}

/// Decompress `path` into the cache as a plain file and return it. A copy made
/// from this same compressed file (same length and mtime) is reused; copies of
/// earlier versions are removed. Written to a temporary name and renamed, so a
/// reader never sees a partial copy.
pub fn materialize(path: &Path, identity_path: &Path, dir: &Path) -> Result<PathBuf> {
    std::fs::create_dir_all(dir)
        .with_context(|| format!("creating compressed-source cache: {}", dir.display()))?;
    let stamp = stamp(path)
        .with_context(|| format!("reading compressed transcript: {}", path.display()))?;
    let target = cache_path_in(dir, identity_path, stamp);
    let expected = content_size(path)?;
    if target
        .metadata()
        .is_ok_and(|metadata| metadata.len() == expected)
    {
        return Ok(target);
    }
    evict(identity_path, dir);
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

/// Remove every cached copy for a source (it finished shipping, or its
/// compressed file changed).
pub fn evict(identity_path: &Path, dir: &Path) {
    let prefix = format!("{}-", cache_prefix(identity_path));
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        if entry.file_name().to_string_lossy().starts_with(&prefix) {
            let _ = std::fs::remove_file(entry.path());
        }
    }
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
        assert_eq!(
            declared_content_size(&path).unwrap(),
            Some(body.len() as u64)
        );
        assert!(fully_shipped(&path, body.len() as u64).unwrap());
        assert!(!fully_shipped(&path, 10).unwrap());
    }

    #[test]
    fn frame_without_size_is_measured_by_reading() {
        let dir = tempfile::tempdir().unwrap();
        let body = b"{\"a\":1}\n".repeat(20);
        let path = dir.path().join("r.jsonl.zst");
        std::fs::write(&path, compress(&body, false)).unwrap();
        assert_eq!(declared_content_size(&path).unwrap(), None);
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
        assert_eq!(declared_content_size(&path).unwrap(), Some(total));
        // The lane standing exactly at the end of the first frame still has
        // the second frame to ship (review rv-20261111T210224Z-e5d7203-fe2d F1).
        assert!(!fully_shipped(&path, first.len() as u64).unwrap());
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

    #[test]
    fn materialize_never_reuses_a_copy_of_a_replaced_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("r.jsonl.zst");
        let cache = dir.path().join("cache");
        let identity = dir.path().join("r.jsonl");
        let old = b"{\"a\":1}\n".repeat(30);
        std::fs::write(&path, compress(&old, true)).unwrap();
        let old_copy = materialize(&path, &identity, &cache).unwrap();

        // Same decompressed length, different bytes, newer mtime.
        let new = b"{\"b\":2}\n".repeat(30);
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::write(&path, compress(&new, true)).unwrap();
        let new_copy = materialize(&path, &identity, &cache).unwrap();
        assert_eq!(std::fs::read(&new_copy).unwrap(), new);
        assert!(!old_copy.exists() || old_copy == new_copy);
    }

    #[test]
    fn declared_size_skips_skippable_frames_and_checksums() {
        let dir = tempfile::tempdir().unwrap();
        let body = b"{\"a\":1}\n".repeat(40);
        let mut encoder = zstd::stream::write::Encoder::new(Vec::new(), 3).unwrap();
        encoder.include_checksum(true).unwrap();
        encoder
            .set_pledged_src_size(Some(body.len() as u64))
            .unwrap();
        encoder.write_all(&body).unwrap();
        let framed = encoder.finish().unwrap();
        let mut skippable = 0x184D_2A53u32.to_le_bytes().to_vec();
        skippable.extend_from_slice(&3u32.to_le_bytes());
        skippable.extend_from_slice(b"xyz");
        let path = dir.path().join("r.jsonl.zst");
        std::fs::write(&path, [skippable, framed].concat()).unwrap();
        assert_eq!(
            declared_content_size(&path).unwrap(),
            Some(body.len() as u64)
        );
        assert_eq!(content_size(&path).unwrap(), body.len() as u64);
    }

    /// The header walk over a real Codex sessions directory:
    /// `LONGHOUSE_CODEX_BENCH_DIR=~/.codex/sessions`. Every declared size must
    /// equal the decompressed length, and prints the walk's cost.
    #[test]
    #[ignore = "benchmark; needs LONGHOUSE_CODEX_BENCH_DIR"]
    fn declared_size_bench_on_real_rollouts() {
        let Some(root) = std::env::var_os("LONGHOUSE_CODEX_BENCH_DIR") else {
            return;
        };
        let files: Vec<PathBuf> = walkdir::WalkDir::new(root)
            .into_iter()
            .filter_map(Result::ok)
            .map(|entry| entry.into_path())
            .filter(|path| is_compressed_transcript(path))
            .collect();
        let started = std::time::Instant::now();
        let declared: Vec<Option<u64>> = files
            .iter()
            .map(|path| declared_content_size(path).unwrap())
            .collect();
        let walk = started.elapsed();
        let mut checked = 0;
        for (path, declared) in files.iter().zip(&declared).take(60) {
            let measured =
                std::io::copy(&mut open_decompressed(path).unwrap(), &mut std::io::sink()).unwrap();
            assert_eq!(*declared, Some(measured), "{}", path.display());
            checked += 1;
        }
        eprintln!(
            "{} rollouts, header walk {walk:?}; {} without a declared size; {checked} checked against a full decompress",
            files.len(),
            declared.iter().filter(|size| size.is_none()).count()
        );
    }
}
