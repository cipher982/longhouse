//! Parser-independent raw + parser-versioned render shipping for storage-v2.

use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{Cursor, Read};
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use chrono::{DateTime, Utc};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::cursor_store;
use crate::opencode_db;
use crate::pipeline::parser::{
    self, ParseResult, ParsedEvent, ParsedMediaObject, Role, SessionMetadata,
};
use crate::raw_records::{
    read_next_raw_batch_with_limits, RawRecordBatch, RawSourceFraming, MAX_RAW_BATCH_BYTES,
};
use crate::shipping::client::ShipperClient;
use crate::shipping::storage_v2::{
    StorageV2BodyEncoding, StorageV2Capabilities, StorageV2Envelope, StorageV2MediaRef,
    StorageV2ProviderFact, StorageV2Record, StorageV2SourceManifest,
};
use crate::shipping::storage_v2::{StorageV2Render, StorageV2RenderRecord, StorageV2SessionFacts};
use crate::state::cursor_store_records;
use crate::state::cursor_store_root;
use crate::state::file_identity::{
    cursor_fingerprint, file_identities_match, identity_from_metadata, rest_stamp,
    wal_database_stamp,
};
use crate::state::file_state::FileState;
use crate::state::pending_source_envelope::{self, PendingSourceEnvelope};
use crate::state::source_epoch::{self, SourceChangeHint, SourceEpochResolution, SourceLane};
use crate::storage_v2_contract::{self, EnvelopeIdentity, RangeKind};

mod cursor_acp_source;
mod cursor_store_source;
mod opencode_source;
mod pending;
mod prepare;
mod reconcile;
mod render;
mod ship;

pub(crate) use self::cursor_acp_source::*;
pub(crate) use self::cursor_store_source::*;
pub(crate) use self::opencode_source::*;
pub(crate) use self::pending::*;
pub(crate) use self::prepare::*;
use self::reconcile::*;
use self::render::*;
pub(crate) use self::ship::*;

pub(crate) const PARSER_REVISION: &str = "engine-parser-v2";
pub(crate) const ORDERING_REVISION: &str = "semantic-order-v2";
const OPENCODE_SESSION_PAGE_SIZE: usize = 64;
// v7: failed/aborted managed prose remains inspectable as abandoned output,
// without authorizing it as a committed head reply. Replay historical sources.
const CURSOR_PARSER_REVISION: &str = "cursor-store-render-v8-turn-outcome";
/// Blobs one Cursor capture reads from the hash-sorted store before it yields.
const CURSOR_BLOB_PAGE_ROWS: usize = 256;
const LIVE_TARGET_BATCH_BYTES: usize = 64 * 1024;
const BACKLOG_TARGET_BATCH_BYTES: usize = 2 * 1024 * 1024;

pub(crate) struct PreparedStorageV2Envelope {
    pub envelope: StorageV2Envelope,
    pub source_epoch: Uuid,
    pub range_start: u64,
    pub range_end: u64,
    pub event_count: usize,
    pub has_reply_evidence: bool,
    pub raw_bytes: u64,
    pub has_more: bool,
    pub media_objects: Vec<ParsedMediaObject>,
}

#[derive(Debug)]
pub(crate) struct StorageV2ShipOutcome {
    pub bytes_shipped: u64,
    pub events_shipped: usize,
    pub has_more: bool,
}

pub(crate) enum CursorPreparationOutcome {
    Envelope(PreparedStorageV2Envelope),
    Current,
    WaitingOnClaim,
    Continue,
}

/// The variant's name, for assertions that must say what they got instead.
#[cfg(test)]
fn outcome_name(outcome: &CursorPreparationOutcome) -> &'static str {
    match outcome {
        CursorPreparationOutcome::Envelope(_) => "Envelope",
        CursorPreparationOutcome::Current => "Current",
        CursorPreparationOutcome::WaitingOnClaim => "WaitingOnClaim",
        CursorPreparationOutcome::Continue => "Continue",
    }
}

#[derive(Debug)]
pub(crate) enum CursorStorageV2ShipResult {
    Shipped(StorageV2ShipOutcome),
    Current,
    WaitingOnClaim,
    Continue,
}

#[derive(Debug, Serialize, Deserialize)]
struct PersistedMediaObject {
    source_offset: u64,
    sha256: String,
    mime_type: String,
    byte_size: usize,
    original_chars: usize,
    original_line_sha256: String,
    data_b64: String,
}

#[derive(Debug, thiserror::Error)]
#[error("storage-v2 source preparation failed: {source:#}")]
pub(crate) struct StorageV2PreparationError {
    #[source]
    source: anyhow::Error,
}

#[derive(Debug, thiserror::Error)]
#[error("storage-v2 source {source_epoch} blocked ({kind}): {detail}")]
pub(crate) struct StorageV2SourceBlocked {
    pub source_epoch: Uuid,
    pub kind: String,
    pub detail: String,
    pub newly_blocked: bool,
}

fn preparation_result<T>(result: Result<T>) -> Result<T> {
    result.map_err(|source| StorageV2PreparationError { source }.into())
}

fn encode_zstd(bytes: &[u8], label: &str) -> Result<Vec<u8>> {
    zstd::stream::encode_all(Cursor::new(bytes), 1)
        .with_context(|| format!("compressing durable {label}"))
}

/// The request body to put on the wire for this host. Envelopes are stored as
/// zstd; a host that accepts zstd gets those exact bytes, so ambiguous outcomes
/// still retry byte-identical content. Only an identity-only host needs them
/// decompressed.
fn wire_body(
    pending: &PendingSourceEnvelope,
    capabilities: &StorageV2Capabilities,
) -> Result<Vec<u8>> {
    match capabilities.envelope_body_encoding() {
        StorageV2BodyEncoding::Zstd => Ok(pending.request_body_zstd.clone()),
        StorageV2BodyEncoding::Identity => {
            decode_zstd(&pending.request_body_zstd, "storage-v2 request body")
        }
    }
}

fn decode_zstd(bytes: &[u8], label: &str) -> Result<Vec<u8>> {
    zstd::stream::decode_all(Cursor::new(bytes))
        .with_context(|| format!("decompressing durable {label}"))
}

impl From<&ParsedMediaObject> for PersistedMediaObject {
    fn from(media: &ParsedMediaObject) -> Self {
        Self {
            source_offset: media.source_offset,
            sha256: media.sha256.clone(),
            mime_type: media.mime_type.clone(),
            byte_size: media.byte_size,
            original_chars: media.original_chars,
            original_line_sha256: media.original_line_sha256.clone(),
            data_b64: BASE64_STANDARD.encode(&media.bytes),
        }
    }
}

impl TryFrom<PersistedMediaObject> for ParsedMediaObject {
    type Error = anyhow::Error;

    fn try_from(media: PersistedMediaObject) -> Result<Self> {
        let bytes = BASE64_STANDARD
            .decode(&media.data_b64)
            .context("decoding durable storage-v2 media bytes")?;
        if bytes.len() != media.byte_size {
            anyhow::bail!("durable storage-v2 media byte size changed");
        }
        Ok(Self {
            source_offset: media.source_offset,
            sha256: media.sha256,
            mime_type: media.mime_type,
            byte_size: media.byte_size,
            original_chars: media.original_chars,
            original_line_sha256: media.original_line_sha256,
            bytes,
        })
    }
}

/// The legacy cursor's one job is to seed a source's first storage-v2 epoch,
/// so it is judged once, when the source has none. After that the epoch owns
/// the position (`observe_source` ignores this value for a source it already
/// tracks, and a replacement epoch starts at zero whatever it says). Judging it
/// on every scan proved nothing new, and for a source whose file was rewritten
/// in place the answer never changes: the stored identity names the old file,
/// so it warned "replaying from zero" on every pass, forever, while nothing was
/// replayed.
fn legacy_offset_for_adoption(
    conn: &Connection,
    provider: &str,
    opaque_source_id: &str,
    path_text: &str,
    path: &Path,
) -> Result<u64> {
    if source_epoch::active_source_epoch(conn, provider, opaque_source_id)?.is_some() {
        return Ok(0);
    }
    validated_legacy_offset(conn, path_text, path)
}

fn validated_legacy_offset(conn: &Connection, path_text: &str, path: &Path) -> Result<u64> {
    let file_state = FileState::new(conn);
    let offset = file_state.get_offset(path_text)?;
    if offset == 0 {
        return Ok(0);
    }
    let metadata = path
        .metadata()
        .with_context(|| format!("reading source metadata: {}", path.display()))?;
    let stored_identity = file_state.get_file_identity(path_text)?;
    let current_identity = identity_from_metadata(&metadata);
    let stored_fingerprint = file_state.get_acked_cursor_fingerprint(path_text)?;
    let current_fingerprint = cursor_fingerprint(path, offset);
    if file_identities_match(stored_identity.as_deref(), current_identity.as_deref())
        && stored_fingerprint == current_fingerprint
        && stored_fingerprint.is_some()
    {
        file_state.record_continuous_file_identity(path_text, current_identity.as_deref())?;
        return Ok(offset);
    }
    tracing::warn!(
        path = %path.display(),
        offset,
        stored_identity = ?stored_identity,
        current_identity = ?current_identity,
        "Legacy cursor lacks matching file identity and boundary proof; replaying storage-v2 source from zero"
    );
    Ok(0)
}

/// Durable lane position for a bound source path, without side effects.
///
/// Callers that need to know whether a live transcript is keeping up cannot
/// afford `observe_file`, which may rotate epochs. This reads the active epoch
/// and its durable position only.
pub(crate) fn durable_lane_position(
    conn: &Connection,
    provider: &str,
    canonical_path: &str,
) -> Result<Option<u64>> {
    let opaque = opaque_source_id(canonical_path);
    let Some(epoch) = crate::state::source_epoch::active_source_epoch(conn, provider, &opaque)?
    else {
        return Ok(None);
    };
    Ok(Some(crate::state::source_epoch::lane_position(
        conn,
        epoch,
        crate::state::source_epoch::SourceLane::Durable,
    )?))
}

pub(crate) fn opaque_source_id(path: &str) -> String {
    format!(
        "path-sha256:{}",
        hex_hash(Sha256::digest(path.as_bytes()).into())
    )
}

/// Cheap identity for the window between reading raw bytes and parsing them.
///
/// Length plus modification time, both from one stat. A rewrite that changes
/// the file at all moves the timestamp, and nanosecond resolution makes a
/// collision within the same stamp vanishingly unlikely; hashing the file
/// instead would cost a full read on every envelope.
fn source_stamp(path: &Path) -> Result<(u64, i128)> {
    let metadata =
        std::fs::metadata(path).with_context(|| format!("stamping source: {}", path.display()))?;
    let modified = metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|value| value.as_nanos() as i128)
        .unwrap_or(-1);
    Ok((metadata.len(), modified))
}

/// Content hashes of sources that were unchanged when hashed, by path.
static FILE_HASHES: std::sync::LazyLock<std::sync::Mutex<HashMap<PathBuf, (String, String)>>> =
    std::sync::LazyLock::new(|| std::sync::Mutex::new(HashMap::new()));

