//! Read-only, source-faithful Cursor `store.db` capture.
//!
//! Cursor's durable transcript is a content-addressed SQLite store. This
//! module deliberately captures the whole observed logical store before any
//! renderer decides which message or block shapes it understands. The emitted
//! records are deterministic wrappers around exact SQLite values, so unknown
//! Cursor material remains re-renderable evidence instead of a decode failure.

#![allow(dead_code)] // Foundation is wired into storage-v2 shipping in the next slice.

use std::path::Path;
use std::time::Duration;

use anyhow::{bail, Context, Result};
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use rusqlite::types::ValueRef;
use rusqlite::{Connection, OpenFlags};
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::state::file_identity::identity_from_metadata;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CursorStoreSnapshot {
    pub conversation_uuid: String,
    pub root_blob_id: String,
    pub created_at_ms: Option<i64>,
    pub meta_rows: Vec<CursorStoreMetaRow>,
    pub blob_rows: Vec<CursorStoreBlobRow>,
    pub root_message_blob_ids: RootMessageBlobIds,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CursorStoreMetaRow {
    pub key: String,
    pub value_bytes: Vec<u8>,
    pub value_storage_class: SqliteStorageClass,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CursorStoreBlobRow {
    pub id: String,
    pub data_bytes: Vec<u8>,
    pub data_storage_class: SqliteStorageClass,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SqliteStorageClass {
    Text,
    Blob,
}

impl SqliteStorageClass {
    fn as_str(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Blob => "blob",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RootMessageBlobIds {
    Parsed(Vec<String>),
    Unavailable { reason: String },
}

#[derive(Debug, Clone)]
pub struct CursorStoreRawSnapshot {
    pub conversation_uuid: String,
    pub root_blob_id: String,
    pub store_incarnation: String,
    pub created_at_ms: Option<i64>,
    pub root_message_blob_ids: RootMessageBlobIds,
    pub source_revision: String,
    pub records: Vec<Vec<u8>>,
}

#[derive(Serialize)]
struct RawMetaRecord<'a> {
    v: u8,
    kind: &'static str,
    conversation_uuid: &'a str,
    store_incarnation: &'a str,
    meta_key: &'a str,
    meta_value_bytes_b64: String,
    meta_value_storage_class: &'static str,
}

#[derive(Serialize)]
struct RawBlobRecord<'a> {
    v: u8,
    kind: &'static str,
    conversation_uuid: &'a str,
    store_incarnation: &'a str,
    blob_id: &'a str,
    blob_bytes_b64: String,
    blob_storage_class: &'static str,
}

#[derive(Serialize)]
struct RawRootObservationRecord<'a> {
    v: u8,
    kind: &'static str,
    conversation_uuid: &'a str,
    store_incarnation: &'a str,
    root_blob_id: &'a str,
    root_blob_bytes_b64: String,
    root_blob_storage_class: &'static str,
}

/// Read the Cursor store through SQLite's WAL-aware read-only URI mode.
///
/// This never checkpoints or changes Cursor's database. A malformed root is a
/// renderer concern, not a source-capture failure: the raw meta/blob rows are
/// still returned with an explicit ordering gap.
pub fn read_cursor_store(path: &Path) -> Result<CursorStoreSnapshot> {
    let mut conn = open_readonly(path)?;
    // Keep `meta` and `blobs` in one WAL-consistent snapshot. In autocommit
    // mode SQLite could otherwise advance between the two SELECTs while
    // cursor-agent is writing a new root.
    let snapshot = conn.transaction()?;
    let meta_rows = read_meta_rows(&snapshot)?;
    let root_meta = meta_rows
        .iter()
        .find(|row| row.key == "0")
        .context("Cursor store has no meta['0'] root metadata")?;
    let root_metadata = decode_root_metadata(&root_meta.value_bytes)?;
    let conversation_uuid = required_string(&root_metadata, "agentId")?;
    let root_blob_id = required_string(&root_metadata, "latestRootBlobId")?;
    let created_at_ms = root_metadata.get("createdAt").and_then(Value::as_i64);
    let blob_rows = read_blob_rows(&snapshot)?;
    let root_blob = blob_rows
        .iter()
        .find(|row| row.id == root_blob_id)
        .with_context(|| format!("Cursor root blob {root_blob_id} is missing from blobs"))?;
    let root_message_blob_ids = match parse_root_message_blob_ids(&root_blob.data_bytes) {
        Ok(ids) => RootMessageBlobIds::Parsed(ids),
        Err(error) => RootMessageBlobIds::Unavailable {
            reason: error.to_string(),
        },
    };
    snapshot.commit()?;
    Ok(CursorStoreSnapshot {
        conversation_uuid,
        root_blob_id,
        created_at_ms,
        meta_rows,
        blob_rows,
        root_message_blob_ids,
    })
}

/// Return deterministic raw storage-v2 records for all observed Cursor data.
///
/// Root observation is intentionally separate from its generic blob record:
/// the same exact root bytes are durable evidence of the ordering snapshot
/// without assigning meaning to unknown protobuf fields.
pub fn cursor_store_raw_snapshot(path: &Path) -> Result<CursorStoreRawSnapshot> {
    let metadata = path
        .metadata()
        .with_context(|| format!("reading Cursor store metadata {}", path.display()))?;
    let store_incarnation = identity_from_metadata(&metadata)
        .context("Cursor store has no stable file incarnation")?;
    let snapshot = read_cursor_store(path)?;
    let root_blob = snapshot
        .blob_rows
        .iter()
        .find(|row| row.id == snapshot.root_blob_id)
        .expect("read_cursor_store verifies the root blob exists");
    let mut records = Vec::with_capacity(snapshot.meta_rows.len() + snapshot.blob_rows.len() + 1);
    for row in &snapshot.meta_rows {
        records.push(serde_json::to_vec(&RawMetaRecord {
            v: 1,
            kind: "meta",
            conversation_uuid: &snapshot.conversation_uuid,
            store_incarnation: &store_incarnation,
            meta_key: &row.key,
            meta_value_bytes_b64: BASE64_STANDARD.encode(&row.value_bytes),
            meta_value_storage_class: row.value_storage_class.as_str(),
        })?);
    }
    for row in &snapshot.blob_rows {
        records.push(serde_json::to_vec(&RawBlobRecord {
            v: 1,
            kind: "blob",
            conversation_uuid: &snapshot.conversation_uuid,
            store_incarnation: &store_incarnation,
            blob_id: &row.id,
            blob_bytes_b64: BASE64_STANDARD.encode(&row.data_bytes),
            blob_storage_class: row.data_storage_class.as_str(),
        })?);
    }
    records.push(serde_json::to_vec(&RawRootObservationRecord {
        v: 1,
        kind: "root_observation",
        conversation_uuid: &snapshot.conversation_uuid,
        store_incarnation: &store_incarnation,
        root_blob_id: &snapshot.root_blob_id,
        root_blob_bytes_b64: BASE64_STANDARD.encode(&root_blob.data_bytes),
        root_blob_storage_class: root_blob.data_storage_class.as_str(),
    })?);
    Ok(CursorStoreRawSnapshot {
        conversation_uuid: snapshot.conversation_uuid,
        root_blob_id: snapshot.root_blob_id,
        store_incarnation,
        created_at_ms: snapshot.created_at_ms,
        root_message_blob_ids: snapshot.root_message_blob_ids,
        source_revision: revision_for_records(&records),
        records,
    })
}

pub fn cursor_opaque_source_id(conversation_uuid: &str) -> String {
    format!("cursor-store-v1:{conversation_uuid}")
}

pub fn is_cursor_store_database_path(path: &Path) -> bool {
    path.file_name()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value == "store.db")
}

pub fn longhouse_session_id_for_cursor(conversation_uuid: &str) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("cursor:{conversation_uuid}").as_bytes(),
    )
    .to_string()
}

