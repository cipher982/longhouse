//! Continuing a host-held epoch on a machine that has lost (or never had) its
//! local shipping state, instead of uploading the file again.
//!
//! The local registry is the only place shipping positions live on the machine,
//! so a reinstalled or migrated machine used to open a fresh epoch at position
//! zero, hit "another epoch is already open for this source" and replay the
//! whole file as a replacement. The host knows what it holds. Before opening a
//! first epoch for a large source the machine asks, proves its file still begins
//! with the bytes the host holds, and continues the host's epoch from
//! `accepted_through`.

use super::*;

use crate::shipping::storage_v2::{StorageV2ResumeEntry, StorageV2ResumeObject};

/// A source smaller than a live batch is cheaper to ship than to ask about, and a
/// brand-new live transcript is always small at its first ship.
pub(super) const RESUME_MINIMUM_SOURCE_BYTES: u64 = 64 * 1024;
/// Largest host object this machine will re-read to verify a prefix.
const RESUME_MAXIMUM_VERIFIED_OBJECT_BYTES: u64 = 64 * 1024 * 1024;

/// Providers whose sources are append-only LF-delimited files shipped by byte
/// offset (the same restriction as `resync_behind_host`).
fn resumable_provider(provider: &str, path: &Path) -> bool {
    let lower = provider.to_ascii_lowercase();
    if lower == "antigravity" {
        // Whole-document `.json` transcripts are rewritten, not appended.
        return path
            .extension()
            .and_then(|ext| ext.to_str())
            .is_some_and(|ext| ext.eq_ignore_ascii_case("jsonl"));
    }
    lower == "claude" || lower == "codex"
}

/// Adopt the host's open epoch for a source this machine has never shipped.
///
/// Returns true when the epoch was adopted. False means "ship the ordinary way"
/// (host lacks the capability, the source is not eligible, the host holds nothing
/// for it, or the evidence does not match this file), each with a log line saying
/// which. A transport failure is an error: shipping the whole file instead would
/// only recreate the conflict this avoids.
pub(super) async fn adopt_host_epoch_if_unshipped(
    conn: &mut Connection,
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    provider: &str,
    request_timeout: Duration,
) -> Result<bool> {
    let Some(resume_path) = capabilities.source_resume_path.as_deref() else {
        return Ok(false);
    };
    if !resumable_provider(provider, path) {
        return Ok(false);
    }
    let canonical_path = stable_source_path(path);
    let opaque_source_id = opaque_source_id(&canonical_path.to_string_lossy());
    if source_epoch::active_source_epoch(conn, provider, &opaque_source_id)?.is_some() {
        return Ok(false);
    }
    let Ok(metadata) = std::fs::metadata(path) else {
        return Ok(false);
    };
    if metadata.len() < RESUME_MINIMUM_SOURCE_BYTES {
        return Ok(false);
    }
    let query = StorageV2ResumeQuery {
        provider: provider.to_ascii_lowercase(),
        opaque_source_id: opaque_source_id.clone(),
        range_kind: "byte_offset".to_string(),
    };
    let answer = client
        .storage_v2_source_resume(resume_path, &[query], Some(request_timeout))
        .await?;
    let [entry] = answer.sources.as_slice() else {
        anyhow::bail!("Runtime Host source resume answered a different number of sources");
    };
    let accepted = match verified_host_epoch(capabilities, path, provider, &opaque_source_id, entry)
    {
        Ok(accepted) => accepted,
        Err(reason) => {
            tracing::info!(
                path = %path.display(),
                provider,
                reason = %reason,
                "Not continuing a host epoch; shipping this source from its own state"
            );
            return Ok(false);
        }
    };
    let adopted = source_epoch::adopt_host_epoch(
        conn,
        provider,
        &opaque_source_id,
        path,
        accepted.source_epoch,
        &accepted.opened_at,
        entry.session_id.as_deref(),
        accepted.accepted_through,
    )?;
    if adopted {
        tracing::info!(
            path = %path.display(),
            provider,
            source_epoch = %accepted.source_epoch,
            accepted_through = accepted.accepted_through,
            source_bytes = metadata.len(),
            "Continuing the Runtime Host's epoch; skipping bytes it already holds"
        );
    }
    Ok(adopted)
}

