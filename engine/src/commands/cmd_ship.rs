//! Ship command — one-shot session shipping and single-file shipping.
//!
//! One transcript lane, storage-v2. A Runtime Host that cannot accept it gets a
//! refusal that names the problem and the fix; there is no second protocol to
//! fall back to, and shipping nothing quietly would lose Source-tier history.

use std::path::PathBuf;
use std::time::{Duration, Instant};

use crate::config::ShipperConfig;
use crate::discovery;
use crate::opencode_db;
use crate::pipeline::compressor::CompressionAlgo;
use crate::shipping::client::ShipperClient;
use crate::shipping::storage_v2::require_storage_v2_cutover;
use crate::state::db::open_db;
use crate::state::spool::Spool;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

pub fn detect_provider_for_file(
    path: &std::path::Path,
    provider_override: Option<&str>,
) -> anyhow::Result<String> {
    if let Some(p) = provider_override {
        return Ok(p.to_lowercase());
    }

    let providers = discovery::get_providers();
    if let Some(p) = discovery::provider_for_path(path, &providers) {
        return Ok(p.to_string());
    }

    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.to_lowercase());

    match ext.as_deref() {
        Some("jsonl") => Ok("claude".to_string()),
        Some("json") => Ok("antigravity".to_string()),
        _ => anyhow::bail!(
            "Unable to determine provider for {} (use --provider)",
            path.display()
        ),
    }
}

async fn ship_path_storage_v2(
    conn: &mut rusqlite::Connection,
    client: &ShipperClient,
    capabilities: &crate::shipping::storage_v2::StorageV2Capabilities,
    path: &std::path::Path,
    provider: &str,
    session_id_override: Option<&str>,
    require_reply_evidence: bool,
    request_timeout: Duration,
) -> anyhow::Result<(usize, bool)> {
    let mut events_shipped = 0usize;
    loop {
        let prepared = if provider == "opencode" && opencode_db::is_opencode_database_path(path) {
            crate::storage_v2_shipper::prepare_next_opencode_envelope(conn, capabilities, path)?
        } else if provider == "cursor" && crate::cursor_store::is_cursor_store_database_path(path) {
            match crate::storage_v2_shipper::prepare_next_cursor_envelope_outcome(
                conn,
                capabilities,
                path,
            )? {
                crate::storage_v2_shipper::CursorPreparationOutcome::Envelope(prepared) => {
                    Some(prepared)
                }
                crate::storage_v2_shipper::CursorPreparationOutcome::Continue => continue,
                crate::storage_v2_shipper::CursorPreparationOutcome::Current
                | crate::storage_v2_shipper::CursorPreparationOutcome::WaitingOnClaim => None,
            }
        } else if provider == "cursor_acp" {
            crate::storage_v2_shipper::prepare_next_cursor_acp_envelope(conn, capabilities, path)?
        } else {
            crate::storage_v2_shipper::prepare_next_envelope(
                conn,
                capabilities,
                path,
                provider,
                session_id_override,
            )?
        };
        let Some(prepared) = prepared else {
            return Ok((events_shipped, false));
        };
        if require_reply_evidence && !prepared.has_reply_evidence {
            let discarded = crate::state::pending_source_envelope::discard_unattempted(
                conn,
                prepared.source_epoch,
                &prepared.envelope.expected_envelope_id,
            )?;
            if discarded {
                return Ok((events_shipped, true));
            }
            // Another sender already attempted this durable intent. It is now
            // the retry authority and cannot be discarded by a product gate.
        }
        let outcome = crate::storage_v2_shipper::ship_prepared_envelope(
            conn,
            client,
            capabilities,
            prepared,
            "repair",
            request_timeout,
        )
        .await?;
        events_shipped += outcome.events_shipped;
        if !outcome.has_more {
            return Ok((events_shipped, false));
        }
    }
}

// ---------------------------------------------------------------------------
// cmd_ship — scan all providers and ship new events
// ---------------------------------------------------------------------------

