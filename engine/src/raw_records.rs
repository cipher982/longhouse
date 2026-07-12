//! Byte-exact, file-backed framing for storage-v2 raw source records.
//!
//! This path never consumes parsed or redacted `source_lines`. JSONL-like
//! sources are split only after LF, preserving every byte (including CRLF and
//! an unterminated final record). Whole-document providers produce one record.

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
    #[error("raw record at byte {start} is {length} bytes; maximum is {maximum}")]
    RecordTooLarge {
        start: u64,
        length: usize,
        maximum: usize,
    },
}

pub(crate) fn frame_raw_file(
    path: &Path,
    framing: RawSourceFraming,
    start_offset: u64,
) -> Result<Vec<RawRecordBatch>, RawRecordError> {
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
        RawSourceFraming::LfDelimited => frame_lf_delimited(path, file, start_offset),
        RawSourceFraming::WholeDocument => frame_whole_document(path, file, start_offset, length),
    }
}

fn frame_lf_delimited(
    path: &Path,
    mut file: File,
    start_offset: u64,
) -> Result<Vec<RawRecordBatch>, RawRecordError> {
    if start_offset > 0 {
        file.seek(SeekFrom::Start(start_offset - 1))
            .map_err(|source| RawRecordError::Read {
                path: path.to_path_buf(),
                source,
            })?;
        let mut preceding = [0u8; 1];
        file.read_exact(&mut preceding)
            .map_err(|source| RawRecordError::Read {
                path: path.to_path_buf(),
                source,
            })?;
        if preceding[0] != b'\n' {
            return Err(RawRecordError::UnalignedStart {
                start: start_offset,
            });
        }
    }
    file.seek(SeekFrom::Start(start_offset))
        .map_err(|source| RawRecordError::Read {
            path: path.to_path_buf(),
            source,
        })?;
    let mut reader = BufReader::new(file);
    let mut batches = Vec::new();
    let mut records = Vec::new();
    let mut batch_bytes = 0usize;
    let mut position = start_offset;

    loop {
        let mut bytes = Vec::new();
        let read = reader
            .read_until(b'\n', &mut bytes)
            .map_err(|source| RawRecordError::Read {
                path: path.to_path_buf(),
                source,
            })?;
        if read == 0 {
            break;
        }
        if read > MAX_RAW_BATCH_BYTES {
            return Err(RawRecordError::RecordTooLarge {
                start: position,
                length: read,
                maximum: MAX_RAW_BATCH_BYTES,
            });
        }
        if !records.is_empty()
            && (batch_bytes + read > MAX_RAW_BATCH_BYTES || records.len() == MAX_RAW_BATCH_RECORDS)
        {
            batches.push(finish_batch(std::mem::take(&mut records)));
            batch_bytes = 0;
        }
        let range_start = position;
        position += read as u64;
        records.push(RawRecord {
            range_start,
            range_end: position,
            bytes,
        });
        batch_bytes += read;
    }
    if !records.is_empty() {
        batches.push(finish_batch(records));
    }
    Ok(batches)
}

fn frame_whole_document(
    path: &Path,
    mut file: File,
    start_offset: u64,
    length: u64,
) -> Result<Vec<RawRecordBatch>, RawRecordError> {
    if start_offset != 0 {
        return Err(RawRecordError::WholeDocumentNonZeroStart);
    }
    if length == 0 {
        return Ok(Vec::new());
    }
    let length_usize = usize::try_from(length).unwrap_or(usize::MAX);
    if length_usize > MAX_RAW_BATCH_BYTES {
        return Err(RawRecordError::RecordTooLarge {
            start: 0,
            length: length_usize,
            maximum: MAX_RAW_BATCH_BYTES,
        });
    }
    let mut bytes = Vec::with_capacity(length_usize);
    file.read_to_end(&mut bytes)
        .map_err(|source| RawRecordError::Read {
            path: path.to_path_buf(),
            source,
        })?;
    Ok(vec![RawRecordBatch {
        range_start: 0,
        range_end: length,
        records: vec![RawRecord {
            range_start: 0,
            range_end: length,
            bytes,
        }],
    }])
}