struct AcceptedHostEpoch {
    source_epoch: Uuid,
    opened_at: String,
    accepted_through: u64,
}

/// Everything that must hold before a host position may become a local cursor.
fn verified_host_epoch(
    capabilities: &StorageV2Capabilities,
    path: &Path,
    provider: &str,
    opaque_source_id: &str,
    entry: &StorageV2ResumeEntry,
) -> std::result::Result<AcceptedHostEpoch, String> {
    if !entry.found {
        return Err(format!(
            "host holds nothing for it ({})",
            entry.reason.as_deref().unwrap_or("unspecified")
        ));
    }
    if entry.provider != provider.to_ascii_lowercase()
        || entry.opaque_source_id != opaque_source_id
        || entry.range_kind != "byte_offset"
    {
        return Err("host answered for a different source".to_string());
    }
    let (Some(epoch), Some(first), Some(last)) = (
        entry.source_epoch.as_ref(),
        entry.first_object.as_ref(),
        entry.last_object.as_ref(),
    ) else {
        return Err("host evidence is incomplete".to_string());
    };
    if epoch.tenant_id != capabilities.tenant_id
        || epoch.machine_id != capabilities.machine_id
        || epoch.provider != provider.to_ascii_lowercase()
        || epoch.opaque_source_id != opaque_source_id
        || epoch.range_kind != "byte_offset"
        || epoch.state != "open"
        || epoch.replaced_by_source_epoch.is_some()
    {
        return Err("host epoch identity does not match this source".to_string());
    }
    if epoch.predecessor_source_epoch.is_some() {
        return Err("host epoch is a replacement; only initial epochs are continued".to_string());
    }
    let source_epoch = Uuid::parse_str(&epoch.source_epoch)
        .map_err(|_| "host epoch id is not a UUID".to_string())?;
    let accepted_through: u64 = epoch
        .accepted_through
        .parse()
        .map_err(|_| "host accepted_through is not a number".to_string())?;
    if accepted_through == 0 || epoch.opened_at.is_empty() {
        return Err("host epoch holds no bytes".to_string());
    }
    let parse = |value: &str| value.parse::<u64>().ok();
    let (Some(first_start), Some(first_end), Some(last_start), Some(last_end)) = (
        parse(&first.range_start),
        parse(&first.range_end),
        parse(&last.range_start),
        parse(&last.range_end),
    ) else {
        return Err("host object ranges are not numbers".to_string());
    };
    if last_end != accepted_through
        || first_start > first_end
        || last_start > last_end
        || first_start > last_start
    {
        return Err("host object ranges disagree with accepted_through".to_string());
    }
    let file_len = std::fs::metadata(path)
        .map_err(|error| format!("cannot stat source: {error}"))?
        .len();
    if file_len < accepted_through {
        return Err(format!(
            "local file ({file_len} bytes) is shorter than the host's {accepted_through}"
        ));
    }
    // The next envelope starts at a record boundary only if the byte before it is LF.
    let mut file = File::open(path).map_err(|error| format!("cannot open source: {error}"))?;
    if !byte_at(&mut file, accepted_through - 1)
        .map_err(|error| format!("cannot read source: {error}"))?
        .is_some_and(|byte| byte == b'\n')
    {
        return Err("host position is not on a record boundary of this file".to_string());
    }
    for object in [first, last] {
        verify_object_against_local_bytes(&mut file, object)?;
    }
    Ok(AcceptedHostEpoch {
        source_epoch,
        opened_at: epoch.opened_at.clone(),
        accepted_through,
    })
}

fn byte_at(file: &mut File, offset: u64) -> std::io::Result<Option<u8>> {
    use std::io::{Seek, SeekFrom};
    file.seek(SeekFrom::Start(offset))?;
    let mut byte = [0u8; 1];
    match file.read_exact(&mut byte) {
        Ok(()) => Ok(Some(byte[0])),
        Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => Ok(None),
        Err(error) => Err(error),
    }
}

