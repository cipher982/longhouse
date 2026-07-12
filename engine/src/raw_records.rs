//! Byte-exact, file-backed framing for storage-v2 raw source records.
//!
//! This path never consumes parsed or redacted `source_lines`. Each call reads
//! at most one bounded batch, so source size cannot become process memory.

#![allow(dead_code)] // Foundation is wired into shipping only at the v2 cutover.

use std::fs::File;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

pub(crate) const MAX_RAW_BATCH_BYTES: usize = 4 * 1024 * 1024;
pub(crate) const MAX_RAW_BATCH_RECORDS: usize = 10_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RawSourceFraming {
    LfDelimited,
    WholeDocument,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RawRecord {
    pub range_start: u64,
    pub range_end: u64,
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RawRecordBatch {
    pub range_start: u64,
    pub range_end: u64,
    pub records: Vec<RawRecord>,
}

impl RawRecordBatch {
    pub fn byte_len(&self) -> u64 {
        self.range_end - self.range_start
    }
}

#[derive(Debug, thiserror::Error)]
pub(crate) enum RawRecordError {
    #[error("opening raw source {path}: {source}")]
    Open {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("reading raw source {path}: {source}")]
    Read {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("start offset {start} exceeds source length {length}")]
    StartPastEnd { start: u64, length: u64 },
    #[error("whole-document framing must start at byte zero")]
    WholeDocumentNonZeroStart,
    #[error("start offset {start} is not an LF-delimited record boundary")]
    UnalignedStart { start: u64 },
    #[error("raw record at byte {start} exceeds the {maximum}-byte maximum")]
    RecordTooLarge { start: u64, maximum: usize },
}

/// Read one bounded raw batch beginning at an exact record boundary.
///
/// Callers advance with the returned batch's `range_end` until this returns
/// `None`. The function reopens the file for each batch and never accumulates
/// prior or later batches in memory.
pub(crate) fn read_next_raw_batch(
    path: &Path,
    framing: RawSourceFraming,
    start_offset: u64,
) -> Result<Option<RawRecordBatch>, RawRecordError> {
    read_next_raw_batch_bounded(path, framing, start_offset, MAX_RAW_BATCH_BYTES)
}

pub(crate) fn read_next_raw_batch_bounded(
    path: &Path,
    framing: RawSourceFraming,
    start_offset: u64,
    maximum_bytes: usize,
) -> Result<Option<RawRecordBatch>, RawRecordError> {
    let maximum_bytes = maximum_bytes.clamp(1, MAX_RAW_BATCH_BYTES);
    let file = File::open(path).map_err(|source| RawRecordError::Open {
        path: path.to_path_buf(),
        source,
    })?;
    let length = file
        .metadata()
        .map_err(|source| RawRecordError::Read {
            path: path.to_path_buf(),
            source,
        })?
        .len();
    if start_offset > length {
        return Err(RawRecordError::StartPastEnd {
            start: start_offset,
            length,
        });
    }
    match framing {
        RawSourceFraming::LfDelimited => read_next_lf_batch(path, file, start_offset, length, maximum_bytes),
        RawSourceFraming::WholeDocument => read_whole_document(path, file, start_offset, length),
    }
}

fn read_next_lf_batch(
    path: &Path,
    mut file: File,
    start_offset: u64,
    source_len: u64,
    maximum_bytes: usize,
) -> Result<Option<RawRecordBatch>, RawRecordError> {
    ensure_lf_boundary(path, &mut file, start_offset)?;
    file.seek(SeekFrom::Start(start_offset))
        .map_err(|source| read_error(path, source))?;
    let mut reader = BufReader::new(file);
    let mut records = Vec::new();
    let mut batch_bytes = 0usize;
    let mut position = start_offset;

    while records.len() < MAX_RAW_BATCH_RECORDS {
        let remaining = maximum_bytes - batch_bytes;
        let mut bytes = Vec::new();
        let mut bounded = (&mut reader).take(remaining as u64);
        let read = bounded
            .read_until(b'\n', &mut bytes)
            .map_err(|source| read_error(path, source))?;
        if read == 0 {
            break;
        }
        let ended_at_lf = bytes.last() == Some(&b'\n');
        let ended_at_eof = position + read as u64 == source_len;
        if read == remaining && !ended_at_lf && !ended_at_eof {
            if records.is_empty() {
                return Err(RawRecordError::RecordTooLarge {
                    start: position,
                    maximum: maximum_bytes,
                });
            }
            break;
        }

        let range_start = position;
        position += read as u64;
        records.push(RawRecord {
            range_start,
            range_end: position,
            bytes,
        });
        batch_bytes += read;
        if batch_bytes == maximum_bytes {
            break;
        }
    }

    if records.is_empty() {
        return Ok(None);
    }
    Ok(Some(finish_batch(records)))
}

fn ensure_lf_boundary(
    path: &Path,
    file: &mut File,
    start_offset: u64,
) -> Result<(), RawRecordError> {
    if start_offset == 0 {
        return Ok(());
    }
    file.seek(SeekFrom::Start(start_offset - 1))
        .map_err(|source| read_error(path, source))?;
    let mut preceding = [0u8; 1];
    file.read_exact(&mut preceding)
        .map_err(|source| read_error(path, source))?;
    if preceding[0] != b'\n' {
        return Err(RawRecordError::UnalignedStart {
            start: start_offset,
        });
    }
    Ok(())
}

fn read_whole_document(
    path: &Path,
    mut file: File,
    start_offset: u64,
    length: u64,
) -> Result<Option<RawRecordBatch>, RawRecordError> {
    if start_offset != 0 {
        return Err(RawRecordError::WholeDocumentNonZeroStart);
    }
    if length == 0 {
        return Ok(None);
    }
    if length > MAX_RAW_BATCH_BYTES as u64 {
        return Err(RawRecordError::RecordTooLarge {
            start: 0,
            maximum: MAX_RAW_BATCH_BYTES,
        });
    }
    let mut bytes = Vec::with_capacity(length as usize);
    file.read_to_end(&mut bytes)
        .map_err(|source| read_error(path, source))?;
    Ok(Some(RawRecordBatch {
        range_start: 0,
        range_end: length,
        records: vec![RawRecord {
            range_start: 0,
            range_end: length,
            bytes,
        }],
    }))
}

fn finish_batch(records: Vec<RawRecord>) -> RawRecordBatch {
    debug_assert!(!records.is_empty());
    RawRecordBatch {
        range_start: records.first().unwrap().range_start,
        range_end: records.last().unwrap().range_end,
        records,
    }
}

fn read_error(path: &Path, source: std::io::Error) -> RawRecordError {
    RawRecordError::Read {
        path: path.to_path_buf(),
        source,
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;

    fn collect_fixture(path: &Path, framing: RawSourceFraming) -> Vec<RawRecordBatch> {
        let mut batches = Vec::new();
        let mut offset = 0;
        while let Some(batch) = read_next_raw_batch(path, framing, offset).unwrap() {
            offset = batch.range_end;
            batches.push(batch);
            if framing == RawSourceFraming::WholeDocument {
                break;
            }
        }
        batches
    }

    #[test]
    fn lf_framing_preserves_crlf_lf_blank_and_unterminated_bytes() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let bytes = b"one\r\ntwo\n\nlast\r";
        fs::write(tmp.path(), bytes).unwrap();
        let batches = collect_fixture(tmp.path(), RawSourceFraming::LfDelimited);
        let records: Vec<&[u8]> = batches
            .iter()
            .flat_map(|batch| batch.records.iter().map(|record| record.bytes.as_slice()))
            .collect();
        assert_eq!(
            records,
            vec![
                b"one\r\n".as_slice(),
                b"two\n".as_slice(),
                b"\n".as_slice(),
                b"last\r".as_slice(),
            ]
        );
        assert_eq!(batches[0].range_end, bytes.len() as u64);
    }

    #[test]
    fn large_source_is_consumed_one_batch_at_a_time() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let mut bytes = vec![b'x', b'\n'].repeat(MAX_RAW_BATCH_RECORDS * 2);
        bytes.extend_from_slice(b"tail\n");
        fs::write(tmp.path(), &bytes).unwrap();

        let first = read_next_raw_batch(tmp.path(), RawSourceFraming::LfDelimited, 0)
            .unwrap()
            .unwrap();
        assert_eq!(first.records.len(), MAX_RAW_BATCH_RECORDS);
        assert_eq!(first.range_end, (MAX_RAW_BATCH_RECORDS * 2) as u64);

        let second =
            read_next_raw_batch(tmp.path(), RawSourceFraming::LfDelimited, first.range_end)
                .unwrap()
                .unwrap();
        assert_eq!(second.records.len(), MAX_RAW_BATCH_RECORDS);
        assert_eq!(second.range_start, first.range_end);

        let third =
            read_next_raw_batch(tmp.path(), RawSourceFraming::LfDelimited, second.range_end)
                .unwrap()
                .unwrap();
        assert_eq!(third.records.len(), 1);
        assert_eq!(third.records[0].bytes, b"tail\n");
        assert!(
            read_next_raw_batch(tmp.path(), RawSourceFraming::LfDelimited, third.range_end)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn byte_limit_starts_a_new_batch_without_splitting_a_record() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let mut bytes = vec![b'a'; MAX_RAW_BATCH_BYTES - 1];
        bytes.push(b'\n');
        bytes.extend_from_slice(b"b\n");
        fs::write(tmp.path(), bytes).unwrap();
        let first = read_next_raw_batch(tmp.path(), RawSourceFraming::LfDelimited, 0)
            .unwrap()
            .unwrap();
        assert_eq!(first.byte_len(), MAX_RAW_BATCH_BYTES as u64);
        let second =
            read_next_raw_batch(tmp.path(), RawSourceFraming::LfDelimited, first.range_end)
                .unwrap()
                .unwrap();
        assert_eq!(second.records[0].bytes, b"b\n");
    }

    #[test]
    fn oversized_record_is_rejected_instead_of_split() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        fs::write(tmp.path(), vec![b'x'; MAX_RAW_BATCH_BYTES + 1]).unwrap();
        let error = read_next_raw_batch(tmp.path(), RawSourceFraming::LfDelimited, 0).unwrap_err();
        assert!(matches!(error, RawRecordError::RecordTooLarge { .. }));
    }

    #[test]
    fn whole_document_is_one_exact_record() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let bytes = b"{\r\n  \"message\": \"exact\\ntext\"\r\n}";
        fs::write(tmp.path(), bytes).unwrap();
        let batch = read_next_raw_batch(tmp.path(), RawSourceFraming::WholeDocument, 0)
            .unwrap()
            .unwrap();
        assert_eq!(batch.records.len(), 1);
        assert_eq!(batch.records[0].bytes, bytes);
    }

    #[test]
    fn framing_from_offset_preserves_absolute_ranges() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        fs::write(tmp.path(), b"old\nnew\r\n").unwrap();
        let batch = read_next_raw_batch(tmp.path(), RawSourceFraming::LfDelimited, 4)
            .unwrap()
            .unwrap();
        assert_eq!(batch.range_start, 4);
        assert_eq!(batch.range_end, 9);
        assert_eq!(batch.records[0].bytes, b"new\r\n");
    }

    #[test]
    fn framing_rejects_a_partial_record_start() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        fs::write(tmp.path(), b"first\nsecond\n").unwrap();
        let error = read_next_raw_batch(tmp.path(), RawSourceFraming::LfDelimited, 2).unwrap_err();
        assert!(matches!(error, RawRecordError::UnalignedStart { start: 2 }));
    }
}