fn finish_batch(records: Vec<RawRecord>) -> RawRecordBatch {
    debug_assert!(!records.is_empty());
    RawRecordBatch {
        range_start: records.first().unwrap().range_start,
        range_end: records.last().unwrap().range_end,
        records,
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;

    #[test]
    fn lf_framing_preserves_crlf_lf_blank_and_unterminated_bytes() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let bytes = b"one\r\ntwo\n\nlast\r";
        fs::write(tmp.path(), bytes).unwrap();

        let batches = frame_raw_file(tmp.path(), RawSourceFraming::LfDelimited, 0).unwrap();
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
        assert_eq!(batches[0].range_start, 0);
        assert_eq!(batches[0].range_end, bytes.len() as u64);
        assert_eq!(batches[0].byte_len(), bytes.len() as u64);
    }

    #[test]
    fn batches_are_half_open_and_bounded_by_record_count() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let mut bytes = vec![b'x', b'\n'].repeat(MAX_RAW_BATCH_RECORDS);
        bytes.extend_from_slice(b"tail\n");
        fs::write(tmp.path(), &bytes).unwrap();

        let batches = frame_raw_file(tmp.path(), RawSourceFraming::LfDelimited, 0).unwrap();
        assert_eq!(batches.len(), 2);
        assert_eq!(batches[0].records.len(), MAX_RAW_BATCH_RECORDS);
        assert_eq!(batches[0].range_start, 0);
        assert_eq!(batches[0].range_end, (MAX_RAW_BATCH_RECORDS * 2) as u64);
        assert_eq!(batches[1].range_start, batches[0].range_end);
        assert_eq!(batches[1].range_end, bytes.len() as u64);
        assert!(batches.iter().all(|batch| {
            batch.byte_len() <= MAX_RAW_BATCH_BYTES as u64
                && batch.records.len() <= MAX_RAW_BATCH_RECORDS
        }));
    }

    #[test]
    fn byte_limit_starts_a_new_batch_without_splitting_a_record() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let first = vec![b'a'; MAX_RAW_BATCH_BYTES - 1];
        let mut bytes = first;
        bytes.push(b'\n');
        bytes.extend_from_slice(b"b\n");
        fs::write(tmp.path(), bytes).unwrap();

        let batches = frame_raw_file(tmp.path(), RawSourceFraming::LfDelimited, 0).unwrap();
        assert_eq!(batches.len(), 2);
        assert_eq!(batches[0].byte_len(), MAX_RAW_BATCH_BYTES as u64);
        assert_eq!(batches[1].records[0].bytes, b"b\n");
    }

    #[test]
    fn oversized_record_is_rejected_instead_of_split() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        fs::write(tmp.path(), vec![b'x'; MAX_RAW_BATCH_BYTES + 1]).unwrap();
        let error = frame_raw_file(tmp.path(), RawSourceFraming::LfDelimited, 0).unwrap_err();
        assert!(matches!(error, RawRecordError::RecordTooLarge { .. }));
    }

    #[test]
    fn whole_document_is_one_exact_record() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let bytes = b"{\r\n  \"message\": \"exact\\ntext\"\r\n}";
        fs::write(tmp.path(), bytes).unwrap();
        let batches = frame_raw_file(tmp.path(), RawSourceFraming::WholeDocument, 0).unwrap();
        assert_eq!(batches.len(), 1);
        assert_eq!(batches[0].records.len(), 1);
        assert_eq!(batches[0].records[0].bytes, bytes);
        assert_eq!(batches[0].range_end, bytes.len() as u64);
    }

    #[test]
    fn framing_from_offset_preserves_absolute_ranges() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        fs::write(tmp.path(), b"old\nnew\r\n").unwrap();
        let batches = frame_raw_file(tmp.path(), RawSourceFraming::LfDelimited, 4).unwrap();
        assert_eq!(batches[0].range_start, 4);
        assert_eq!(batches[0].range_end, 9);
        assert_eq!(batches[0].records[0].bytes, b"new\r\n");
    }

    #[test]
    fn framing_rejects_a_partial_record_start() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        fs::write(tmp.path(), b"first\nsecond\n").unwrap();
        let error = frame_raw_file(tmp.path(), RawSourceFraming::LfDelimited, 2).unwrap_err();
        assert!(matches!(error, RawRecordError::UnalignedStart { start: 2 }));
    }
}