/// Recompute a host object's record-hash digest from the local file: the same
/// LF-delimited records the engine ships, each hashed, the hashes hashed.
fn verify_object_against_local_bytes(
    file: &mut File,
    object: &StorageV2ResumeObject,
) -> std::result::Result<(), String> {
    use std::io::{Seek, SeekFrom};
    let start: u64 = object.range_start.parse().map_err(|_| "bad object start")?;
    let end: u64 = object.range_end.parse().map_err(|_| "bad object end")?;
    let length = end - start;
    if length > RESUME_MAXIMUM_VERIFIED_OBJECT_BYTES {
        return Err("host object is too large to verify locally".to_string());
    }
    file.seek(SeekFrom::Start(start))
        .map_err(|error| format!("cannot read source: {error}"))?;
    let mut bytes = vec![0u8; length as usize];
    file.read_exact(&mut bytes)
        .map_err(|error| format!("cannot read source: {error}"))?;
    let mut digest = Sha256::new();
    let mut records = 0u64;
    for record in bytes.split_inclusive(|byte| *byte == b'\n') {
        digest.update(Sha256::digest(record));
        records += 1;
    }
    let local = hex_hash(digest.finalize().into());
    if records != object.record_count || local != object.record_hashes_hash {
        return Err(format!(
            "local bytes {start}..{end} do not match the host's object {}",
            object.envelope_id
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    use super::*;
    use crate::config::ShipperConfig;
    use crate::pipeline::compressor::CompressionAlgo;
    use crate::state::db::open_db;

    const RESUME_PATH: &str = "/api/agents/storage/v2/sources/resume";

    /// What a Runtime Host holds for one source, and what it was asked.
    #[derive(Default)]
    struct HostState {
        epoch: Option<(String, String)>,
        session_id: String,
        objects: Vec<(u64, u64, u64, String)>,
        accepted_through: u64,
        resume_calls: usize,
        conflicts: usize,
        bytes_received: u64,
        envelope_starts: Vec<u64>,
    }

    fn record_digest(records: &[Vec<u8>]) -> String {
        let mut digest = Sha256::new();
        for record in records {
            digest.update(Sha256::digest(record));
        }
        hex_hash(digest.finalize().into())
    }

    async fn read_request(socket: &mut tokio::net::TcpStream) -> (String, Vec<u8>) {
        let mut bytes = Vec::new();
        let mut buffer = [0_u8; 8192];
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

    async fn respond(socket: &mut tokio::net::TcpStream, status: &str, body: String) {
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        socket.write_all(response.as_bytes()).await.unwrap();
    }

    /// A stateful fake host: it enforces the contiguity and epoch-identity rules of
    /// the real one and counts every conflict and received byte.
    fn spawn_host(
        listener: TcpListener,
        state: Arc<Mutex<HostState>>,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            loop {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, body) = read_request(&mut socket).await;
                if request_line.contains(RESUME_PATH) {
                    let query: serde_json::Value = serde_json::from_slice(&body).unwrap();
                    let asked = &query["sources"][0];
                    let body = {
                        let mut host = state.lock().unwrap();
                        host.resume_calls += 1;
                        let answer = match host.epoch.clone() {
                            None => serde_json::json!({
                                "provider": asked["provider"], "opaque_source_id": asked["opaque_source_id"],
                                "range_kind": "byte_offset", "found": false, "reason": "no_open_epoch",
                            }),
                            Some((epoch, opened_at)) => {
                                let object = |(start, end, count, hash): &(
                                    u64,
                                    u64,
                                    u64,
                                    String,
                                )| {
                                    serde_json::json!({
                                        "range_start": start.to_string(), "range_end": end.to_string(),
                                        "record_count": count, "record_hashes_hash": hash,
                                        "envelope_id": "e".repeat(64),
                                    })
                                };
                                serde_json::json!({
                                    "provider": asked["provider"], "opaque_source_id": asked["opaque_source_id"],
                                    "range_kind": "byte_offset", "found": true,
                                    "source_epoch": {
                                        "source_epoch": epoch, "tenant_id": "tenant-a", "machine_id": "cinder",
                                        "provider": asked["provider"], "opaque_source_id": asked["opaque_source_id"],
                                        "range_kind": "byte_offset", "state": "open",
                                        "predecessor_source_epoch": null, "replaced_by_source_epoch": null,
                                        "accepted_through": host.accepted_through.to_string(), "opened_at": opened_at,
                                    },
                                    "session_id": host.session_id,
                                    "first_object": object(host.objects.first().unwrap()),
                                    "last_object": object(host.objects.last().unwrap()),
                                })
                            }
                        };
                        serde_json::json!({
                            "v": 1, "sources": [answer], "commit_seq": "1", "observed_at": "2026-07-12T12:00:00+00:00",
                        })
                    };
                    respond(&mut socket, "200 OK", body.to_string()).await;
                    continue;
                }
                let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                let records: Vec<Vec<u8>> = envelope
                    .records
                    .iter()
                    .map(|record| BASE64_STANDARD.decode(&record.data_b64).unwrap())
                    .collect();
                let (status, reply) = loop {
                    let mut host = state.lock().unwrap();
                    let admitted = match host.epoch.as_ref() {
                        None => {
                            host.epoch = Some((
                                envelope.source_epoch.clone(),
                                envelope.epoch_opened_at.clone(),
                            ));
                            host.session_id = envelope.session_id.clone();
                            true
                        }
                        Some((epoch, opened_at)) => {
                            *epoch == envelope.source_epoch
                                && *opened_at == envelope.epoch_opened_at
                                && envelope.predecessor_source_epoch.is_none()
                                && envelope.range_start == host.accepted_through
                                && envelope.session_id == host.session_id
                        }
                    };
                    if !admitted {
                        host.conflicts += 1;
                        break (
                            "409 Conflict",
                            serde_json::json!({"detail": {
                                "code": "source_epoch_conflict", "message": "conflict",
                                "details": {"reason": "another_epoch_is_already_open_for_this_source"},
                            }}),
                        );
                    }
                    host.accepted_through = envelope.range_end;
                    host.bytes_received += records
                        .iter()
                        .map(|record| record.len() as u64)
                        .sum::<u64>();
                    host.envelope_starts.push(envelope.range_start);
                    host.objects.push((
                        envelope.range_start,
                        envelope.range_end,
                        records.len() as u64,
                        record_digest(&records),
                    ));
                    break (
                        "200 OK",
                        serde_json::json!({
                            "v": 2, "envelope_id": envelope.expected_envelope_id, "object_hash": "b".repeat(64),
                            "commit_seq": "42", "raw_state": "durable", "render_state": "ready",
                            "media_state": "complete", "missing_media_hashes": [],
                        }),
                    );
                };
                respond(&mut socket, status, reply.to_string()).await;
            }
        })
    }

    fn capabilities(resume: bool) -> StorageV2Capabilities {
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
            source_resume_path: resume.then(|| RESUME_PATH.to_string()),
        }
    }

    fn transcript_lines(range: std::ops::Range<usize>) -> Vec<u8> {
        range
            .flat_map(|index| {
                format!(
                    "{{\"type\":\"user\",\"uuid\":\"u{index}\",\"timestamp\":\"2026-07-12T12:{:02}:00Z\",\"message\":{{\"content\":\"line {index} {}\"}}}}\n",
                    index % 60,
                    "x".repeat(150)
                )
                .into_bytes()
            })
            .collect()
    }

    async fn ship_until_idle(
        conn: &mut Connection,
        client: &ShipperClient,
        capabilities: &StorageV2Capabilities,
        path: &Path,
    ) {
        for _ in 0..64 {
            let shipped = ship_next_envelope(
                conn,
                client,
                capabilities,
                path,
                "claude",
                None,
                "repair",
                Duration::from_secs(10),
            )
            .await
            .unwrap();
            if shipped.is_none() {
                return;
            }
        }
        panic!("source did not drain");
    }

    fn client_for(address: std::net::SocketAddr) -> ShipperClient {
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 10,
            ..ShipperConfig::default()
        };
        ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap()
    }

    #[tokio::test]
    async fn wiped_local_state_continues_the_host_epoch_and_sends_only_the_missing_bytes() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let state = Arc::new(Mutex::new(HostState::default()));
        let host = spawn_host(listener, state.clone());
        let client = client_for(address);

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let held = transcript_lines(0..800);
        std::fs::write(&path, &held).unwrap();

        // The original machine ships what exists, then its state is lost.
        let mut original = open_db(Some(&dir.path().join("original.db"))).unwrap();
        ship_until_idle(&mut original, &client, &capabilities(true), &path).await;
        drop(original);
        let (host_epoch, held_bytes) = {
            let host = state.lock().unwrap();
            assert_eq!(host.accepted_through, held.len() as u64);
            (host.epoch.clone().unwrap().0, host.accepted_through)
        };

        // The file grows while the machine is wiped; a fresh state database ships it.
        let mut grown = held.clone();
        grown.extend(transcript_lines(800..1000));
        std::fs::write(&path, &grown).unwrap();
        {
            let mut host = state.lock().unwrap();
            host.bytes_received = 0;
            host.envelope_starts.clear();
            // The original machine's own first ship asked once and was told nothing is held.
            assert_eq!(host.resume_calls, 1);
        }
        let mut wiped = open_db(Some(&dir.path().join("wiped.db"))).unwrap();
        ship_until_idle(&mut wiped, &client, &capabilities(true), &path).await;

        let host_state = state.lock().unwrap();
        assert_eq!(host_state.conflicts, 0, "continuation must not conflict");
        assert_eq!(
            host_state.resume_calls, 2,
            "one more question: the wiped machine's"
        );
        assert_eq!(
            host_state.bytes_received,
            grown.len() as u64 - held_bytes,
            "only the bytes the host lacked were sent"
        );
        assert_eq!(host_state.envelope_starts.first(), Some(&held_bytes));
        assert_eq!(host_state.accepted_through, grown.len() as u64);
        assert_eq!(host_state.epoch.as_ref().unwrap().0, host_epoch);
        drop(host_state);
        host.abort();
    }

    #[tokio::test]
    async fn a_file_that_does_not_match_the_host_prefix_is_not_adopted() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let state = Arc::new(Mutex::new(HostState::default()));
        let host = spawn_host(listener, state.clone());
        let client = client_for(address);
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        std::fs::write(&path, transcript_lines(0..800)).unwrap();
        let mut original = open_db(Some(&dir.path().join("original.db"))).unwrap();
        ship_until_idle(&mut original, &client, &capabilities(true), &path).await;

        // Same path and length, different bytes in the held prefix.
        let mut different = transcript_lines(0..800);
        different[40] ^= 0x01;
        std::fs::write(&path, &different).unwrap();
        let mut wiped = open_db(Some(&dir.path().join("wiped.db"))).unwrap();
        let adopted = adopt_host_epoch_if_unshipped(
            &mut wiped,
            &client,
            &capabilities(true),
            &path,
            "claude",
            Duration::from_secs(10),
        )
        .await
        .unwrap();
        assert!(!adopted);
        assert!(source_epoch::active_source_epoch(
            &wiped,
            "claude",
            &opaque_source_id(&stable_source_path(&path).to_string_lossy())
        )
        .unwrap()
        .is_none());
        assert_eq!(state.lock().unwrap().resume_calls, 2);
        host.abort();
    }

    #[tokio::test]
    async fn a_host_without_the_capability_is_never_asked() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let state = Arc::new(Mutex::new(HostState::default()));
        let host = spawn_host(listener, state.clone());
        let client = client_for(address);
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        std::fs::write(&path, transcript_lines(0..800)).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let adopted = adopt_host_epoch_if_unshipped(
            &mut conn,
            &client,
            &capabilities(false),
            &path,
            "claude",
            Duration::from_secs(10),
        )
        .await
        .unwrap();
        assert!(!adopted);
        assert_eq!(state.lock().unwrap().resume_calls, 0);
        host.abort();
    }
}