fn open_readonly(path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(
        sqlite_readonly_uri(path),
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .with_context(|| format!("opening Cursor store {}", path.display()))?;
    conn.busy_timeout(Duration::from_secs(2))?;
    Ok(conn)
}

fn sqlite_readonly_uri(path: &Path) -> String {
    let path = path.to_string_lossy();
    let mut uri = String::from("file:");
    for byte in path.as_bytes() {
        match *byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'/' | b'.' | b'-' | b'_' => {
                uri.push(char::from(*byte));
            }
            _ => uri.push_str(&format!("%{byte:02X}")),
        }
    }
    uri.push_str("?mode=ro");
    uri
}

fn read_meta_rows(conn: &Connection) -> Result<Vec<CursorStoreMetaRow>> {
    let mut statement = conn.prepare("SELECT key, value FROM meta ORDER BY key")?;
    let rows = statement.query_map([], |row| {
        let (value_bytes, value_storage_class) = sqlite_bytes(row.get_ref(1)?)?;
        Ok(CursorStoreMetaRow {
            key: row.get(0)?,
            value_bytes,
            value_storage_class,
        })
    })?;
    rows.collect::<std::result::Result<Vec<_>, _>>()
        .context("reading Cursor meta rows")
}

fn read_blob_rows(conn: &Connection) -> Result<Vec<CursorStoreBlobRow>> {
    let mut statement = conn.prepare("SELECT id, data FROM blobs ORDER BY id")?;
    let rows = statement.query_map([], |row| {
        let (data_bytes, data_storage_class) = sqlite_bytes(row.get_ref(1)?)?;
        Ok(CursorStoreBlobRow {
            id: row.get(0)?,
            data_bytes,
            data_storage_class,
        })
    })?;
    rows.collect::<std::result::Result<Vec<_>, _>>()
        .context("reading Cursor blob rows")
}