#[allow(clippy::too_many_arguments)]
pub async fn cmd_ship(
    url: Option<&str>,
    token: Option<&str>,
    db_path: Option<&std::path::Path>,
    workers: usize,
    json_output: bool,
    algo: CompressionAlgo,
    max_batch_bytes: Option<u64>,
    machine_name: Option<&str>,
) -> anyhow::Result<()> {
    let start = Instant::now();

    let config = ShipperConfig::from_env()?.with_overrides(
        url,
        token,
        db_path,
        if workers > 0 { Some(workers) } else { None },
        machine_name,
        max_batch_bytes,
    );
    crate::pipeline::compressor::set_machine_name(&config.machine_name);

    if !json_output {
        eprintln!("Shipping to: {}", config.api_url);
    }

    let mut conn = open_db(config.db_path.as_deref())?;
    let client = ShipperClient::with_compression(&config, algo)?;
    let negotiated = client
        .storage_v2_capabilities(&config.machine_name, Some(Duration::from_secs(5)))
        .await?;
    let capabilities = require_storage_v2_cutover(negotiated, &config.api_url)?;

    let providers = discovery::get_providers();
    let mut all_files = discovery::discover_all_files(&providers);
    // Legacy v1 spool rows are byte-range pointers into sources that still
    // exist on disk. Including their paths lets the storage-v2 lane cover the
    // same bytes from source and then retire the pointer row.
    for pending in Spool::new(&conn).pending_paths_now(10_000)? {
        if pending.provider == "cursor" {
            Spool::new(&conn).dead_letter_pending_for_path(
                &pending.file_path,
                "Cursor legacy pointer spool retired: storage-v2 source receipt is required",
            )?;
            continue;
        }
        let path = PathBuf::from(&pending.file_path);
        if path.exists() && !all_files.iter().any(|(known, _)| known == &path) {
            let provider = providers
                .iter()
                .find(|item| item.name == pending.provider)
                .map(|item| item.name)
                .unwrap_or("claude");
            all_files.push((path, provider));
        }
    }

    let mut files_shipped = 0usize;
    let mut events_shipped = 0usize;
    for (path, provider) in &all_files {
        let (events, _) = ship_path_storage_v2(
            &mut conn,
            &client,
            &capabilities,
            path,
            provider,
            None,
            false,
            Duration::from_secs(config.timeout_seconds),
        )
        .await?;
        if events > 0 {
            files_shipped += 1;
            events_shipped += events;
        }
        let pending_entries =
            Spool::new(&conn).pending_entries_for_path_now(&path.to_string_lossy(), 10_000)?;
        for entry in pending_entries {
            Spool::new(&conn).mark_shipped(entry.id)?;
        }
    }

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "status": "ok",
                "protocol": "storage-v2",
                "files_scanned": all_files.len(),
                "files_shipped": files_shipped,
                "events_shipped": events_shipped,
                "total_seconds": start.elapsed().as_secs_f64(),
            }))?
        );
    } else {
        println!(
            "Shipped {} events from {} files",
            events_shipped, files_shipped
        );
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// cmd_ship_file — ship a single explicit file
// ---------------------------------------------------------------------------

#[allow(clippy::too_many_arguments)]
pub async fn cmd_ship_file(
    path: &std::path::Path,
    provider_override: Option<&str>,
    url: Option<&str>,
    token: Option<&str>,
    db_path: Option<&std::path::Path>,
    json_output: bool,
    algo: CompressionAlgo,
    max_batch_bytes: Option<u64>,
    session_id_override: Option<&str>,
    require_reply_evidence: bool,
    replay: bool,
    machine_name: Option<&str>,
) -> anyhow::Result<()> {
    if !path.exists() {
        anyhow::bail!("File not found: {}", path.display());
    }

    let provider = detect_provider_for_file(path, provider_override)?;

    let config = ShipperConfig::from_env()?.with_overrides(
        url,
        token,
        db_path,
        None,
        machine_name,
        max_batch_bytes,
    );
    crate::pipeline::compressor::set_machine_name(&config.machine_name);

    if !json_output {
        eprintln!("Shipping file: {}", path.display());
        eprintln!("Provider: {}", provider);
    }

    let mut conn = open_db(config.db_path.as_deref())?;
    let client = ShipperClient::with_compression(&config, algo)?;
    let negotiated = client
        .storage_v2_capabilities(&config.machine_name, Some(Duration::from_secs(5)))
        .await?;
    // Settle the lane before touching durable state: rewinding for a replay the
    // host cannot accept would leave the local cursor behind for nothing.
    let capabilities = require_storage_v2_cutover(negotiated, &config.api_url)?;

    if replay {
        match crate::storage_v2_shipper::replay_file_source(&mut conn, path, &provider)? {
            Some(epoch) => tracing::info!(
                path = %path.display(),
                source_epoch = %epoch,
                "Opened a replacement epoch for replay; the host deduplicates re-shipped events by hash"
            ),
            None => tracing::info!(
                path = %path.display(),
                "Source was never shipped; nothing to rewind"
            ),
        }
    }

    let (events_shipped, reply_evidence_pending) = ship_path_storage_v2(
        &mut conn,
        &client,
        &capabilities,
        path,
        &provider,
        session_id_override,
        require_reply_evidence,
        Duration::from_secs(config.timeout_seconds),
    )
    .await?;

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "status": "ok",
                "protocol": "storage-v2",
                "file": path.display().to_string(),
                "provider": provider,
                "events_shipped": events_shipped,
                "reply_evidence_pending": reply_evidence_pending,
            }))?
        );
    } else if reply_evidence_pending {
        println!("No new events with reply evidence");
    } else {
        println!("Shipped {} events", events_shipped);
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::file_state::FileState;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn make_claude_file(dir: &tempfile::TempDir, name: &str, content: &str) -> PathBuf {
        let path = dir.path().join(name);
        std::fs::write(&path, content).unwrap();
        path
    }

    const ONE_TURN: &str = concat!(
        r#"{"type":"user","uuid":"probe-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
        "\n",
        r#"{"type":"assistant","uuid":"probe-2","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi"}]}}"#,
        "\n",
    );

    fn spawn_http_response_server(
        status_line: &str,
        body: &str,
    ) -> (String, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let status_line = status_line.to_string();
        let body = body.to_string();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0_u8; 8192];
            let _ = stream.read(&mut buf);
            let response = format!(
                "HTTP/1.1 {}\r\nContent-Length: {}\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}",
                status_line,
                body.len(),
                body,
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        (format!("http://{}", addr), handle)
    }

    fn assert_nothing_was_recorded(db_path: &std::path::Path, source: &std::path::Path) {
        let conn = open_db(Some(db_path)).unwrap();
        let file_str = source.to_string_lossy().to_string();
        let file_state = FileState::new(&conn);
        assert_eq!(file_state.get_offset(&file_str).unwrap(), 0);
        assert_eq!(file_state.get_queued_offset(&file_str).unwrap(), 0);
        assert!(Spool::new(&conn)
            .pending_entries_for_path_now(&file_str, 10)
            .unwrap()
            .is_empty());
    }

    /// A Runtime Host with no storage-v2 route answers 404 on the capability
    /// probe. There is no legacy lane left, so `ship --file` has to refuse by
    /// name rather than appear to succeed while shipping nothing.
    #[test]
    fn ship_file_refuses_a_runtime_host_without_storage_v2() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(&dir, "aaaa1111-2222-3333-4444-555566667777.jsonl", ONE_TURN);
        let db_path = dir.path().join("engine.db");
        let (url, handle) = spawn_http_response_server("404 Not Found", "{}");

        let error = rt
            .block_on(cmd_ship_file(
                &file,
                Some("claude"),
                Some(&url),
                Some("test-token"),
                Some(&db_path),
                true,
                CompressionAlgo::Gzip,
                None,
                None,
                false,
                false,
                None,
            ))
            .expect_err("a host without storage-v2 must refuse, not ship nothing quietly");
        handle.join().unwrap();

        let message = format!("{error:#}");
        assert!(
            message.contains("too old for this Machine Agent"),
            "{message}"
        );
        assert!(
            message.contains("does not serve GET /api/agents/storage/v2/capabilities"),
            "{message}"
        );
        assert!(message.contains("upgrade the Runtime Host"), "{message}");
        assert!(
            message.contains(crate::shipping::storage_v2::STORAGE_V2_MINIMUM_RUNTIME_HOST),
            "{message}"
        );
        assert!(message.contains("untouched on disk"), "{message}");
        assert_nothing_was_recorded(&db_path, &file);
    }

    /// A host that serves storage-v2 with `cutover: false` is the subtler half
    /// of the same problem: the route answers 200, so only the flag says the
    /// engine cannot ship here.
    #[test]
    fn ship_file_refuses_a_runtime_host_that_has_not_cut_over() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(&dir, "cccc1111-2222-3333-4444-555566667777.jsonl", ONE_TURN);
        let db_path = dir.path().join("engine.db");
        let body = serde_json::to_string(&serde_json::json!({
            "protocol_version": 2,
            "cutover": false,
            "tenant_id": "tenant",
            "machine_id": "probe-machine",
            "ingest_path": "/api/agents/storage/v2/envelopes",
            "max_wire_body_bytes": 8 * 1024 * 1024,
            "max_raw_record_bytes": 4 * 1024 * 1024,
            "max_records": 1000,
            "media_claim_path": "/api/agents/storage/v2/media/claims",
            "media_upload_path_template": "/api/agents/storage/v2/media/{sha256}",
            "max_media_bytes": 32 * 1024 * 1024,
            "max_media_claims": 512,
            "range_kinds": ["byte_offset", "record_ordinal"],
            "lanes": ["live", "repair"],
            "lane_header": "X-Longhouse-Storage-Lane",
        }))
        .unwrap();
        let (url, handle) = spawn_http_response_server("200 OK", &body);

        let error = rt
            .block_on(cmd_ship_file(
                &file,
                Some("claude"),
                Some(&url),
                Some("test-token"),
                Some(&db_path),
                true,
                CompressionAlgo::Gzip,
                None,
                None,
                false,
                false,
                Some("probe-machine"),
            ))
            .expect_err("cutover=false must refuse, not silently ship nothing");
        handle.join().unwrap();

        let message = format!("{error:#}");
        assert!(message.contains("cutover=false"), "{message}");
        assert!(message.contains("upgrade the Runtime Host"), "{message}");
        assert_nothing_was_recorded(&db_path, &file);
    }

    /// Auth, upgrade-required and server failures are not "host too old", and
    /// none of them may open a second lane: every one is an error that leaves
    /// local state untouched.
    #[test]
    fn ship_file_capability_failures_never_fall_back_to_a_second_lane() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        for status_line in [
            "401 Unauthorized",
            "426 Upgrade Required",
            "503 Service Unavailable",
        ] {
            let dir = tempfile::tempdir().unwrap();
            let file =
                make_claude_file(&dir, "bbbb1111-2222-3333-4444-555566667777.jsonl", ONE_TURN);
            let db_path = dir.path().join("engine.db");
            let (url, handle) = spawn_http_response_server(status_line, "{}");
            let result = rt.block_on(cmd_ship_file(
                &file,
                Some("claude"),
                Some(&url),
                Some("test-token"),
                Some(&db_path),
                true,
                CompressionAlgo::Gzip,
                None,
                None,
                false,
                false,
                None,
            ));
            handle.join().unwrap();
            assert!(result.is_err(), "{status_line} must not ship");
            assert_nothing_was_recorded(&db_path, &file);
        }
    }

    /// An unreachable Runtime Host used to be absorbed into the v1 spool and
    /// reported as success. With one lane there is nothing to absorb it into,
    /// so the transport error has to reach the caller as itself — and it must
    /// not be dressed up as the "host too old" refusal, which names a
    /// different fix.
    #[test]
    fn ship_file_surfaces_an_unreachable_host_as_a_transport_error() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(&dir, "dddd1111-2222-3333-4444-555566667777.jsonl", ONE_TURN);
        let db_path = dir.path().join("engine.db");

        let error = rt
            .block_on(cmd_ship_file(
                &file,
                Some("claude"),
                Some("http://127.0.0.1:9"),
                Some("test-token"),
                Some(&db_path),
                true,
                CompressionAlgo::Gzip,
                None,
                None,
                false,
                false,
                None,
            ))
            .expect_err("an unreachable host must not report success");

        let message = format!("{error:#}");
        assert!(
            message.contains("storage-v2 capability request failed"),
            "{message}"
        );
        assert!(
            !message.contains("too old for this Machine Agent"),
            "{message}"
        );
        assert_nothing_was_recorded(&db_path, &file);
    }
}