#[cfg(test)]
thread_local! {
    /// Whole-file reads `hash_file` made on this thread, so a test can say a
    /// pass over an unchanged file did not make one.
    static FILE_HASH_READS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

/// The source count is small and stable; the bound only stops a machine that
/// churns through paths from growing the map without limit.
const MAX_CACHED_FILE_HASHES: usize = 50_000;

/// The hash of a whole file, re-read only when the file changed.
///
/// A source whose revision is its content hash is one a provider rewrites in
/// place, so it cannot be told apart from its last look by length alone. Every
/// scan pass used to read each such file whole to learn it had not moved. The
/// stat stamp says the same thing for free: it is taken before the read and
/// never trusts a file written in the last couple of seconds, so a write that
/// lands during or after a hash changes what the next call sees. The copy lives
/// in this process, so the first pass after a restart reads every file once.
fn hash_file(path: &Path) -> Result<String> {
    let stamp = std::fs::metadata(path)
        .ok()
        .and_then(|metadata| rest_stamp(&metadata));
    if let (Some(stamp), Ok(hashes)) = (stamp.as_deref(), FILE_HASHES.lock()) {
        if let Some((known_stamp, hash)) = hashes.get(path) {
            if known_stamp == stamp {
                return Ok(hash.clone());
            }
        }
    }
    #[cfg(test)]
    FILE_HASH_READS.with(|reads| reads.set(reads.get() + 1));
    let bytes = std::fs::read(path)
        .with_context(|| format!("reading source revision: {}", path.display()))?;
    let hash = hex_hash(Sha256::digest(bytes).into());
    if let (Some(stamp), Ok(mut hashes)) = (stamp, FILE_HASHES.lock()) {
        if hashes.len() >= MAX_CACHED_FILE_HASHES && !hashes.contains_key(path) {
            hashes.clear();
        }
        hashes.insert(path.to_path_buf(), (stamp, hash.clone()));
    }
    Ok(hash)
}

/// Revision signal for a Pi-lineage JSONL archive (Pi and OMP).
///
/// The signal has one hard constraint: it must be **invariant under append**.
/// A revision change ends the epoch and re-ships the file from the start, so a
/// signal that moved on every appended message would re-upload the whole
/// transcript on every message. It must still move when the provider *rewrites*
/// the file in place, which OMP does at least for its title slot and may do for
/// ordinary records.
///
/// Both are satisfied by hashing a bounded head: once a file is longer than the
/// cut, an append never touches those bytes, while an in-place rewrite that
/// preserves length has to change something in the head. A file shorter than
/// the cut hashes its first complete line instead, which an append likewise
/// cannot change, and reports no signal at all until that line exists.
///
/// Residual, stated rather than hidden: a same-size rewrite confined to the
/// middle of a large file is not visible to this signal. Length still catches
/// shrink and truncate-then-regrow, and the shipped envelope always carries the
/// bytes it read, so the exposure is a rewrite we do not notice rather than
/// bytes we mis-attribute.
fn pi_lineage_source_revision(path: &Path) -> Result<Option<String>> {
    const PREFIX_BYTES: usize = 4096;
    let metadata = std::fs::metadata(path)
        .with_context(|| format!("reading source revision: {}", path.display()))?;
    let mut file =
        File::open(path).with_context(|| format!("reading source revision: {}", path.display()))?;
    let mut bytes = vec![0_u8; PREFIX_BYTES];
    let read = file.read(&mut bytes)?;
    let head = &bytes[..read];
    let mut hasher = Sha256::new();
    if metadata.len() > PREFIX_BYTES as u64 {
        hasher.update(head);
    } else {
        let Some(end) = head.iter().position(|byte| *byte == b'\n') else {
            return Ok(None);
        };
        hasher.update(&head[..=end]);
    }
    Ok(Some(hex_hash(hasher.finalize().into())))
}

/// Cursor's agent transcript JSONL is a live provider-owned projection rather
/// than an append-only file. Cursor may rewrite an existing line while a turn
/// is still in flight, so a byte offset that was valid for the previous file
/// contents can land in the middle of a different line. Source revisions make
/// that rewrite an explicit source epoch and restart framing at byte zero.
/// The projection loses its render only when the authoritative source for the
/// same conversation actually exists. A conversation Cursor never gave a
/// `store.db` has no other source, and suppressing its render would leave the
/// session with no current generation rather than a lossy one.
fn cursor_projection_is_raw_only(provider: &str, path: &Path) -> bool {
    cursor_agent_transcript_conversation_id(provider, path).is_some_and(|conversation_id| {
        crate::cursor_visibility::cursor_store_exists(&conversation_id)
    })
}

fn pending_carries_render(
    pending: &pending_source_envelope::PendingSourceEnvelope,
) -> Result<bool> {
    Ok(pending_to_prepared(pending.clone())?
        .envelope
        .render
        .is_some())
}

fn is_cursor_agent_transcript_path(provider: &str, path: &Path) -> bool {
    provider.eq_ignore_ascii_case("cursor")
        && path
            .components()
            .any(|component| component.as_os_str() == "agent-transcripts")
}

fn cursor_agent_transcript_conversation_id(provider: &str, path: &Path) -> Option<String> {
    if !is_cursor_agent_transcript_path(provider, path) {
        return None;
    }
    path.file_stem()
        .and_then(|value| value.to_str())
        .and_then(|value| Uuid::parse_str(value).ok())
        .map(|value| value.to_string())
}

fn hex_hash(hash: [u8; 32]) -> String {
    hash.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {

    #[test]
    fn replay_opens_a_replacement_epoch_so_the_host_stops_deduplicating() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("transcript.jsonl");
        fs::write(
            &path,
            b"{\"type\":\"user\",\"uuid\":\"019c638d-0000-0000-0000-0000000000aa\",\"timestamp\":\"2026-01-01T00:00:00Z\",\"message\":{\"content\":\"hi\"},\"cwd\":\"/tmp/proj\"}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        // Nothing shipped yet: a normal ship already covers this source.
        assert_eq!(
            replay_file_source(&mut conn, &path, "claude").unwrap(),
            None
        );

        let first = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        let original_epoch = first.source_epoch;

        // Rewinding the local cursor alone would re-send a range the host has
        // already accepted. A replacement epoch is what makes the same bytes
        // land, which is why replay mints one.
        let replayed = replay_file_source(&mut conn, &path, "claude")
            .unwrap()
            .expect("a shipped source yields a replacement epoch");
        assert_ne!(replayed, original_epoch);

        // What matters is that the source becomes shippable again from the
        // start, carrying the facts the parser now recovers. Without the
        // replay this call returns None, because the lane sits at EOF.
        let after = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .expect("replay makes an exhausted source shippable again");
        assert_eq!(after.envelope.range_start, 0);
        assert_eq!(after.envelope.session.cwd.as_deref(), Some("/tmp/proj"));
        assert_eq!(after.envelope.session.project.as_deref(), Some("proj"));
    }

    #[test]
    fn omp_partial_header_is_fenced_before_shadow_source_epoch_creation() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("reserved.jsonl");
        fs::write(&path, b"{\"type\":\"title\",\"v\":1,\"title\":\"early\"}\n").unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let managed_session_id = "018f0c3a-7b2d-7f10-8a11-123456789abe";

        assert!(prepare_next_envelope(
            &mut conn,
            &capabilities(),
            &path,
            "omp",
            Some(managed_session_id),
        )
        .unwrap()
        .is_none());
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM source_epoch_registry", [], |row| row
                .get::<_, i64>(
                0
            ),)
                .unwrap(),
            0
        );
    }

    #[test]
    fn an_uppercase_claude_session_id_is_canonicalised() {
        // Claude names some transcripts with an uppercase UUID and the
        // unmanaged path takes the session id from that file stem. The Runtime
        // Host rejects anything that is not byte-identical to the lowercase
        // rendering, so those envelopes 422'd forever
        // (server/zerg/routers/agents_storage_v2.py:213-220).
        assert_eq!(
            canonical_session_id("8D188FD9-348C-4D1D-996B-86C8FCE02F04".to_string()),
            "8d188fd9-348c-4d1d-996b-86c8fce02f04"
        );
        // Already canonical stays byte-identical.
        assert_eq!(
            canonical_session_id("642eef6a-01b1-4054-86d7-30ed152ac0b9".to_string()),
            "642eef6a-01b1-4054-86d7-30ed152ac0b9"
        );
        // Not every provider-native id is a UUID. Lowercasing an opaque
        // identifier would change which session it names.
        assert_eq!(
            canonical_session_id("ses_Longhouse_E2E".to_string()),
            "ses_Longhouse_E2E"
        );
    }
    use std::fs;
    use std::io::Write;
    use std::sync::{Arc, Mutex};

    use rusqlite::params;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    use super::*;
    use crate::config::ShipperConfig;
    use crate::pipeline::compressor::CompressionAlgo;
    use crate::shipping::client::ShipperClient;
    use crate::state::db::open_db;
    use crate::state::file_state::FileState;
    use crate::state::wal_window::WalWindow;

    const CURSOR_CONVERSATION_ID: &str = "60bf2c11-01da-456e-8216-c5dbd2fa52b4";
    const CURSOR_ROOT_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const CURSOR_ROOT_B: &str = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
    const CURSOR_ROOT_C: &str = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
    const CURSOR_MESSAGE_A: &str =
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    const CURSOR_MESSAGE_B: &str =
        "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
    const CURSOR_MESSAGE_C: &str =
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";

    #[test]
    fn preparation_errors_are_distinct_from_transport_failures() {
        let error =
            preparation_result::<()>(Err(anyhow::anyhow!("unsupported local shape"))).unwrap_err();
        assert!(error.downcast_ref::<StorageV2PreparationError>().is_some());
    }

    #[test]
    fn conversation_reset_boundary_precedes_the_new_source_records() {
        let mut records = vec![StorageV2RenderRecord {
            event_id: "first-event".to_string(),
            order_time_us: 200,
            source_position: 0,
            event_subordinal: 0,
            role: "user".to_string(),
            content_text: Some("after".to_string()),
            tool_name: None,
            tool_input_json: None,
            tool_output_text: None,
            tool_call_id: None,
            parent_uuid: None,
            thread_id: None,
            branch_kind: None,
            interaction_kind: None,
            raw_record_ordinal: 0,
        }];

        insert_conversation_reset_boundary(
            &mut records,
            Uuid::parse_str("018f0c3a-7b2d-7f10-8a11-123456789abc").unwrap(),
            0,
            "2026-07-31T12:00:00Z",
            "managed-session",
            "provider-old",
            "provider-new",
        )
        .unwrap();

        assert_eq!(records.len(), 2);
        assert_eq!(records[0].role, "system");
        assert_eq!(
            records[0].content_text.as_deref(),
            Some("Conversation reset")
        );
        assert_eq!(
            records[0].branch_kind.as_deref(),
            Some("conversation_reset")
        );
        // The rotation ids must be recoverable as data, not only hashed into
        // the event_id: server ingest reads them to alias the new native id.
        assert_eq!(
            records[0].tool_input_json,
            Some(serde_json::json!({
                "previous_provider_session_id": "provider-old",
                "provider_session_id": "provider-new",
            }))
        );
        // Replay idempotency: the event_id derivation is frozen. This exact
        // value is uuid5(NAMESPACE_URL, "longhouse:conversation-reset:<session>
        // :<epoch>:<position>:<previous>:<new>") for the inputs above.
        assert_eq!(records[0].event_id, "49e83858-bb4b-57a5-8745-01ae48cecf04");
        assert_eq!(records[1].event_subordinal, 1);
        assert!(records[0].order_time_us < records[1].order_time_us);

        let first_boundary_id = records[0].event_id.clone();
        let mut repeated_transition = records[1..].to_vec();
        insert_conversation_reset_boundary(
            &mut repeated_transition,
            Uuid::parse_str("028f0c3a-7b2d-7f10-8a11-123456789abc").unwrap(),
            0,
            "2026-07-31T12:01:00Z",
            "managed-session",
            "provider-old",
            "provider-new",
        )
        .unwrap();
        assert_ne!(repeated_transition[0].event_id, first_boundary_id);
    }

    fn capabilities() -> StorageV2Capabilities {
        StorageV2Capabilities {
            protocol_version: 2,
            cutover: true,
            tenant_id: "tenant-a".to_string(),
            machine_id: "cinder".to_string(),
            ingest_path: "/api/agents/storage/v2/envelopes".to_string(),
            max_wire_body_bytes: 12 * 1024 * 1024,
            max_raw_record_bytes: 4 * 1024 * 1024,
            max_records: 10_000,
            media_claim_path: "/api/agents/storage/v2/media/claims".to_string(),
            media_upload_path_template: "/api/agents/storage/v2/media/{sha256}".to_string(),
            max_media_bytes: 32 * 1024 * 1024,
            max_media_claims: 512,
            range_kinds: vec!["byte_offset".to_string(), "record_ordinal".to_string()],
            lanes: vec!["live".to_string(), "repair".to_string()],
            lane_header: "X-Longhouse-Storage-Lane".to_string(),
            envelope_content_encodings: Vec::new(),
        }
    }

    #[test]
    fn fresh_cursor_source_waits_for_launch_reservation_before_materializing_shadow() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let state_root = dir
            .path()
            .join("longhouse/managed-local/cursor-helm/launch-reservations");
        fs::create_dir_all(&state_root).unwrap();
        fs::write(
            state_root.join("session.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 1,
                "provider": "cursor",
                "status": "pending",
                "session_id": "session-id",
                "launch_id": "launch-id",
                "expires_at": "2099-01-01T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", dir.path().join("longhouse"));
        }
        let path = dir.path().join("store.db");
        let _store = make_cursor_store(&path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let outcome = prepare_next_cursor_envelope_outcome_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            LIVE_TARGET_BATCH_BYTES as u64,
        )
        .unwrap();
        match previous_home {
            Some(value) => unsafe { std::env::set_var("LONGHOUSE_HOME", value) },
            None => unsafe { std::env::remove_var("LONGHOUSE_HOME") },
        }
        assert!(matches!(outcome, CursorPreparationOutcome::WaitingOnClaim));
    }

    #[test]
    fn fresh_cursor_agent_transcript_waits_for_and_then_uses_managed_claim() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let longhouse_home = dir.path().join("longhouse");
        let reservation_dir = longhouse_home.join("managed-local/cursor-helm/launch-reservations");
        let claim_dir = longhouse_home.join("managed-local/cursor-helm/binding-probes");
        fs::create_dir_all(&reservation_dir).unwrap();
        fs::create_dir_all(&claim_dir).unwrap();
        fs::write(
            reservation_dir.join("session.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 1,
                "provider": "cursor",
                "status": "pending",
                "session_id": "018f0c3a-7b2d-7f10-8a11-123456789abd",
                "launch_id": "launch-id",
                "expires_at": "2099-01-01T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();
        let conversation_id = "22a940a2-8256-4042-856e-a3b5ade40bd6";
        let transcript_dir = dir
            .path()
            .join(".cursor/projects/workspace/agent-transcripts")
            .join(conversation_id);
        fs::create_dir_all(&transcript_dir).unwrap();
        let path = transcript_dir.join(format!("{conversation_id}.jsonl"));
        fs::write(
            &path,
            b"{\"role\":\"user\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"hello\"}]}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", &longhouse_home);
        }

        assert!(
            prepare_next_envelope(&mut conn, &capabilities(), &path, "cursor", None)
                .unwrap()
                .is_none()
        );
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);

        let managed_session_id = "018f0c3a-7b2d-7f10-8a11-123456789abd";
        fs::write(
            claim_dir.join("claim.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 2,
                "provider": "cursor",
                "status": "observed",
                "session_id": managed_session_id,
                "conversation_uuid": conversation_id,
                "hook_observed_at": "2026-07-17T00:00:00Z"
            }))
            .unwrap(),
        )
        .unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "cursor", None)
            .unwrap()
            .unwrap();
        match previous_home {
            Some(value) => unsafe { std::env::set_var("LONGHOUSE_HOME", value) },
            None => unsafe { std::env::remove_var("LONGHOUSE_HOME") },
        }

        assert_eq!(prepared.envelope.session_id, managed_session_id);
    }

    #[test]
    fn resumed_cursor_source_waits_for_exact_rollover_claim_even_after_source_seen() {
        assert!(should_wait_for_unclaimed_cursor_source(true, false, true));
        assert!(!should_wait_for_unclaimed_cursor_source(true, false, false));
        assert!(should_wait_for_unclaimed_cursor_source(false, true, false));
        assert!(!should_wait_for_unclaimed_cursor_source(
            false, false, false
        ));
    }

    fn acknowledge_prepared(conn: &mut Connection, prepared: &PreparedStorageV2Envelope) {
        pending_source_envelope::acknowledge_and_delete(
            conn,
            prepared.source_epoch,
            &prepared.envelope.expected_envelope_id,
            prepared.range_start,
            prepared.range_end,
        )
        .unwrap();
    }

    async fn read_http_request(socket: &mut tokio::net::TcpStream) -> (String, Vec<u8>) {
        let mut bytes = Vec::new();
        let mut buffer = [0_u8; 4096];
        let header_end = loop {
            let read = socket.read(&mut buffer).await.unwrap();
            assert!(read > 0, "request closed before headers completed");
            bytes.extend_from_slice(&buffer[..read]);
            if let Some(offset) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
                break offset + 4;
            }
        };
        let headers = String::from_utf8_lossy(&bytes[..header_end]).into_owned();
        let content_length = headers
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().unwrap())
            })
            .unwrap_or(0);
        while bytes.len() - header_end < content_length {
            let read = socket.read(&mut buffer).await.unwrap();
            assert!(read > 0, "request closed before body completed");
            bytes.extend_from_slice(&buffer[..read]);
        }
        (
            headers.lines().next().unwrap_or_default().to_string(),
            bytes[header_end..header_end + content_length].to_vec(),
        )
    }

    async fn read_http_body(socket: &mut tokio::net::TcpStream) -> Vec<u8> {
        read_http_request(socket).await.1
    }

    fn cursor_root(ids: &[u8]) -> Vec<u8> {
        let mut root = Vec::new();
        for id in ids.chunks_exact(32) {
            root.extend_from_slice(&[0x0a, 0x20]);
            root.extend_from_slice(id);
        }
        root
    }

    fn cursor_metadata(root_blob_id: &str) -> String {
        cursor_metadata_for(CURSOR_CONVERSATION_ID, root_blob_id)
    }

    fn cursor_metadata_for(conversation_id: &str, root_blob_id: &str) -> String {
        let json = format!(
            r#"{{"agentId":"{conversation_id}","latestRootBlobId":"{root_blob_id}","createdAt":1773403200000}}"#
        );
        json.as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect()
    }

    fn make_cursor_store(path: &Path) -> Connection {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB);
             CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('0', ?1)",
            [cursor_metadata(CURSOR_ROOT_A)],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('unknown', X'00FF')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
            params![CURSOR_ROOT_A, cursor_root(&[0xbb; 32])],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO blobs (id, data) VALUES (?1, X'0102')",
            [CURSOR_MESSAGE_A],
        )
        .unwrap();
        conn
    }

    fn set_cursor_root(conn: &Connection, root_id: &str, message_ids: &[u8]) {
        conn.execute(
            "UPDATE meta SET value = ?1 WHERE key = '0'",
            [cursor_metadata(root_id)],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
            params![root_id, cursor_root(message_ids)],
        )
        .unwrap();
    }

    #[test]
    fn cursor_archives_keep_source_activity_across_replay_and_progress() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let cursor_home = dir.path().join("cursor");
        let store_path = cursor_home
            .join("chats/workspace")
            .join(CURSOR_CONVERSATION_ID)
            .join("store.db");
        fs::create_dir_all(store_path.parent().unwrap()).unwrap();
        let store = make_cursor_store(&store_path);
        let transcript_path = cursor_home
            .join("projects/workspace/agent-transcripts")
            .join(CURSOR_CONVERSATION_ID)
            .join(format!("{CURSOR_CONVERSATION_ID}.jsonl"));
        fs::create_dir_all(transcript_path.parent().unwrap()).unwrap();
        let transcript = "{\"role\":\"user\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"hello\"}]}}\n";
        fs::write(&transcript_path, transcript).unwrap();
        let sidecar_path = store_path.parent().unwrap().join("meta.json");
        let write_activity = |updated_at_ms| {
            fs::write(
                &sidecar_path,
                serde_json::to_vec(&serde_json::json!({
                    "schemaVersion": 1,
                    "createdAtMs": 1_773_403_200_000_i64,
                    "updatedAtMs": updated_at_ms,
                }))
                .unwrap(),
            )
            .unwrap();
        };
        write_activity(1_773_403_260_000_i64);
        let previous_cursor_home = std::env::var_os("CURSOR_HOME");
        let previous_xdg_home = std::env::var_os("XDG_CONFIG_HOME");
        unsafe {
            std::env::set_var("CURSOR_HOME", &cursor_home);
            std::env::set_var("XDG_CONFIG_HOME", dir.path().join("config"));
        }
        // Restore the process environment even if a regression panics.
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            let first_store = prepare_next_cursor_envelope(&mut conn, &capabilities(), &store_path)
                .unwrap()
                .unwrap();
            let first_transcript =
                prepare_next_envelope(&mut conn, &capabilities(), &transcript_path, "cursor", None)
                    .unwrap()
                    .unwrap();
            let expected_start = DateTime::from_timestamp_millis(1_773_403_200_000)
                .unwrap()
                .to_rfc3339();
            let expected_activity = DateTime::from_timestamp_millis(1_773_403_260_000)
                .unwrap()
                .to_rfc3339();
            for prepared in [&first_store, &first_transcript] {
                assert_eq!(prepared.envelope.session.started_at, expected_start);
                assert_eq!(
                    prepared.envelope.session.last_activity_at,
                    expected_activity
                );
                assert_eq!(prepared.envelope.session.ended_at, None);
                acknowledge_prepared(&mut conn, prepared);
            }
            // The lossy agent-transcripts projection ships bytes only; the
            // store owns the render. Source activity above is what this test
            // is about, and it holds for both sources either way.
            assert!(first_transcript.envelope.render.is_none());
            assert!(!first_transcript.envelope.records.is_empty());

            // A parser upgrade or historical reimport opens a new ingestion
            // epoch without changing when this provider conversation happened.
            conn.execute("UPDATE source_epoch_registry SET source_revision = 'older-parser' WHERE source_epoch = ?1",
                [first_store.source_epoch.to_string()]).unwrap();
            replay_file_source(&mut conn, &transcript_path, "cursor").unwrap();
            let replay_store =
                prepare_next_cursor_envelope(&mut conn, &capabilities(), &store_path)
                    .unwrap()
                    .unwrap();
            let replay_transcript =
                prepare_next_envelope(&mut conn, &capabilities(), &transcript_path, "cursor", None)
                    .unwrap()
                    .unwrap();
            for prepared in [&replay_store, &replay_transcript] {
                assert_eq!(
                    prepared.envelope.session.last_activity_at,
                    expected_activity
                );
                acknowledge_prepared(&mut conn, prepared);
            }
            assert!(replay_transcript.envelope.render.is_none());

            write_activity(1_773_403_320_000_i64);
            let mut root_ids = vec![0xbb; 32];
            root_ids.extend_from_slice(&[0xdd; 32]);
            set_cursor_root(&store, CURSOR_ROOT_B, &root_ids);
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![
                        CURSOR_MESSAGE_B,
                        br#"{"role":"assistant","content":"done"}"#
                    ],
                )
                .unwrap();
            fs::write(
                &transcript_path,
                format!(
                    "{transcript}{{\"role\":\"assistant\",\"message\":{{\"content\":\"done\"}}}}\n"
                ),
            )
            .unwrap();
            let progressed_store =
                prepare_next_cursor_envelope(&mut conn, &capabilities(), &store_path)
                    .unwrap()
                    .unwrap();
            let progressed_transcript =
                prepare_next_envelope(&mut conn, &capabilities(), &transcript_path, "cursor", None)
                    .unwrap()
                    .unwrap();
            let expected_progress = DateTime::from_timestamp_millis(1_773_403_320_000)
                .unwrap()
                .to_rfc3339();
            for prepared in [&progressed_store, &progressed_transcript] {
                assert_eq!(
                    prepared.envelope.session.last_activity_at,
                    expected_progress
                );
                assert_eq!(prepared.envelope.session.ended_at, None);
            }
        }));
        for (name, previous) in [
            ("CURSOR_HOME", previous_cursor_home),
            ("XDG_CONFIG_HOME", previous_xdg_home),
        ] {
            match previous {
                Some(value) => unsafe { std::env::set_var(name, value) },
                None => unsafe { std::env::remove_var(name) },
            }
        }
        if let Err(error) = outcome {
            std::panic::resume_unwind(error);
        }
    }

    #[test]
    fn cursor_store_without_sidecar_activity_uses_creation_not_ingestion() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let _store = make_cursor_store(&path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(
            prepared.envelope.session.last_activity_at,
            DateTime::from_timestamp_millis(1_773_403_200_000)
                .unwrap()
                .to_rfc3339()
        );
    }

    #[test]
    fn cursor_transcript_without_clock_still_archives_exact_raw_records() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let conversation = Uuid::new_v4().to_string();
        let path = dir
            .path()
            .join("agent-transcripts")
            .join(format!("{conversation}.jsonl"));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let raw = b"{\"role\":\"user\",\"message\":{\"content\":\"undated source\"}}\n";
        fs::write(&path, raw).unwrap();
        let cursor_home = dir.path().join("cursor");
        let store_path = cursor_home
            .join("chats/workspace")
            .join(&conversation)
            .join("store.db");
        fs::create_dir_all(store_path.parent().unwrap()).unwrap();
        let previous_cursor_home = std::env::var_os("CURSOR_HOME");
        let previous_xdg_home = std::env::var_os("XDG_CONFIG_HOME");
        unsafe {
            std::env::set_var("CURSOR_HOME", &cursor_home);
            std::env::set_var("XDG_CONFIG_HOME", dir.path().join("config"));
        }
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            for source in ["absent", "corrupt"] {
                if source == "corrupt" {
                    fs::write(&store_path, b"not a SQLite database").unwrap();
                }
                let mut conn =
                    open_db(Some(&dir.path().join(format!("{source}-state.db")))).unwrap();
                let prepared =
                    prepare_next_envelope(&mut conn, &capabilities(), &path, "cursor", None)
                        .unwrap()
                        .unwrap();
                // No render manifest: the server must keep this pending, not
                // publish an apparently complete empty transcript.
                assert!(prepared.envelope.render.is_none());
                assert_eq!(
                    BASE64_STANDARD
                        .decode(&prepared.envelope.records[0].data_b64)
                        .unwrap(),
                    raw
                );
                assert_eq!(
                    prepared.envelope.session.started_at,
                    prepared.envelope.epoch_opened_at
                );
                acknowledge_prepared(&mut conn, &prepared);
                assert!(
                    prepare_next_envelope(&mut conn, &capabilities(), &path, "cursor", None)
                        .unwrap()
                        .is_none()
                );
            }
        }));
        for (name, previous) in [
            ("CURSOR_HOME", previous_cursor_home),
            ("XDG_CONFIG_HOME", previous_xdg_home),
        ] {
            match previous {
                Some(value) => unsafe { std::env::set_var(name, value) },
                None => unsafe { std::env::remove_var(name) },
            }
        }
        if let Err(error) = outcome {
            std::panic::resume_unwind(error);
        }
    }

    #[test]
    fn cursor_acp_source_preserves_exact_notifications_and_needs_a_receipt_to_advance() {
        let dir = tempfile::tempdir().unwrap();
        let session_id = "019c638d-0000-0000-0000-000000000099";
        let path = dir.path().join(session_id).join("run-1.jsonl");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let raw = b" {\"jsonrpc\":\"2.0\",\"method\":\"session/update\"}\n";
        fs::write(&path, raw).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_cursor_acp_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(first.envelope.session_id, session_id);
        assert_eq!(first.envelope.provider, "cursor");
        assert!(first.envelope.render.is_none());
        assert_eq!(
            BASE64_STANDARD
                .decode(&first.envelope.records[0].data_b64)
                .unwrap(),
            raw
        );
        assert_eq!(
            source_epoch::lane_position(&conn, first.source_epoch, SourceLane::Durable).unwrap(),
            0,
        );
        acknowledge_prepared(&mut conn, &first);
        assert!(
            prepare_next_cursor_acp_envelope(&mut conn, &capabilities(), &path)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn cursor_prepares_source_faithful_raw_records_and_rotates_only_on_root_rewrite() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(first.envelope.provider, "cursor");
        assert_eq!(
            first.envelope.opaque_source_id,
            format!("cursor-store-v1:{CURSOR_CONVERSATION_ID}")
        );
        assert!(first.envelope.render.is_none());
        assert!(first.envelope.records.iter().any(|record| {
            let bytes = BASE64_STANDARD.decode(&record.data_b64).unwrap();
            let raw: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            raw["kind"] == "meta" && raw["meta_key"] == "unknown"
        }));
        acknowledge_prepared(&mut conn, &first);

        let mut extended_root = vec![0xbb; 32];
        extended_root.extend_from_slice(&[0xdd; 32]);
        set_cursor_root(&store, CURSOR_ROOT_B, &extended_root);
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                params![
                    CURSOR_MESSAGE_B,
                    br#"{"role":"assistant","content":[{"type":"text","text":"second turn"}]}"#
                ],
            )
            .unwrap();
        let extension = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(extension.source_epoch, first.source_epoch);
        assert_eq!(
            extension.envelope.render.as_ref().unwrap().generation_id,
            cursor_render_generation_id(&extension.envelope.session_id).to_string()
        );
        let extension_render = extension.envelope.render.as_ref().unwrap();
        assert_eq!(extension_render.records.len(), 1);
        assert_eq!(
            extension_render.records[0].content_text.as_deref(),
            Some("second turn")
        );
        acknowledge_prepared(&mut conn, &extension);

        set_cursor_root(&store, CURSOR_ROOT_C, &[0xff; 32]);
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, X'0506')",
                [CURSOR_MESSAGE_C],
            )
            .unwrap();
        let rewrite = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_ne!(rewrite.source_epoch, first.source_epoch);
        assert_eq!(
            rewrite.envelope.predecessor_source_epoch,
            Some(first.source_epoch.to_string())
        );
        assert_eq!(rewrite.range_start, 0);
    }

    #[test]
    fn cursor_open_epoch_drain_keeps_identity_and_ships_only_the_new_tail() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let first_epoch = first.source_epoch;
        let first_end = first.range_end;
        acknowledge_prepared(&mut conn, &first);

        let drained = cursor_store_records::drain_receipted_cursor_records(&conn).unwrap();
        assert!(drained > 0);
        let retained_rows = cursor_store_records::cursor_record_count(&conn, first_epoch).unwrap();
        assert_eq!(retained_rows, first_end);
        let retained_payload_bytes: i64 = conn
            .query_row(
                "SELECT COALESCE(SUM(length(record_bytes)), 0)
                 FROM cursor_store_raw_record WHERE source_epoch = ?1",
                [first_epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(retained_payload_bytes, 0);

        assert!(
            prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
                .unwrap()
                .is_none(),
            "an unchanged source must not rotate or re-spool after drain"
        );
        assert_eq!(
            source_epoch::active_source_epoch(&conn, "cursor", &first.envelope.opaque_source_id)
                .unwrap(),
            Some(first_epoch)
        );
        assert_eq!(
            cursor_store_records::cursor_record_count(&conn, first_epoch).unwrap(),
            retained_rows,
            "snapshot hashes must suppress already-receipted records"
        );

        let mut extended_root = vec![0xbb; 32];
        extended_root.extend_from_slice(&[0xdd; 32]);
        set_cursor_root(&store, CURSOR_ROOT_B, &extended_root);
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                params![
                    CURSOR_MESSAGE_B,
                    br#"{"role":"assistant","content":[{"type":"text","text":"after drain"}]}"#
                ],
            )
            .unwrap();

        let tail = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(tail.source_epoch, first_epoch);
        assert_eq!(tail.range_start, first_end);
        assert!(tail
            .envelope
            .render
            .unwrap()
            .records
            .iter()
            .any(|record| { record.content_text.as_deref() == Some("after drain") }));
    }

    #[test]
    fn cursor_payloads_missing_past_the_first_blob_page_are_repaired_from_the_store() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        for index in 0..(CURSOR_BLOB_PAGE_ROWS + 44) {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("{index:064x}"), vec![index as u8; 8]],
                )
                .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        // Capture and ship the whole store so the spool holds every record.
        let mut epoch = None;
        for _ in 0..30 {
            match prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &path).unwrap() {
                CursorPreparationOutcome::Envelope(prepared) => {
                    epoch = Some(prepared.source_epoch);
                    acknowledge_prepared(&mut conn, &prepared);
                }
                CursorPreparationOutcome::Current => break,
                _ => {}
            }
        }
        let epoch = epoch.expect("the store must have produced an envelope");
        let records =
            cursor_store_records::cursor_records_from(&conn, epoch, 0, 10_000, u64::MAX).unwrap();
        let mut blobs = records
            .iter()
            .filter_map(|record| {
                let value: Value = serde_json::from_slice(&record.bytes).ok()?;
                (value["kind"] == "blob").then(|| {
                    (
                        value["blob_id"].as_str().unwrap().to_string(),
                        cursor_store_records::cursor_record_hash(&record.bytes),
                    )
                })
            })
            .collect::<Vec<_>>();
        blobs.sort();
        assert!(blobs.len() > CURSOR_BLOB_PAGE_ROWS);

        // Unshipped again, with the payloads of every blob past the first page
        // gone: exactly the shape a hash-sorted page from the head cannot reach.
        conn.execute(
            "UPDATE source_epoch_lane_state SET last_position = 1
             WHERE source_epoch = ?1 AND lane = 'durable'",
            [epoch.to_string()],
        )
        .unwrap();
        let root = crate::state::payload_store::root_for_connection(&conn)
            .unwrap()
            .join("records");
        let lost = blobs[CURSOR_BLOB_PAGE_ROWS..]
            .iter()
            .map(|(_, hash)| crate::state::payload_store::relative_path_for(hash, "rec"))
            .collect::<Vec<_>>();
        for relative in &lost {
            crate::state::payload_store::remove(&root, relative).unwrap();
        }

        let repaired = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .expect("the missing payloads must be re-read from the Cursor store");
        assert_eq!(repaired.range_start, 1);
        assert!(lost
            .iter()
            .all(|relative| crate::state::payload_store::exists(&root, relative)));
    }

    #[test]
    fn cursor_renders_blob_first_referenced_after_its_raw_receipt() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        let orphan_id = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                params![
                    orphan_id,
                    br#"{"role":"assistant","content":[{"type":"text","text":"referenced later"}]}"#
                ],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        acknowledge_prepared(&mut conn, &first);

        let mut extended_root = vec![0xbb; 32];
        extended_root.extend_from_slice(&[0xee; 32]);
        set_cursor_root(&store, CURSOR_ROOT_B, &extended_root);
        let extension = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let rendered = extension.envelope.render.unwrap().records;
        assert!(rendered
            .iter()
            .any(|record| record.content_text.as_deref() == Some("referenced later")));
    }

    #[test]
    fn cursor_store_emits_readable_text_and_tool_render_records() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        let message = serde_json::json!({
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hello from Cursor"},
                {"type": "tool-call", "toolName": "Shell", "toolCallId": "call-1", "args": {"command": "pwd"}},
                {"type": "tool-result", "toolName": "Shell", "toolCallId": "call-1", "result": "/tmp"}
            ]
        });
        store
            .execute(
                "UPDATE blobs SET data = ?1 WHERE id = ?2",
                params![serde_json::to_vec(&message).unwrap(), CURSOR_MESSAGE_A],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let render = prepared.envelope.render.unwrap();
        assert_eq!(render.parser_revision, CURSOR_PARSER_REVISION);
        assert_eq!(render.records.len(), 3);
        assert_eq!(
            render.records[0].content_text.as_deref(),
            Some("hello from Cursor")
        );
        assert_eq!(render.records[1].tool_call_id.as_deref(), Some("call-1"));
        assert_eq!(render.records[2].role, "tool");
        assert_eq!(render.records[2].tool_output_text.as_deref(), Some("/tmp"));
    }

    fn cursor_visibility_fixture(
        messages: Vec<(&str, Value)>,
    ) -> (
        cursor_store::CursorStoreSnapshot,
        Vec<cursor_store_records::CursorRawRecord>,
    ) {
        let mut blob_rows = Vec::new();
        let mut selected = Vec::new();
        let mut root_ids = Vec::new();
        for (source_position, (blob_id, message)) in messages.into_iter().enumerate() {
            let bytes = serde_json::to_vec(&message).unwrap();
            root_ids.push(blob_id.to_string());
            blob_rows.push(cursor_store::CursorStoreBlobRow {
                id: blob_id.to_string(),
                data_bytes: bytes.clone(),
                data_storage_class: cursor_store::SqliteStorageClass::Blob,
            });
            selected.push(cursor_store_records::CursorRawRecord {
                source_position: source_position as u64,
                bytes: serde_json::to_vec(&serde_json::json!({
                    "kind": "blob",
                    "blob_id": blob_id,
                    "blob_bytes_b64": BASE64_STANDARD.encode(bytes),
                }))
                .unwrap(),
            });
        }
        (
            cursor_store::CursorStoreSnapshot {
                conversation_uuid: CURSOR_CONVERSATION_ID.to_string(),
                root_blob_id: None,
                created_at_ms: Some(1_773_403_200_000),
                updated_at_ms: None,
                meta_rows: Vec::new(),
                blob_rows,
                root_message_blob_ids: cursor_store::RootMessageBlobIds::Parsed(root_ids),
            },
            selected,
        )
    }

    #[test]
    fn cursor_failed_retry_prose_is_inspectable_without_becoming_head_replies() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let attempt_one = "2222222222222222222222222222222222222222222222222222222222222222";
        let attempt_two = "3333333333333333333333333333333333333333333333333333333333333333";
        let attempt_three = "4444444444444444444444444444444444444444444444444444444444444444";
        let attempt_four = "5555555555555555555555555555555555555555555555555555555555555555";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>hello test 1</user_query>"}]}),
            ),
            (
                attempt_one,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"retry thought one"},{"type":"text","text":"reply one"}]}),
            ),
            (
                attempt_two,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"retry thought two"},{"type":"text","text":"reply two"}]}),
            ),
            (
                attempt_three,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"retry thought three"},{"type":"text","text":"reply three"}]}),
            ),
            (
                attempt_four,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"retry thought four"},{"type":"text","text":"reply four"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-1".to_string(),
                launch_id: None,
                prompt: "hello test 1".to_string(),
                response_text: None,
                stop_status: Some("error".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        let head = rendered
            .iter()
            .filter(|record| record.branch_kind.as_deref() != Some("abandoned"))
            .map(|record| (record.role.as_str(), record.content_text.as_deref()))
            .collect::<Vec<_>>();
        assert_eq!(head, vec![("user", Some("hello test 1"))]);
        let abandoned = rendered
            .iter()
            .filter(|record| record.branch_kind.as_deref() == Some("abandoned"))
            .map(|record| record.content_text.as_deref().unwrap())
            .collect::<Vec<_>>();
        assert_eq!(
            abandoned,
            vec!["reply one", "reply two", "reply three", "reply four"]
        );
    }

    #[test]
    fn cursor_completed_turn_renders_exact_provider_receipt_once() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let progress = "2222222222222222222222222222222222222222222222222222222222222222";
        let tool = "3333333333333333333333333333333333333333333333333333333333333333";
        let final_text = "4444444444444444444444444444444444444444444444444444444444444444";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                progress,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"internal progress"},{"type":"text","text":"progress"}]}),
            ),
            (
                tool,
                serde_json::json!({"role":"assistant","content":[{"type":"tool-call","toolName":"Shell","toolCallId":"call-1","args":{"command":"pwd"}}]}),
            ),
            (
                final_text,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"done"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-2".to_string(),
                launch_id: None,
                prompt: "do work".to_string(),
                response_text: Some("progressdone".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        assert_eq!(rendered.len(), 4);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(rendered[1].role, "assistant");
        assert_eq!(rendered[1].content_text.as_deref(), Some("progress"));
        assert_eq!(rendered[2].tool_call_id.as_deref(), Some("call-1"));
        assert_eq!(rendered[3].content_text.as_deref(), Some("done"));
    }

    /// A failed turn keeps its prose out of the head branch, which is right,
    /// but silence is not an outcome. The session row names the failure so the
    /// timeline says what the terminal said.
    #[test]
    fn cursor_failed_turn_renders_a_typed_outcome_row() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let final_text = "4444444444444444444444444444444444444444444444444444444444444444";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                final_text,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"a thought"},{"type":"text","text":"done"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-2".to_string(),
                launch_id: None,
                prompt: "do work".to_string(),
                response_text: Some("done".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            failures: vec![crate::cursor_visibility::CursorTurnFailure {
                generation_id: "continuation".to_string(),
                launch_id: None,
                status: "error".to_string(),
                observed_at: None,
            }],
            latest_terminal: Some(("continuation".to_string(), true)),
            ..Default::default()
        };

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        let outcome = rendered.last().expect("a failure row is appended");
        assert_eq!(outcome.role, "system");
        assert_eq!(
            outcome.interaction_kind.as_deref(),
            Some("provider_notification")
        );
        assert!(outcome
            .content_text
            .as_deref()
            .is_some_and(|text| text.contains("failed")));
        // It sorts after the turn it describes and reuses that turn's raw
        // evidence rather than inventing a raw record of its own.
        let previous = &rendered[rendered.len() - 2];
        assert!(outcome.order_time_us > previous.order_time_us);
        assert_eq!(outcome.raw_record_ordinal, previous.raw_record_ordinal);
        // The reasoning block stays suppressed; only the committed reply and
        // the outcome row reach the head branch.
        assert!(!rendered
            .iter()
            .any(|record| record.content_text.as_deref() == Some("a thought")));
    }

    /// Cursor wraps its own auto-continuation in the same `<user_query>`
    /// envelope as a typed prompt, so the envelope alone cannot tell them
    /// apart. `prompt_history.json` records what a person actually submitted.
    #[test]
    fn cursor_prompt_history_separates_typed_prompts_from_provider_continuations() {
        let dir = tempfile::tempdir().unwrap();
        let store_path = dir.path().join("store.db");
        fs::write(
            dir.path().join("prompt_history.json"),
            serde_json::to_vec(&serde_json::json!(["do work"])).unwrap(),
        )
        .unwrap();

        let history = CursorPromptHistory::load(&store_path);
        assert!(!history.is_empty());
        assert_eq!(
            classify_cursor_text_with_witness(
                "user",
                "<user_query>do work</user_query>",
                Some(&history)
            ),
            ("user".to_string(), "do work".to_string(), None)
        );
        let (role, _, kind) = classify_cursor_text_with_witness(
            "user",
            "<user_query>Briefly inform the user about the task result.</user_query>",
            Some(&history),
        );
        assert_eq!(role, "system");
        // Marked, not hidden: a capped history must cost a label, not a message.
        assert_eq!(kind.as_deref(), Some("provider_notification"));
        // No witness, no demotion: Shadow sessions and unreadable histories
        // keep every prompt a user prompt.
        assert_eq!(
            classify_cursor_text_with_witness(
                "user",
                "<user_query>Briefly inform the user about the task result.</user_query>",
                None
            )
            .0,
            "user"
        );
    }

    /// Cursor elides pastes in the history and expands them in the store, so a
    /// real prompt carrying pasted text would look unmatched without the
    /// sidecar. This is the case that would have hidden one of David's own
    /// messages.
    #[test]
    fn cursor_prompt_history_expands_pasted_text_before_matching() {
        let dir = tempfile::tempdir().unwrap();
        let store_path = dir.path().join("store.db");
        fs::write(
            dir.path().join("prompt_history.json"),
            serde_json::to_vec(&serde_json::json!([
                "\"\"\"[Pasted text #1 +83 lines]\"\"\"\n\nLooks like there is follow-up on teams."
            ]))
            .unwrap(),
        )
        .unwrap();
        fs::write(
            dir.path().join("pasted_text.json"),
            serde_json::to_vec(&serde_json::json!({"entries": {"1": "line one\nline two"}}))
                .unwrap(),
        )
        .unwrap();

        let history = CursorPromptHistory::load(&store_path);
        let expanded = "\"\"\"line one\nline two\"\"\"\n\nLooks like there is follow-up on teams.";
        assert!(history.contains(expanded));
        assert_eq!(
            classify_cursor_text_with_witness(
                "user",
                &format!("<user_query>{expanded}</user_query>"),
                Some(&history)
            )
            .0,
            "user"
        );
    }

    /// A label must never be able to hide an answer. The witness demotes a row
    /// at render time only; turn alignment still sees every store user turn, so
    /// a wrongly demoted prompt cannot shift receipts and suppress the reply
    /// that followed it.
    #[test]
    fn a_demoted_prompt_still_anchors_its_turn_for_receipt_alignment() {
        let first_user = "1111111111111111111111111111111111111111111111111111111111111111";
        let first_reply = "2222222222222222222222222222222222222222222222222222222222222222";
        let second_user = "3333333333333333333333333333333333333333333333333333333333333333";
        let second_reply = "4444444444444444444444444444444444444444444444444444444444444444";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                first_user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                first_reply,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"first answer"}]}),
            ),
            (
                second_user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>keep going</user_query>"}]}),
            ),
            (
                second_reply,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"second answer"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![
                crate::cursor_visibility::CursorProviderTurn {
                    generation_id: "g1".to_string(),
                    launch_id: None,
                    prompt: "do work".to_string(),
                    response_text: Some("first answer".to_string()),
                    stop_status: Some("completed".to_string()),
                    stop_observed_at: None,
                },
                crate::cursor_visibility::CursorProviderTurn {
                    generation_id: "g2".to_string(),
                    launch_id: None,
                    prompt: "keep going".to_string(),
                    response_text: Some("second answer".to_string()),
                    stop_status: Some("completed".to_string()),
                    stop_observed_at: None,
                },
            ],
            ..Default::default()
        };
        // A history that covers only the first prompt: the second is exactly
        // the capped/rotated case that must cost a label, not an answer.
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join("prompt_history.json"),
            serde_json::to_vec(&serde_json::json!(["do work"])).unwrap(),
        )
        .unwrap();
        let witness = CursorPromptHistory::load(&dir.path().join("store.db"));

        let rendered = cursor_render_records(
            &snapshot,
            &selected,
            0,
            Some(&evidence),
            Some(&witness),
            None,
        )
        .unwrap();

        let text: Vec<&str> = rendered
            .iter()
            .filter_map(|record| record.content_text.as_deref())
            .collect();
        assert!(text.contains(&"first answer"));
        assert!(
            text.contains(&"second answer"),
            "the demoted prompt must not suppress the reply that followed it"
        );
        let demoted = rendered
            .iter()
            .find(|record| record.content_text.as_deref() == Some("keep going"))
            .expect("the demoted prompt is still rendered");
        assert_eq!(demoted.role, "system");
        assert_eq!(
            demoted.interaction_kind.as_deref(),
            Some("provider_notification")
        );
    }

    /// A pasted block can quote a placeholder of its own; the store holds the
    /// fully expanded text, so one pass would leave a real prompt unmatched.
    #[test]
    fn cursor_prompt_history_expands_nested_paste_placeholders() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join("prompt_history.json"),
            serde_json::to_vec(&serde_json::json!([
                "before [Pasted text #1 +2 lines] after"
            ]))
            .unwrap(),
        )
        .unwrap();
        fs::write(
            dir.path().join("pasted_text.json"),
            serde_json::to_vec(&serde_json::json!({
                "entries": {"1": "outer [Pasted text #2 +1 lines]", "2": "inner"}
            }))
            .unwrap(),
        )
        .unwrap();

        let history = CursorPromptHistory::load(&dir.path().join("store.db"));

        assert!(history.contains("before outer inner after"));
    }

    #[test]
    fn cursor_prompt_history_keeps_unknown_paste_placeholders_verbatim() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join("prompt_history.json"),
            serde_json::to_vec(&serde_json::json!(["see [Pasted text #9 +4 lines] please"]))
                .unwrap(),
        )
        .unwrap();
        let history = CursorPromptHistory::load(&dir.path().join("store.db"));
        assert!(history.contains("see [Pasted text #9 +4 lines] please"));
    }

    #[test]
    fn cursor_prompt_history_is_ignored_when_it_does_not_cover_the_conversation() {
        let dir = tempfile::tempdir().unwrap();
        let store_path = dir.path().join("store.db");
        fs::write(
            dir.path().join("prompt_history.json"),
            serde_json::to_vec(&serde_json::json!([
                "a prompt from some other conversation"
            ]))
            .unwrap(),
        )
        .unwrap();
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let (snapshot, _selected) = cursor_visibility_fixture(vec![(
            user,
            serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
        )]);
        // A rotated or capped history that matches nothing here must not demote
        // every real prompt in the conversation.
        assert!(cursor_human_prompt_witness(&snapshot, &store_path).is_none());
    }

    #[test]
    fn cursor_missing_prompt_history_leaves_authorship_alone() {
        let dir = tempfile::tempdir().unwrap();
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let (snapshot, _selected) = cursor_visibility_fixture(vec![(
            user,
            serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
        )]);
        assert!(cursor_human_prompt_witness(&snapshot, &dir.path().join("store.db")).is_none());
    }

    /// A later completed turn supersedes an earlier failure, so the outcome row
    /// clears itself without anyone tracking acknowledgement.
    #[test]
    fn cursor_superseded_failure_renders_no_outcome_row() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let final_text = "4444444444444444444444444444444444444444444444444444444444444444";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                final_text,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"done"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-2".to_string(),
                launch_id: None,
                prompt: "do work".to_string(),
                response_text: Some("done".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            failures: vec![crate::cursor_visibility::CursorTurnFailure {
                generation_id: "older".to_string(),
                launch_id: None,
                status: "error".to_string(),
                observed_at: None,
            }],
            latest_terminal: Some(("generation-2".to_string(), false)),
            ..Default::default()
        };
        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        assert!(rendered
            .iter()
            .all(|record| record.interaction_kind.is_none()));
    }

    /// The hook stream says a turn failed; only the projection says why. When
    /// that string is available the row uses the provider's own words.
    #[test]
    fn cursor_failure_row_uses_the_provider_error_when_one_is_known() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let final_text = "4444444444444444444444444444444444444444444444444444444444444444";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                final_text,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"done"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            failures: vec![crate::cursor_visibility::CursorTurnFailure {
                generation_id: "continuation".to_string(),
                launch_id: None,
                status: "error".to_string(),
                observed_at: None,
            }],
            latest_terminal: Some(("continuation".to_string(), true)),
            ..Default::default()
        };

        let rendered = cursor_render_records(
            &snapshot,
            &selected,
            0,
            Some(&evidence),
            None,
            Some("WritableIterable is closed"),
        )
        .unwrap();

        let outcome = rendered.last().expect("a failure row is appended");
        assert_eq!(
            outcome.content_text.as_deref(),
            Some("Cursor reported this turn failed: WritableIterable is closed")
        );
    }

    #[test]
    fn cursor_healthy_turn_renders_no_outcome_row() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let final_text = "4444444444444444444444444444444444444444444444444444444444444444";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                final_text,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"done"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-2".to_string(),
                launch_id: None,
                prompt: "do work".to_string(),
                response_text: Some("done".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };
        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        assert!(rendered
            .iter()
            .all(|record| record.interaction_kind.is_none()));
    }

    #[test]
    fn cursor_ambiguous_retry_receipt_does_not_choose_first_or_last_artifact() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let attempt_one = "2222222222222222222222222222222222222222222222222222222222222222";
        let attempt_two = "3333333333333333333333333333333333333333333333333333333333333333";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>repeat</user_query>"}]}),
            ),
            (
                attempt_one,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"same answer"}]}),
            ),
            (
                attempt_two,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"same answer"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-ambiguous".to_string(),
                launch_id: None,
                prompt: "repeat".to_string(),
                response_text: Some("same answer".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        assert_eq!(rendered.len(), 1);
        assert_eq!(rendered[0].role, "user");
    }

    #[test]
    fn cursor_conflicting_hook_receipts_fail_closed() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let reply = "2222222222222222222222222222222222222222222222222222222222222222";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>hello</user_query>"}]}),
            ),
            (
                reply,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"world"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-conflict".to_string(),
                launch_id: None,
                prompt: "hello".to_string(),
                response_text: Some("world".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ambiguous: true,
            ..Default::default()
        };

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        assert_eq!(rendered.len(), 1);
        assert_eq!(rendered[0].role, "user");
    }

    #[test]
    fn cursor_aborted_turn_keeps_tools_and_labels_uncommitted_prose() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let tool = "2222222222222222222222222222222222222222222222222222222222222222";
        let prose = "3333333333333333333333333333333333333333333333333333333333333333";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>run it</user_query>"}]}),
            ),
            (
                tool,
                serde_json::json!({"role":"assistant","content":[{"type":"tool-call","toolName":"Shell","toolCallId":"call-1","args":{"command":"pwd"}}]}),
            ),
            (
                prose,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"uncommitted prose"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-failed-tool".to_string(),
                launch_id: None,
                prompt: "run it".to_string(),
                response_text: None,
                stop_status: Some("aborted".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        assert_eq!(rendered.len(), 3);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(rendered[1].tool_call_id.as_deref(), Some("call-1"));
        assert_eq!(rendered[1].branch_kind, None);
        assert_eq!(
            rendered[2].content_text.as_deref(),
            Some("uncommitted prose")
        );
        assert_eq!(rendered[2].branch_kind.as_deref(), Some("abandoned"));
    }

    #[test]
    fn cursor_managed_turn_without_matching_hook_evidence_is_raw_only() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let attempt = "2222222222222222222222222222222222222222222222222222222222222222";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>missing hook</user_query>"}]}),
            ),
            (
                attempt,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"unverified artifact"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence::default();

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        assert_eq!(rendered.len(), 1);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(selected.len(), 2, "unverified text remains in raw storage");
    }

    #[test]
    fn cursor_injected_user_context_does_not_shift_receipt_alignment() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let injected = "2222222222222222222222222222222222222222222222222222222222222222";
        let reply = "3333333333333333333333333333333333333333333333333333333333333333";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                injected,
                serde_json::json!({
                    "role":"user",
                    "content":[{"type":"text","text":"<user_info>workspace context</user_info><rules>runtime guidance</rules>"}]
                }),
            ),
            (
                reply,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"done"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-injected-context".to_string(),
                launch_id: None,
                prompt: "do work".to_string(),
                response_text: Some("done".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();

        assert_eq!(rendered.len(), 3);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(rendered[0].content_text.as_deref(), Some("do work"));
        assert_eq!(rendered[1].role, "system");
        assert_eq!(rendered[2].role, "assistant");
        assert_eq!(rendered[2].content_text.as_deref(), Some("done"));
    }

    #[test]
    fn cursor_unstored_provider_receipt_does_not_shift_turn_alignment() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let reply = "2222222222222222222222222222222222222222222222222222222222222222";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                reply,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"done"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![
                crate::cursor_visibility::CursorProviderTurn {
                    generation_id: "generation-internal".to_string(),
                    launch_id: None,
                    prompt: "provider internal setup".to_string(),
                    response_text: Some("setup complete".to_string()),
                    stop_status: Some("completed".to_string()),
                    stop_observed_at: None,
                },
                crate::cursor_visibility::CursorProviderTurn {
                    generation_id: "generation-user".to_string(),
                    launch_id: None,
                    prompt: "do work".to_string(),
                    response_text: Some("done".to_string()),
                    stop_status: Some("completed".to_string()),
                    stop_observed_at: None,
                },
            ],
            ..Default::default()
        };

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();

        assert_eq!(rendered.len(), 2);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(rendered[1].role, "assistant");
        assert_eq!(rendered[1].content_text.as_deref(), Some("done"));
    }

    #[test]
    fn cursor_local_exit_receipt_does_not_suppress_matching_assistant_text() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let reply = "2222222222222222222222222222222222222222222222222222222222222222";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                reply,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"done"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"generation-1","prompt":"do work"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"generation-1","text":"done"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"generation-1","status":"completed"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"generation-2","prompt":"/exit"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"generation-2","status":"completed"}}"#,
            "conversation",
            None,
        )
        .unwrap();

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();

        assert_eq!(rendered.len(), 2);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(rendered[1].role, "assistant");
        assert_eq!(rendered[1].content_text.as_deref(), Some("done"));
    }

    #[test]
    fn cursor_repeated_prompt_without_unique_turn_alignment_is_raw_only() {
        let user_one = "1111111111111111111111111111111111111111111111111111111111111111";
        let reply_one = "2222222222222222222222222222222222222222222222222222222222222222";
        let user_two = "3333333333333333333333333333333333333333333333333333333333333333";
        let reply_two = "4444444444444444444444444444444444444444444444444444444444444444";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user_one,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>same prompt</user_query>"}]}),
            ),
            (
                reply_one,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"same reply"}]}),
            ),
            (
                user_two,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>same prompt</user_query>"}]}),
            ),
            (
                reply_two,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"same reply"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorProviderTurn {
                generation_id: "generation-repeated".to_string(),
                launch_id: None,
                prompt: "same prompt".to_string(),
                response_text: Some("same reply".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered =
            cursor_render_records(&snapshot, &selected, 0, Some(&evidence), None, None).unwrap();
        assert_eq!(rendered.len(), 2);
        assert!(rendered.iter().all(|record| record.role == "user"));
    }

    #[test]
    fn cursor_parser_revision_upgrade_replays_from_a_replacement_epoch() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        store
            .execute(
                "UPDATE blobs SET data = ?1 WHERE id = ?2",
                params![
                    br#"{"role":"assistant","content":[{"type":"text","text":"legacy render"}]}"#,
                    CURSOR_MESSAGE_A,
                ],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let first_epoch = first.source_epoch;
        acknowledge_prepared(&mut conn, &first);
        conn.execute(
            "UPDATE source_epoch_registry
             SET source_revision = NULL, max_observed_len = 132
             WHERE source_epoch = ?1",
            [first_epoch.to_string()],
        )
        .unwrap();

        let replay = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_ne!(replay.source_epoch, first_epoch);
        assert_eq!(
            replay.envelope.predecessor_source_epoch,
            Some(first_epoch.to_string())
        );
        assert_eq!(replay.range_start, 0);
        assert_eq!(
            replay.envelope.render.as_ref().unwrap().parser_revision,
            CURSOR_PARSER_REVISION
        );
        let epoch_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM source_epoch_registry", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(
            epoch_count, 2,
            "parser replay must not look like a truncation"
        );
    }

    #[tokio::test]
    async fn cursor_lineage_repair_requires_host_proof_and_collapses_empty_epochs() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let _store = make_cursor_store(&path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let first_epoch = first.source_epoch;
        acknowledge_prepared(&mut conn, &first);
        let durable_position =
            source_epoch::lane_position(&conn, first_epoch, SourceLane::Durable).unwrap();
        let source_id = first.envelope.opaque_source_id.clone();
        let incarnation = source_epoch::active_source_incarnation(&conn, "cursor", &source_id)
            .unwrap()
            .unwrap();
        let mut predecessor = first_epoch;
        for revision in ["fixture-empty-1", "fixture-empty-2", CURSOR_PARSER_REVISION] {
            let next = source_epoch::observe_source(
                &mut conn,
                "cursor",
                &source_id,
                &incarnation,
                0,
                SourceLane::Durable,
                0,
                Some(revision),
                None,
                SourceChangeHint::None,
            )
            .unwrap();
            assert_eq!(next.predecessor_epoch, Some(predecessor));
            predecessor = next.source_epoch;
        }
        cursor_store_records::append_unseen_cursor_records(
            &mut conn,
            predecessor,
            &[b"descendant-record".to_vec()],
        )
        .unwrap();

        let fresh = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(fresh.source_epoch, predecessor);
        assert_eq!(
            fresh.envelope.predecessor_source_epoch,
            Some(first_epoch.to_string())
        );
        let pending = pending_source_envelope::load_for_epoch(&conn, predecessor)
            .unwrap()
            .unwrap();
        let mut poisoned = fresh.envelope.clone();
        poisoned.predecessor_source_epoch = Some(
            source_epoch::resolution_for_epoch(&conn, predecessor)
                .unwrap()
                .predecessor_epoch
                .unwrap()
                .to_string(),
        );
        let poisoned_zstd = encode_zstd(
            &serde_json::to_vec(&poisoned).unwrap(),
            "poisoned fixture body",
        )
        .unwrap();
        pending_source_envelope::replace_request_body_after_render_conflict(
            &conn,
            predecessor,
            &pending.envelope_id,
            &pending.request_body_zstd,
            &poisoned_zstd,
        )
        .unwrap();
        pending_source_envelope::quarantine(
            &mut conn,
            predecessor,
            "source_epoch_conflict_unresolved",
            "source_epoch_not_found: fixture predecessor absent",
        )
        .unwrap();
        let poisoned_prepared = pending_to_prepared(
            pending_source_envelope::load_for_epoch(&conn, predecessor)
                .unwrap()
                .unwrap(),
        )
        .unwrap();

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let host_manifest = serde_json::json!({
            "v": 2,
            "source_epoch": {
                "source_epoch": first_epoch.to_string(),
                "tenant_id": fresh.envelope.tenant_id,
                "machine_id": fresh.envelope.machine_id,
                "provider": "cursor",
                "opaque_source_id": source_id,
                "range_kind": "record_ordinal",
                "state": "open",
                "predecessor_source_epoch": null,
                "replaced_by_source_epoch": null,
                "accepted_through": durable_position.to_string()
            },
            "objects": [],
            "commit_seq": "42",
            "observed_at": "2026-07-22T12:00:00Z"
        })
        .to_string();
        let server = tokio::spawn(async move {
            for (status, body) in [
                ("200 OK", host_manifest.clone()),
                (
                    "404 Not Found",
                    r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#.to_string(),
                ),
                (
                    "404 Not Found",
                    r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#.to_string(),
                ),
                (
                    "404 Not Found",
                    r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#.to_string(),
                ),
                ("200 OK", host_manifest),
            ] {
                let (mut socket, _) = listener.accept().await.unwrap();
                let _ = read_http_request(&mut socket).await;
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(), body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });
        let client = ShipperClient::with_compression(
            &ShipperConfig {
                api_url: format!("http://{address}"),
                timeout_seconds: 5,
                ..ShipperConfig::default()
            },
            CompressionAlgo::Gzip,
        )
        .unwrap();

        assert!(!reconcile_blocked_lineage(
            &mut conn,
            &client,
            &poisoned_prepared,
            Duration::from_secs(5),
        )
        .await
        .unwrap());
        assert!(
            pending_source_envelope::load_for_epoch(&conn, predecessor)
                .unwrap()
                .unwrap()
                .blocked_at
                .is_some(),
            "a hosted manifest for the requested epoch must keep the body quarantined"
        );
        assert!(reconcile_blocked_lineage(
            &mut conn,
            &client,
            &poisoned_prepared,
            Duration::from_secs(5),
        )
        .await
        .unwrap());
        server.await.unwrap();
        let repaired = pending_source_envelope::load_for_epoch(&conn, predecessor)
            .unwrap()
            .unwrap();
        assert!(repaired.blocked_at.is_none());
        assert_eq!(
            pending_to_prepared(repaired)
                .unwrap()
                .envelope
                .predecessor_source_epoch,
            Some(first_epoch.to_string())
        );
    }

    #[tokio::test]
    async fn blocked_cursor_reexamines_missing_local_epoch_through_host_authority() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let _store = make_cursor_store(&path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        pending_source_envelope::quarantine(
            &mut conn,
            prepared.source_epoch,
            "source_epoch_conflict_unresolved",
            "source_epoch_not_found: fixture missing local epoch",
        )
        .unwrap();

        // Inside its backoff the Cursor store lane must not touch the wire: it
        // reaches re-examination without passing through `ship_prepared_envelope`,
        // so a client with nothing listening proves the gate is on this door too.
        let closed = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let closed_address = closed.local_addr().unwrap();
        drop(closed);
        let unreachable = ShipperClient::with_compression(
            &ShipperConfig {
                api_url: format!("http://{closed_address}"),
                timeout_seconds: 1,
                ..ShipperConfig::default()
            },
            CompressionAlgo::Gzip,
        )
        .unwrap();
        for _ in 0..3 {
            assert!(matches!(
                ship_next_cursor_envelope(
                    &mut conn,
                    &unreachable,
                    &capabilities(),
                    &path,
                    "live",
                    Duration::from_secs(1),
                )
                .await
                .unwrap(),
                CursorStorageV2ShipResult::Current
            ));
        }
        // A restart is new evidence: the row is due and the recovery runs.
        assert_eq!(
            pending_source_envelope::wake_blocked_for_new_engine(&conn).unwrap(),
            1
        );

        let host_epoch = Uuid::new_v4();
        let host_manifest = serde_json::json!({
            "v": 2,
            "source_epoch": {
                "source_epoch": host_epoch.to_string(),
                "tenant_id": prepared.envelope.tenant_id,
                "machine_id": prepared.envelope.machine_id,
                "provider": "cursor",
                "opaque_source_id": prepared.envelope.opaque_source_id,
                "range_kind": "record_ordinal",
                "state": "open",
                "predecessor_source_epoch": null,
                "replaced_by_source_epoch": null,
                "accepted_through": "2"
            },
            "objects": [{
                "envelope_id": "host-envelope",
                "tenant_id": prepared.envelope.tenant_id,
                "session_id": prepared.envelope.session_id,
                "machine_id": prepared.envelope.machine_id,
                "provider": "cursor",
                "opaque_source_id": prepared.envelope.opaque_source_id,
                "source_epoch": host_epoch.to_string(),
                "range_kind": "record_ordinal",
                "range_start": "0",
                "range_end": "2",
                "retired_at": null
            }],
            "commit_seq": "1",
            "observed_at": "2026-09-01T00:00:00Z"
        })
        .to_string();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let expected_source_epoch = prepared.source_epoch;
        let expected_host_epoch = host_epoch;
        let server = tokio::spawn(async move {
            for request_index in 0..3 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, body) = read_http_request(&mut socket).await;
                let (status, response_body) = match request_index {
                    0 => {
                        assert!(request_line.contains(&expected_source_epoch.to_string()));
                        ("404 Not Found", r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#.to_string())
                    }
                    1 => {
                        let posted: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                        assert_eq!(posted.source_epoch, expected_source_epoch.to_string());
                        (
                            "409 Conflict",
                            serde_json::json!({
                                "detail": {
                                    "code": "source_epoch_conflict",
                                    "message": "source range overlaps or conflicts with the registered epoch",
                                    "details": {
                                        "reason": "another_epoch_is_already_open_for_this_source",
                                        "open_source_epochs": [expected_host_epoch.to_string()]
                                    }
                                }
                            })
                            .to_string(),
                        )
                    }
                    _ => {
                        assert!(request_line.contains(&expected_host_epoch.to_string()));
                        ("200 OK", host_manifest.clone())
                    }
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });
        let client = ShipperClient::with_compression(
            &ShipperConfig {
                api_url: format!("http://{address}"),
                timeout_seconds: 5,
                ..ShipperConfig::default()
            },
            CompressionAlgo::Gzip,
        )
        .unwrap();

        assert!(matches!(
            ship_next_cursor_envelope(
                &mut conn,
                &client,
                &capabilities(),
                &path,
                "repair",
                Duration::from_secs(5),
            )
            .await
            .unwrap(),
            CursorStorageV2ShipResult::Continue
        ));
        server.await.unwrap();
        let repaired = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .unwrap();
        assert!(repaired.blocked_at.is_none());
        assert_eq!(
            pending_to_prepared(repaired)
                .unwrap()
                .envelope
                .predecessor_source_epoch,
            Some(host_epoch.to_string())
        );
    }

    #[test]
    fn cursor_unattempted_obsolete_pending_render_is_rebuilt_before_shipping() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        store
            .execute(
                "UPDATE blobs SET data = ?1 WHERE id = ?2",
                params![
                    br#"{"role":"assistant","content":[{"type":"text","text":"legacy render"}]}"#,
                    CURSOR_MESSAGE_A,
                ],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let pending = pending_source_envelope::load_for_epoch(&conn, first.source_epoch)
            .unwrap()
            .unwrap();
        let mut obsolete = first.envelope.clone();
        obsolete.render.as_mut().unwrap().parser_revision =
            "cursor-store-render-v3-receipts".to_string();
        let obsolete_body = serde_json::to_vec(&obsolete).unwrap();
        let obsolete_body_zstd = encode_zstd(&obsolete_body, "obsolete Cursor request").unwrap();
        pending_source_envelope::replace_request_body_after_render_conflict(
            &conn,
            first.source_epoch,
            &pending.envelope_id,
            &pending.request_body_zstd,
            &obsolete_body_zstd,
        )
        .unwrap();
        conn.execute(
            "UPDATE source_epoch_registry SET source_revision = NULL WHERE source_epoch = ?1",
            [first.source_epoch.to_string()],
        )
        .unwrap();
        let rebuilt = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(
            rebuilt.source_epoch, first.source_epoch,
            "the old undrained raw epoch must ship before its parser-replay successor"
        );
        assert_eq!(
            rebuilt.envelope.render.as_ref().unwrap().parser_revision,
            CURSOR_PARSER_REVISION
        );
        let active_epoch: String = conn
            .query_row(
                "SELECT source_epoch FROM source_epoch_registry
                 WHERE provider = 'cursor' AND opaque_source_id = ?1 AND ended_at IS NULL",
                [&first.envelope.opaque_source_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_ne!(active_epoch, first.source_epoch.to_string());
    }

    #[tokio::test]
    async fn cursor_generation_conflict_adopts_hosted_revision_without_changing_raw_identity() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        store
            .execute(
                "UPDATE blobs SET data = ?1 WHERE id = ?2",
                params![
                    br#"{"role":"assistant","content":[{"type":"text","text":"hello"}]}"#,
                    CURSOR_MESSAGE_A,
                ],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let stable_generation_id = prepared
            .envelope
            .render
            .as_ref()
            .unwrap()
            .generation_id
            .clone();
        let obsolete_generation_id = Uuid::new_v4().to_string();
        let mut obsolete_envelope = prepared.envelope.clone();
        obsolete_envelope.render.as_mut().unwrap().generation_id = obsolete_generation_id.clone();
        let pending = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .unwrap();
        let obsolete_body = serde_json::to_vec(&obsolete_envelope).unwrap();
        let obsolete_body_zstd = encode_zstd(&obsolete_body, "obsolete Cursor request").unwrap();
        pending_source_envelope::replace_request_body_after_render_conflict(
            &conn,
            prepared.source_epoch,
            &pending.envelope_id,
            &pending.request_body_zstd,
            &obsolete_body_zstd,
        )
        .unwrap();
        let obsolete_prepared = pending_to_prepared(
            pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
                .unwrap()
                .unwrap(),
        )
        .unwrap();

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let expected_envelope_id = prepared.envelope.expected_envelope_id.clone();
        let hosted_generation_id = stable_generation_id.clone();
        let requested_generation_id = obsolete_generation_id.clone();
        let server = tokio::spawn(async move {
            for request_index in 0..2 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, body) = read_http_request(&mut socket).await;
                assert!(request_line.starts_with("POST /api/agents/storage/v2/envelopes "));
                let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                assert_eq!(envelope.expected_envelope_id, expected_envelope_id);
                let render = envelope.render.as_ref().unwrap();
                let response_body = if request_index == 0 {
                    assert_eq!(render.generation_id, requested_generation_id);
                    serde_json::json!({
                        "detail": {
                            "code": "source_epoch_conflict",
                            "message": "render generation drift",
                            "details": {
                                "reason": "render_generation_revision_conflict",
                                "existing_generation_id": hosted_generation_id,
                                "requested_generation_id": requested_generation_id,
                                "parser_revision": render.parser_revision,
                                "ordering_revision": render.ordering_revision,
                            }
                        }
                    })
                    .to_string()
                } else {
                    assert_eq!(render.generation_id, hosted_generation_id);
                    serde_json::json!({
                        "v": 2,
                        "envelope_id": envelope.expected_envelope_id,
                        "object_hash": "b".repeat(64),
                        "commit_seq": "42",
                        "raw_state": "durable",
                        "render_state": "ready",
                        "media_state": "complete",
                        "missing_media_hashes": [],
                    })
                    .to_string()
                };
                let status = if request_index == 0 {
                    "409 Conflict"
                } else {
                    "200 OK"
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let reconciled = ship_prepared_envelope(
            &mut conn,
            &client,
            &capabilities(),
            obsolete_prepared,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap();
        assert_eq!(reconciled.bytes_shipped, 0);
        assert!(reconciled.has_more);
        let repaired = pending_to_prepared(
            pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            repaired.envelope.render.as_ref().unwrap().generation_id,
            stable_generation_id
        );
        let shipped = ship_prepared_envelope(
            &mut conn,
            &client,
            &capabilities(),
            repaired,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap();
        assert!(shipped.bytes_shipped > 0);
        assert!(
            pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
                .unwrap()
                .is_none()
        );
        server.await.unwrap();
    }

    #[test]
    fn cursor_render_hides_injected_context_and_unwraps_real_user_query() {
        assert_eq!(
            classify_cursor_text(
                "user",
                "<user_info>darwin</user_info><rules>workspace</rules>"
            ),
            (
                "system".to_string(),
                "<user_info>darwin</user_info><rules>workspace</rules>".to_string()
            )
        );
        assert_eq!(
            classify_cursor_text(
                "user",
                "<user_info>darwin</user_info><user_query>  ship it  </user_query>"
            ),
            ("user".to_string(), "ship it".to_string())
        );
        assert_eq!(
            classify_cursor_text(
                "user",
                "<agent_transcripts>\n<user_query>old history</user_query>\n</agent_transcripts>\n<user_query>current prompt</user_query>"
            ),
            ("user".to_string(), "current prompt".to_string())
        );
        assert_eq!(
            classify_cursor_text("user", "Please explain the literal <rules> marker."),
            (
                "user".to_string(),
                "Please explain the literal <rules> marker.".to_string()
            )
        );
        assert_eq!(
            classify_cursor_text("user", "plain follow-up"),
            ("user".to_string(), "plain follow-up".to_string())
        );
    }

    #[test]
    fn cursor_prepares_raw_records_without_a_root_pointer() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        let metadata =
            format!(r#"{{"agentId":"{CURSOR_CONVERSATION_ID}","createdAt":1773403200000}}"#);
        let encoded: String = metadata
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        store
            .execute("UPDATE meta SET value = ?1 WHERE key = '0'", [encoded])
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.envelope.provider, "cursor");
        assert!(prepared.envelope.records.iter().all(|record| {
            let bytes = BASE64_STANDARD.decode(&record.data_b64).unwrap();
            let raw: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            raw["kind"] != "root_observation"
        }));
    }

    #[test]
    fn prepares_exact_raw_bytes_and_versioned_render_without_advancing_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let bytes = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        fs::write(&path, bytes).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.range_start, 0);
        assert_eq!(prepared.range_end, bytes.len() as u64);
        assert_eq!(
            prepared.envelope.records[0].data_b64,
            BASE64_STANDARD.encode(bytes)
        );
        assert_eq!(prepared.envelope.render.as_ref().unwrap().records.len(), 1);
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            0
        );
    }

    /// A host that accepts zstd receives the envelope bytes exactly as stored,
    /// so the wire carries the compressed form and an ambiguous outcome still
    /// retries identical bytes. An identity-only host gets them decompressed.
    #[test]
    fn zstd_host_receives_the_stored_envelope_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(
            &path,
            b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let identity_host = capabilities();
        let mut zstd_host = capabilities();
        zstd_host.envelope_content_encodings = vec!["zstd".to_string(), "identity".to_string()];
        assert_eq!(
            identity_host.envelope_body_encoding(),
            StorageV2BodyEncoding::Identity
        );
        assert_eq!(
            zstd_host.envelope_body_encoding(),
            StorageV2BodyEncoding::Zstd
        );

        let (identity_body, prepared) = prepare_next_envelope_body_for_lane(
            &mut conn,
            &identity_host,
            &path,
            "claude",
            "repair",
        )
        .unwrap()
        .unwrap();
        let (zstd_body, reloaded) =
            prepare_next_envelope_body_for_lane(&mut conn, &zstd_host, &path, "claude", "repair")
                .unwrap()
                .unwrap();
        let pending = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .unwrap();

        assert_eq!(
            reloaded.envelope.expected_envelope_id,
            prepared.envelope.expected_envelope_id
        );
        assert_eq!(zstd_body, pending.request_body_zstd);
        assert_eq!(decode_zstd(&zstd_body, "test").unwrap(), identity_body);
        serde_json::from_slice::<serde_json::Value>(&identity_body).unwrap();
    }

    #[test]
    fn cursor_agent_transcript_rewrite_rotates_source_epoch_before_framing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join(".cursor")
            .join("projects/workspace/agent-transcripts/session/transcript.jsonl");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let first = br#"{"role":"user","message":{"content":[{"type":"text","text":"hello"}]}}
"#;
        fs::write(&path, first).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let initial = prepare_next_envelope(&mut conn, &capabilities(), &path, "cursor", None)
            .unwrap()
            .unwrap();
        acknowledge_prepared(&mut conn, &initial);

        // Cursor may replace an in-flight JSONL record in place. The new
        // content is longer, so the old byte cursor would point into its
        // middle if the source stayed in the same epoch.
        let rewritten = br#"{"role":"user","message":{"content":[{"type":"text","text":"hello after a live rewrite"}]}}
"#;
        fs::write(&path, rewritten).unwrap();

        let replacement = prepare_next_envelope(&mut conn, &capabilities(), &path, "cursor", None)
            .unwrap()
            .unwrap();
        let initial_epoch = initial.source_epoch.to_string();
        assert_ne!(replacement.source_epoch, initial.source_epoch);
        assert_eq!(replacement.range_start, 0);
        assert_eq!(replacement.range_end, rewritten.len() as u64);
        assert_eq!(
            replacement.envelope.predecessor_source_epoch.as_deref(),
            Some(initial_epoch.as_str())
        );
    }

    #[test]
    fn cursor_agent_transcript_skips_unshipped_rewrite_epochs() {
        let dir = tempfile::tempdir().unwrap();
        let conversation_id = "22a940a2-8256-4042-856e-a3b5ade40bd6";
        let path = dir
            .path()
            .join(".cursor/projects/workspace/agent-transcripts")
            .join(conversation_id)
            .join(format!("{conversation_id}.jsonl"));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(
            &path,
            b"{\"role\":\"user\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"hello\"}]}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let managed_session = "018f0c3a-7b2d-7f10-8a11-123456789abd";

        let initial = prepare_next_envelope(
            &mut conn,
            &capabilities(),
            &path,
            "cursor",
            Some(managed_session),
        )
        .unwrap()
        .unwrap();
        acknowledge_prepared(&mut conn, &initial);

        for partial in [b"{".as_slice(), b"{\"role\":\"assistant\"".as_slice()] {
            fs::write(&path, partial).unwrap();
            assert!(prepare_next_envelope(
                &mut conn,
                &capabilities(),
                &path,
                "cursor",
                Some(managed_session),
            )
            .unwrap()
            .is_none());
        }

        fs::write(
            &path,
            b"{\"role\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"finished\"}]}}\n",
        )
        .unwrap();
        let final_revision = prepare_next_envelope(
            &mut conn,
            &capabilities(),
            &path,
            "cursor",
            Some(managed_session),
        )
        .unwrap()
        .unwrap();

        assert_eq!(
            final_revision.envelope.predecessor_source_epoch,
            Some(initial.source_epoch.to_string())
        );
        assert_ne!(
            source_epoch::resolution_for_epoch(&conn, final_revision.source_epoch)
                .unwrap()
                .predecessor_epoch,
            Some(initial.source_epoch),
            "the wire predecessor differs from the immediate empty revision"
        );
    }

    #[test]
    fn skips_provider_startup_metadata_without_freezing_a_durable_envelope() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let metadata_line =
            r#"{"type":"mode","mode":"normal","sessionId":"018f0c3a-7b2d-7f10-8a11-123456789abc"}"#;
        let metadata = (0..8)
            .map(|_| format!("{metadata_line}\n"))
            .collect::<String>();
        let user = r#"{"type":"user","uuid":"u1","timestamp":"2026-07-12T12:00:00Z","message":{"content":"hello"}}"#;
        fs::write(&path, format!("{metadata}{user}\n")).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let prepared = prepare_next_envelope_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            "claude",
            None,
            metadata.len(),
        )
        .unwrap()
        .unwrap();

        assert_eq!(prepared.range_start, metadata.len() as u64);
        assert_eq!(prepared.event_count, 1);
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 1);
    }

    #[test]
    fn ships_late_claude_startup_metadata_after_an_accepted_prefix() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let user = r#"{"type":"user","uuid":"u1","timestamp":"2026-07-12T12:00:00Z","message":{"content":"hello"}}"#;
        let metadata_line =
            r#"{"type":"mode","mode":"normal","sessionId":"018f0c3a-7b2d-7f10-8a11-123456789abc"}"#;
        let user_line = format!("{user}\n");
        fs::write(&path, &user_line).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        acknowledge_prepared(&mut conn, &first);
        fs::write(&path, format!("{user_line}{metadata_line}\n")).unwrap();

        let late_metadata =
            prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                .unwrap()
                .unwrap();

        assert_eq!(late_metadata.source_epoch, first.source_epoch);
        assert_eq!(late_metadata.range_start, user_line.len() as u64);
        assert_eq!(late_metadata.event_count, 0);
        assert_eq!(
            late_metadata
                .envelope
                .render
                .as_ref()
                .unwrap()
                .records
                .len(),
            0
        );
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 1);
    }

    /// Prepare the first envelope for one transcript and return its session facts.
    fn shipped_session_facts(
        provider: &str,
        file_name: &str,
        lines: &str,
    ) -> StorageV2SessionFacts {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(file_name);
        fs::write(&path, lines).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        prepare_next_envelope(&mut conn, &capabilities(), &path, provider, None)
            .unwrap()
            .unwrap()
            .envelope
            .session
    }

    #[test]
    fn claude_session_ships_the_cli_version_from_its_transcript() {
        let session = shipped_session_facts(
            "claude",
            "claude-version.jsonl",
            "{\"type\":\"user\",\"uuid\":\"019c638d-0000-0000-0000-0000000000bb\",\"timestamp\":\"2026-01-01T00:00:00Z\",\"message\":{\"content\":\"hi\"},\"cwd\":\"/tmp/proj\",\"version\":\"2.1.142\"}\n",
        );

        assert_eq!(session.provider_version.as_deref(), Some("2.1.142"));
    }

    #[test]
    fn codex_session_ships_the_cli_version_from_session_meta() {
        let session = shipped_session_facts(
            "codex",
            "rollout-2026-02-15T10-00-00-eeee5555.jsonl",
            concat!(
                r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"session_meta","id":"eeeeeeee-1111-2222-3333-444455556666","cwd":"/tmp/test","cli_version":"0.145.0"}}"#,
                "\n",
                r#"{"type":"response_item","timestamp":"2026-02-15T10:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}}"#,
                "\n",
            ),
        );

        assert_eq!(session.provider_version.as_deref(), Some("0.145.0"));
    }

    #[test]
    fn omp_session_ships_no_cli_version_even_though_its_header_carries_one() {
        // The header version is OMP's file-format number, not a CLI release.
        let session = shipped_session_facts(
            "omp",
            "omp-version.jsonl",
            concat!(
                r#"{"type":"session","version":3,"id":"omp-version-1","timestamp":"2026-09-09T00:00:00.000Z","cwd":"/tmp/omp"}"#,
                "\n",
                r#"{"type":"message","id":"omp-user-01","parentId":null,"timestamp":"2026-09-09T00:00:01.000Z","message":{"role":"user","content":[{"type":"text","text":"hello"}]}}"#,
                "\n",
            ),
        );

        assert_eq!(session.provider_version, None);
        let wire = serde_json::to_value(&session).unwrap();
        assert!(wire.get("provider_version").is_none());
    }

    fn codex_forked_child_lines() -> String {
        concat!(
            r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"session_meta","id":"dddddddd-1111-2222-3333-444455556666","forked_from_id":"cccccccc-1111-2222-3333-444455556666","cwd":"/tmp/test","cli_version":"0.1.0"}}"#,
            "\n",
            r#"{"type":"response_item","timestamp":"2026-02-15T10:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from the fork"}]}}"#,
            "\n",
        )
        .to_string()
    }

    const FORK_CHILD_THREAD: &str = "dddddddd-1111-2222-3333-444455556666";

    /// A fork Longhouse started binds the child's path to the child's session
    /// and records the thread it bound. That binding names this transcript's own
    /// thread, so it is the child's identity and must be honored.
    #[test]
    fn codex_fork_bound_for_its_own_thread_is_owned_and_visible() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-2026-02-15T10-00-00-dddd1111.jsonl");
        fs::write(&path, codex_forked_child_lines()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let child_longhouse_id = "019d2869-1111-7222-8333-aaaaaaaaaaaa";
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind_for_thread(
                &stable_source_path(&path).to_string_lossy(),
                child_longhouse_id,
                "codex",
                Some(FORK_CHILD_THREAD),
            )
            .unwrap();

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "codex", None)
            .unwrap()
            .unwrap();

        assert_eq!(prepared.envelope.session_id, child_longhouse_id);
        assert!(!prepared.envelope.session.is_subagent);
        assert!(!prepared.envelope.session.hidden_from_default_timeline);
    }

    /// A fork taken by hand inherits whatever binding the managed parent left
    /// behind. That binding names the parent's thread, not this one, so it says
    /// nothing about who owns this transcript: keep the provider's own id and
    /// stay behind the parent, exactly as before this split existed.
    #[test]
    fn codex_fork_bound_for_another_thread_keeps_its_own_identity() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-2026-02-15T10-00-00-inherited.jsonl");
        fs::write(&path, codex_forked_child_lines()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        crate::state::session_binding::SessionBinding::new(&conn)
            .bind_for_thread(
                &stable_source_path(&path).to_string_lossy(),
                "019d2869-1111-7222-8333-aaaaaaaaaaaa",
                "codex",
                Some("cccccccc-1111-2222-3333-444455556666"),
            )
            .unwrap();

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "codex", None)
            .unwrap()
            .unwrap();

        assert_eq!(prepared.envelope.session_id, FORK_CHILD_THREAD);
        assert!(prepared.envelope.session.hidden_from_default_timeline);
    }

    /// A binding written before threads were recorded cannot prove anything.
    /// Absence of evidence is not an exact match.
    #[test]
    fn codex_fork_with_threadless_binding_stays_hidden() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout-2026-02-15T10-00-00-legacy.jsonl");
        fs::write(&path, codex_forked_child_lines()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(
                &stable_source_path(&path).to_string_lossy(),
                "019d2869-1111-7222-8333-aaaaaaaaaaaa",
                "codex",
            )
            .unwrap();

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "codex", None)
            .unwrap()
            .unwrap();

        assert_eq!(prepared.envelope.session_id, FORK_CHILD_THREAD);
        assert!(prepared.envelope.session.hidden_from_default_timeline);
    }

    #[test]
    fn fresh_antigravity_source_waits_for_managed_binding() {
        let dir = tempfile::tempdir().unwrap();
        let provider_id = "018f0c3a-7b2d-7f10-8a11-123456789abc";
        let path = dir
            .path()
            .join("brain")
            .join(provider_id)
            .join(".system_generated/logs/transcript_full.jsonl");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(
            &path,
            concat!(
                r#"{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","status":"DONE","created_at":"2026-07-12T12:00:00Z","content":"hello"}"#,
                "\n",
                r#"{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","created_at":"2026-07-12T12:00:01Z","content":"hi"}"#,
                "\n"
            ),
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        assert!(
            prepare_next_envelope(&mut conn, &capabilities(), &path, "antigravity", None)
                .unwrap()
                .is_none()
        );
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);

        let managed_session_id = "018f0c3a-7b2d-7f10-8a11-123456789abd";
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(
                &stable_source_path(&path).to_string_lossy(),
                managed_session_id,
                "antigravity",
            )
            .unwrap();
        let prepared =
            prepare_next_envelope(&mut conn, &capabilities(), &path, "antigravity", None)
                .unwrap()
                .unwrap();

        assert_eq!(prepared.envelope.session_id, managed_session_id);
        assert_eq!(
            source_epoch::resolution_for_epoch(&conn, prepared.source_epoch)
                .unwrap()
                .bound_session_id
                .as_deref(),
            Some(managed_session_id)
        );

        crate::state::session_binding::SessionBinding::new(&conn)
            .unbind(&stable_source_path(&path).to_string_lossy())
            .unwrap();
        let exact_retry =
            prepare_next_envelope(&mut conn, &capabilities(), &path, "antigravity", None)
                .unwrap()
                .unwrap();
        assert_eq!(
            exact_retry.envelope.expected_envelope_id,
            prepared.envelope.expected_envelope_id
        );
    }

    #[test]
    fn antigravity_mirror_hint_cannot_duplicate_a_snapshot_or_erase_later_steps() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(
            "brain/eb514596-95e0-4e96-a1cc-e355b576127c/.system_generated/logs/transcript_full.jsonl",
        );
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let mirror = path.with_file_name("transcript.jsonl");
        // The live failure's sources differ in tool argument representation,
        // but name the same native final step. They are not byte-identical.
        let tool = r#"{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","created_at":"2026-09-07T02:15:54Z","tool_calls":[{"name":"run_command","args":{"CommandLine":"sleep 6"}}]}"#;
        let reply = r#"{"step_index":3,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","created_at":"2026-09-07T02:16:02Z","content":"repeated answer"}"#;
        let result = r#"{"step_index":2,"source":"MODEL","type":"GENERIC","status":"DONE","created_at":"2026-09-07T02:16:01Z","content":"full tool output retained"}"#;
        let native = format!("{tool}\n{result}\n{reply}\n");
        let summary = native
            .replace("sleep 6", r#"\"sleep 6\""#)
            .replace("full tool output retained", "[truncated]");
        fs::write(&path, &native).unwrap();
        fs::write(&mirror, &summary).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let session_id = "c75c18e2-2726-4ad6-94a4-0756fdb340c4";
        let first = prepare_next_envelope(
            &mut conn,
            &capabilities(),
            &path,
            "antigravity",
            Some(session_id),
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            first
                .envelope
                .render
                .as_ref()
                .unwrap()
                .records
                .iter()
                .filter(|record| record.event_id == "antigravity-step-3-content")
                .count(),
            1
        );
        acknowledge_prepared(&mut conn, &first);
        let hinted = crate::discovery::canonical_transcript_hint("antigravity", &mirror);
        assert!(prepare_next_envelope(
            &mut conn,
            &capabilities(),
            &hinted,
            "antigravity",
            Some(session_id)
        )
        .unwrap()
        .is_none());

        // A later native snapshot may repeat the exact reply at a distinct step.
        // It must rotate this source's epoch, not dedupe prose or lose raw history.
        let repeated = reply.replace("\"step_index\":3", "\"step_index\":5");
        let updated = format!("{native}{repeated}\n");
        fs::write(&path, &updated).unwrap();
        let next = prepare_next_envelope(
            &mut conn,
            &capabilities(),
            &hinted,
            "antigravity",
            Some(session_id),
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            next.envelope.opaque_source_id,
            first.envelope.opaque_source_id
        );
        assert_eq!(
            next.envelope.predecessor_source_epoch,
            Some(first.source_epoch.to_string())
        );
        let replies = next
            .envelope
            .render
            .as_ref()
            .unwrap()
            .records
            .iter()
            .filter(|record| record.content_text.as_deref() == Some("repeated answer"))
            .map(|record| record.event_id.as_str())
            .collect::<Vec<_>>();
        assert_eq!(
            replies,
            vec!["antigravity-step-3-content", "antigravity-step-5-content"]
        );
        assert_eq!(
            decode_envelope_record_bytes(&first.envelope.records).unwrap(),
            vec![
                format!("{tool}\n").into_bytes(),
                format!("{result}\n").into_bytes(),
                format!("{reply}\n").into_bytes(),
            ]
        );
        assert_eq!(
            decode_envelope_record_bytes(&next.envelope.records).unwrap(),
            vec![
                format!("{tool}\n").into_bytes(),
                format!("{result}\n").into_bytes(),
                format!("{reply}\n").into_bytes(),
                format!("{repeated}\n").into_bytes(),
            ]
        );
        assert_eq!(fs::read(&mirror).unwrap(), summary.into_bytes());
    }

    #[test]
    fn antigravity_pending_init_holds_sources_then_binds_only_its_native_thread() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let _guard = crate::console_adapter::longhouse_home_test_guard();
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("longhouse");
        let agent_dir = home.join("agent");
        let registry = crate::turn_claims::TurnClaimRegistry::new(agent_dir.join("turn-claims"));
        let session_id = Uuid::new_v4().to_string();
        let run_id = Uuid::new_v4().to_string();
        let native_id = Uuid::new_v4().to_string();
        let shadow_id = Uuid::new_v4().to_string();
        registry
            .claim(
                &run_id,
                &session_id,
                &Uuid::new_v4().to_string(),
                None,
                None,
                "antigravity",
            )
            .unwrap();
        let run_dir = agent_dir
            .join("antigravity-console")
            .join(&session_id)
            .join(&run_id);
        fs::create_dir_all(&run_dir).unwrap();
        let stdout = run_dir.join("stdout.log");
        // An unrelated malformed historical output and a damaged claim file
        // must not poison this launch or permanently suppress genuine Shadow.
        let bad_run = Uuid::new_v4().to_string();
        registry
            .claim(
                &bad_run,
                &session_id,
                &Uuid::new_v4().to_string(),
                None,
                None,
                "antigravity",
            )
            .unwrap();
        registry
            .mark_failed(&bad_run, "historical failure")
            .unwrap();
        let bad_dir = run_dir.with_file_name(&bad_run);
        fs::create_dir_all(&bad_dir).unwrap();
        fs::write(
            bad_dir.join("stdout.log"),
            b"{\"conversation_id\":\"not-a-uuid\",\"status\":\"ERROR\"}\n",
        )
        .unwrap();
        fs::write(
            agent_dir
                .join("turn-claims")
                .join(format!("{}.json", Uuid::new_v4())),
            b"damaged claim",
        )
        .unwrap();

        let source = b"{\"step_index\":0,\"source\":\"USER_EXPLICIT\",\"type\":\"USER_INPUT\",\"status\":\"DONE\",\"created_at\":\"2026-09-06T22:07:35Z\",\"content\":\"hello\"}\n";
        let transcript = |id: &str| {
            let path = dir
                .path()
                .join("brain")
                .join(id)
                .join(".system_generated/logs/transcript_full.jsonl");
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, source).unwrap();
            fs::File::options()
                .write(true)
                .open(&path)
                .unwrap()
                .set_times(
                    fs::FileTimes::new()
                        .set_modified(std::time::SystemTime::now() - Duration::from_secs(60)),
                )
                .unwrap();
            path
        };
        let path = transcript(&native_id);
        let shadow = transcript(&shadow_id);
        temp_env::with_var("LONGHOUSE_HOME", Some(&home), || {
            let mut conn = open_db(Some(&dir.path().join("first.db"))).unwrap();
            // Neither a missing init nor a partial write can age out to Shadow,
            // even after the ordinary five-second hook grace has elapsed.
            assert!(
                prepare_next_envelope(&mut conn, &capabilities(), &path, "antigravity", None)
                    .unwrap()
                    .is_none()
            );
            fs::write(&stdout, b"{\"event\":\"init\",\"conversation_id\":\"").unwrap();
            assert!(
                prepare_next_envelope(&mut conn, &capabilities(), &path, "antigravity", None)
                    .unwrap()
                    .is_none()
            );
            assert!(prepare_next_envelope(
                &mut conn,
                &capabilities(),
                &shadow,
                "antigravity",
                None
            )
            .unwrap()
            .is_none());
            assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
            drop(conn);

            // Pending ownership also survives a cold shipper restart. Init is
            // the identity proof; no result or monitor-written binding exists.
            let mut conn = open_db(Some(&dir.path().join("first.db"))).unwrap();
            assert!(
                prepare_next_envelope(&mut conn, &capabilities(), &path, "antigravity", None)
                    .unwrap()
                    .is_none()
            );
            fs::write(
                &stdout,
                format!("{{\"event\":\"init\",\"conversation_id\":\"{native_id}\"}}\n"),
            )
            .unwrap();
            conn.execute_batch("CREATE TRIGGER reject_binding BEFORE INSERT ON session_binding BEGIN SELECT RAISE(FAIL, 'binding unavailable'); END;").unwrap();
            assert!(
                prepare_next_envelope(&mut conn, &capabilities(), &path, "antigravity", None)
                    .is_err()
            );
            assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
            conn.execute_batch("DROP TRIGGER reject_binding").unwrap();
            let prepared =
                prepare_next_envelope(&mut conn, &capabilities(), &path, "antigravity", None)
                    .unwrap()
                    .unwrap();
            assert_eq!(prepared.envelope.session_id, session_id);
            let unrelated =
                prepare_next_envelope(&mut conn, &capabilities(), &shadow, "antigravity", None)
                    .unwrap()
                    .unwrap();
            assert_eq!(unrelated.envelope.session_id, shadow_id);
            assert_eq!(fs::read(&path).unwrap().as_slice(), source);
            drop(conn);

            // A terminal claim without a monitor-written binding still owns
            // its source through the durable structured stdout after restart.
            registry.mark_failed(&run_id, "engine restarted").unwrap();
            let mut recovered = open_db(Some(&dir.path().join("recovered.db"))).unwrap();
            let prepared =
                prepare_next_envelope(&mut recovered, &capabilities(), &path, "antigravity", None)
                    .unwrap()
                    .unwrap();
            assert_eq!(prepared.envelope.session_id, session_id);
            drop(recovered);

            // Retained single-JSON stdout from deployed versions also recovers
            // exact ownership without init, logs, or a confirmed binding.
            fs::write(
                &stdout,
                format!("{{\"conversation_id\":\"{native_id}\",\"status\":\"SUCCESS\"}}\n"),
            )
            .unwrap();
            let mut legacy = open_db(Some(&dir.path().join("legacy.db"))).unwrap();
            let prepared =
                prepare_next_envelope(&mut legacy, &capabilities(), &path, "antigravity", None)
                    .unwrap()
                    .unwrap();
            assert_eq!(prepared.envelope.session_id, session_id);
        });
    }

    #[test]
    fn durable_prepare_survives_source_growth_restart_and_deletion() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let db_path = dir.path().join("state.db");
        let first_line = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let second_line = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"later\"}}\n";
        fs::write(&path, first_line).unwrap();
        let mut conn = open_db(Some(&db_path)).unwrap();

        let first = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        let persisted_first = pending_source_envelope::load_for_epoch(&conn, first.source_epoch)
            .unwrap()
            .unwrap();
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 1);

        fs::write(
            &path,
            [first_line.as_slice(), second_line.as_slice()].concat(),
        )
        .unwrap();
        drop(conn);
        let mut conn = open_db(Some(&db_path)).unwrap();
        let after_growth = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        let persisted_after_growth =
            pending_source_envelope::load_for_epoch(&conn, first.source_epoch)
                .unwrap()
                .unwrap();
        assert_eq!(after_growth.source_epoch, first.source_epoch);
        assert_eq!(after_growth.range_end, first_line.len() as u64);
        assert_eq!(
            after_growth.envelope.expected_envelope_id,
            first.envelope.expected_envelope_id
        );
        assert_eq!(
            persisted_after_growth.request_body_zstd,
            persisted_first.request_body_zstd
        );

        fs::remove_file(&path).unwrap();
        drop(conn);
        let mut conn = open_db(Some(&db_path)).unwrap();
        let after_deletion =
            prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                .unwrap()
                .unwrap();
        assert_eq!(
            after_deletion.envelope.expected_envelope_id,
            first.envelope.expected_envelope_id
        );
        assert_eq!(
            serde_json::to_vec(&after_deletion.envelope.records).unwrap(),
            serde_json::to_vec(&first.envelope.records).unwrap()
        );
    }

    #[test]
    fn receipt_acknowledgement_updates_cursor_and_deletes_intent_atomically() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(
            &path,
            b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();

        let error = pending_source_envelope::acknowledge_and_delete(
            &mut conn,
            prepared.source_epoch,
            &"f".repeat(64),
            prepared.range_start,
            prepared.range_end,
        )
        .unwrap_err();
        assert!(error
            .to_string()
            .contains("does not match the durable pending envelope"));
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            0
        );
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 1);

        acknowledge_prepared(&mut conn, &prepared);
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            prepared.range_end
        );
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
        pending_source_envelope::acknowledge_and_delete(
            &mut conn,
            prepared.source_epoch,
            &prepared.envelope.expected_envelope_id,
            prepared.range_start,
            prepared.range_end,
        )
        .unwrap();
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            prepared.range_end,
            "a concurrent exact-replay receipt must be an idempotent local success"
        );
    }

    #[test]
    fn unsent_product_gate_can_discard_and_refreeze_after_reply_arrives() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let user = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let assistant = b"{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"hi\"}]}}\n";
        fs::write(&path, user).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let user_only = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert!(!user_only.has_reply_evidence);
        assert!(pending_source_envelope::discard_unattempted(
            &conn,
            user_only.source_epoch,
            &user_only.envelope.expected_envelope_id,
        )
        .unwrap());

        fs::write(&path, [user.as_slice(), assistant.as_slice()].concat()).unwrap();
        let with_reply = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert!(with_reply.has_reply_evidence);
        assert_eq!(with_reply.range_end, (user.len() + assistant.len()) as u64);
    }

    #[test]
    fn live_prepare_refreezes_unattempted_backlog_to_live_budget() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let content = "x".repeat(40 * 1024);
        let first = format!(
            "{{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{{\"content\":{}}}}}\n",
            serde_json::to_string(&content).unwrap()
        );
        let second = format!(
            "{{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{{\"content\":{}}}}}\n",
            serde_json::to_string(&content).unwrap()
        );
        fs::write(&path, format!("{first}{second}")).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let backlog = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert!(backlog.raw_bytes > LIVE_TARGET_BATCH_BYTES as u64);

        let live = prepare_next_envelope_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            "claude",
            None,
            LIVE_TARGET_BATCH_BYTES,
        )
        .unwrap()
        .unwrap();
        assert_ne!(
            live.envelope.expected_envelope_id,
            backlog.envelope.expected_envelope_id
        );
        assert!(live.raw_bytes <= LIVE_TARGET_BATCH_BYTES as u64);
        assert!(live.has_more);
    }

    #[test]
    fn cursor_live_prepare_refreezes_record_backlog_to_live_budget() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        for index in 0..4 {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("large-{index}"), vec![b'x'; 40 * 1024]],
                )
                .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let backlog = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert!(backlog.raw_bytes > LIVE_TARGET_BATCH_BYTES as u64);

        let live = prepare_next_cursor_envelope_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            LIVE_TARGET_BATCH_BYTES as u64,
        )
        .unwrap()
        .unwrap();
        assert_ne!(
            live.envelope.expected_envelope_id,
            backlog.envelope.expected_envelope_id
        );
        assert!(live.raw_bytes <= LIVE_TARGET_BATCH_BYTES as u64);
        assert!(live.has_more);
    }

    #[test]
    fn cursor_preparation_reports_current_only_after_acknowledged_head() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        for index in 0..300 {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("page-{index:03}"), vec![index as u8]],
                )
                .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        // An externally triggered re-observation may walk deduplicated pages,
        // but this two-page fixture must make bounded progress without shipping
        // its unchanged records again.
        let prepare = |conn: &mut Connection| {
            for _ in 0..4 {
                let outcome = prepare_next_cursor_envelope_outcome_with_limit(
                    conn,
                    &capabilities(),
                    &path,
                    LIVE_TARGET_BATCH_BYTES as u64,
                )
                .unwrap();
                if !matches!(outcome, CursorPreparationOutcome::Continue) {
                    return outcome;
                }
            }
            panic!("capture did not finish its bounded re-observation")
        };
        let mut shipped = 0;
        let mut source_epoch = None;
        let mut reached_current = false;

        for _ in 0..16 {
            match prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap()
            {
                CursorPreparationOutcome::Envelope(prepared) => {
                    source_epoch = Some(prepared.source_epoch);
                    acknowledge_prepared(&mut conn, &prepared);
                    shipped += 1;
                }
                CursorPreparationOutcome::Continue => continue,
                CursorPreparationOutcome::Current => {
                    assert!(shipped > 0);
                    reached_current = true;
                    break;
                }
                CursorPreparationOutcome::WaitingOnClaim => {
                    panic!("fixture must not be held by a managed Cursor claim")
                }
            }
        }
        assert!(reached_current, "Cursor source did not reach current");
        let source_epoch = source_epoch.expect("fixture must prepare at least one envelope");
        assert!(matches!(
            prepare(&mut conn),
            CursorPreparationOutcome::Current
        ));

        // This blob is not referenced by the root. A completed scan must not
        // permanently exclude later inserts that sort before its last page.
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES ('000-unreferenced', X'CAFE')",
                [],
            )
            .unwrap();

        let lower_message_id = "1111111111111111111111111111111111111111111111111111111111111111";
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                params![
                    lower_message_id,
                    br#"{"role":"assistant","content":[{"type":"text","text":"lower hash extension"}]}"#
                ],
            )
            .unwrap();
        let mut extended_root = vec![0xbb; 32];
        extended_root.extend_from_slice(&[0x11; 32]);
        set_cursor_root(&store, CURSOR_ROOT_B, &extended_root);
        let lower_extension = match prepare(&mut conn) {
            CursorPreparationOutcome::Envelope(prepared) => prepared,
            _ => panic!("a lower-ID blob referenced by a root extension must be captured"),
        };
        assert_eq!(lower_extension.source_epoch, source_epoch);
        assert!(lower_extension.envelope.records.iter().any(|record| {
            let bytes = BASE64_STANDARD.decode(&record.data_b64).unwrap();
            let raw: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            raw["kind"] == "blob" && raw["blob_id"] == "000-unreferenced"
        }));
        assert!(lower_extension.envelope.records.iter().any(|record| {
            let bytes = BASE64_STANDARD.decode(&record.data_b64).unwrap();
            let raw: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            raw["kind"] == "root_reference" && raw["blob_id"] == lower_message_id
        }));
        assert!(lower_extension
            .envelope
            .render
            .as_ref()
            .unwrap()
            .records
            .iter()
            .any(|record| record.content_text.as_deref() == Some("lower hash extension")));
        acknowledge_prepared(&mut conn, &lower_extension);
        assert!(matches!(
            prepare(&mut conn),
            CursorPreparationOutcome::Current
        ));

        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES ('zzzz-tail', X'CAFE')",
                [],
            )
            .unwrap();
        let tail = match prepare(&mut conn) {
            CursorPreparationOutcome::Envelope(prepared) => prepared,
            _ => panic!("a later-page blob must be captured after bounded continuation"),
        };
        assert!(tail.envelope.records.iter().any(|record| {
            let bytes = BASE64_STANDARD.decode(&record.data_b64).unwrap();
            let raw: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            raw["kind"] == "blob" && raw["blob_id"] == "zzzz-tail"
        }));
        acknowledge_prepared(&mut conn, &tail);
        assert!(matches!(
            prepare(&mut conn),
            CursorPreparationOutcome::Current
        ));
    }

    #[test]
    fn cursor_exact_blob_page_reaches_current_after_empty_follow_up() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        // The fixture contributes two blobs, making this exactly one 256-row
        // capture page. The visitor conservatively reports has_more until the
        // next empty page confirms EOF.
        for index in 0..254 {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("page-{index:03}"), vec![index as u8]],
                )
                .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = match prepare_next_cursor_envelope_outcome_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            LIVE_TARGET_BATCH_BYTES as u64,
        )
        .unwrap()
        {
            CursorPreparationOutcome::Envelope(prepared) => prepared,
            other => panic!(
                "the initial exact page must prepare an envelope, got {}",
                outcome_name(&other)
            ),
        };
        acknowledge_prepared(&mut conn, &first);
        assert!(matches!(
            prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap(),
            CursorPreparationOutcome::Current
        ));
        assert!(matches!(
            prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap(),
            CursorPreparationOutcome::Continue
        ));
        assert!(matches!(
            prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap(),
            CursorPreparationOutcome::Current
        ));
    }

    #[test]
    fn cursor_capture_pages_large_unreferenced_blob_tables() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        for index in 0..20 {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("bulk-{index:03}"), vec![b'x'; 1024 * 1024]],
                )
                .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let captured_bytes: i64 = conn
            .query_row(
                "SELECT COALESCE(SUM(length(record_bytes)), 0) FROM cursor_store_raw_record WHERE source_epoch = ?1",
                [prepared.source_epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();

        assert!(captured_bytes <= (MAX_RAW_BATCH_BYTES + 2 * 1024 * 1024) as i64);
        assert!(prepared.has_more);
    }

    #[tokio::test]
    async fn lost_receipt_retries_identical_body_after_source_growth() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let bodies = Arc::new(Mutex::new(Vec::<Vec<u8>>::new()));
        let server_bodies = bodies.clone();
        let server = tokio::spawn(async move {
            for attempt in 0..2 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let body = read_http_body(&mut socket).await;
                server_bodies.lock().unwrap().push(body.clone());
                if attempt == 0 {
                    drop(socket);
                    continue;
                }
                let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                let response_body = serde_json::json!({
                    "v": 2,
                    "envelope_id": envelope.expected_envelope_id,
                    "object_hash": "b".repeat(64),
                    "commit_seq": "42",
                    "raw_state": "durable",
                    "render_state": "ready",
                    "media_state": "complete",
                    "missing_media_hashes": [],
                })
                .to_string();
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first_line = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let second_line = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"later\"}}\n";
        fs::write(&path, first_line).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let first = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await;
        assert!(first.is_err());
        fs::write(
            &path,
            [first_line.as_slice(), second_line.as_slice()].concat(),
        )
        .unwrap();

        let second = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(
            second.has_more,
            "a successful retry must rescan source growth"
        );
        server.await.unwrap();

        let observed = bodies.lock().unwrap();
        assert_eq!(observed.len(), 2);
        assert_eq!(observed[0], observed[1]);
        let envelope: StorageV2Envelope = serde_json::from_slice(&observed[1]).unwrap();
        assert_eq!(envelope.range_start, 0);
        assert_eq!(envelope.range_end, first_line.len() as u64);
        assert_eq!(
            source_epoch::lane_position(
                &conn,
                Uuid::parse_str(&envelope.source_epoch).unwrap(),
                SourceLane::Durable,
            )
            .unwrap(),
            first_line.len() as u64
        );
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
    }

    #[tokio::test]
    async fn missing_epoch_after_conflict_quarantines_exact_intent() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let mut envelope = None;
            for request_index in 0..2 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (_request_line, body) = read_http_request(&mut socket).await;
                let response_body = if request_index == 0 {
                    envelope = Some(serde_json::from_slice::<StorageV2Envelope>(&body).unwrap());
                    r#"{"detail":{"code":"source_epoch_conflict","message":"range overlap","details":{}}}"#.to_string()
                } else {
                    assert!(envelope.is_some());
                    r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#.to_string()
                };
                let status = if request_index == 0 {
                    "409 Conflict"
                } else {
                    "404 Not Found"
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(
            &path,
            b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let error = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap_err();
        let blocked = error.downcast_ref::<StorageV2SourceBlocked>().unwrap();
        assert_eq!(blocked.kind, "source_epoch_conflict_unresolved");
        assert!(blocked.newly_blocked);
        server.await.unwrap();

        let pending = pending_source_envelope::load_for_path(
            &conn,
            &stable_source_path(&path).to_string_lossy(),
        )
        .unwrap()
        .unwrap();
        assert_eq!(pending.attempt_count, 1);
        assert!(pending.blocked_at.is_some());
        let snapshot = pending_source_envelope::snapshot(&conn).unwrap();
        assert_eq!(snapshot.pending_count, 0);
        assert_eq!(snapshot.blocked_source_count, 1);
    }

    #[tokio::test]
    async fn admitted_salvaged_epoch_restores_host_predecessor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first_line = format!(
            "{{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{{\"content\":\"{}\"}}}}\n",
            "x".repeat(2_000)
        )
        .into_bytes();
        let second_line = b"{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{\"content\":\"world\"}}\n";
        fs::write(&path, &first_line).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let first = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        acknowledge_prepared(&mut conn, &first);
        fs::write(
            &path,
            [first_line.as_slice(), second_line.as_slice()].concat(),
        )
        .unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        pending_source_envelope::quarantine(
            &mut conn,
            prepared.source_epoch,
            "source_epoch_conflict",
            "host rejected salvaged identity",
        )
        .unwrap();
        let pending = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .unwrap();

        let predecessor = Uuid::new_v4();
        let parent = Uuid::new_v4();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let expected_epoch = prepared.source_epoch;
        let envelope = prepared.envelope.clone();
        let accepted = prepared.range_start;
        let server = tokio::spawn(async move {
            let hosted_opened_at = DateTime::parse_from_rfc3339(&envelope.epoch_opened_at)
                .unwrap()
                .to_rfc3339_opts(chrono::SecondsFormat::Micros, true);
            for request_index in 0..3 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, _) = read_http_request(&mut socket).await;
                let (epoch, state, predecessor_epoch, replaced_by, through, retired_at) =
                    if request_index <= 1 {
                        assert!(request_line.contains(&expected_epoch.to_string()));
                        if request_index == 1 {
                            assert!(request_line.contains("after_position=1000"));
                        }
                        (
                            expected_epoch,
                            "open",
                            Some(predecessor),
                            None,
                            accepted,
                            None,
                        )
                    } else {
                        assert!(request_line.contains(&predecessor.to_string()));
                        (
                            predecessor,
                            "closed",
                            Some(parent),
                            Some(expected_epoch),
                            17,
                            Some("2026-08-31T00:00:00Z"),
                        )
                    };
                let objects = if request_index == 0 {
                    (0..1_000)
                        .map(|position| {
                            serde_json::json!({
                                "envelope_id": format!("host-envelope-{position}"),
                                "tenant_id": envelope.tenant_id,
                                "session_id": envelope.session_id,
                                "machine_id": envelope.machine_id,
                                "provider": envelope.provider,
                                "opaque_source_id": envelope.opaque_source_id,
                                "source_epoch": epoch.to_string(),
                                "range_kind": envelope.range_kind,
                                "range_start": position.to_string(),
                                "range_end": (position + 1).to_string(),
                                "retired_at": null,
                            })
                        })
                        .collect::<Vec<_>>()
                } else {
                    vec![serde_json::json!({
                        "envelope_id": "host-envelope",
                        "tenant_id": envelope.tenant_id,
                        "session_id": if request_index == 2 {
                            "legacy-session"
                        } else {
                            envelope.session_id.as_str()
                        },
                        "machine_id": envelope.machine_id,
                        "provider": envelope.provider,
                        "opaque_source_id": envelope.opaque_source_id,
                        "source_epoch": epoch.to_string(),
                        "range_kind": envelope.range_kind,
                        "range_start": "0",
                        "range_end": through.to_string(),
                        "retired_at": retired_at,
                    })]
                };
                let response_body = serde_json::json!({
                    "v": 2,
                    "source_epoch": {
                        "source_epoch": epoch.to_string(),
                        "tenant_id": envelope.tenant_id,
                        "machine_id": envelope.machine_id,
                        "provider": envelope.provider,
                        "opaque_source_id": envelope.opaque_source_id,
                        "range_kind": envelope.range_kind,
                        "state": state,
                        "predecessor_source_epoch": predecessor_epoch.map(|value| value.to_string()),
                        "replaced_by_source_epoch": replaced_by.map(|value| value.to_string()),
                        "accepted_through": through.to_string(),
                        "opened_at": hosted_opened_at,
                    },
                    "objects": objects,
                    "commit_seq": (40 + request_index).to_string(),
                    "observed_at": "2026-08-31T00:00:00Z",
                })
                .to_string();
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });
        let client = ShipperClient::with_compression(
            &ShipperConfig {
                api_url: format!("http://{address}"),
                timeout_seconds: 5,
                ..ShipperConfig::default()
            },
            CompressionAlgo::Gzip,
        )
        .unwrap();

        // The block's backoff is still running; a restart is what makes it due.
        assert_eq!(
            pending_source_envelope::wake_blocked_for_new_engine(&conn).unwrap(),
            1
        );
        assert!(reexamine_blocked_source(
            &mut conn,
            &client,
            &capabilities(),
            &pending,
            &prepared,
            "repair",
            Duration::from_secs(5),
        )
        .await
        .unwrap());
        server.await.unwrap();
        let repaired = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .unwrap();
        assert!(repaired.blocked_at.is_none());
        assert_eq!(
            pending_to_prepared(repaired)
                .unwrap()
                .envelope
                .predecessor_source_epoch,
            Some(predecessor.to_string())
        );
        let registry_predecessor: String = conn
            .query_row(
                "SELECT predecessor_epoch FROM source_epoch_registry WHERE source_epoch = ?1",
                [prepared.source_epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(registry_predecessor, predecessor.to_string());
    }

    #[test]
    fn host_timestamp_comparison_accepts_microsecond_storage_precision() {
        assert!(timestamps_match_at_host_precision(
            "2026-08-31T12:00:00.123456+00:00",
            "2026-08-31T12:00:00.123456789Z",
        ));
        assert!(!timestamps_match_at_host_precision(
            "2026-08-31T12:00:00.123455+00:00",
            "2026-08-31T12:00:00.123456789Z",
        ));
    }

    #[tokio::test]
    async fn unresolved_cross_provider_binding_returns_to_transcript_identity() {
        let dir = tempfile::tempdir().unwrap();
        let conversation_id = Uuid::new_v4().to_string();
        let transcript_dir = dir.path().join("agent-transcripts").join(&conversation_id);
        fs::create_dir_all(&transcript_dir).unwrap();
        let path = transcript_dir.join(format!("{conversation_id}.jsonl"));
        fs::write(
            &path,
            b"{\"role\":\"user\",\"timestamp\":\"2026-07-01T12:00:00Z\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"hello\"}]}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let correct = prepare_next_envelope(&mut conn, &capabilities(), &path, "cursor", None)
            .unwrap()
            .expect("a fresh Cursor transcript must prepare an envelope");
        assert_eq!(correct.envelope.session_id, conversation_id);
        let stale_session_id = Uuid::new_v4().to_string();
        let pending = pending_source_envelope::load_for_epoch(&conn, correct.source_epoch)
            .unwrap()
            .unwrap();
        let mut poisoned = correct.envelope.clone();
        poisoned.session_id = stale_session_id.clone();
        // Cursor's agent-transcripts projection ships raw-only, so there may be
        // no render to poison. The stale session id is what this test is about.
        if let Some(render) = poisoned.render.as_mut() {
            render.generation_id =
                render_generation_id(Uuid::parse_str(&stale_session_id).unwrap()).to_string();
        }
        let poisoned_body = encode_zstd(
            &serde_json::to_vec(&poisoned).unwrap(),
            "cross-provider fixture body",
        )
        .unwrap();
        pending_source_envelope::replace_request_body_after_render_conflict(
            &conn,
            correct.source_epoch,
            &pending.envelope_id,
            &pending.request_body_zstd,
            &poisoned_body,
        )
        .unwrap();
        conn.execute(
            "UPDATE source_epoch_registry SET bound_session_id = ?1 WHERE source_epoch = ?2",
            params![stale_session_id, correct.source_epoch.to_string()],
        )
        .unwrap();
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(
                &stable_source_path(&path).to_string_lossy(),
                &stale_session_id,
                "claude",
            )
            .unwrap();
        pending_source_envelope::quarantine(
            &mut conn,
            correct.source_epoch,
            "source_epoch_conflict_unresolved",
            "source_epoch_not_found after generic admission conflict",
        )
        .unwrap();
        let pending = pending_source_envelope::load_for_epoch(&conn, correct.source_epoch)
            .unwrap()
            .unwrap();
        let prepared = pending_to_prepared(pending.clone()).unwrap();

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let expected_stale = stale_session_id.clone();
        let server = tokio::spawn(async move {
            for request_index in 0..2 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, body) = read_http_request(&mut socket).await;
                let (status, response_body) = if request_index == 0 {
                    assert!(request_line.starts_with("GET "));
                    (
                        "404 Not Found",
                        r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#
                            .to_string(),
                    )
                } else {
                    assert!(request_line.starts_with("POST "));
                    let posted: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                    assert_eq!(posted.session_id, expected_stale);
                    (
                        "409 Conflict",
                        serde_json::json!({
                            "detail": {
                                "code": "source_epoch_conflict",
                                "message": "session identity conflict",
                                "details": {
                                    "reason": "session_identity_conflict",
                                    "existing_tenant_id": posted.tenant_id,
                                    "requested_tenant_id": posted.tenant_id,
                                    "existing_provider": "claude",
                                    "requested_provider": "cursor",
                                }
                            }
                        })
                        .to_string(),
                    )
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });
        let client = ShipperClient::with_compression(
            &ShipperConfig {
                api_url: format!("http://{address}"),
                timeout_seconds: 5,
                ..ShipperConfig::default()
            },
            CompressionAlgo::Gzip,
        )
        .unwrap();

        // The block's backoff is still running; a restart is what makes it due.
        assert_eq!(
            pending_source_envelope::wake_blocked_for_new_engine(&conn).unwrap(),
            1
        );
        assert!(reexamine_blocked_source(
            &mut conn,
            &client,
            &capabilities(),
            &pending,
            &prepared,
            "repair",
            Duration::from_secs(5),
        )
        .await
        .unwrap());
        server.await.unwrap();
        let repaired = pending_to_prepared(
            pending_source_envelope::load_for_epoch(&conn, correct.source_epoch)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(repaired.envelope.session_id, conversation_id);
        if let Some(render) = repaired.envelope.render {
            assert_eq!(
                render.generation_id,
                render_generation_id(Uuid::parse_str(&conversation_id).unwrap()).to_string()
            );
        }
        assert!(crate::state::session_binding::SessionBinding::new(&conn)
            .get_for_provider(&stable_source_path(&path).to_string_lossy(), "claude")
            .unwrap()
            .is_none());
        let bound: Option<String> = conn
            .query_row(
                "SELECT bound_session_id FROM source_epoch_registry WHERE source_epoch = ?1",
                [correct.source_epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert!(bound.is_none());
    }

    #[tokio::test]
    async fn missing_local_epoch_adopts_single_host_open_predecessor() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let host_epoch = Uuid::new_v4();
        let replacement_host_epoch = Uuid::new_v4();
        let stale_local_epoch = Uuid::new_v4();
        let expected_host_epoch = host_epoch;
        let expected_replacement_host_epoch = replacement_host_epoch;
        let expected_stale_local_epoch = stale_local_epoch;
        let server = tokio::spawn(async move {
            let mut original: Option<StorageV2Envelope> = None;
            for request_index in 0..11 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, body) = read_http_request(&mut socket).await;
                let (status, response_body) = match request_index {
                    0 | 3 => {
                        let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                        if request_index == 0 {
                            assert!(envelope.predecessor_source_epoch.is_none());
                            assert!(envelope.range_start > 0);
                        } else {
                            assert_eq!(
                                envelope.predecessor_source_epoch.as_deref(),
                                Some(expected_stale_local_epoch.to_string().as_str())
                            );
                            assert_eq!(envelope.range_start, 0);
                        }
                        original = Some(envelope);
                        (
                            "409 Conflict",
                            serde_json::json!({
                                "detail": {
                                    "code": "source_epoch_conflict",
                                    "message": "source range overlaps or conflicts with the registered epoch",
                                    "details": {
                                        "reason": "another_epoch_is_already_open_for_this_source",
                                        "open_source_epochs": [expected_host_epoch.to_string()],
                                    }
                                }
                            })
                            .to_string(),
                        )
                    }
                    1 | 4 | 7 => (
                        "404 Not Found",
                        r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#
                            .to_string(),
                    ),
                    2 | 5 => {
                        assert!(request_line.contains(&expected_host_epoch.to_string()));
                        let envelope = original.as_ref().unwrap();
                        (
                            "200 OK",
                            serde_json::json!({
                                "v": 2,
                                "source_epoch": {
                                    "source_epoch": expected_host_epoch.to_string(),
                                    "tenant_id": envelope.tenant_id,
                                    "machine_id": envelope.machine_id,
                                    "provider": envelope.provider,
                                    "opaque_source_id": envelope.opaque_source_id,
                                    "range_kind": envelope.range_kind,
                                    "state": "open",
                                    "predecessor_source_epoch": null,
                                    "replaced_by_source_epoch": null,
                                    "accepted_through": "91",
                                },
                                "objects": [{
                                    "envelope_id": "host-envelope",
                                    "tenant_id": envelope.tenant_id,
                                    "machine_id": envelope.machine_id,
                                    "provider": envelope.provider,
                                    "opaque_source_id": envelope.opaque_source_id,
                                    "source_epoch": expected_host_epoch.to_string(),
                                    "range_kind": envelope.range_kind,
                                    "range_start": "0",
                                    "range_end": "91",
                                    "retired_at": null,
                                }],
                                "commit_seq": "40",
                                "observed_at": "2026-08-31T00:00:00Z",
                            })
                            .to_string(),
                        )
                    }
                    6 => {
                        let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                        assert_eq!(
                            envelope.predecessor_source_epoch.as_deref(),
                            Some(expected_host_epoch.to_string().as_str())
                        );
                        (
                            "409 Conflict",
                            serde_json::json!({
                                "detail": {
                                    "code": "source_epoch_conflict",
                                    "message": "source range overlaps or conflicts with the registered epoch",
                                    "details": {
                                        "reason": "predecessor_not_open_for_this_identity",
                                        "predecessor_exists": true,
                                        "expected_predecessor": expected_host_epoch.to_string(),
                                        "predecessor_state": "closed",
                                    }
                                }
                            })
                            .to_string(),
                        )
                    }
                    8 => {
                        assert!(request_line.contains(&expected_host_epoch.to_string()));
                        let envelope = original.as_ref().unwrap();
                        (
                            "200 OK",
                            serde_json::json!({
                                "v": 2,
                                "source_epoch": {
                                    "source_epoch": expected_host_epoch.to_string(),
                                    "tenant_id": envelope.tenant_id,
                                    "machine_id": envelope.machine_id,
                                    "provider": envelope.provider,
                                    "opaque_source_id": envelope.opaque_source_id,
                                    "range_kind": envelope.range_kind,
                                    "state": "closed",
                                    "predecessor_source_epoch": null,
                                    "replaced_by_source_epoch": expected_replacement_host_epoch.to_string(),
                                    "accepted_through": "91",
                                },
                                "objects": [{
                                    "envelope_id": "host-envelope",
                                    "tenant_id": envelope.tenant_id,
                                    "machine_id": envelope.machine_id,
                                    "provider": envelope.provider,
                                    "opaque_source_id": envelope.opaque_source_id,
                                    "source_epoch": expected_host_epoch.to_string(),
                                    "range_kind": envelope.range_kind,
                                    "range_start": "0",
                                    "range_end": "91",
                                    "retired_at": null,
                                }],
                                "commit_seq": "41",
                                "observed_at": "2026-08-31T00:01:00Z",
                            })
                            .to_string(),
                        )
                    }
                    9 => {
                        assert!(request_line.contains(&expected_replacement_host_epoch.to_string()));
                        let envelope = original.as_ref().unwrap();
                        (
                            "200 OK",
                            serde_json::json!({
                                "v": 2,
                                "source_epoch": {
                                    "source_epoch": expected_replacement_host_epoch.to_string(),
                                    "tenant_id": envelope.tenant_id,
                                    "machine_id": envelope.machine_id,
                                    "provider": envelope.provider,
                                    "opaque_source_id": envelope.opaque_source_id,
                                    "range_kind": envelope.range_kind,
                                    "state": "open",
                                    "predecessor_source_epoch": expected_host_epoch.to_string(),
                                    "replaced_by_source_epoch": null,
                                    "accepted_through": "102",
                                },
                                "objects": [{
                                    "envelope_id": "replacement-host-envelope",
                                    "tenant_id": envelope.tenant_id,
                                    "machine_id": envelope.machine_id,
                                    "provider": envelope.provider,
                                    "opaque_source_id": envelope.opaque_source_id,
                                    "source_epoch": expected_replacement_host_epoch.to_string(),
                                    "range_kind": envelope.range_kind,
                                    "range_start": "0",
                                    "range_end": "102",
                                    "retired_at": null,
                                }],
                                "commit_seq": "42",
                                "observed_at": "2026-08-31T00:02:00Z",
                            })
                            .to_string(),
                        )
                    }
                    _ => {
                        let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                        assert_eq!(
                            envelope.predecessor_source_epoch.as_deref(),
                            Some(expected_replacement_host_epoch.to_string().as_str())
                        );
                        (
                            "200 OK",
                            serde_json::json!({
                                "v": 2,
                                "envelope_id": envelope.expected_envelope_id,
                                "object_hash": "b".repeat(64),
                                "commit_seq": "41",
                                "raw_state": "durable",
                                "render_state": "ready",
                                "media_state": "complete",
                                "missing_media_hashes": [],
                            })
                            .to_string(),
                        )
                    }
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first_line = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let second_line = b"{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{\"content\":\"world\"}}\n";
        fs::write(&path, first_line).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let locally_acked =
            prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                .unwrap()
                .unwrap();
        pending_source_envelope::acknowledge_and_delete(
            &mut conn,
            locally_acked.source_epoch,
            &locally_acked.envelope.expected_envelope_id,
            locally_acked.range_start,
            locally_acked.range_end,
        )
        .unwrap();
        fs::write(
            &path,
            [first_line.as_slice(), second_line.as_slice()].concat(),
        )
        .unwrap();

        let rewound = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "repair",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(rewound.bytes_shipped, 0);
        assert!(rewound.has_more);

        conn.execute(
            "INSERT INTO source_epoch_registry (
                 source_epoch, provider, opaque_source_id, file_incarnation,
                 predecessor_epoch, start_reason, max_observed_len,
                 source_revision, bound_session_id, created_at, updated_at,
                 ended_at, end_reason
             ) SELECT ?1, provider, opaque_source_id, file_incarnation,
                      predecessor_epoch, 'truncation', max_observed_len,
                      source_revision, bound_session_id, created_at, updated_at,
                      updated_at, 'truncation'
               FROM source_epoch_registry WHERE source_epoch = ?2",
            params![
                stale_local_epoch.to_string(),
                locally_acked.source_epoch.to_string()
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO source_epoch_lane_state (
                 source_epoch, lane, last_position, updated_at
             ) VALUES (?1, 'durable', 0, '2026-08-31T00:00:00Z')",
            [stale_local_epoch.to_string()],
        )
        .unwrap();
        conn.execute(
            "UPDATE source_epoch_registry
             SET predecessor_epoch = ?1, start_reason = 'truncation'
             WHERE source_epoch = ?2",
            params![
                stale_local_epoch.to_string(),
                locally_acked.source_epoch.to_string()
            ],
        )
        .unwrap();

        let recovered = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "repair",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(recovered.bytes_shipped, 0);
        assert!(recovered.has_more);

        let retargeted = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "repair",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(retargeted.bytes_shipped, 0);
        assert!(retargeted.has_more);

        let shipped = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "repair",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(shipped.bytes_shipped > 0);
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
        let local_predecessor: String = conn
            .query_row(
                "SELECT predecessor_epoch FROM source_epoch_registry
                 WHERE ended_at IS NULL AND provider = 'claude'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(local_predecessor, replacement_host_epoch.to_string());
        let host_position: i64 = conn
            .query_row(
                "SELECT last_position FROM source_epoch_lane_state
                 WHERE source_epoch = ?1 AND lane = 'durable'",
                [host_epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(host_position, 91);
        let replacement_host_position: i64 = conn
            .query_row(
                "SELECT last_position FROM source_epoch_lane_state
                 WHERE source_epoch = ?1 AND lane = 'durable'",
                [replacement_host_epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(replacement_host_position, 102);
        server.await.unwrap();
    }

    /// 2026-09-28: a Codex upgrade rewrote every rollout in place (new inode,
    /// same content plus an `ordinal` field). Thirty sources whose local
    /// registry had been salvaged named a predecessor epoch the host had never
    /// seen, and the host refused each of them with nothing to adopt. The host
    /// now names its open epoch, and the engine re-parents onto it.
    #[tokio::test]
    async fn replacement_over_a_predecessor_the_host_never_saw_adopts_the_host_open_epoch() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let host_epoch = Uuid::new_v4();
        let first_line = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let host_accepted_through = first_line.len();
        let server = tokio::spawn(async move {
            let mut original: Option<StorageV2Envelope> = None;
            for request_index in 0..4 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, body) = read_http_request(&mut socket).await;
                let (status, response_body) = match request_index {
                    0 => {
                        let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                        let phantom = envelope
                            .predecessor_source_epoch
                            .clone()
                            .expect("a rewritten source names the epoch it replaces");
                        assert_eq!(envelope.range_start, 0);
                        original = Some(envelope);
                        (
                            "409 Conflict",
                            serde_json::json!({
                                "detail": {
                                    "code": "source_epoch_conflict",
                                    "message": "source range overlaps or conflicts with the registered epoch",
                                    "details": {
                                        "reason": "predecessor_not_open_for_this_identity",
                                        "predecessor_exists": false,
                                        "expected_predecessor": phantom,
                                        "open_source_epochs": [host_epoch.to_string()],
                                    }
                                }
                            })
                            .to_string(),
                        )
                    }
                    1 => (
                        "404 Not Found",
                        r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#
                            .to_string(),
                    ),
                    2 => {
                        assert!(request_line.contains(&host_epoch.to_string()));
                        let envelope = original.as_ref().unwrap();
                        (
                            "200 OK",
                            serde_json::json!({
                                "v": 2,
                                "source_epoch": {
                                    "source_epoch": host_epoch.to_string(),
                                    "tenant_id": envelope.tenant_id,
                                    "machine_id": envelope.machine_id,
                                    "provider": envelope.provider,
                                    "opaque_source_id": envelope.opaque_source_id,
                                    "range_kind": envelope.range_kind,
                                    "state": "open",
                                    "predecessor_source_epoch": null,
                                    "replaced_by_source_epoch": null,
                                    "accepted_through": host_accepted_through.to_string(),
                                },
                                "objects": [{
                                    "envelope_id": "host-envelope",
                                    "tenant_id": envelope.tenant_id,
                                    "machine_id": envelope.machine_id,
                                    "provider": envelope.provider,
                                    "opaque_source_id": envelope.opaque_source_id,
                                    "source_epoch": host_epoch.to_string(),
                                    "range_kind": envelope.range_kind,
                                    "range_start": "0",
                                    "range_end": host_accepted_through.to_string(),
                                    "retired_at": null,
                                }],
                                "commit_seq": "40",
                                "observed_at": "2026-09-28T00:00:00Z",
                            })
                            .to_string(),
                        )
                    }
                    _ => {
                        let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                        assert_eq!(
                            envelope.predecessor_source_epoch.as_deref(),
                            Some(host_epoch.to_string().as_str())
                        );
                        assert_eq!(envelope.range_start, 0);
                        (
                            "200 OK",
                            serde_json::json!({
                                "v": 2,
                                "envelope_id": envelope.expected_envelope_id,
                                "object_hash": "c".repeat(64),
                                "commit_seq": "41",
                                "raw_state": "durable",
                                "render_state": "ready",
                                "media_state": "complete",
                                "missing_media_hashes": [],
                            })
                            .to_string(),
                        )
                    }
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(&path, first_line).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        // The phantom: a local epoch whose cursor was adopted from the file, so
        // it claims the whole file durable although the host has never heard
        // its name.
        let phantom = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        pending_source_envelope::acknowledge_and_delete(
            &mut conn,
            phantom.source_epoch,
            &phantom.envelope.expected_envelope_id,
            phantom.range_start,
            phantom.range_end,
        )
        .unwrap();

        // The provider rewrites the transcript in place: new inode, same records.
        fs::rename(&path, dir.path().join("rewritten.old")).unwrap();
        let rewritten = [first_line.as_slice(), b"{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{\"content\":\"world\"}}\n".as_slice()].concat();
        fs::write(&path, &rewritten).unwrap();

        let adopted = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "repair",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(adopted.bytes_shipped, 0);
        assert!(adopted.has_more);
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 1);

        let shipped = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "repair",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(shipped.bytes_shipped, rewritten.len() as u64);
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);

        let (active_predecessor, active_reason): (String, String) = conn
            .query_row(
                "SELECT predecessor_epoch, start_reason FROM source_epoch_registry
                 WHERE ended_at IS NULL AND provider = 'claude'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(active_predecessor, host_epoch.to_string());
        assert_eq!(active_reason, "host_authority_reconciled");
        let phantom_ended: bool = conn
            .query_row(
                "SELECT ended_at IS NOT NULL FROM source_epoch_registry WHERE source_epoch = ?1",
                [phantom.source_epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert!(phantom_ended);
        server.await.unwrap();
    }

    /// 2026-09-29: from the moment thirty sources were blocked, the bound-source
    /// reconciler (one-second tick, source still behind its file) sent each of
    /// them straight back into the shipper, which re-asked the host every time:
    /// a manifest 404 and a 409 per attempt, ~7 refused requests a second for
    /// hours. The six-hour backoff was only honoured by the restart scheduler.
    /// A refusal now stands until its backoff elapses or a restart wakes it.
    #[tokio::test]
    async fn a_refused_source_is_not_asked_again_until_its_backoff_elapses() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        // 0: phantom-predecessor 409 with nothing to adopt; 1: invalid-envelope 422.
        let mode = Arc::new(AtomicUsize::new(0));
        let posts = Arc::new(AtomicUsize::new(0));
        let manifests = Arc::new(AtomicUsize::new(0));
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = {
            let (mode, posts, manifests) = (mode.clone(), posts.clone(), manifests.clone());
            tokio::spawn(async move {
                loop {
                    let (mut socket, _) = listener.accept().await.unwrap();
                    let (request_line, body) = read_http_request(&mut socket).await;
                    let (status, response_body) = if request_line.starts_with("POST ") {
                        posts.fetch_add(1, Ordering::SeqCst);
                        let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                        if mode.load(Ordering::SeqCst) == 0 {
                            (
                                "409 Conflict",
                                serde_json::json!({
                                    "detail": {
                                        "code": "source_epoch_conflict",
                                        "message": "source range overlaps or conflicts with the registered epoch",
                                        "details": {
                                            "reason": "predecessor_not_open_for_this_identity",
                                            "predecessor_exists": false,
                                            "expected_predecessor": envelope.predecessor_source_epoch,
                                        }
                                    }
                                })
                                .to_string(),
                            )
                        } else {
                            (
                                "422 Unprocessable Entity",
                                r#"{"detail":{"code":"invalid_envelope","message":"refused","details":{}}}"#
                                    .to_string(),
                            )
                        }
                    } else {
                        manifests.fetch_add(1, Ordering::SeqCst);
                        (
                            "404 Not Found",
                            r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#
                                .to_string(),
                        )
                    };
                    let response = format!(
                        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        response_body.len(),
                        response_body
                    );
                    socket.write_all(response.as_bytes()).await.unwrap();
                }
            })
        };

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first_line = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        fs::write(&path, first_line).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        // The production shape: a local epoch the host never heard of, then the
        // provider rewrites the file, so the replacement names a phantom.
        let phantom = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        pending_source_envelope::acknowledge_and_delete(
            &mut conn,
            phantom.source_epoch,
            &phantom.envelope.expected_envelope_id,
            phantom.range_start,
            phantom.range_end,
        )
        .unwrap();
        fs::rename(&path, dir.path().join("rewritten.old")).unwrap();
        let rewritten = [first_line.as_slice(), b"{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{\"content\":\"world\"}}\n".as_slice()].concat();
        fs::write(&path, &rewritten).unwrap();

        async fn attempt(conn: &mut Connection, client: &ShipperClient, path: &Path) -> bool {
            let error = ship_next_envelope(
                conn,
                client,
                &capabilities(),
                path,
                "claude",
                None,
                "live",
                Duration::from_secs(5),
            )
            .await
            .expect_err("a refused source stays blocked");
            error
                .downcast_ref::<StorageV2SourceBlocked>()
                .unwrap_or_else(|| panic!("blocked, not a transient failure: {error:#}"))
                .newly_blocked
        }
        let wire_requests = || posts.load(Ordering::SeqCst) + manifests.load(Ordering::SeqCst);

        // The refusal itself: one POST, one manifest probe, then quarantined.
        assert!(attempt(&mut conn, &client, &path).await, "newly blocked");
        assert_eq!(posts.load(Ordering::SeqCst), 1);
        let after_first_refusal = wire_requests();

        // A minute of one-second reconciler ticks, and then some: no network.
        for _ in 0..90 {
            assert!(!attempt(&mut conn, &client, &path).await);
        }
        assert_eq!(
            wire_requests(),
            after_first_refusal,
            "a refusal within its backoff must cost the host nothing"
        );

        // The backoff elapses: exactly one re-examination, which finds the same
        // refusal and pushes the next look out again.
        conn.execute(
            "UPDATE pending_source_envelope SET wake_at = '1970-01-01T00:00:00.000000000Z'",
            [],
        )
        .unwrap();
        attempt(&mut conn, &client, &path).await;
        assert_eq!(posts.load(Ordering::SeqCst), 2);
        let after_reexamination = wire_requests();
        for _ in 0..90 {
            attempt(&mut conn, &client, &path).await;
        }
        assert_eq!(wire_requests(), after_reexamination);
        assert!(
            pending_source_envelope::retry_paths(&conn)
                .unwrap()
                .is_empty(),
            "an unchanged re-examination earns a longer backoff, not an immediate retry"
        );

        // A new engine is new evidence: the restart wake still forces one look.
        assert_eq!(
            pending_source_envelope::wake_blocked_for_new_engine(&conn).unwrap(),
            1
        );
        attempt(&mut conn, &client, &path).await;
        assert_eq!(posts.load(Ordering::SeqCst), 3);

        // An invalid-envelope verdict on a re-examination is just as
        // deterministic. It used to escape as a plain error, which the daemon
        // retries every half second on the live lane.
        mode.store(1, Ordering::SeqCst);
        pending_source_envelope::wake_blocked_for_new_engine(&conn).unwrap();
        attempt(&mut conn, &client, &path).await;
        assert_eq!(posts.load(Ordering::SeqCst), 4);
        for _ in 0..90 {
            attempt(&mut conn, &client, &path).await;
        }
        assert_eq!(posts.load(Ordering::SeqCst), 4);

        server.abort();
    }

    #[tokio::test]
    async fn legacy_conflict_proves_hosted_prefix_and_ships_only_the_suffix() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let first_line = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n".to_vec();
        let second_line = b"{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{\"content\":\"world\"}}\n".to_vec();
        let prefix_end = first_line.len() as u64;
        let server = tokio::spawn(async move {
            let mut original: Option<StorageV2Envelope> = None;
            for request_index in 0..3 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, body) = read_http_request(&mut socket).await;
                if request_index == 0 {
                    assert!(request_line.starts_with("POST /api/agents/storage/v2/envelopes "));
                    let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                    assert_eq!(envelope.range_start, 0);
                    assert!(envelope.range_end > prefix_end);
                    original = Some(envelope);
                    let response_body = r#"{"detail":{"code":"source_epoch_conflict","message":"range overlap","details":{"reason":"range_overlap"}}}"#;
                    let response = format!(
                        "HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        response_body.len(),
                        response_body
                    );
                    socket.write_all(response.as_bytes()).await.unwrap();
                } else if request_index == 1 {
                    assert!(request_line.starts_with("GET /api/agents/storage/v2/source-epochs/"));
                    let envelope = original.as_ref().unwrap();
                    let prefix_id = envelope_id_for_subrange(envelope, 0, prefix_end).unwrap();
                    let response_body = serde_json::json!({
                        "v": 2,
                        "source_epoch": {
                            "source_epoch": envelope.source_epoch,
                            "tenant_id": envelope.tenant_id,
                            "machine_id": envelope.machine_id,
                            "provider": envelope.provider,
                            "opaque_source_id": envelope.opaque_source_id,
                            "range_kind": envelope.range_kind,
                            "state": "open",
                            "accepted_through": prefix_end.to_string(),
                        },
                        "objects": [{
                            "envelope_id": prefix_id,
                            "tenant_id": envelope.tenant_id,
                            "machine_id": envelope.machine_id,
                            "provider": envelope.provider,
                            "opaque_source_id": envelope.opaque_source_id,
                            "source_epoch": envelope.source_epoch,
                            "range_kind": envelope.range_kind,
                            "range_start": "0",
                            "range_end": prefix_end.to_string(),
                            "retired_at": null,
                        }],
                        "commit_seq": "41",
                        "observed_at": "2026-07-15T00:00:00Z",
                    })
                    .to_string();
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        response_body.len(),
                        response_body
                    );
                    socket.write_all(response.as_bytes()).await.unwrap();
                } else {
                    assert!(request_line.starts_with("POST /api/agents/storage/v2/envelopes "));
                    let suffix: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                    assert_eq!(suffix.range_start, prefix_end);
                    assert_eq!(suffix.records.len(), 1);
                    let response_body = serde_json::json!({
                        "v": 2,
                        "envelope_id": suffix.expected_envelope_id,
                        "object_hash": "b".repeat(64),
                        "commit_seq": "42",
                        "raw_state": "durable",
                        "render_state": "ready",
                        "media_state": "complete",
                        "missing_media_hashes": [],
                    })
                    .to_string();
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        response_body.len(),
                        response_body
                    );
                    socket.write_all(response.as_bytes()).await.unwrap();
                }
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(
            &path,
            [first_line.as_slice(), second_line.as_slice()].concat(),
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let reconciled = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(reconciled.bytes_shipped, prefix_end);
        assert!(reconciled.has_more);
        let pending = pending_source_envelope::load_for_path(
            &conn,
            &stable_source_path(&path).to_string_lossy(),
        )
        .unwrap()
        .unwrap();
        assert_eq!(pending.range_start, prefix_end);

        let suffix = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(!suffix.has_more);
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
        server.await.unwrap();
    }

    #[test]
    fn prepare_reuses_durable_managed_binding_without_a_wake_hint() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(
            &path,
            b"{\"type\":\"user\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let canonical = fs::canonicalize(&path).unwrap();
        let managed_session_id = "018f0c3a-7b2d-7f10-8a11-000000000042";
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical.to_string_lossy(), managed_session_id, "claude")
            .unwrap();

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();

        assert_eq!(prepared.envelope.session_id, managed_session_id);
    }

    #[test]
    fn managed_source_rollover_emits_one_boundary_and_continuation_emits_none() {
        let dir = tempfile::tempdir().unwrap();
        let old_provider_id = "018f0c3a-7b2d-7f10-8a11-000000000001";
        let new_provider_id = "018f0c3a-7b2d-7f10-8a11-000000000002";
        let managed_session_id = "018f0c3a-7b2d-7f10-8a11-000000000042";
        let old_path = dir.path().join(format!("{old_provider_id}.jsonl"));
        let new_path = dir.path().join(format!("{new_provider_id}.jsonl"));
        fs::write(
            &old_path,
            format!(
                "{{\"type\":\"user\",\"sessionId\":\"{old_provider_id}\",\"uuid\":\"old-user\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{{\"content\":\"before\"}}}}\n"
            ),
        )
        .unwrap();
        fs::write(
            &new_path,
            format!(
                "{{\"type\":\"user\",\"sessionId\":\"{new_provider_id}\",\"uuid\":\"new-user\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{{\"content\":\"after\"}}}}\n"
            ),
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let binding = crate::state::session_binding::SessionBinding::new(&conn);
        binding
            .bind(
                &fs::canonicalize(&old_path).unwrap().to_string_lossy(),
                managed_session_id,
                "claude",
            )
            .unwrap();
        binding
            .bind(
                &fs::canonicalize(&new_path).unwrap().to_string_lossy(),
                managed_session_id,
                "claude",
            )
            .unwrap();

        let old = prepare_next_envelope(&mut conn, &capabilities(), &old_path, "claude", None)
            .unwrap()
            .unwrap();
        assert!(old
            .envelope
            .render
            .as_ref()
            .unwrap()
            .records
            .iter()
            .all(|record| record.branch_kind.as_deref() != Some("conversation_reset")));
        acknowledge_prepared(&mut conn, &old);

        let reset = prepare_next_envelope(&mut conn, &capabilities(), &new_path, "claude", None)
            .unwrap()
            .unwrap();
        let reset_records = &reset.envelope.render.as_ref().unwrap().records;
        assert_eq!(
            reset_records
                .iter()
                .filter(|record| record.branch_kind.as_deref() == Some("conversation_reset"))
                .count(),
            1
        );
        // The detected rotation must ship both native ids as data so server
        // ingest can alias the new id back to this session.
        let boundary = reset_records
            .iter()
            .find(|record| record.branch_kind.as_deref() == Some("conversation_reset"))
            .unwrap();
        assert_eq!(
            boundary.tool_input_json,
            Some(serde_json::json!({
                "previous_provider_session_id": old_provider_id,
                "provider_session_id": new_provider_id,
            }))
        );
        let retry = prepare_next_envelope(&mut conn, &capabilities(), &new_path, "claude", None)
            .unwrap()
            .unwrap();
        assert_eq!(
            serde_json::to_value(&retry.envelope.render).unwrap(),
            serde_json::to_value(&reset.envelope.render).unwrap()
        );
        acknowledge_prepared(&mut conn, &reset);

        let mut continuation = fs::OpenOptions::new().append(true).open(&new_path).unwrap();
        writeln!(
            continuation,
            "{{\"type\":\"assistant\",\"sessionId\":\"{new_provider_id}\",\"uuid\":\"new-assistant\",\"timestamp\":\"2026-07-12T12:02:00Z\",\"message\":{{\"content\":\"continued\"}}}}"
        )
        .unwrap();
        let continuation =
            prepare_next_envelope(&mut conn, &capabilities(), &new_path, "claude", None)
                .unwrap()
                .unwrap();
        assert!(continuation
            .envelope
            .render
            .as_ref()
            .unwrap()
            .records
            .iter()
            .all(|record| record.branch_kind.as_deref() != Some("conversation_reset")));
    }

    #[test]
    fn initial_v2_epoch_adopts_only_a_proven_legacy_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let second = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"world\"}}\n";
        fs::write(&path, [first.as_slice(), second.as_slice()].concat()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let canonical = fs::canonicalize(&path).unwrap();
        let path_text = canonical.to_string_lossy();
        FileState::new(&conn)
            .set_offset(
                &path_text,
                first.len() as u64,
                "018f0c3a-7b2d-7f10-8a11-123456789abc",
                "provider-session",
                "claude",
            )
            .unwrap();

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.range_start, first.len() as u64);
        assert_eq!(
            FileState::new(&conn).get_file_identity(&path_text).unwrap(),
            identity_from_metadata(&path.metadata().unwrap())
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn initial_v2_epoch_adopts_proven_cursor_across_macos_device_remap() {
        use std::os::unix::fs::MetadataExt;

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let second = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"world\"}}\n";
        fs::write(&path, [first.as_slice(), second.as_slice()].concat()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let canonical = fs::canonicalize(&path).unwrap();
        let path_text = canonical.to_string_lossy();
        FileState::new(&conn)
            .set_offset(
                &path_text,
                first.len() as u64,
                "018f0c3a-7b2d-7f10-8a11-123456789abc",
                "provider-session",
                "claude",
            )
            .unwrap();
        let inode = path.metadata().unwrap().ino();
        conn.execute(
            "UPDATE file_state SET file_identity = ?1 WHERE path = ?2",
            params![format!("unix:16777230:{inode}"), path_text.as_ref()],
        )
        .unwrap();

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.range_start, first.len() as u64);
    }

    #[test]
    fn initial_v2_epoch_replays_after_same_inode_truncate_and_regrow() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let replacement = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"jello\"}}\n";
        let second = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"world\"}}\n";
        fs::write(&path, [first.as_slice(), second.as_slice()].concat()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let canonical = fs::canonicalize(&path).unwrap();
        let path_text = canonical.to_string_lossy();
        let file_state = FileState::new(&conn);
        file_state
            .set_offset(
                &path_text,
                first.len() as u64,
                "018f0c3a-7b2d-7f10-8a11-123456789abc",
                "provider-session",
                "claude",
            )
            .unwrap();
        let stored_identity = file_state.get_file_identity(&path_text).unwrap();

        fs::write(&path, [replacement.as_slice(), second.as_slice()].concat()).unwrap();
        assert_eq!(
            stored_identity,
            identity_from_metadata(&path.metadata().unwrap()),
            "the regression must exercise truncate/regrow of the same file identity"
        );

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.range_start, 0);
    }

    /// Run `run` and return the warnings it logged on this thread.
    fn warnings_during<T>(run: impl FnOnce() -> T) -> (T, Vec<String>) {
        #[derive(Clone, Default)]
        struct Sink(Arc<Mutex<Vec<u8>>>);
        impl Write for Sink {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                self.0.lock().unwrap().extend_from_slice(bytes);
                Ok(bytes.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Sink {
            type Writer = Sink;
            fn make_writer(&'a self) -> Sink {
                self.clone()
            }
        }
        let sink = Sink::default();
        let subscriber = tracing_subscriber::fmt()
            .with_writer(sink.clone())
            .with_max_level(tracing::Level::WARN)
            .with_ansi(false)
            .finish();
        let output = tracing::subscriber::with_default(subscriber, run);
        let text = String::from_utf8(sink.0.lock().unwrap().clone()).unwrap();
        (output, text.lines().map(str::to_string).collect())
    }

    const REPLAY_WARNING: &str = "replaying storage-v2 source from zero";

    /// The legacy cursor seeds a source's first epoch. It is judged there, and
    /// never again: once the source has an epoch and the lane sits at the file
    /// head, a scan that revisits it has nothing to decide about the cursor.
    #[test]
    fn an_unproven_legacy_cursor_is_judged_at_adoption_and_never_again() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let replacement = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"jello\"}}\n";
        let second = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"world\"}}\n";
        fs::write(&path, [first.as_slice(), second.as_slice()].concat()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let canonical = fs::canonicalize(&path).unwrap();
        FileState::new(&conn)
            .set_offset(
                &canonical.to_string_lossy(),
                first.len() as u64,
                "018f0c3a-7b2d-7f10-8a11-123456789abc",
                "provider-session",
                "claude",
            )
            .unwrap();
        // Truncate and regrow the same file: the cursor no longer holds.
        fs::write(&path, [replacement.as_slice(), second.as_slice()].concat()).unwrap();

        let (adopted, warnings) = warnings_during(|| {
            prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                .unwrap()
                .unwrap()
        });
        assert_eq!(adopted.range_start, 0, "an unproven cursor replays");
        assert_eq!(
            warnings
                .iter()
                .filter(|line| line.contains(REPLAY_WARNING))
                .count(),
            1,
            "the adoption says so once: {warnings:?}"
        );
        acknowledge_prepared(&mut conn, &adopted);

        // Every later scan finds the lane at the head of a source it tracks.
        let (passes, warnings) = warnings_during(|| {
            (0..5)
                .map(|_| {
                    prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                        .unwrap()
                })
                .collect::<Vec<_>>()
        });
        assert!(passes.iter().all(Option::is_none), "nothing left to ship");
        assert!(
            warnings.is_empty(),
            "a tracked source is not re-judged on every scan: {warnings:?}"
        );
    }

    /// A provider that rewrites a transcript in place (new inode, same
    /// content) leaves the legacy cursor naming a file that is gone. The engine
    /// opens a replacement epoch for it and ships it once; the stale cursor
    /// must not keep the source in a warn-and-recheck loop afterwards.
    #[test]
    fn a_file_rewritten_in_place_does_not_leave_the_legacy_cursor_warning_every_scan() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let second = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"world\"}}\n";
        let content = [first.as_slice(), second.as_slice()].concat();
        fs::write(&path, &content).unwrap();
        let head = content.len() as u64;
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let canonical = fs::canonicalize(&path).unwrap();
        FileState::new(&conn)
            .set_offset(
                &canonical.to_string_lossy(),
                head,
                "018f0c3a-7b2d-7f10-8a11-123456789abc",
                "provider-session",
                "claude",
            )
            .unwrap();
        // The proven cursor is adopted at the head, so the source is current.
        assert!(
            prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                .unwrap()
                .is_none()
        );

        // The provider rewrites the file in place: same bytes, new inode.
        let rewrite = dir.path().join("rewrite.tmp");
        fs::write(&rewrite, &content).unwrap();
        fs::rename(&rewrite, &path).unwrap();

        let (replacement, warnings) = warnings_during(|| {
            prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                .unwrap()
                .expect("a replaced file is shipped again from the start")
        });
        assert_eq!(replacement.range_start, 0);
        acknowledge_prepared(&mut conn, &replacement);

        let (passes, later) = warnings_during(|| {
            (0..5)
                .map(|_| {
                    prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                        .unwrap()
                })
                .collect::<Vec<_>>()
        });
        assert!(
            passes.iter().all(Option::is_none),
            "the lane is at the head"
        );
        let all: Vec<_> = warnings.iter().chain(later.iter()).collect();
        assert!(
            all.is_empty(),
            "the epoch owns the position; the old cursor is not consulted: {all:?}"
        );
    }

    #[test]
    fn media_is_declared_without_changing_exact_provider_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("019c638d-0000-0000-0000-000000000012.jsonl");
        let image_data = BASE64_STANDARD.encode([0_u8; 600]);
        let bytes = format!(
            "{{\"type\":\"response_item\",\"timestamp\":\"2026-03-01T10:00:00Z\",\"payload\":{{\"type\":\"message\",\"role\":\"user\",\"content\":[{{\"type\":\"input_image\",\"image_url\":\"data:image/png;base64,{image_data}\"}}]}}}}\n"
        )
        .into_bytes();
        fs::write(&path, &bytes).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "codex", None)
            .unwrap()
            .unwrap();

        assert_eq!(
            prepared.envelope.records[0].data_b64,
            BASE64_STANDARD.encode(&bytes)
        );
        assert_eq!(prepared.media_objects.len(), 1);
        assert_eq!(prepared.media_objects[0].bytes, vec![0; 600]);
        assert_eq!(prepared.envelope.media.len(), 1);
        assert_eq!(
            prepared.envelope.media[0].sha256,
            prepared.media_objects[0].sha256
        );
        assert_eq!(prepared.envelope.media[0].availability, "available");
        assert_eq!(prepared.envelope.media[0].source_position, 0);
    }

    #[tokio::test]
    async fn media_upload_precedes_envelope_and_failed_upload_keeps_cursor_for_exact_retry() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Arc::new(Mutex::new(Vec::<String>::new()));
        let server_requests = requests.clone();
        let server = tokio::spawn(async move {
            for request_index in 0..5 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut bytes = Vec::new();
                let mut buffer = [0_u8; 4096];
                let header_end = loop {
                    let read = socket.read(&mut buffer).await.unwrap();
                    assert!(read > 0);
                    bytes.extend_from_slice(&buffer[..read]);
                    if let Some(offset) = bytes.windows(4).position(|window| window == b"\r\n\r\n")
                    {
                        break offset + 4;
                    }
                };
                let headers = String::from_utf8_lossy(&bytes[..header_end]);
                let request_line = headers.lines().next().unwrap().to_string();
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().unwrap())
                    })
                    .unwrap_or(0);
                while bytes.len() - header_end < content_length {
                    let read = socket.read(&mut buffer).await.unwrap();
                    assert!(read > 0);
                    bytes.extend_from_slice(&buffer[..read]);
                }
                server_requests.lock().unwrap().push(request_line.clone());
                let (status, body) = match request_index {
                    0 | 2 => {
                        let claim: serde_json::Value =
                            serde_json::from_slice(&bytes[header_end..]).unwrap();
                        let hash = claim["items"][0]["sha256"].as_str().unwrap();
                        (
                            "200 OK",
                            serde_json::json!({"needed":[hash],"present":[],"rejected":[]})
                                .to_string(),
                        )
                    }
                    1 => ("503 Service Unavailable", "{}".to_string()),
                    3 => ("200 OK", "{}".to_string()),
                    4 => {
                        let envelope: serde_json::Value =
                            serde_json::from_slice(&bytes[header_end..]).unwrap();
                        let envelope_id = envelope["expected_envelope_id"].as_str().unwrap();
                        (
                            "200 OK",
                            serde_json::json!({
                                "v":2,
                                "envelope_id":envelope_id,
                                "object_hash":"b".repeat(64),
                                "commit_seq":"9",
                                "raw_state":"durable",
                                "render_state":"ready",
                                "media_state":"complete",
                                "missing_media_hashes":[]
                            })
                            .to_string(),
                        )
                    }
                    _ => unreachable!(),
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("019c638d-0000-0000-0000-000000000013.jsonl");
        let image_data = BASE64_STANDARD.encode([0_u8; 600]);
        let line = format!(
            "{{\"type\":\"response_item\",\"timestamp\":\"2026-03-01T10:00:00Z\",\"payload\":{{\"type\":\"message\",\"role\":\"user\",\"content\":[{{\"type\":\"input_image\",\"image_url\":\"data:image/png;base64,{image_data}\"}}]}}}}\n"
        );
        fs::write(&path, line).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let first = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "codex",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await;
        assert!(first.is_err());
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "codex", None)
            .unwrap()
            .unwrap();
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            0
        );

        let second = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "codex",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(second.events_shipped, 1);
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            prepared.range_end
        );
        server.await.unwrap();
        let observed = requests.lock().unwrap().clone();
        assert!(observed[0].starts_with("POST /api/agents/storage/v2/media/claims "));
        assert!(observed[1].starts_with("PUT /api/agents/storage/v2/media/"));
        assert!(observed[2].starts_with("POST /api/agents/storage/v2/media/claims "));
        assert!(observed[3].starts_with("PUT /api/agents/storage/v2/media/"));
        assert!(observed[4].starts_with("POST /api/agents/storage/v2/envelopes "));
    }

    fn create_opencode_db(path: &Path) {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE project (id text PRIMARY KEY, worktree text NOT NULL, name text);
            CREATE TABLE session (
                id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
                directory text, path text, title text, version text,
                time_created integer NOT NULL, time_updated integer NOT NULL
            );
            CREATE TABLE message (
                id text PRIMARY KEY, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
            );
            CREATE TABLE part (
                id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
            );
            INSERT INTO project VALUES ('project-1', '/tmp/longhouse', 'longhouse');
            INSERT INTO session VALUES (
                'session-1', 'project-1', NULL, '/tmp/longhouse', '/tmp/longhouse',
                'OpenCode test', '1', 1779000000000, 1779000000100
            );
            INSERT INTO message VALUES (
                'message-1', 'session-1', 1779000000010, 1779000000020, '{"role":"user"}'
            );
            INSERT INTO part VALUES (
                'part-1', 'message-1', 'session-1', 1779000000011, 1779000000011,
                '{"type":"text","text":"hello"}'
            );
            "#,
        )
        .unwrap();
    }

    #[test]
    fn opencode_delegation_facts_stay_inside_their_raw_record_range() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_opencode_db(&db_path);
        let mut parsed = crate::opencode_db::parse_opencode_session(&db_path, "session-1").unwrap();
        let at = chrono::Utc::now();
        parsed.provider_facts = vec![
            crate::pipeline::parser::ParsedProviderFact {
                kind: "delegation.metadata".to_string(),
                at,
                source_offset: 0,
                payload: serde_json::json!({"parent_provider_session_id": "parent-1"}),
            },
            crate::pipeline::parser::ParsedProviderFact {
                kind: "delegation.spawn".to_string(),
                at,
                source_offset: parsed.source_lines[0].source_offset,
                payload: serde_json::json!({"children": [{"provider_session_id": "child-1"}]}),
            },
        ];
        // The one part's raw record sits at ordinal 2, after the session and
        // its message.
        let ordinals: Vec<(u64, u64)> = parsed
            .source_lines
            .iter()
            .enumerate()
            .map(|(index, line)| (line.source_offset, 2 + index as u64))
            .collect();
        let prefix = opencode_provider_facts_for_range(&parsed, &ordinals, 0, 2).unwrap();
        let suffix = opencode_provider_facts_for_range(&parsed, &ordinals, 2, 3).unwrap();
        assert_eq!(
            prefix
                .iter()
                .map(|fact| (fact.kind.as_str(), fact.source_position))
                .collect::<Vec<_>>(),
            vec![("delegation.metadata", 0)]
        );
        assert_eq!(
            suffix
                .iter()
                .map(|fact| (fact.kind.as_str(), fact.source_position))
                .collect::<Vec<_>>(),
            vec![("delegation.spawn", 2)]
        );
    }

    #[test]
    fn record_ordinal_reconciliation_rebases_render_ordinals_without_source_read() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_opencode_db(&db_path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
            .unwrap()
            .unwrap();
        assert_eq!((prepared.range_start, prepared.range_end), (0, 3));
        let prefix_end = 2;
        let prefix_id = envelope_id_for_subrange(&prepared.envelope, 0, prefix_end).unwrap();
        let manifest = StorageV2SourceManifest {
            v: 2,
            source_epoch: crate::shipping::storage_v2::StorageV2SourceEpoch {
                source_epoch: prepared.envelope.source_epoch.clone(),
                tenant_id: prepared.envelope.tenant_id.clone(),
                machine_id: prepared.envelope.machine_id.clone(),
                provider: prepared.envelope.provider.clone(),
                opaque_source_id: prepared.envelope.opaque_source_id.clone(),
                range_kind: prepared.envelope.range_kind.clone(),
                state: "open".to_string(),
                predecessor_source_epoch: None,
                replaced_by_source_epoch: None,
                accepted_through: prefix_end.to_string(),
                opened_at: prepared.envelope.epoch_opened_at.clone(),
            },
            objects: vec![crate::shipping::storage_v2::StorageV2SourceObject {
                envelope_id: prefix_id,
                tenant_id: prepared.envelope.tenant_id.clone(),
                session_id: prepared.envelope.session_id.clone(),
                machine_id: prepared.envelope.machine_id.clone(),
                provider: prepared.envelope.provider.clone(),
                opaque_source_id: prepared.envelope.opaque_source_id.clone(),
                source_epoch: prepared.envelope.source_epoch.clone(),
                range_kind: prepared.envelope.range_kind.clone(),
                range_start: "0".to_string(),
                range_end: prefix_end.to_string(),
                retired_at: None,
            }],
            commit_seq: "7".to_string(),
            observed_at: "2026-07-15T00:00:00Z".to_string(),
        };

        assert_eq!(
            proven_manifest_prefix(&prepared, &manifest).unwrap(),
            Some(prefix_end)
        );
        let suffix = split_prepared_suffix(&prepared, prefix_end).unwrap();
        assert_eq!((suffix.range_start, suffix.range_end), (2, 3));
        assert_eq!(suffix.envelope.records[0].source_position, 2);
        let render = suffix.envelope.render.as_ref().unwrap();
        assert_eq!(render.records[0].source_position, 2);
        assert_eq!(render.records[0].raw_record_ordinal, 0);
        assert_eq!(
            suffix.envelope.expected_envelope_id,
            envelope_id_for_subrange(&suffix.envelope, 2, 3).unwrap()
        );
    }

    #[test]
    fn opencode_uses_record_ordinals_and_keeps_one_parser_generation_across_source_revisions() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_opencode_db(&db_path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
            .unwrap()
            .unwrap();
        assert_eq!(first.envelope.range_kind, "record_ordinal");
        assert_eq!((first.range_start, first.range_end), (0, 3));
        assert_eq!(first.envelope.records[0].source_position, 0);
        let first_render = first.envelope.render.as_ref().unwrap();
        assert_eq!(first_render.records[0].source_position, 2);
        assert_eq!(first_render.records[0].raw_record_ordinal, 2);
        assert_eq!(
            source_epoch::lane_position(&conn, first.source_epoch, SourceLane::Durable).unwrap(),
            0
        );
        acknowledge_prepared(&mut conn, &first);

        let provider = Connection::open(&db_path).unwrap();
        provider
            .execute(
                "UPDATE part SET data = ?1, time_updated = ?2 WHERE id = 'part-1'",
                params![r#"{"type":"text","text":"hello again"}"#, 1779000000200_i64],
            )
            .unwrap();
        let second = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
            .unwrap()
            .unwrap();
        assert_ne!(second.source_epoch, first.source_epoch);
        assert_eq!(second.range_start, 0);
        let first_epoch = first.source_epoch.to_string();
        assert_eq!(
            second.envelope.predecessor_source_epoch.as_deref(),
            Some(first_epoch.as_str())
        );
        assert_eq!(
            second.envelope.render.as_ref().unwrap().generation_id,
            first_render.generation_id
        );
    }

    #[test]
    fn opencode_storage_v2_exhausts_sessions_older_than_the_newest_64() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_opencode_db(&db_path);
        let provider = Connection::open(&db_path).unwrap();
        for index in 2..=65 {
            let session_id = format!("session-{index}");
            let message_id = format!("message-{index}");
            let part_id = format!("part-{index}");
            let timestamp = 1_779_000_000_000_i64 + index;
            provider
                .execute(
                    "INSERT INTO session VALUES (?1, 'project-1', NULL, '/tmp/longhouse', '/tmp/longhouse', 'OpenCode test', '1', ?2, ?2)",
                    params![session_id, timestamp],
                )
                .unwrap();
            provider
                .execute(
                    "INSERT INTO message VALUES (?1, ?2, ?3, ?3, '{\"role\":\"user\"}')",
                    params![message_id, session_id, timestamp + 1],
                )
                .unwrap();
            provider
                .execute(
                    "INSERT INTO part VALUES (?1, ?2, ?3, ?4, ?4, '{\"type\":\"text\",\"text\":\"hello\"}')",
                    params![part_id, message_id, session_id, timestamp + 2],
                )
                .unwrap();
        }
        drop(provider);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let mut shipped_sources = std::collections::HashSet::new();
        while let Some(prepared) =
            prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path).unwrap()
        {
            shipped_sources.insert(prepared.envelope.opaque_source_id.clone());
            acknowledge_prepared(&mut conn, &prepared);
        }
        assert_eq!(shipped_sources.len(), 65);
    }

    #[test]
    fn byte_offset_resync_refuses_stale_watermark_without_discarding_evidence() {
        fn fixture() -> (
            tempfile::TempDir,
            Connection,
            std::path::PathBuf,
            PreparedStorageV2Envelope,
        ) {
            let dir = tempfile::tempdir().unwrap();
            let path = dir
                .path()
                .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
            let first = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
            let second = b"{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{\"content\":\"world\"}}\n";
            fs::write(&path, [first.as_slice(), second.as_slice()].concat()).unwrap();
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            let first_prepared = prepare_next_envelope_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                "claude",
                None,
                first.len(),
            )
            .unwrap()
            .unwrap();
            acknowledge_prepared(&mut conn, &first_prepared);
            let second_prepared =
                prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                    .unwrap()
                    .unwrap();
            assert_eq!(second_prepared.range_start, first.len() as u64);
            (dir, conn, path, second_prepared)
        }

        fn manifest_for(
            prepared: &PreparedStorageV2Envelope,
            accepted_through: u64,
            predecessor: Option<String>,
        ) -> StorageV2SourceManifest {
            StorageV2SourceManifest {
                v: 2,
                source_epoch: crate::shipping::storage_v2::StorageV2SourceEpoch {
                    source_epoch: prepared.envelope.source_epoch.clone(),
                    tenant_id: prepared.envelope.tenant_id.clone(),
                    machine_id: prepared.envelope.machine_id.clone(),
                    provider: prepared.envelope.provider.clone(),
                    opaque_source_id: prepared.envelope.opaque_source_id.clone(),
                    range_kind: prepared.envelope.range_kind.clone(),
                    state: "open".to_string(),
                    predecessor_source_epoch: predecessor,
                    replaced_by_source_epoch: None,
                    accepted_through: accepted_through.to_string(),
                    opened_at: prepared.envelope.epoch_opened_at.clone(),
                },
                objects: Vec::new(),
                commit_seq: "1".to_string(),
                observed_at: "2026-09-11T00:00:00Z".to_string(),
            }
        }

        let (_dir, mut conn, path, prepared) = fixture();
        let local_before =
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap();
        let mut foreign = manifest_for(&prepared, 0, None);
        foreign.source_epoch.tenant_id = "foreign-tenant".to_string();
        assert!(resync_behind_host(&mut conn, &prepared, &foreign)
            .unwrap()
            .is_none());
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            local_before
        );

        let (_dir, mut conn, path, prepared) = fixture();
        let future = manifest_for(&prepared, prepared.range_start + 1, None);
        assert!(resync_behind_host(&mut conn, &prepared, &future)
            .unwrap()
            .is_none());
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            prepared.range_start
        );

        let (_dir, mut conn, path, prepared) = fixture();
        let predecessor = Uuid::new_v4().to_string();
        let mismatched_predecessor = manifest_for(&prepared, 0, Some(predecessor));
        assert!(
            resync_behind_host(&mut conn, &prepared, &mismatched_predecessor)
                .unwrap()
                .is_none()
        );
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            prepared.range_start
        );

        let (_dir, mut conn, path, prepared) = fixture();
        let stale = manifest_for(&prepared, 0, None);
        let pending_before = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .expect("prepared range remains retained until acknowledgement");
        let local_before =
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap();
        let error = resync_behind_host(&mut conn, &prepared, &stale)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("preserved the immutable envelope"),
            "{error}"
        );
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            local_before
        );
        let pending_after = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .expect("source conflict must retain the pending envelope");
        assert_eq!(pending_after.envelope_id, pending_before.envelope_id);
        assert_eq!(
            pending_after.request_body_zstd,
            pending_before.request_body_zstd
        );

        let (_dir, mut conn, path, prepared) = fixture();
        let stale = manifest_for(&prepared, 0, None);
        let pending_before = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .expect("prepared range remains retained until acknowledgement");
        let local_before =
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap();
        let mut source = fs::read(&path).unwrap();
        source[prepared.range_start as usize] ^= 1;
        fs::write(&path, source).unwrap();

        let error = resync_behind_host(&mut conn, &prepared, &stale)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("preserved the immutable envelope"),
            "{error}"
        );
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            local_before
        );
        let pending_after = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .expect("source block must retain the pending body");
        assert_eq!(
            pending_after.request_body_zstd, pending_before.request_body_zstd,
            "source conflict must not replace or discard retained evidence"
        );
        assert_eq!(
            pending_after.envelope_id, pending_before.envelope_id,
            "source conflict must retain the original retry identity"
        );
    }

    #[test]
    fn a_behind_live_lane_may_use_the_backlog_batch_size() {
        // At or under one live batch nothing changes, so the latency tuning
        // that keeps the newest content close to the client still holds.
        assert_eq!(live_catch_up_batch_bytes(0), LIVE_TARGET_BATCH_BYTES);
        assert_eq!(
            live_catch_up_batch_bytes(LIVE_TARGET_BATCH_BYTES as u64),
            LIVE_TARGET_BATCH_BYTES
        );
        // Behind by more than one batch: catch up in one pass rather than one
        // batch per scheduled turn.
        assert_eq!(
            live_catch_up_batch_bytes(LIVE_TARGET_BATCH_BYTES as u64 + 1),
            BACKLOG_TARGET_BATCH_BYTES
        );
        assert!(BACKLOG_TARGET_BATCH_BYTES > LIVE_TARGET_BATCH_BYTES);
    }

    #[test]
    fn live_lag_is_zero_without_an_active_epoch() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = rusqlite::Connection::open(db.path()).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        std::fs::write(&path, vec![b'x'; 4096]).unwrap();

        assert_eq!(live_lag_bytes(&conn, "omp", &path), 0);
        assert_eq!(
            live_lag_bytes(&conn, "omp", &dir.path().join("missing.jsonl")),
            0
        );
    }

    #[test]
    fn a_pi_lineage_revision_ignores_appends_and_notices_rewrites() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        let line = format!("{{\"type\":\"message\",\"pad\":\"{}\"}}\n", "x".repeat(200));
        let mut body = String::new();
        for _ in 0..40 {
            body.push_str(&line);
        }
        std::fs::write(&path, &body).unwrap();
        let before = pi_lineage_source_revision(&path).unwrap().unwrap();

        // An appended message must not move the signal: a change here would
        // re-ship the whole transcript on every message.
        let mut appended = body.clone();
        appended.push_str(&line);
        std::fs::write(&path, &appended).unwrap();
        assert_eq!(
            pi_lineage_source_revision(&path).unwrap().unwrap(),
            before,
            "appending must not rotate the source epoch"
        );

        // Rewriting the head in place, same size, must move it.
        let mut rewritten = appended.clone().into_bytes();
        rewritten[0] = b'[';
        std::fs::write(&path, &rewritten).unwrap();
        assert_ne!(
            pi_lineage_source_revision(&path).unwrap().unwrap(),
            before,
            "an in-place rewrite must rotate the source epoch"
        );
    }

    #[test]
    fn a_pi_lineage_revision_of_a_short_file_uses_its_first_line() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("short.jsonl");
        std::fs::write(&path, "{\"type\":\"session\"}\n").unwrap();
        let first = pi_lineage_source_revision(&path).unwrap();

        std::fs::write(&path, "{\"type\":\"session\"}\n{\"type\":\"message\"}\n").unwrap();
        assert_eq!(
            pi_lineage_source_revision(&path).unwrap(),
            first,
            "a second line must not rotate the epoch either"
        );

        // No complete line yet: no signal, rather than a wrong one.
        std::fs::write(&path, "{\"type\":\"sess").unwrap();
        assert_eq!(pi_lineage_source_revision(&path).unwrap(), None);
    }

    #[test]
    fn codex_subagent_keeps_native_child_identity_and_parent_edge() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout-child.jsonl");
        fs::write(
            &path,
            concat!(
                r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"id":"dddddddd-1111-2222-3333-444455556666","source":{"subagent":{"thread_spawn":{"parent_thread_id":"cccccccc-1111-2222-3333-444455556666","depth":2}}},"cwd":"/tmp/test"}}"#,
                "\n",
                r#"{"type":"response_item","timestamp":"2026-02-15T10:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"inspect the child"}]}}"#,
                "\n",
            ),
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "codex", None)
            .unwrap()
            .unwrap();
        assert_eq!(
            prepared.envelope.session.provider_session_id.as_deref(),
            Some("dddddddd-1111-2222-3333-444455556666")
        );
        assert_eq!(
            prepared
                .envelope
                .session
                .parent_provider_session_id
                .as_deref(),
            Some("cccccccc-1111-2222-3333-444455556666")
        );
        assert!(prepared.envelope.session.is_subagent);
        assert!(prepared.envelope.session.hidden_from_default_timeline);
    }

    #[test]
    fn omp_recorded_parent_session_reaches_storage_facts() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("native.jsonl");
        fs::write(
            &path,
            include_str!("../../tests/fixtures/golden/omp/native.jsonl"),
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let managed_id = "019d2869-1111-7222-8333-aaaaaaaaaaaa";
        let prepared =
            prepare_next_envelope(&mut conn, &capabilities(), &path, "omp", Some(managed_id))
                .unwrap()
                .unwrap();
        assert_eq!(prepared.envelope.session_id, managed_id);
        assert_eq!(
            prepared.envelope.session.provider_session_id.as_deref(),
            Some("omp-native-18-1-14")
        );
        assert_eq!(
            prepared
                .envelope
                .session
                .parent_provider_session_id
                .as_deref(),
            Some("omp-parent-opaque")
        );
        assert!(prepared.envelope.session.is_subagent);
        assert!(prepared.envelope.session.hidden_from_default_timeline);
    }

    #[test]
    fn a_source_stamp_moves_when_the_file_changes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("source.jsonl");
        std::fs::write(&path, b"{\"type\":\"session\"}\n").unwrap();

        let first = source_stamp(&path).unwrap();
        assert_eq!(
            source_stamp(&path).unwrap(),
            first,
            "an untouched file keeps its stamp"
        );

        std::fs::write(&path, b"{\"type\":\"session\"}\n{\"type\":\"message\"}\n").unwrap();
        assert_ne!(
            source_stamp(&path).unwrap(),
            first,
            "an append moves the stamp"
        );

        // Same length, different bytes: the post-read re-check is what catches
        // a provider rewriting the file between the raw read and the parse.
        let rewritten = b"{\"type\":\"session\"}\n{\"type\":\"message\"}\n";
        let mut mutated = rewritten.to_vec();
        mutated[3] = b'X';
        std::fs::write(&path, &mutated).unwrap();
        assert_ne!(source_stamp(&path).unwrap(), first);
    }

    /// Sources at rest: an epoch parked at EOF, exactly what a source that has
    /// finished shipping looks like to the next reconciliation scan.
    fn parked_file_sources(
        dir: &Path,
        conn: &mut Connection,
        count: usize,
    ) -> Vec<(PathBuf, &'static str)> {
        let root = dir.join("sources");
        fs::create_dir_all(&root).unwrap();
        (0..count)
            .map(|index| {
                let provider = if index % 3 == 0 { "codex" } else { "claude" };
                let path = root.join(format!("{}.jsonl", Uuid::new_v4()));
                fs::write(
                    &path,
                    format!("{{\"type\":\"user\",\"uuid\":\"{}\",\"timestamp\":\"2026-01-01T00:00:00Z\",\"message\":{{\"content\":\"hi {index}\"}}}}\n", Uuid::new_v4()),
                )
                .unwrap();
                let canonical = stable_source_path(&path);
                let length = fs::metadata(&path).unwrap().len();
                source_epoch::observe_file(
                    conn,
                    provider,
                    &opaque_source_id(&canonical.to_string_lossy()),
                    &path,
                    SourceLane::Durable,
                    length,
                    None,
                    None,
                    SourceChangeHint::None,
                )
                .unwrap();
                (path, provider)
            })
            .collect()
    }

    #[test]
    fn a_scan_pass_over_unchanged_sources_writes_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let sources = parked_file_sources(dir.path(), &mut conn, 200);

        let window = WalWindow::open(&conn);
        for _pass in 0..2 {
            for (path, provider) in &sources {
                assert!(
                    prepare_next_envelope(&mut conn, &capabilities(), path, provider, None)
                        .unwrap()
                        .is_none()
                );
            }
        }
        let cost = window.cost(&conn);
        assert!(
            cost.is_zero(),
            "two scans over {} unchanged sources wrote: {cost:?}",
            sources.len()
        );

        // A source that did change still records what it learned.
        let (path, provider) = &sources[0];
        let mut file = fs::OpenOptions::new().append(true).open(path).unwrap();
        writeln!(
            file,
            "{{\"type\":\"user\",\"uuid\":\"{}\",\"timestamp\":\"2026-01-01T00:00:01Z\",\"message\":{{\"content\":\"more\"}}}}",
            Uuid::new_v4()
        )
        .unwrap();
        drop(file);
        let window = WalWindow::open(&conn);
        assert!(
            prepare_next_envelope(&mut conn, &capabilities(), path, provider, None)
                .unwrap()
                .is_some()
        );
        assert!(window.cost(&conn).commits > 0);
    }

    #[test]
    fn a_scan_pass_over_an_unchanged_opencode_database_writes_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_opencode_db(&db_path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let first = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
            .unwrap()
            .unwrap();
        acknowledge_prepared(&mut conn, &first);
        assert!(
            prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .is_none()
        );

        let window = WalWindow::open(&conn);
        for _pass in 0..2 {
            assert!(
                prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                    .unwrap()
                    .is_none()
            );
        }
        let cost = window.cost(&conn);
        assert!(cost.is_zero(), "an unchanged OpenCode scan wrote: {cost:?}");
    }

    #[test]
    fn a_scan_pass_over_an_unchanged_cursor_store_writes_nothing() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let cursor_home = dir.path().join("cursor");
        let store_path = cursor_home
            .join("chats/workspace")
            .join(CURSOR_CONVERSATION_ID)
            .join("store.db");
        fs::create_dir_all(store_path.parent().unwrap()).unwrap();
        let _store = make_cursor_store(&store_path);
        let saved: Vec<(&str, Option<std::ffi::OsString>)> =
            ["CURSOR_HOME", "XDG_CONFIG_HOME", "LONGHOUSE_HOME"]
                .into_iter()
                .map(|name| (name, std::env::var_os(name)))
                .collect();
        unsafe {
            std::env::set_var("CURSOR_HOME", &cursor_home);
            std::env::set_var("XDG_CONFIG_HOME", dir.path().join("config"));
            std::env::set_var("LONGHOUSE_HOME", dir.path().join("longhouse"));
        }
        // Restore the process environment even if a regression panics.
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &store_path)
                .unwrap()
                .unwrap();
            acknowledge_prepared(&mut conn, &first);
            assert!(
                prepare_next_cursor_envelope(&mut conn, &capabilities(), &store_path)
                    .unwrap()
                    .is_none()
            );

            let window = WalWindow::open(&conn);
            for _pass in 0..2 {
                assert!(
                    prepare_next_cursor_envelope(&mut conn, &capabilities(), &store_path)
                        .unwrap()
                        .is_none()
                );
            }
            window.cost(&conn)
        }));
        for (name, value) in saved {
            match value {
                Some(value) => unsafe { std::env::set_var(name, value) },
                None => unsafe { std::env::remove_var(name) },
            }
        }
        let cost = outcome.unwrap();
        assert!(cost.is_zero(), "an unchanged Cursor scan wrote: {cost:?}");
    }

    /// Runs `body` against private Cursor and Longhouse state, so a scan reads
    /// no real machine's claims or history.
    fn with_private_agent_state<T>(dir: &Path, body: impl FnOnce() -> T) -> T {
        temp_env::with_vars(
            [
                ("CURSOR_HOME", Some(dir.join("cursor").into_os_string())),
                ("XDG_CONFIG_HOME", Some(dir.join("config").into_os_string())),
                (
                    "LONGHOUSE_HOME",
                    Some(dir.join("longhouse").into_os_string()),
                ),
            ],
            body,
        )
    }

    /// Make a store look like it has been idle: `wal_database_stamp` does not
    /// vouch for a store written in the last couple of seconds.
    fn rest_cursor_store(path: &Path) {
        let long_ago = std::time::SystemTime::now() - Duration::from_secs(3600);
        for file in [
            path.to_path_buf(),
            PathBuf::from(format!("{}-wal", path.display())),
        ] {
            if let Ok(handle) = fs::OpenOptions::new().write(true).open(&file) {
                handle.set_modified(long_ago).unwrap();
            }
        }
    }

    /// Ship a Cursor store until nothing is left, the way the daemon reruns a
    /// source that reports `Continue`.
    fn drain_cursor_store(conn: &mut Connection, path: &Path) {
        for _ in 0..40 {
            match prepare_next_cursor_envelope_outcome(conn, &capabilities(), path).unwrap() {
                CursorPreparationOutcome::Envelope(prepared) => {
                    acknowledge_prepared(conn, &prepared)
                }
                CursorPreparationOutcome::Current => return,
                _ => {}
            }
        }
        panic!("a Cursor store did not settle");
    }

    fn captured_blob_ids(conn: &Connection) -> Vec<String> {
        let epoch = source_epoch::active_source_epoch(
            conn,
            "cursor",
            &cursor_store::cursor_opaque_source_id(CURSOR_CONVERSATION_ID),
        )
        .unwrap()
        .expect("the store has an active epoch");
        cursor_store_records::cursor_records_from(conn, epoch, 0, 100_000, u64::MAX)
            .unwrap()
            .iter()
            .filter_map(|record| {
                let value: Value = serde_json::from_slice(&record.bytes).ok()?;
                (value["kind"] == "blob").then(|| value["blob_id"].as_str().unwrap().to_string())
            })
            .collect()
    }

    #[test]
    fn a_scan_pass_over_an_unchanged_large_cursor_store_reads_no_blobs_and_writes_nothing() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let store_path = dir
            .path()
            .join("cursor/chats/workspace")
            .join(CURSOR_CONVERSATION_ID)
            .join("store.db");
        fs::create_dir_all(store_path.parent().unwrap()).unwrap();
        let store = make_cursor_store(&store_path);
        // Cursor keeps its stores in WAL mode.
        store
            .query_row("PRAGMA journal_mode=WAL", [], |row| row.get::<_, String>(0))
            .unwrap();
        // More than two pages, so the walk cannot finish in one.
        for index in 0..(CURSOR_BLOB_PAGE_ROWS * 2 + 44) {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("{index:064x}"), vec![index as u8; 8]],
                )
                .unwrap();
        }
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            drain_cursor_store(&mut conn, &store_path);

            // Idle now. The first pass after that finds a store it can vouch
            // for and walks it one last time; every pass after it rests.
            rest_cursor_store(&store_path);
            drain_cursor_store(&mut conn, &store_path);
            let reads = cursor_store::BLOB_PAGE_READS.with(|reads| reads.get());
            let window = WalWindow::open(&conn);
            for _pass in 0..3 {
                assert!(matches!(
                    prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                        .unwrap(),
                    CursorPreparationOutcome::Current
                ));
            }
            let cost = window.cost(&conn);
            assert!(
                cost.is_zero(),
                "an unchanged large Cursor store wrote: {cost:?}"
            );
            assert_eq!(
                cursor_store::BLOB_PAGE_READS.with(|reads| reads.get()),
                reads,
                "an unchanged large Cursor store re-read its blobs"
            );
        });
    }

    #[test]
    fn a_large_cursor_store_that_gained_a_blob_is_walked_again_and_finds_it() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let store_path = dir
            .path()
            .join("cursor/chats/workspace")
            .join(CURSOR_CONVERSATION_ID)
            .join("store.db");
        fs::create_dir_all(store_path.parent().unwrap()).unwrap();
        let store = make_cursor_store(&store_path);
        store
            .query_row("PRAGMA journal_mode=WAL", [], |row| row.get::<_, String>(0))
            .unwrap();
        for index in 0..(CURSOR_BLOB_PAGE_ROWS + 44) {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("{index:064x}"), vec![index as u8; 8]],
                )
                .unwrap();
        }
        // Blob ids are hashes, so a late one can sort below everything read.
        let insert_below_the_head = |id: &str| {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![id, vec![7u8; 8]],
                )
                .unwrap();
        };
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            rest_cursor_store(&store_path);
            drain_cursor_store(&mut conn, &store_path);
            drain_cursor_store(&mut conn, &store_path);
            let reads = cursor_store::BLOB_PAGE_READS.with(|reads| reads.get());
            drain_cursor_store(&mut conn, &store_path);
            assert_eq!(
                cursor_store::BLOB_PAGE_READS.with(|reads| reads.get()),
                reads,
                "the walk should have settled for this store"
            );

            // A store at rest that then changes and is idle again: the stamp
            // moved, so the settled walk no longer vouches for it.
            insert_below_the_head("-late-blob-after-the-walk-settled");
            rest_cursor_store(&store_path);
            drain_cursor_store(&mut conn, &store_path);
            assert!(
                captured_blob_ids(&conn)
                    .iter()
                    .any(|id| id == "-late-blob-after-the-walk-settled"),
                "a blob added to a settled store must still be discovered"
            );

            // And one that is still being written: never called unchanged.
            rest_cursor_store(&store_path);
            drain_cursor_store(&mut conn, &store_path);
            insert_below_the_head("-blob-written-just-now");
            drain_cursor_store(&mut conn, &store_path);
            assert!(
                captured_blob_ids(&conn)
                    .iter()
                    .any(|id| id == "-blob-written-just-now"),
                "a store written this instant must be walked"
            );
        });
    }

    /// An OpenCode database whose one session is named `session_id` and lives in
    /// `directory`. The managed-state root is process-wide while a test using it
    /// runs, and tests that do not take the guard read it too, so these tests
    /// name a session and workspace that no other test's database has.
    fn opencode_db_with_session(path: &Path, session_id: &str, directory: &str) {
        create_opencode_db(path);
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(&format!(
            "UPDATE session SET id = '{session_id}', directory = '{directory}';
             UPDATE message SET session_id = '{session_id}';
             UPDATE part SET session_id = '{session_id}';"
        ))
        .unwrap();
    }

    /// An OpenCode database shipped and then left alone.
    fn settled_opencode_database(dir: &Path, session_id: &str) -> (PathBuf, Connection) {
        let db_path = dir.join("opencode.db");
        opencode_db_with_session(&db_path, session_id, "/tmp/settled-workspace");
        let mut conn = open_db(Some(&dir.join("state.db"))).unwrap();
        rest_cursor_store(&db_path);
        let first = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
            .unwrap()
            .unwrap();
        acknowledge_prepared(&mut conn, &first);
        // The walk that finds nothing left to ship is the one that records it.
        assert!(
            prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .is_none()
        );
        (db_path, conn)
    }

    fn opencode_opens() -> usize {
        opencode_db::DATABASE_OPENS.with(|opens| opens.get())
    }

    fn with_opencode_state_root<T>(root: &Path, body: impl FnOnce() -> T) -> T {
        temp_env::with_vars(
            [
                (
                    "LONGHOUSE_OPENCODE_STATE_ROOT",
                    Some(root.as_os_str().to_owned()),
                ),
                ("LONGHOUSE_HOME", Some(root.join("home").into_os_string())),
            ],
            body,
        )
    }

    #[test]
    fn a_scan_pass_over_an_opencode_database_that_has_not_moved_does_not_open_it() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        with_opencode_state_root(&dir.path().join("state-root"), || {
            let (db_path, mut conn) = settled_opencode_database(dir.path(), "settled-session");

            let opens = opencode_opens();
            let window = WalWindow::open(&conn);
            for _pass in 0..3 {
                assert!(
                    prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                        .unwrap()
                        .is_none()
                );
            }
            assert_eq!(
                opencode_opens(),
                opens,
                "an OpenCode database that had not moved was walked again"
            );
            let cost = window.cost(&conn);
            assert!(cost.is_zero(), "an unchanged OpenCode scan wrote: {cost:?}");
        });
    }

    /// OpenCode keeps every session in one database, so the import scope is
    /// applied where the walk reads each session: a session outside it is never
    /// read or shipped, and widening the scope makes the next walk pick it up.
    #[test]
    fn the_opencode_walk_ships_only_sessions_inside_the_import_scope() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("state-root");
        with_opencode_state_root(&root, || {
            let machine = root.join("home").join("machine");
            let db_path = dir.path().join("opencode.db");
            // The fixture session was created long before any scope chosen today.
            opencode_db_with_session(&db_path, "scoped-session", "/tmp/scoped-workspace");
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            rest_cursor_store(&db_path);

            let from_now = crate::import_scope::ImportScope::starting(chrono::Utc::now(), "cli");
            from_now.save(&machine).unwrap();
            assert!(
                prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                    .unwrap()
                    .is_none(),
                "a session that began before the scope was shipped"
            );

            // Another project opted in does not bring it back; its own folder does.
            crate::import_scope::ImportScope {
                projects: vec![PathBuf::from("/tmp/elsewhere")],
                ..from_now.clone()
            }
            .save(&machine)
            .unwrap();
            assert!(
                prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                    .unwrap()
                    .is_none()
            );
            crate::import_scope::ImportScope {
                projects: vec![PathBuf::from("/tmp/scoped-workspace")],
                ..from_now
            }
            .save(&machine)
            .unwrap();
            let shipped = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .expect("an opted-in project's old session ships");
            acknowledge_prepared(&mut conn, &shipped);
        });
    }

    #[test]
    fn widening_the_import_scope_makes_the_opencode_walk_pick_up_what_it_skipped() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("state-root");
        with_opencode_state_root(&root, || {
            let machine = root.join("home").join("machine");
            let db_path = dir.path().join("opencode.db");
            opencode_db_with_session(&db_path, "widened-session", "/tmp/widened-workspace");
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            rest_cursor_store(&db_path);
            crate::import_scope::ImportScope::starting(chrono::Utc::now(), "cli")
                .save(&machine)
                .unwrap();
            // The walk that skipped it settles, as a scan over an idle database does.
            for _ in 0..2 {
                assert!(
                    prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                        .unwrap()
                        .is_none()
                );
            }
            crate::import_scope::ImportScope::all("cli")
                .save(&machine)
                .unwrap();
            let shipped = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .expect("widening the scope ends the rest: the skipped session ships");
            acknowledge_prepared(&mut conn, &shipped);
        });
    }

    #[test]
    fn an_opencode_database_that_changed_is_walked_again_and_ships() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        with_opencode_state_root(&dir.path().join("state-root"), || {
            let (db_path, mut conn) = settled_opencode_database(dir.path(), "settled-session");
            let db = Connection::open(&db_path).unwrap();
            db.execute(
                "INSERT INTO message VALUES (
                     'message-2', 'settled-session', 1779000000030, 1779000000030, '{\"role\":\"assistant\"}'
                 )",
                [],
            )
            .unwrap();

            // Written this instant: nothing may vouch for it.
            let opens = opencode_opens();
            let prepared = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .expect("a database written just now must ship its new message");
            assert!(opencode_opens() > opens);
            acknowledge_prepared(&mut conn, &prepared);

            // Written, then left alone: the stamp moved, so the last walk no
            // longer describes it, and the walk after that settles again.
            db.execute(
                "INSERT INTO message VALUES (
                     'message-3', 'settled-session', 1779000000040, 1779000000040, '{\"role\":\"user\"}'
                 )",
                [],
            )
            .unwrap();
            rest_cursor_store(&db_path);
            let opens = opencode_opens();
            let prepared = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .expect("a changed database must ship its new message");
            assert!(opencode_opens() > opens);
            acknowledge_prepared(&mut conn, &prepared);
            assert!(
                prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                    .unwrap()
                    .is_none()
            );
            let opens = opencode_opens();
            assert!(
                prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                    .unwrap()
                    .is_none()
            );
            assert_eq!(opencode_opens(), opens);
        });
    }

    #[test]
    fn an_opencode_database_with_an_envelope_waiting_is_not_called_settled() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        with_opencode_state_root(&dir.path().join("state-root"), || {
            let (db_path, conn) = settled_opencode_database(dir.path(), "settled-session");
            let rest_key = opencode_rest_key(&conn, &db_path).unwrap();
            assert!(rest_key.is_some());
            assert!(opencode_database_is_settled(&conn, &db_path, rest_key.as_deref()).unwrap());

            // Something is queued for one of its sessions, blocked or not.
            let epoch: String = conn
                .query_row(
                    "SELECT source_epoch FROM source_epoch_registry WHERE provider = 'opencode'",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            conn.execute(
                "INSERT INTO pending_source_envelope (
                     source_epoch, source_path, range_start, range_end, envelope_id,
                     request_body_zstd, media_objects_zstd, raw_bytes, event_count,
                     has_reply_evidence, has_more, created_at
                 ) VALUES (?1, 'opencode.db', 0, 1, 'envelope', X'00', X'00', 0, 0, 0, 0, 'now')",
                [epoch],
            )
            .unwrap();
            assert!(!opencode_database_is_settled(&conn, &db_path, rest_key.as_deref()).unwrap());
        });
    }

    #[test]
    fn an_opencode_database_whose_lane_was_rewound_is_walked_again() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        with_opencode_state_root(&dir.path().join("state-root"), || {
            let (db_path, mut conn) = settled_opencode_database(dir.path(), "settled-session");

            // The OpenCode file is untouched, but the host's receipts no longer
            // cover what it holds.
            conn.execute(
                "UPDATE source_epoch_lane_state SET last_position = 0 WHERE lane = 'durable'",
                [],
            )
            .unwrap();
            let opens = opencode_opens();
            let prepared = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .expect("a rewound lane owes the host its records again");
            assert!(opencode_opens() > opens);
            assert_eq!(prepared.range_start, 0);
        });
    }

    #[test]
    fn managed_state_that_arrives_for_a_settled_opencode_database_still_rebinds_it() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let state_root = dir.path().join("state-root");
        with_opencode_state_root(&state_root, || {
            let (db_path, mut conn) = settled_opencode_database(dir.path(), "settled-session");

            // No write to the database, but a managed launch now owns its session.
            let managed_session_id = "018f0c3a-7b2d-7f10-8a11-123456789abd";
            fs::create_dir_all(&state_root).unwrap();
            fs::write(
                state_root.join("managed.json"),
                serde_json::json!({
                    "provider": "opencode",
                    "longhouse_session_id": managed_session_id,
                    "opencode_session_id": "settled-session",
                })
                .to_string(),
            )
            .unwrap();
            let prepared = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .expect("managed state for a settled database must rebind it");
            assert_eq!(prepared.envelope.session_id, managed_session_id);
        });
    }

    /// The managed launch's state file was there all along, but the engine could
    /// not read it. Making it readable changes no length, inode or mtime, so
    /// nothing a stat can see tells the settled database that its binding is now
    /// known: the walk that missed the file must not be the one that settles it.
    #[cfg(unix)]
    #[test]
    fn a_managed_binding_that_becomes_readable_still_rebinds_a_settled_opencode_database() {
        use std::os::unix::fs::PermissionsExt;
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let state_root = dir.path().join("state-root");
        with_opencode_state_root(&state_root, || {
            let managed_session_id = "018f0c3a-7b2d-7f10-8a11-123456789abe";
            fs::create_dir_all(&state_root).unwrap();
            let managed = state_root.join("managed.json");
            fs::write(
                &managed,
                serde_json::json!({
                    "provider": "opencode",
                    "longhouse_session_id": managed_session_id,
                    "opencode_session_id": "settled-session",
                })
                .to_string(),
            )
            .unwrap();
            // Old enough for a stat to vouch for, then made unreadable.
            fs::OpenOptions::new()
                .write(true)
                .open(&managed)
                .unwrap()
                .set_modified(std::time::SystemTime::now() - Duration::from_secs(3600))
                .unwrap();
            fs::set_permissions(&managed, fs::Permissions::from_mode(0o000)).unwrap();
            // A user that reads anything has no unreadable file to make.
            if fs::read(&managed).is_ok() {
                return;
            }

            // Shipped under its own id, since the binding could not be read.
            let (db_path, mut conn) = settled_opencode_database(dir.path(), "settled-session");
            for _pass in 0..2 {
                let opens = opencode_opens();
                assert!(
                    prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                        .unwrap()
                        .is_none()
                );
                assert!(
                    opencode_opens() > opens,
                    "a walk that could not read the binding evidence was called settled"
                );
            }

            fs::set_permissions(&managed, fs::Permissions::from_mode(0o600)).unwrap();
            let prepared = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .expect("a binding that became readable must rebind the session");
            assert_eq!(prepared.envelope.session_id, managed_session_id);
        });
    }

    #[test]
    fn an_opencode_session_held_back_for_its_binding_is_not_called_settled() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let state_root = dir.path().join("state-root");
        with_opencode_state_root(&state_root, || {
            let db_path = dir.path().join("opencode.db");
            opencode_db_with_session(&db_path, "held-back-session", "/tmp/held-back-workspace");
            // Started this instant, in a workspace where another session is
            // live: it is held back until its managed binding could arrive.
            let db = Connection::open(&db_path).unwrap();
            db.execute(
                "UPDATE session SET time_created = ?1, time_updated = ?1",
                [Utc::now().timestamp_millis()],
            )
            .unwrap();
            fs::create_dir_all(&state_root).unwrap();
            fs::write(
                state_root.join("live.json"),
                serde_json::json!({
                    "provider": "opencode",
                    "cwd": "/tmp/held-back-workspace",
                    "provider_session_id": "another-session",
                    "pid": std::process::id(),
                })
                .to_string(),
            )
            .unwrap();
            rest_cursor_store(&db_path);
            for file in fs::read_dir(&state_root).unwrap().flatten() {
                fs::OpenOptions::new()
                    .write(true)
                    .open(file.path())
                    .unwrap()
                    .set_modified(std::time::SystemTime::now() - Duration::from_secs(3600))
                    .unwrap();
            }
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

            // The wait ends with the clock, not with a change to the database,
            // so every pass must look again.
            for _pass in 0..3 {
                let opens = opencode_opens();
                assert!(
                    prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                        .unwrap()
                        .is_none()
                );
                assert!(
                    opencode_opens() > opens,
                    "a session still waiting on its binding was called settled"
                );
            }
        });
    }

    #[test]
    fn an_opencode_database_with_a_row_in_flight_is_settled_only_until_it_is_given_up_on() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        with_opencode_state_root(&dir.path().join("state-root"), || {
            let db_path = dir.path().join("opencode.db");
            opencode_db_with_session(&db_path, "in-flight-session", "/tmp/in-flight-workspace");
            // A tool call OpenCode wrote a minute ago and has not finished.
            let provider = Connection::open(&db_path).unwrap();
            provider
                .execute(
                    "INSERT INTO part VALUES ('prt-running', 'message-1', 'in-flight-session', ?1, ?1,
                     '{\"type\":\"tool\",\"tool\":\"bash\",\"callID\":\"c\",\"state\":{\"status\":\"running\",\"input\":{}}}')",
                    [Utc::now().timestamp_millis() - 60_000],
                )
                .unwrap();
            rest_cursor_store(&db_path);
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            let first = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                .unwrap()
                .unwrap();
            assert!(!first.envelope.records.iter().any(|record| BASE64_STANDARD
                .decode(&record.data_b64)
                .unwrap()
                .windows(7)
                .any(|w| w == b"running")));
            acknowledge_prepared(&mut conn, &first);
            assert!(
                prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                    .unwrap()
                    .is_none()
            );

            // Nothing moved and nothing will until the row is given up on, so
            // the walk that found nothing to ship still vouches for the file.
            let opens = opencode_opens();
            assert!(
                prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                    .unwrap()
                    .is_none()
            );
            assert_eq!(opencode_opens(), opens, "a settled database was opened");

            // The wait is on the clock, not on the file: once the moment the row
            // would be given up on has passed, the next pass looks again.
            OPENCODE_AT_REST
                .lock()
                .unwrap()
                .get_mut(&db_path)
                .expect("the walk was recorded at rest")
                .1 = Some(Utc::now().timestamp_millis() - 1);
            assert!(
                prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
                    .unwrap()
                    .is_none()
            );
            assert!(opencode_opens() > opens, "the wake time was not honoured");
        });
    }

    /// A Cursor store shipped and then left alone: the state a laptop's history
    /// is in nearly all the time.
    fn settled_cursor_store(dir: &Path) -> (PathBuf, Connection) {
        let store_path = dir
            .join("cursor/chats/workspace")
            .join(CURSOR_CONVERSATION_ID)
            .join("store.db");
        fs::create_dir_all(store_path.parent().unwrap()).unwrap();
        let store = make_cursor_store(&store_path);
        // Cursor keeps its stores in WAL mode.
        store
            .query_row("PRAGMA journal_mode=WAL", [], |row| row.get::<_, String>(0))
            .unwrap();
        // A write is what creates the `-wal` file, which a store that has been
        // used has and `rest_cursor_store` needs to be able to age.
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES ('orphan', X'0900')",
                [],
            )
            .unwrap();
        (store_path, store)
    }

    fn store_opens() -> usize {
        cursor_store::STORE_OPENS.with(|opens| opens.get())
    }

    fn settle(conn: &mut Connection, store_path: &Path) {
        rest_cursor_store(store_path);
        drain_cursor_store(conn, store_path);
        // The pass that found it current is the one that recorded it at rest.
        let opens = store_opens();
        assert!(matches!(
            prepare_next_cursor_envelope_outcome(conn, &capabilities(), store_path).unwrap(),
            CursorPreparationOutcome::Current
        ));
        assert_eq!(store_opens(), opens, "the store did not settle");
    }

    fn grow_cursor_store(store: &Connection) {
        let mut extended_root = vec![0xbb; 32];
        extended_root.extend_from_slice(&[0xdd; 32]);
        set_cursor_root(store, CURSOR_ROOT_B, &extended_root);
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                params![
                    CURSOR_MESSAGE_B,
                    br#"{"role":"assistant","content":[{"type":"text","text":"second turn"}]}"#
                ],
            )
            .unwrap();
    }

    #[test]
    fn a_scan_pass_over_a_settled_cursor_store_does_not_open_it() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let (store_path, _store) = settled_cursor_store(dir.path());
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            settle(&mut conn, &store_path);

            let opens = store_opens();
            let window = WalWindow::open(&conn);
            for _pass in 0..3 {
                assert!(matches!(
                    prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                        .unwrap(),
                    CursorPreparationOutcome::Current
                ));
            }
            assert_eq!(
                store_opens(),
                opens,
                "a settled Cursor store was opened again"
            );
            let cost = window.cost(&conn);
            assert!(cost.is_zero(), "a settled Cursor store wrote: {cost:?}");
        });
    }

    #[test]
    fn a_settled_cursor_store_that_changed_is_read_and_ships_again() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let (store_path, store) = settled_cursor_store(dir.path());
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            settle(&mut conn, &store_path);

            // Written this instant: inside the stamp's resolution, so nothing
            // may vouch for it, whatever the files' times say.
            grow_cursor_store(&store);
            let opens = store_opens();
            let CursorPreparationOutcome::Envelope(prepared) =
                prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                    .unwrap()
            else {
                panic!("a store written just now must ship its new turn");
            };
            assert!(store_opens() > opens);
            acknowledge_prepared(&mut conn, &prepared);
            drain_cursor_store(&mut conn, &store_path);

            // Written, then left alone long enough to be stamped: the stamp
            // moved, so the last look no longer describes it.
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES ('late-blob', X'0708')",
                    [],
                )
                .unwrap();
            rest_cursor_store(&store_path);
            let opens = store_opens();
            drain_cursor_store(&mut conn, &store_path);
            assert!(store_opens() > opens, "a changed store was not read");
            assert!(
                captured_blob_ids(&conn).iter().any(|id| id == "late-blob"),
                "a blob added to a settled store must still be discovered"
            );

            // And it settles again.
            let opens = store_opens();
            assert!(matches!(
                prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                    .unwrap(),
                CursorPreparationOutcome::Current
            ));
            assert_eq!(store_opens(), opens);
        });
    }

    #[test]
    fn a_launch_claim_that_arrives_for_a_settled_cursor_store_still_rebinds_it() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let (store_path, store) = settled_cursor_store(dir.path());
        // The claim directory is process-wide while this runs, and tests that do
        // not take the guard read it too, so the claim names a conversation
        // that no other test's store has.
        let conversation_id = "0d0c5c2e-8d8b-4a6e-9a51-6f4b3c0f6a11";
        store
            .execute(
                "UPDATE meta SET value = ?1 WHERE key = '0'",
                [cursor_metadata_for(conversation_id, CURSOR_ROOT_A)],
            )
            .unwrap();
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            settle(&mut conn, &store_path);

            // Nothing was written to the store, but a managed launch now owns
            // the conversation: the source rebinds to that session.
            let managed_session_id = "018f0c3a-7b2d-7f10-8a11-123456789abd";
            let claim_dir = dir
                .path()
                .join("longhouse/managed-local/cursor-helm/binding-probes");
            fs::create_dir_all(&claim_dir).unwrap();
            fs::write(
                claim_dir.join("claim.json"),
                serde_json::to_vec(&serde_json::json!({
                    "schema_version": 2,
                    "provider": "cursor",
                    "status": "observed",
                    "session_id": managed_session_id,
                    "conversation_uuid": conversation_id,
                    "hook_observed_at": "2026-07-17T00:00:00Z"
                }))
                .unwrap(),
            )
            .unwrap();
            let CursorPreparationOutcome::Envelope(prepared) =
                prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                    .unwrap()
            else {
                panic!("a claim for a settled store must rebind it");
            };
            assert_eq!(prepared.envelope.session_id, managed_session_id);
        });
    }

    #[test]
    fn a_settled_cursor_store_whose_lane_was_rewound_is_still_read() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let (store_path, _store) = settled_cursor_store(dir.path());
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            settle(&mut conn, &store_path);

            // The store is untouched and every payload is on disk, but the
            // host's receipts no longer cover what it holds.
            conn.execute(
                "UPDATE source_epoch_lane_state SET last_position = 0 WHERE lane = 'durable'",
                [],
            )
            .unwrap();
            let opens = store_opens();
            let CursorPreparationOutcome::Envelope(prepared) =
                prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                    .unwrap()
            else {
                panic!("a rewound lane owes the host its records again");
            };
            assert!(store_opens() > opens);
            assert_eq!(prepared.range_start, 0);
        });
    }

    #[test]
    fn a_settled_cursor_store_that_lost_a_sealed_payload_is_still_repaired() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let (store_path, _store) = settled_cursor_store(dir.path());
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            settle(&mut conn, &store_path);
            let epoch = source_epoch::active_source_epoch(
                &conn,
                "cursor",
                &cursor_store::cursor_opaque_source_id(CURSOR_CONVERSATION_ID),
            )
            .unwrap()
            .unwrap();

            // The lane was rewound and a sealed payload was lost from disk:
            // with the store itself untouched, the whole-store repair walk
            // must still run.
            conn.execute(
                "UPDATE source_epoch_lane_state SET last_position = 0 WHERE source_epoch = ?1",
                [epoch.to_string()],
            )
            .unwrap();
            let records_root = crate::state::payload_store::root_for_connection(&conn)
                .unwrap()
                .join("records");
            let hashes: Vec<String> = conn
                .prepare("SELECT record_hash FROM cursor_store_raw_record WHERE source_epoch = ?1")
                .unwrap()
                .query_map([epoch.to_string()], |row| row.get(0))
                .unwrap()
                .collect::<std::result::Result<_, _>>()
                .unwrap();
            let lost = hashes
                .iter()
                .map(|hash| {
                    records_root.join(crate::state::payload_store::relative_path_for(hash, "rec"))
                })
                .filter(|path| fs::remove_file(path).is_ok())
                .count();
            assert!(lost > 0, "the records are sealed to files");

            let opens = store_opens();
            let CursorPreparationOutcome::Envelope(prepared) =
                prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                    .unwrap()
            else {
                panic!("a rewound lane owes the host its records again");
            };
            assert!(store_opens() > opens);
            assert_eq!(prepared.range_start, 0);
            assert!(
                hashes.iter().all(|hash| records_root
                    .join(crate::state::payload_store::relative_path_for(hash, "rec"))
                    .exists()),
                "the repair walk re-sealed what was lost"
            );
        });
    }

    #[test]
    fn a_settled_cursor_store_with_an_envelope_waiting_is_not_called_settled() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let (store_path, _store) = settled_cursor_store(dir.path());
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            settle(&mut conn, &store_path);
            let stamp = wal_database_stamp(&store_path);
            let path_text = stable_source_path(&store_path)
                .to_string_lossy()
                .to_string();
            assert!(cursor_store_is_settled(&conn, &path_text, stamp.as_deref()).unwrap());

            // Something is queued for its epoch, blocked or not.
            let epoch: String = conn
                .query_row(
                    "SELECT source_epoch FROM source_epoch_registry WHERE provider = 'cursor'",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            conn.execute(
                "INSERT INTO pending_source_envelope (
                     source_epoch, source_path, range_start, range_end, envelope_id,
                     request_body_zstd, media_objects_zstd, raw_bytes, event_count,
                     has_reply_evidence, has_more, created_at
                 ) VALUES (?1, 'store.db', 0, 1, 'envelope', X'00', X'00', 0, 0, 0, 0, 'now')",
                [epoch],
            )
            .unwrap();
            assert!(!cursor_store_is_settled(&conn, &path_text, stamp.as_deref()).unwrap());
        });
    }

    #[test]
    fn a_settled_cursor_store_whose_walk_is_not_finished_is_still_read() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let (store_path, _store) = settled_cursor_store(dir.path());
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            settle(&mut conn, &store_path);

            // The store is as it was, but the walk is said to be part-way.
            conn.execute(
                "UPDATE cursor_store_capture_cursor SET last_blob_id = 'mid-walk'",
                [],
            )
            .unwrap();
            let opens = store_opens();
            assert!(matches!(
                prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                    .unwrap(),
                CursorPreparationOutcome::Current
            ));
            assert!(
                store_opens() > opens,
                "a walk that had not finished was called settled"
            );
        });
    }

    #[test]
    fn a_settled_cursor_store_whose_epoch_rotated_is_still_read() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let (store_path, _store) = settled_cursor_store(dir.path());
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            settle(&mut conn, &store_path);
            let opaque_source_id = cursor_store::cursor_opaque_source_id(CURSOR_CONVERSATION_ID);
            let epoch = source_epoch::active_source_epoch(&conn, "cursor", &opaque_source_id)
                .unwrap()
                .unwrap();
            let incarnation =
                source_epoch::active_source_incarnation(&conn, "cursor", &opaque_source_id)
                    .unwrap()
                    .unwrap();

            // The source moved to a fresh epoch (a rewrite the host asked for)
            // that holds none of the store yet, on the same parser revision.
            let rotated = source_epoch::observe_source(
                &mut conn,
                "cursor",
                &opaque_source_id,
                &incarnation,
                0,
                SourceLane::Durable,
                0,
                Some(CURSOR_PARSER_REVISION),
                None,
                SourceChangeHint::Rewrite,
            )
            .unwrap();
            assert_ne!(rotated.source_epoch, epoch);

            let CursorPreparationOutcome::Envelope(prepared) =
                prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                    .unwrap()
            else {
                panic!("a new epoch is owed the whole store");
            };
            assert_eq!(prepared.source_epoch, rotated.source_epoch);
            assert_eq!(prepared.range_start, 0);
        });
    }

    #[test]
    fn a_parser_upgrade_replays_a_settled_cursor_store() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let (store_path, _store) = settled_cursor_store(dir.path());
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            settle(&mut conn, &store_path);
            let epoch = source_epoch::active_source_epoch(
                &conn,
                "cursor",
                &cursor_store::cursor_opaque_source_id(CURSOR_CONVERSATION_ID),
            )
            .unwrap()
            .unwrap();

            // The source was rendered by an older parser than this build.
            conn.execute(
                "UPDATE source_epoch_registry SET source_revision = 'cursor-store-render-old'
                 WHERE source_epoch = ?1",
                [epoch.to_string()],
            )
            .unwrap();
            let CursorPreparationOutcome::Envelope(prepared) =
                prepare_next_cursor_envelope_outcome(&mut conn, &capabilities(), &store_path)
                    .unwrap()
            else {
                panic!("an out-of-date render must be replayed from the store");
            };
            assert_ne!(prepared.source_epoch, epoch);
            assert_eq!(prepared.range_start, 0);
        });
    }

    #[test]
    fn a_whole_file_revision_hash_is_read_again_only_when_the_file_changed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("transcript.jsonl");
        let age = |path: &Path| {
            fs::OpenOptions::new()
                .write(true)
                .open(path)
                .unwrap()
                .set_modified(std::time::SystemTime::now() - Duration::from_secs(60))
                .unwrap();
        };
        let reads = || FILE_HASH_READS.with(|reads| reads.get());
        fs::write(&path, "first version\n").unwrap();
        age(&path);

        let before = reads();
        let first = hash_file(&path).unwrap();
        for _ in 0..3 {
            assert_eq!(hash_file(&path).unwrap(), first);
        }
        assert_eq!(reads() - before, 1, "an unchanged file was hashed again");

        // Rewritten in place to the same length: only the modification time
        // and the bytes say so.
        fs::write(&path, "other version\n").unwrap();
        age(&path);
        let second = hash_file(&path).unwrap();
        assert_ne!(second, first);
        assert_eq!(reads() - before, 2);

        // Written just now: never vouched for, so never served from the copy.
        fs::write(&path, "third version\n").unwrap();
        let third = hash_file(&path).unwrap();
        assert_ne!(third, second);
        assert_eq!(hash_file(&path).unwrap(), third);
        assert_eq!(reads() - before, 4);
    }

    #[test]
    fn a_scan_pass_over_unchanged_managed_omp_and_pi_sources_leaves_their_bindings_alone() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let omp_path = dir.path().join("omp-session.jsonl");
        fs::write(
            &omp_path,
            include_str!("../../tests/fixtures/golden/omp/native.jsonl"),
        )
        .unwrap();
        let pi_path = dir.path().join("pi-session.jsonl");
        fs::write(
            &pi_path,
            include_str!("../../tests/fixtures/golden/pi/native.jsonl"),
        )
        .unwrap();
        let omp_managed = "019d2869-1111-7222-8333-aaaaaaaaaaaa";
        let pi_managed = "019d2869-1111-7222-8333-bbbbbbbbbbbb";
        with_private_agent_state(dir.path(), || {
            let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
            // Launch: the managed owner is named once, ships, and is caught up.
            for (path, provider, managed) in [
                (&omp_path, "omp", omp_managed),
                (&pi_path, "pi", pi_managed),
            ] {
                let first = prepare_next_envelope(
                    &mut conn,
                    &capabilities(),
                    path,
                    provider,
                    Some(managed),
                )
                .unwrap()
                .unwrap();
                acknowledge_prepared(&mut conn, &first);
                assert!(
                    prepare_next_envelope(&mut conn, &capabilities(), path, provider, None)
                        .unwrap()
                        .is_none()
                );
            }
            // The runs ended: their bindings say so.
            conn.execute("UPDATE session_binding SET state = 'exited'", [])
                .unwrap();
            let bindings = |conn: &Connection| -> Vec<(String, String, String, Option<String>, String, String)> {
                conn.prepare(
                    "SELECT path, session_id, provider, provider_session_id, state, updated_at
                     FROM session_binding ORDER BY path",
                )
                .unwrap()
                .query_map([], |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                })
                .unwrap()
                .collect::<std::result::Result<_, _>>()
                .unwrap()
            };
            let before = bindings(&conn);
            assert_eq!(before.len(), 2);

            // Rediscovery is a scan pass with no override: the sources are on
            // disk, unchanged, owned as they were.
            let window = WalWindow::open(&conn);
            for _pass in 0..3 {
                for (path, provider) in [(&omp_path, "omp"), (&pi_path, "pi")] {
                    assert!(prepare_next_envelope(
                        &mut conn,
                        &capabilities(),
                        path,
                        provider,
                        None
                    )
                    .unwrap()
                    .is_none());
                }
            }
            let cost = window.cost(&conn);
            assert!(
                cost.is_zero(),
                "rediscovering managed sources wrote: {cost:?}"
            );
            assert_eq!(
                bindings(&conn),
                before,
                "a file still on disk is not evidence that its run is alive"
            );

            // A source that did grow still ships, and growth alone does not
            // make its exited run live either.
            let mut file = fs::OpenOptions::new().append(true).open(&omp_path).unwrap();
            writeln!(
                file,
                "{{\"type\":\"message\",\"id\":\"late-1\",\"parentId\":null,\"timestamp\":\"2026-09-09T00:00:09.000Z\",\"message\":{{\"role\":\"user\",\"content\":[{{\"type\":\"text\",\"text\":\"more\"}}]}}}}"
            )
            .unwrap();
            drop(file);
            let window = WalWindow::open(&conn);
            assert!(
                prepare_next_envelope(&mut conn, &capabilities(), &omp_path, "omp", None)
                    .unwrap()
                    .is_some()
            );
            assert!(window.cost(&conn).commits > 0);
            assert_eq!(bindings(&conn), before);
        });
    }

    // OpenCode sessions written the way OpenCode writes them: rows appear while
    // work is in flight and are rewritten as it finishes.

    /// Deterministic prose-like filler: words from a fixed vocabulary in a
    /// pseudo-random order, which zstd shrinks about the way real transcripts shrink.
    fn oc_filler(seed: u64, bytes: usize) -> String {
        let vocabulary: Vec<String> = (0..512u32)
            .map(|i| {
                format!(
                    "w{:x}{}",
                    i.wrapping_mul(2654435761) >> 20,
                    "aeiou".chars().nth((i % 5) as usize).unwrap()
                )
            })
            .collect();
        let mut state = seed
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let mut out = String::with_capacity(bytes + 16);
        while out.len() < bytes {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            out.push_str(&vocabulary[((state >> 33) % 512) as usize]);
            out.push(' ');
        }
        out.truncate(bytes);
        out
    }

    fn oc_now_ms() -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64
    }

    fn oc_create_schema(conn: &Connection) {
        conn.execute_batch(
            r#"
            CREATE TABLE project (id text PRIMARY KEY, worktree text NOT NULL, name text);
            CREATE TABLE session (
                id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
                directory text, path text, title text, version text,
                time_created integer NOT NULL, time_updated integer NOT NULL
            );
            CREATE TABLE message (
                id text PRIMARY KEY, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
            );
            CREATE TABLE part (
                id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
            );
            INSERT INTO project VALUES ('global', '/', NULL);
            "#,
        )
        .unwrap();
    }

    fn oc_add_session(conn: &Connection, session: &str, t: i64) {
        conn.execute(
            "INSERT INTO session VALUES (?1, 'global', NULL, '/tmp/work', '/tmp/work', 'Work', '1.16', ?2, ?2)",
            params![session, t],
        )
        .unwrap();
    }

    /// One agent turn, the way OpenCode writes it: rows appear in flight and
    /// are rewritten as they complete. `midpoint` runs while the tool call is
    /// still running (a live ship point); the caller ships again at the end.
    fn oc_turn(
        conn: &Connection,
        session: &str,
        turn: usize,
        t: i64,
        tool_output_bytes: usize,
        midpoint: &mut dyn FnMut(),
    ) {
        let user_m = format!("{session}-um{turn:04}");
        let asst_m = format!("{session}-am{turn:04}");
        let insert_msg = |id: &str, t: i64, data: &str| {
            conn.execute(
                "INSERT INTO message VALUES (?1, ?2, ?3, ?3, ?4)",
                params![id, session, t, data],
            )
            .unwrap();
        };
        let insert_part = |id: &str, msg: &str, t: i64, data: &str| {
            conn.execute(
                "INSERT INTO part VALUES (?1, ?2, ?3, ?4, ?4, ?5)",
                params![id, msg, session, t, data],
            )
            .unwrap();
        };
        insert_msg(
            &user_m,
            t,
            &format!(
                r#"{{"role":"user","time":{{"created":{t}}},"agent":"build","summary":{{"diffs":[]}}}}"#
            ),
        );
        insert_part(
            &format!("{session}-up{turn:04}"),
            &user_m,
            t + 1,
            &format!(r#"{{"type":"text","text":"question number {turn}"}}"#),
        );
        insert_msg(
            &asst_m,
            t + 2,
            &format!(
                r#"{{"role":"assistant","time":{{"created":{}}},"mode":"build","cost":0,"tokens":{{"input":0,"output":0}}}}"#,
                t + 2
            ),
        );
        insert_part(
            &format!("{session}-sp{turn:04}"),
            &asst_m,
            t + 3,
            r#"{"type":"step-start","snapshot":"abc"}"#,
        );
        let tool_id = format!("{session}-tp{turn:04}");
        insert_part(
            &tool_id,
            &asst_m,
            t + 4,
            &format!(
                r#"{{"type":"tool","tool":"bash","callID":"c{turn}","state":{{"status":"running","input":{{"command":"ls"}}}}}}"#
            ),
        );
        midpoint();
        let output = oc_filler((t as u64) ^ (turn as u64), tool_output_bytes);
        conn.execute(
            "UPDATE part SET data = ?1, time_updated = ?2 WHERE id = ?3",
            params![
                format!(r#"{{"type":"tool","tool":"bash","callID":"c{turn}","state":{{"status":"completed","input":{{"command":"ls"}},"output":"{output}"}}}}"#),
                t + 50,
                tool_id
            ],
        )
        .unwrap();
        insert_part(
            &format!("{session}-xp{turn:04}"),
            &asst_m,
            t + 51,
            &format!(
                r#"{{"type":"text","text":"{}","time":{{"start":{},"end":{}}}}}"#,
                oc_filler(turn as u64 + 7, 800),
                t + 51,
                t + 52
            ),
        );
        conn.execute(
            "UPDATE part SET time_updated = ?1 WHERE id = ?2",
            params![t + 52, format!("{session}-xp{turn:04}")],
        )
        .unwrap();
        insert_part(
            &format!("{session}-fp{turn:04}"),
            &asst_m,
            t + 53,
            r#"{"type":"step-finish","reason":"stop","tokens":{"input":10,"output":5}}"#,
        );
        conn.execute(
            "UPDATE message SET data = ?1, time_updated = ?2 WHERE id = ?3",
            params![
                format!(r#"{{"role":"assistant","time":{{"created":{},"completed":{}}},"mode":"build","cost":0.01,"tokens":{{"input":10,"output":5}},"finish":"stop"}}"#, t + 2, t + 54),
                t + 54,
                asst_m
            ],
        )
        .unwrap();
        conn.execute(
            "UPDATE message SET data = ?1, time_updated = ?2 WHERE id = ?3",
            params![
                format!(r#"{{"role":"user","time":{{"created":{t}}},"agent":"build","summary":{{"diffs":[{{"file":"a","patch":"{}"}}]}}}}"#, oc_filler(turn as u64 + 13, 300)),
                t + 55,
                user_m
            ],
        )
        .unwrap();
    }

    /// One envelope of an OpenCode drain: which epoch, which records, and what
    /// it rendered.
    struct OcShipped {
        epoch: Uuid,
        predecessor: Option<String>,
        start: u64,
        end: u64,
        raw_bytes: u64,
        records: Vec<String>,
        event_ids: Vec<String>,
    }

    fn oc_drain_shipped(conn: &mut Connection, db_path: &Path) -> Vec<OcShipped> {
        oc_drain_shipped_with(conn, db_path, &capabilities())
    }

    fn oc_drain_shipped_with(
        conn: &mut Connection,
        db_path: &Path,
        capabilities: &StorageV2Capabilities,
    ) -> Vec<OcShipped> {
        let mut shipped = Vec::new();
        while let Some(prepared) =
            prepare_next_opencode_envelope(conn, capabilities, db_path).unwrap()
        {
            shipped.push(OcShipped {
                epoch: prepared.source_epoch,
                predecessor: prepared.envelope.predecessor_source_epoch.clone(),
                start: prepared.range_start,
                end: prepared.range_end,
                raw_bytes: prepared.raw_bytes,
                records: prepared
                    .envelope
                    .records
                    .iter()
                    .map(|record| {
                        String::from_utf8(BASE64_STANDARD.decode(&record.data_b64).unwrap())
                            .unwrap()
                    })
                    .collect(),
                event_ids: prepared
                    .envelope
                    .render
                    .as_ref()
                    .map(|render| {
                        render
                            .records
                            .iter()
                            .map(|record| record.event_id.clone())
                            .collect()
                    })
                    .unwrap_or_default(),
            });
            acknowledge_prepared(conn, &prepared);
        }
        shipped
    }

    struct OcFixture {
        _dir: tempfile::TempDir,
        db_path: PathBuf,
        provider: Connection,
        state: Connection,
    }

    fn oc_fixture(session: &str) -> OcFixture {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        let provider = Connection::open(&db_path).unwrap();
        oc_create_schema(&provider);
        oc_add_session(&provider, session, oc_now_ms() - 3_600_000);
        let state = open_db(Some(&dir.path().join("state.db"))).unwrap();
        OcFixture {
            _dir: dir,
            db_path,
            provider,
            state,
        }
    }

    #[test]
    fn opencode_growth_ships_each_row_once_in_one_epoch() {
        let mut fixture = oc_fixture("ses_grow");
        let base = oc_now_ms() - 60_000;
        let mut shipped: Vec<OcShipped> = Vec::new();
        let turns = 30;
        for turn in 0..turns {
            let t = base + (turn as i64) * 100;
            // A live ship point while the tool call is still running, then one
            // when the turn is done.
            let (provider, state, db_path) =
                (&fixture.provider, &mut fixture.state, &fixture.db_path);
            let mut mid = Vec::new();
            oc_turn(provider, "ses_grow", turn, t, 2000, &mut || {
                mid.extend(oc_drain_shipped(state, db_path));
            });
            shipped.extend(mid);
            shipped.extend(oc_drain_shipped(state, db_path));
        }

        // One epoch, and every envelope picks up exactly where the last ended.
        let epochs: std::collections::HashSet<Uuid> =
            shipped.iter().map(|envelope| envelope.epoch).collect();
        assert_eq!(epochs.len(), 1, "growth must not rotate the epoch");
        assert!(shipped[0].predecessor.is_none());
        let mut next = 0;
        for envelope in &shipped {
            assert_eq!(envelope.start, next);
            next = envelope.end;
        }

        // Each turn adds one small envelope, not the session so far. A turn is
        // a user message and part, an assistant message and its three settled
        // parts (the tool call, the answer, the step end) and the step start.
        for envelope in &shipped[1..] {
            assert!(
                envelope.records.len() <= 8,
                "an append carried {} records",
                envelope.records.len()
            );
        }
        assert!(shipped.len() <= 2 * turns);

        // Every settled row went out exactly once: the bytes shipped are the
        // bytes of the stream, not a multiple of them. (Not equal: a user
        // message ships when it is written, and the diff summary OpenCode adds
        // once the turn is over is not shipped after the fact.)
        let stream =
            opencode_db::opencode_session_stream(&fixture.db_path, "ses_grow", oc_now_ms())
                .unwrap();
        let stream_bytes: u64 = stream
            .records
            .iter()
            .map(|record| record.len() as u64)
            .sum();
        let shipped_bytes: u64 = shipped.iter().map(|envelope| envelope.raw_bytes).sum();
        assert!(shipped_bytes <= stream_bytes);
        assert!(
            shipped_bytes * 100 >= stream_bytes * 90,
            "shipped {shipped_bytes} of {stream_bytes} bytes"
        );
        assert_eq!(next, stream.records.len() as u64);

        // Nothing rendered twice.
        let mut event_ids: Vec<&String> = shipped
            .iter()
            .flat_map(|envelope| envelope.event_ids.iter())
            .collect();
        let rendered = event_ids.len();
        event_ids.sort();
        event_ids.dedup();
        assert_eq!(event_ids.len(), rendered);
        assert!(rendered >= turns * 3);
    }

    #[test]
    fn opencode_appends_continue_a_session_that_shipped_in_several_batches() {
        let mut fixture = oc_fixture("ses_batches");
        let base = oc_now_ms() - 60_000;
        let mut small = capabilities();
        small.max_records = 3;
        oc_turn(&fixture.provider, "ses_batches", 0, base, 200, &mut || {});
        let first = oc_drain_shipped_with(&mut fixture.state, &fixture.db_path, &small);
        assert!(first.len() >= 3, "{} envelopes", first.len());
        oc_turn(
            &fixture.provider,
            "ses_batches",
            1,
            base + 100,
            200,
            &mut || {},
        );
        let second = oc_drain_shipped_with(&mut fixture.state, &fixture.db_path, &small);
        assert!(!second.is_empty());

        // One epoch, contiguous ranges across the batch boundaries, and every
        // event rendered once with its raw record in the same envelope.
        let all: Vec<&OcShipped> = first.iter().chain(&second).collect();
        assert!(all.iter().all(|envelope| envelope.epoch == all[0].epoch));
        let mut next = 0;
        for envelope in &all {
            assert_eq!(envelope.start, next);
            assert!(envelope.records.len() <= 3);
            next = envelope.end;
        }
        let mut event_ids: Vec<&String> = all.iter().flat_map(|e| e.event_ids.iter()).collect();
        let rendered = event_ids.len();
        event_ids.sort();
        event_ids.dedup();
        assert_eq!(event_ids.len(), rendered);
        assert_eq!(rendered, 8);
    }

    #[test]
    fn opencode_rows_still_being_written_wait_and_then_join_the_same_epoch() {
        let mut fixture = oc_fixture("ses_wait");
        let base = oc_now_ms() - 60_000;
        let (provider, state, db_path) = (&fixture.provider, &mut fixture.state, &fixture.db_path);
        let mut at_midpoint = Vec::new();
        oc_turn(provider, "ses_wait", 0, base, 500, &mut || {
            at_midpoint = oc_drain_shipped(state, db_path);
        });
        let at_end = oc_drain_shipped(state, db_path);

        // While the tool call was running neither it nor the assistant message
        // (which has no completion time yet) were shipped; the user's message
        // and its text were.
        let midpoint_records: Vec<&String> = at_midpoint
            .iter()
            .flat_map(|envelope| envelope.records.iter())
            .collect();
        assert!(midpoint_records
            .iter()
            .any(|record| record.contains("question number 0")));
        assert!(!midpoint_records
            .iter()
            .any(|record| record.contains("running") || record.contains("callID")));
        assert!(!midpoint_records
            .iter()
            .any(|record| record.contains("\"kind\":\"message\"") && record.contains("assistant")));

        // Once finished they ship, in the same epoch, each with its events.
        assert!(!at_end.is_empty());
        assert!(at_end
            .iter()
            .all(|envelope| envelope.epoch == at_midpoint[0].epoch));
        assert_eq!(at_end[0].start, at_midpoint.last().unwrap().end);
        let end_records: Vec<&String> = at_end
            .iter()
            .flat_map(|envelope| envelope.records.iter())
            .collect();
        assert!(end_records.iter().any(|record| record.contains("callID")));
        assert!(end_records
            .iter()
            .any(|record| record.contains("\"kind\":\"message\"") && record.contains("completed")));
        let midpoint_events: usize = at_midpoint.iter().map(|e| e.event_ids.len()).sum();
        let end_events: usize = at_end.iter().map(|e| e.event_ids.len()).sum();
        // user text at the midpoint; tool call, tool result and answer after.
        assert_eq!(midpoint_events, 1);
        assert_eq!(end_events, 3);
    }

    #[test]
    fn opencode_rewriting_a_shipped_row_rotates_the_epoch() {
        let mut fixture = oc_fixture("ses_rewrite");
        let base = oc_now_ms() - 60_000;
        oc_turn(&fixture.provider, "ses_rewrite", 0, base, 500, &mut || {});
        let first = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        assert!(!first.is_empty());
        let stream_len = first.last().unwrap().end;

        // A settled tool part is rewritten, as compaction does to old output.
        fixture
            .provider
            .execute(
                "UPDATE part SET data = replace(data, 'completed', 'completed '), time_updated = time_updated + 5000
                 WHERE id = 'ses_rewrite-tp0000'",
                [],
            )
            .unwrap();
        let second = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        assert!(!second.is_empty());
        assert_ne!(second[0].epoch, first[0].epoch);
        assert_eq!(
            second[0].predecessor.as_deref(),
            Some(first[0].epoch.to_string().as_str())
        );
        assert_eq!(second[0].start, 0);
        assert_eq!(second.last().unwrap().end, stream_len);

        // And the new epoch is stable again.
        assert!(oc_drain_shipped(&mut fixture.state, &fixture.db_path).is_empty());
    }

    #[test]
    fn opencode_a_row_that_settles_before_the_tail_rotates_the_epoch() {
        let mut fixture = oc_fixture("ses_late");
        let base = oc_now_ms() - 60_000;
        oc_turn(&fixture.provider, "ses_late", 0, base, 500, &mut || {});
        let first = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        // A row appears whose settling time is earlier than rows already sent.
        fixture
            .provider
            .execute(
                "INSERT INTO part VALUES ('ses_late-early', 'ses_late-um0000', 'ses_late', ?1, ?1, '{\"type\":\"step-start\"}')",
                params![base + 10],
            )
            .unwrap();
        let second = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        assert!(!second.is_empty());
        assert_ne!(second[0].epoch, first[0].epoch);
        assert_eq!(second[0].start, 0);
    }

    #[test]
    fn opencode_changes_nobody_owns_do_not_rotate_the_epoch() {
        let mut fixture = oc_fixture("ses_quiet");
        let base = oc_now_ms() - 60_000;
        oc_turn(&fixture.provider, "ses_quiet", 0, base, 500, &mut || {});
        let first = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        assert!(!first.is_empty());

        // The shared "global" project's worktree is rewritten by whichever
        // OpenCode process last ran outside a repository.
        for worktree in ["/private/tmp/agents/oc-research/ws", "/"] {
            fixture
                .provider
                .execute(
                    "UPDATE project SET worktree = ?1 WHERE id = 'global'",
                    params![worktree],
                )
                .unwrap();
            assert!(oc_drain_shipped(&mut fixture.state, &fixture.db_path).is_empty());
        }
        // OpenCode recomputes a user message's diff summary after the turn and
        // touches rows without changing them.
        fixture
            .provider
            .execute(
                "UPDATE message SET data = replace(data, '\"diffs\":[', '\"diffs\":[{\"file\":\"a\",\"patch\":\"p\"},'),
                        time_updated = time_updated + 9000
                 WHERE id = 'ses_quiet-um0000'",
                [],
            )
            .unwrap();
        fixture
            .provider
            .execute(
                "UPDATE message SET time_updated = time_updated + 9000 WHERE id = 'ses_quiet-am0000'",
                [],
            )
            .unwrap();
        fixture
            .provider
            .execute(
                "UPDATE session SET version = '9.9', time_updated = time_updated + 9000",
                [],
            )
            .unwrap();
        assert!(oc_drain_shipped(&mut fixture.state, &fixture.db_path).is_empty());
    }

    #[test]
    fn opencode_a_session_named_after_its_first_exchange_replaces_the_epoch_once() {
        let mut fixture = oc_fixture("ses_named");
        let base = oc_now_ms() - 60_000;
        oc_turn(&fixture.provider, "ses_named", 0, base, 500, &mut || {});
        let first = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        assert!(!first.is_empty());
        assert!(first[0].records[0].contains("\"title\":\"Work\""));

        // OpenCode generates the title once the first turn is done, and the
        // session record is the only place the archive keeps it.
        fixture
            .provider
            .execute("UPDATE session SET title = 'Fix the shipper'", [])
            .unwrap();
        let second = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        assert!(!second.is_empty());
        assert_ne!(second[0].epoch, first[0].epoch);
        assert_eq!(second[0].start, 0);
        assert!(second[0].records[0].contains("Fix the shipper"));
        assert!(oc_drain_shipped(&mut fixture.state, &fixture.db_path).is_empty());
    }

    #[test]
    fn opencode_gives_up_on_a_row_still_in_flight_after_two_hours() {
        let mut fixture = oc_fixture("ses_orphan");
        let now = oc_now_ms();
        let insert = |id: &str, message: &str, t: i64| {
            fixture
                .provider
                .execute(
                    "INSERT INTO part VALUES (?1, ?2, 'ses_orphan', ?3, ?3, '{\"type\":\"tool\",\"tool\":\"bash\",\"callID\":\"c\",\"state\":{\"status\":\"running\",\"input\":{}}}')",
                    params![id, message, t],
                )
                .unwrap();
        };
        fixture
            .provider
            .execute(
                "INSERT INTO message VALUES ('ses_orphan-um', 'ses_orphan', ?1, ?1, '{\"role\":\"user\"}')",
                params![now - 6 * 3_600_000],
            )
            .unwrap();
        insert("ses_orphan-crashed", "ses_orphan-um", now - 3 * 3_600_000);
        insert("ses_orphan-running", "ses_orphan-um", now - 1_000);
        let shipped = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        let records: Vec<&String> = shipped.iter().flat_map(|e| e.records.iter()).collect();
        assert!(records
            .iter()
            .any(|record| record.contains("ses_orphan-crashed")));
        assert!(!records
            .iter()
            .any(|record| record.contains("ses_orphan-running")));
    }

    #[test]
    fn opencode_epochs_from_before_the_stream_revision_are_replaced_once() {
        let mut fixture = oc_fixture("ses_legacy");
        let base = oc_now_ms() - 60_000;
        oc_turn(&fixture.provider, "ses_legacy", 0, base, 500, &mut || {});
        let first = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        // What every epoch shipped before this one holds: a bare hash of the
        // whole session.
        fixture
            .state
            .execute(
                "UPDATE source_epoch_registry SET source_revision = ?1 WHERE provider = 'opencode'",
                params!["a".repeat(64)],
            )
            .unwrap();
        let second = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        assert_ne!(second[0].epoch, first[0].epoch);
        assert_eq!(second[0].start, 0);
        assert!(oc_drain_shipped(&mut fixture.state, &fixture.db_path).is_empty());

        // An epoch that never recorded a revision cannot vouch for where its
        // records sit either.
        fixture
            .state
            .execute(
                "UPDATE source_epoch_registry SET source_revision = NULL WHERE provider = 'opencode' AND ended_at IS NULL",
                [],
            )
            .unwrap();
        let third = oc_drain_shipped(&mut fixture.state, &fixture.db_path);
        assert_ne!(third[0].epoch, second[0].epoch);
        assert_eq!(third[0].start, 0);
        assert!(oc_drain_shipped(&mut fixture.state, &fixture.db_path).is_empty());
    }
}