fn sqlite_bytes(value: ValueRef<'_>) -> rusqlite::Result<(Vec<u8>, SqliteStorageClass)> {
    match value {
        ValueRef::Text(bytes) => Ok((bytes.to_vec(), SqliteStorageClass::Text)),
        ValueRef::Blob(bytes) => Ok((bytes.to_vec(), SqliteStorageClass::Blob)),
        other => Err(rusqlite::Error::FromSqlConversionFailure(
            1,
            other.data_type(),
            "Cursor store value must be TEXT or BLOB".into(),
        )),
    }
}

fn decode_root_metadata(value: &[u8]) -> Result<Value> {
    let decoded = decode_hex(value).context("decoding Cursor meta['0'] hex payload")?;
    serde_json::from_slice(&decoded).context("parsing Cursor meta['0'] JSON payload")
}

fn required_string(value: &Value, key: &str) -> Result<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .filter(|value| !value.is_empty())
        .with_context(|| format!("Cursor meta['0'] is missing {key}"))
}

fn decode_hex(value: &[u8]) -> Result<Vec<u8>> {
    if value.len() % 2 != 0 {
        bail!("hex payload has odd length");
    }
    value
        .chunks_exact(2)
        .map(|chunk| {
            let high = hex_nibble(chunk[0])?;
            let low = hex_nibble(chunk[1])?;
            Ok((high << 4) | low)
        })
        .collect()
}

fn hex_nibble(byte: u8) -> Result<u8> {
    match byte {
        b'0'..=b'9' => Ok(byte - b'0'),
        b'a'..=b'f' => Ok(byte - b'a' + 10),
        b'A'..=b'F' => Ok(byte - b'A' + 10),
        _ => bail!("hex payload contains non-hex byte {byte}"),
    }
}

fn parse_root_message_blob_ids(bytes: &[u8]) -> Result<Vec<String>> {
    let mut index = 0usize;
    let mut ids = Vec::new();
    while index < bytes.len() {
        let key = read_varint(bytes, &mut index)?;
        let field_number = key >> 3;
        match key & 0x07 {
            0 => {
                read_varint(bytes, &mut index)?;
            }
            1 => skip(bytes, &mut index, 8)?,
            2 => {
                let length = usize::try_from(read_varint(bytes, &mut index)?)
                    .context("protobuf length exceeds usize")?;
                let value = take(bytes, &mut index, length)?;
                if field_number == 1 {
                    if value.len() != 32 {
                        bail!(
                            "Cursor root field 1 blob id has length {}, expected 32",
                            value.len()
                        );
                    }
                    ids.push(hex_bytes(value));
                }
            }
            5 => skip(bytes, &mut index, 4)?,
            wire => bail!("unsupported Cursor root protobuf wire type {wire}"),
        }
    }
    Ok(ids)
}

fn read_varint(bytes: &[u8], index: &mut usize) -> Result<u64> {
    let mut value = 0u64;
    for shift in (0..64).step_by(7) {
        let byte = *bytes.get(*index).context("truncated protobuf varint")?;
        *index += 1;
        value |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return Ok(value);
        }
    }
    bail!("protobuf varint exceeds 64 bits")
}

fn take<'a>(bytes: &'a [u8], index: &mut usize, length: usize) -> Result<&'a [u8]> {
    let end = index
        .checked_add(length)
        .context("protobuf length overflow")?;
    let value = bytes.get(*index..end).context("truncated protobuf field")?;
    *index = end;
    Ok(value)
}

fn skip(bytes: &[u8], index: &mut usize, length: usize) -> Result<()> {
    take(bytes, index, length)?;
    Ok(())
}

fn hex_bytes(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn revision_for_records(records: &[Vec<u8>]) -> String {
    let mut digest = Sha256::new();
    for record in records {
        digest.update((record.len() as u64).to_be_bytes());
        digest.update(record);
    }
    format!("{:x}", digest.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONVERSATION_ID: &str = "60bf2c11-01da-456e-8216-c5dbd2fa52b4";
    const ROOT_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const MESSAGE_ID: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    fn fixture(path: &Path) -> Connection {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB);
             CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);",
        )
        .unwrap();
        let root_metadata = format!(
            r#"{{"agentId":"{CONVERSATION_ID}","latestRootBlobId":"{ROOT_ID}","unknown":{{"preserved":true}}}}"#
        );
        let encoded_metadata: String = root_metadata
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?1, ?2)",
            rusqlite::params!["0", encoded_metadata],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?1, ?2)",
            rusqlite::params!["extra", vec![0, 0xff, 42]],
        )
        .unwrap();
        let mut root = vec![0x0a, 0x20];
        root.extend_from_slice(&[0xbb; 32]);
        root.extend_from_slice(&[0x10, 0x01]);
        conn.execute(
            "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
            rusqlite::params![ROOT_ID, root],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
            rusqlite::params![
                MESSAGE_ID,
                br#"{"role":"assistant","content":[{"type":"unknown","binary":true}]}"#
                    .to_vec()
            ],
        )
        .unwrap();
        conn
    }

    #[test]
    fn capture_preserves_all_meta_and_blob_bytes_in_stable_records() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("store.db");
        drop(fixture(&path));

        let first = cursor_store_raw_snapshot(&path).unwrap();
        let second = cursor_store_raw_snapshot(&path).unwrap();
        assert_eq!(first.source_revision, second.source_revision);
        assert_eq!(first.records, second.records);
        assert_eq!(first.records.len(), 5);

        let records: Vec<Value> = first
            .records
            .iter()
            .map(|record| serde_json::from_slice(record).unwrap())
            .collect();
        assert_eq!(records[0]["kind"], "meta");
        assert_eq!(records[0]["meta_key"], "0");
        assert_eq!(records[0]["meta_value_storage_class"], "text");
        assert_eq!(records[1]["meta_key"], "extra");
        assert_eq!(records[1]["meta_value_storage_class"], "blob");
        assert_eq!(records[2]["kind"], "blob");
        assert_eq!(records[4]["kind"], "root_observation");
        assert_eq!(records[0]["store_incarnation"], first.store_incarnation);
        let extra = BASE64_STANDARD
            .decode(records[1]["meta_value_bytes_b64"].as_str().unwrap())
            .unwrap();
        assert_eq!(extra, vec![0, 0xff, 42]);
        let message = records
            .iter()
            .find(|record| record["blob_id"] == MESSAGE_ID)
            .unwrap();
        assert_eq!(
            BASE64_STANDARD
                .decode(message["blob_bytes_b64"].as_str().unwrap())
                .unwrap(),
            br#"{"role":"assistant","content":[{"type":"unknown","binary":true}]}"#
        );
    }

    #[test]
    fn capture_reads_wal_without_checkpointing_or_writer_interference() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("store.db");
        let conn = fixture(&path);
        conn.pragma_update(None, "journal_mode", "WAL").unwrap();
        drop(conn);

        let writer = Connection::open(&path).unwrap();
        writer
            .execute_batch(
                "BEGIN IMMEDIATE; INSERT INTO meta (key, value) VALUES ('pending', X'01');",
            )
            .unwrap();

        let snapshot = read_cursor_store(&path).unwrap();
        assert!(snapshot.meta_rows.iter().all(|row| row.key != "pending"));
        assert!(path.with_file_name("store.db-wal").exists());
        writer.execute_batch("ROLLBACK").unwrap();
    }

    #[test]
    fn malformed_root_keeps_raw_capture_and_reports_ordering_gap() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("store.db");
        let conn = fixture(&path);
        conn.execute("UPDATE blobs SET data = X'0b' WHERE id = ?1", [ROOT_ID])
            .unwrap();
        drop(conn);

        let snapshot = read_cursor_store(&path).unwrap();
        assert!(matches!(
            snapshot.root_message_blob_ids,
            RootMessageBlobIds::Unavailable { .. }
        ));
        assert_eq!(cursor_store_raw_snapshot(&path).unwrap().records.len(), 5);
    }

    #[test]
    fn root_field_one_preserves_provider_order_without_interpreting_other_fields() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("store.db");
        drop(fixture(&path));

        let snapshot = read_cursor_store(&path).unwrap();
        assert_eq!(
            snapshot.root_message_blob_ids,
            RootMessageBlobIds::Parsed(vec![MESSAGE_ID.to_string()])
        );
    }
}
