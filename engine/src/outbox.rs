//! Outbox drain for presence events.
//!
//! Hook integrations write small JSON files to `~/.longhouse/agent/outbox/` instead of
//! calling the API directly. This eliminates network I/O from the hook hot path,
//! allowing hooks to run as `async: false` without risking stalls.
//!
//! The daemon drains the outbox on a short tick: reads all ready files,
//! coalesces by session_id (latest state wins), returns local phase signals for
//! transcript catch-up, POSTs to `/api/agents/presence`, and deletes files on
//! success. Files are kept on failure and retried next tick.
//! Files older than `STALE_SECS` are deleted without posting (presence is ephemeral).

use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::Value;
use tracing::warn;

use crate::shipping::client::ShipperClient;
use crate::state::session_phase::{PhaseSource, SessionPhaseSignal, SessionPhaseStore};
use crate::state::unmanaged_process_binding::{
    UnmanagedProcessBindingSignal, UnmanagedProcessBindingStore,
};

/// Maximum age for an outbox file before it is considered stale and deleted.
const STALE_SECS: u64 = 600; // 10 minutes
const PRESENCE_POST_TIMEOUT: Duration = Duration::from_secs(3);
const RUNTIME_EVENT_POST_TIMEOUT: Duration = Duration::from_secs(3);
const RUNTIME_EVENT_BATCH_LIMIT: usize = 128;

#[derive(Debug, Clone, Deserialize)]
struct PresenceOutboxPayload {
    session_id: String,
    state: String,
    #[serde(default)]
    tool_name: Option<String>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default)]
    transcript_path: Option<String>,
    #[serde(default)]
    provider: Option<String>,
    #[serde(default)]
    control_path: Option<String>,
    #[serde(default)]
    provider_pid: Option<u32>,
    #[serde(default)]
    occurred_at: Option<String>,
}

#[derive(Debug)]
struct PendingPresenceFile {
    path: PathBuf,
    bytes: Vec<u8>,
    payload: PresenceOutboxPayload,
    observed_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrainedPresenceSignal {
    pub session_id: String,
    pub provider: String,
    pub phase: String,
    pub observed_at: DateTime<Utc>,
    pub transcript_path: Option<PathBuf>,
}

#[derive(Debug, Default)]
#[cfg_attr(not(test), allow(dead_code))]
pub struct OutboxDrainResult {
    pub sent: usize,
    pub kept: usize,
    pub signals: Vec<DrainedPresenceSignal>,
}

#[derive(Debug, Default)]
pub struct OutboxLocalDrainResult {
    pub signals: Vec<DrainedPresenceSignal>,
    pub posts: Vec<PendingPresencePost>,
}

#[derive(Debug)]
pub struct PendingPresencePost {
    path: PathBuf,
    bytes: Vec<u8>,
}

#[derive(Debug)]
pub struct PendingRuntimeEventPost {
    path: PathBuf,
    event: Value,
}

/// Durably enqueue one runtime event for the daemon's shared retrying outbox.
/// Writers never POST directly: an atomic rename makes an event visible to the
/// drain loop only after its complete JSON payload reaches disk.
pub fn enqueue_runtime_event(dir: &Path, event: &Value) -> anyhow::Result<()> {
    std::fs::create_dir_all(dir)?;
    let bytes = serde_json::to_vec(event)?;
    let nonce = uuid::Uuid::new_v4();
    let temporary = dir.join(format!(".{nonce}.tmp"));
    let ready = dir.join(format!("{nonce}.json"));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    drop(file);
    std::fs::rename(&temporary, &ready)?;
    Ok(())
}

/// Drain all ready presence events from the outbox directory.
///
/// Returns `(sent, kept)`:
/// - `sent`: number of events successfully POSTed (files deleted)
/// - `kept`: number of files kept for retry (POST failed)
#[cfg_attr(not(test), allow(dead_code))]
pub async fn drain_outbox(dir: &Path, client: &ShipperClient) -> (usize, usize) {
    let result = drain_outbox_impl(dir, client, None, false).await;
    (result.sent, result.kept)
}

/// Same as `drain_outbox`, but also mirrors the latest coalesced phase into
/// the local agent DB so local-health can render accurate per-session phase.
#[cfg_attr(not(test), allow(dead_code))]
pub async fn drain_outbox_with_local_state(
    dir: &Path,
    client: &ShipperClient,
    db_path: Option<&Path>,
) -> (usize, usize) {
    let result = drain_outbox_impl(dir, client, db_path, true).await;
    (result.sent, result.kept)
}

/// Drain outbox files and return locally valid phase signals so the daemon can
/// schedule transcript catch-up work for the same sessions. Signals are returned
/// even when the presence POST fails because transcript shipping is local truth
/// and should not depend on the runtime accepting a presence update first.
#[cfg_attr(not(test), allow(dead_code))]
pub async fn drain_outbox_with_local_state_result(
    dir: &Path,
    client: &ShipperClient,
    db_path: Option<&Path>,
) -> OutboxDrainResult {
    drain_outbox_impl(dir, client, db_path, true).await
}

async fn drain_outbox_impl(
    dir: &Path,
    client: &ShipperClient,
    db_path: Option<&Path>,
    persist_local_state: bool,
) -> OutboxDrainResult {
    let local = collect_outbox_impl(dir, db_path, persist_local_state);
    let (sent, kept) = post_pending_presence_files(client, local.posts).await;
    OutboxDrainResult {
        sent,
        kept,
        signals: local.signals,
    }
}

pub fn collect_outbox_with_local_state_result(
    dir: &Path,
    db_path: Option<&Path>,
) -> OutboxLocalDrainResult {
    collect_outbox_impl(dir, db_path, true)
}

fn collect_outbox_impl(
    dir: &Path,
    db_path: Option<&Path>,
    persist_local_state: bool,
) -> OutboxLocalDrainResult {
    // Nothing to do if outbox doesn't exist yet.
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return OutboxLocalDrainResult::default(),
    };

    let now = SystemTime::now();
    // session_id → latest payload — newest observation per session wins
    let mut by_session: HashMap<String, PendingPresenceFile> = HashMap::new();

    for entry in entries.flatten() {
        let path = entry.path();

        // Skip non-JSON and in-progress tmp files (start with '.')
        let file_name = match path.file_name().and_then(|n| n.to_str()) {
            Some(n) => n.to_owned(),
            None => continue,
        };
        if !file_name.ends_with(".json") || file_name.starts_with('.') {
            // Prune stale dot-files (orphaned atomic-write temps) — they will never
            // be renamed to prs.*.json and would otherwise accumulate forever.
            if file_name.starts_with('.') {
                if let Ok(meta) = entry.metadata() {
                    if let Ok(modified) = meta.modified() {
                        if let Ok(age) = now.duration_since(modified) {
                            if age > Duration::from_secs(STALE_SECS) {
                                let _ = std::fs::remove_file(&path);
                            }
                        }
                    }
                }
            }
            continue;
        }

        // Delete stale files without POSTing — presence is ephemeral.
        if let Ok(meta) = entry.metadata() {
            if let Ok(modified) = meta.modified() {
                if let Ok(age) = now.duration_since(modified) {
                    if age > Duration::from_secs(STALE_SECS) {
                        let _ = std::fs::remove_file(&path);
                        continue;
                    }
                }
            }
        }

        // Read and validate JSON.
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(_) => continue, // file disappeared between read_dir and read
        };
        let payload: PresenceOutboxPayload = match serde_json::from_slice(&bytes) {
            Ok(v) => v,
            Err(_) => {
                // Malformed JSON — delete to avoid indefinite retry.
                let _ = std::fs::remove_file(&path);
                continue;
            }
        };

        // Must have a non-empty session_id.
        let sid = payload.session_id.trim().to_string();
        if sid.is_empty() {
            let _ = std::fs::remove_file(&path);
            continue;
        }
        let state = payload.state.trim();
        if state.is_empty() {
            let _ = std::fs::remove_file(&path);
            continue;
        }

        let observed_at = observed_at_for_payload(&payload, &entry, now);
        let next_file = PendingPresenceFile {
            path: path.clone(),
            bytes,
            payload,
            observed_at,
        };

        match by_session.get(&sid) {
            Some(existing) => {
                if next_file.observed_at > existing.observed_at {
                    let _ = std::fs::remove_file(&existing.path);
                    by_session.insert(sid, next_file);
                } else {
                    let _ = std::fs::remove_file(&path);
                }
            }
            None => {
                by_session.insert(sid, next_file);
            }
        }
    }

    let mut result = OutboxLocalDrainResult::default();
    let local_phase_conn = if persist_local_state && !by_session.is_empty() {
        match crate::state::db::open_db(db_path) {
            Ok(conn) => Some(conn),
            Err(err) => {
                warn!("opening local session phase DB failed: {err}");
                None
            }
        }
    } else {
        None
    };

    for pending in by_session.into_values() {
        let PendingPresenceFile {
            path,
            bytes,
            payload,
            observed_at,
        } = pending;
        let provider = normalize_provider(payload.provider.as_deref()).to_string();
        let session_id = payload.session_id.trim().to_string();
        let phase = payload.state.trim().to_string();
        let transcript_path = normalize_transcript_path(payload.transcript_path.as_deref());

        result.signals.push(DrainedPresenceSignal {
            session_id: session_id.clone(),
            provider: provider.clone(),
            phase: phase.clone(),
            observed_at: observed_at.clone(),
            transcript_path,
        });
        result.posts.push(PendingPresencePost { path, bytes });

        if let Some(conn) = local_phase_conn.as_ref() {
            let signal = SessionPhaseSignal {
                session_id: session_id.clone(),
                provider: provider.clone(),
                phase: phase.clone(),
                tool_name: payload.tool_name.clone(),
                source: PhaseSource::for_hook_provider(&provider)
                    .as_str()
                    .to_string(),
                observed_at,
            };
            if let Err(err) = SessionPhaseStore::new(conn).record(&signal) {
                warn!(
                    "persisting local phase failed for session {}: {err}",
                    signal.session_id
                );
            }
            if let Some(binding_signal) =
                unmanaged_binding_signal_for_payload(&payload, &provider, &session_id, observed_at)
            {
                if let Err(err) = UnmanagedProcessBindingStore::new(conn).record(&binding_signal) {
                    warn!(
                        "persisting unmanaged process binding failed for session {}: {err}",
                        binding_signal.provider_session_id
                    );
                }
            }
        }
    }

    result
}

pub async fn post_pending_presence_files(
    client: &ShipperClient,
    posts: Vec<PendingPresencePost>,
) -> (usize, usize) {
    post_pending_presence_files_with_timeout(client, posts, PRESENCE_POST_TIMEOUT).await
}

pub fn collect_runtime_event_outbox(dir: &Path) -> Vec<PendingRuntimeEventPost> {
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return Vec::new(),
    };

    let now = SystemTime::now();
    let mut posts = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let file_name = match path.file_name().and_then(|n| n.to_str()) {
            Some(n) => n.to_owned(),
            None => continue,
        };
        if !file_name.ends_with(".json") || file_name.starts_with('.') {
            prune_stale_dot_file(&entry, &path, now);
            continue;
        }

        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let event: Value = match serde_json::from_slice::<Value>(&bytes) {
            Ok(value) if value.is_object() => value,
            Ok(_) | Err(_) => {
                let _ = std::fs::remove_file(&path);
                continue;
            }
        };
        posts.push(PendingRuntimeEventPost { path, event });
    }
    posts
}

pub async fn post_pending_runtime_event_files(
    client: &ShipperClient,
    posts: Vec<PendingRuntimeEventPost>,
) -> (usize, usize) {
    let mut sent = 0usize;
    let mut kept = 0usize;
    for chunk in posts.chunks(RUNTIME_EVENT_BATCH_LIMIT) {
        let events: Vec<Value> = chunk.iter().map(|post| post.event.clone()).collect();
        let body = match serde_json::to_vec(&serde_json::json!({ "events": events })) {
            Ok(value) => value,
            Err(_) => {
                kept += chunk.len();
                continue;
            }
        };
        match client
            .post_json_with_timeout(
                "/api/agents/runtime/events/batch",
                body,
                Some(RUNTIME_EVENT_POST_TIMEOUT),
            )
            .await
        {
            Ok(_) => {
                for post in chunk {
                    let _ = std::fs::remove_file(&post.path);
                }
                sent += chunk.len();
            }
            Err(_) => {
                kept += chunk.len();
            }
        }
    }
    (sent, kept)
}

#[cfg_attr(not(test), allow(dead_code))]
pub async fn drain_runtime_event_outbox(dir: &Path, client: &ShipperClient) -> (usize, usize) {
    let posts = collect_runtime_event_outbox(dir);
    post_pending_runtime_event_files(client, posts).await
}

async fn post_pending_presence_files_with_timeout(
    client: &ShipperClient,
    posts: Vec<PendingPresencePost>,
    request_timeout: Duration,
) -> (usize, usize) {
    let mut sent = 0usize;
    let mut kept = 0usize;
    for post in posts {
        match client
            .post_json_with_timeout("/api/agents/presence", post.bytes, Some(request_timeout))
            .await
        {
            Ok(_) => {
                let _ = std::fs::remove_file(&post.path);
                sent += 1;
            }
            Err(_) => {
                // Keep for retry next tick.
                kept += 1;
            }
        }
    }
    (sent, kept)
}

fn observed_at_for_payload(
    payload: &PresenceOutboxPayload,
    entry: &std::fs::DirEntry,
    now: SystemTime,
) -> DateTime<Utc> {
    if let Some(parsed) = payload.occurred_at.as_deref().and_then(parse_rfc3339_utc) {
        return parsed;
    }

    if let Ok(metadata) = entry.metadata() {
        if let Ok(modified) = metadata.modified() {
            return DateTime::<Utc>::from(modified);
        }
    }

    DateTime::<Utc>::from(now)
}

fn prune_stale_dot_file(entry: &std::fs::DirEntry, path: &Path, now: SystemTime) {
    if let Ok(meta) = entry.metadata() {
        if let Ok(modified) = meta.modified() {
            if let Ok(age) = now.duration_since(modified) {
                if age > Duration::from_secs(STALE_SECS) {
                    let _ = std::fs::remove_file(path);
                }
            }
        }
    }
}

fn parse_rfc3339_utc(raw: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(raw)
        .ok()
        .map(|value| value.with_timezone(&Utc))
}

fn normalize_provider(provider: Option<&str>) -> &str {
    provider
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("claude")
}

fn normalize_transcript_path(path: Option<&str>) -> Option<PathBuf> {
    path.map(str::trim)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

fn unmanaged_binding_signal_for_payload(
    payload: &PresenceOutboxPayload,
    provider: &str,
    session_id: &str,
    observed_at: DateTime<Utc>,
) -> Option<UnmanagedProcessBindingSignal> {
    if payload.control_path.as_deref().map(str::trim) != Some("unmanaged") {
        return None;
    }
    let pid = payload.provider_pid?;
    let process = crate::unmanaged_bindings::process_info_for_pid(pid, provider)?;
    let source_path = normalize_transcript_path(payload.transcript_path.as_deref());

    Some(UnmanagedProcessBindingSignal {
        provider: provider.to_string(),
        provider_session_id: session_id.to_string(),
        source_path,
        pid,
        process_start_time: process.start_time,
        process_start_time_key: process.start_time_key,
        cwd: payload.cwd.clone(),
        observed_at,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn make_outbox() -> TempDir {
        tempfile::tempdir().expect("tempdir")
    }

    #[test]
    fn enqueue_runtime_event_is_atomically_visible_to_the_shared_drain() {
        let dir = make_outbox();
        let event = serde_json::json!({"session_id":"cursor-session","kind":"progress_signal"});

        enqueue_runtime_event(dir.path(), &event).unwrap();

        let posts = collect_runtime_event_outbox(dir.path());
        assert_eq!(posts.len(), 1);
        assert_eq!(posts[0].event, event);
        assert!(fs::read_dir(dir.path()).unwrap().all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with('.')));
    }

    fn write_presence(dir: &Path, name: &str, session_id: &str, state: &str) -> PathBuf {
        let path = dir.join(name);
        let json = serde_json::json!({
            "session_id": session_id,
            "state": state,
            "tool_name": "",
            "cwd": "/tmp",
            "transcript_path": "/tmp/transcript.jsonl"
        });
        fs::write(&path, serde_json::to_vec(&json).unwrap()).unwrap();
        path
    }

    fn write_runtime_event(dir: &Path, name: &str, session_id: &str) -> PathBuf {
        let path = dir.join(name);
        let json = serde_json::json!({
            "runtime_key": format!("claude:{}", session_id),
            "session_id": session_id,
            "provider": "claude",
            "device_id": "work-laptop",
            "source": "claude_channel_wrapper",
            "kind": "terminal_signal",
            "occurred_at": "2026-05-20T21:06:20Z",
            "dedupe_key": format!("claude-terminal:{}:0:2026-05-20T21:06:20Z", session_id),
            "payload": {
                "terminal_state": "session_ended",
                "terminal_reason": "provider_exit",
                "terminal_source": "claude_channel_wrapper",
                "provider_session_id": session_id,
                "exit_code": 0
            }
        });
        fs::write(&path, serde_json::to_vec(&json).unwrap()).unwrap();
        path
    }

    // ShipperClient can't be easily constructed without a real config, so
    // for unit tests we verify file-level behavior only (no HTTP).
    // HTTP behavior (delete-on-success, keep-on-failure) is validated in E2E.

    fn filter_ready(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
        fs::read_dir(dir)
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                name.ends_with(".json") && !name.starts_with('.')
            })
            .collect()
    }

    #[test]
    fn test_skips_tmp_files() {
        let dir = make_outbox();
        // In-progress atomic write — starts with '.'
        let tmp = dir.path().join(".tmp.ABC123");
        fs::write(&tmp, b"{}").unwrap();
        // Also the old-style final name that was the bug: .tmp.ABC123.json
        let old_bad = dir.path().join(".tmp.ABC123.json");
        fs::write(&old_bad, b"{}").unwrap();
        // Correct final name from hook: prs.ABC123.json (no leading dot)
        write_presence(dir.path(), "prs.ABC123.json", "sess-1", "thinking");

        let ready = filter_ready(dir.path());
        assert_eq!(
            ready.len(),
            1,
            "only prs.*.json should be ready, not .tmp.* files"
        );
        assert_eq!(
            ready[0].file_name().unwrap().to_str().unwrap(),
            "prs.ABC123.json"
        );
    }

    #[test]
    fn test_hook_filename_pattern_is_picked_up() {
        // Verify the exact rename pattern the hook uses:
        //   mv "$TMPFILE" "${TMPFILE/\/.tmp\./\/prs.}.json"
        // which turns .tmp.XXXXXX → prs.XXXXXX.json
        let dir = make_outbox();
        let tmp_name = ".tmp.Zakvof";
        // Simulate the bash rename: replace /.tmp. with /prs. then append .json
        let final_name = tmp_name.replace(".tmp.", "prs.").to_owned() + ".json";
        assert_eq!(final_name, "prs.Zakvof.json");
        assert!(
            !final_name.starts_with('.'),
            "final name must not start with dot"
        );

        write_presence(dir.path(), &final_name, "sess-hook", "idle");
        let ready = filter_ready(dir.path());
        assert_eq!(
            ready.len(),
            1,
            "hook-produced filename must be picked up by drain"
        );
    }

    #[test]
    fn test_skips_nonexistent_dir() {
        // drain_outbox returns (0,0) when dir doesn't exist — no panic.
        let entries = std::fs::read_dir("/nonexistent/outbox/path/xyz");
        assert!(entries.is_err(), "nonexistent dir returns error");
        // drain_outbox handles this gracefully (returns (0,0))
    }

    // -----------------------------------------------------------------------
    // Integration tests — use a real inline HTTP server via tokio
    // -----------------------------------------------------------------------

    /// Write a presence file using the EXACT atomic rename pattern the hook uses:
    ///   mktemp .tmp.XXXXXX  →  mv to prs.XXXXXX.json
    /// This is the producer-consumer contract. If the naming convention drifts
    /// on either side, this test catches it.
    fn write_hook_style(
        dir: &std::path::Path,
        suffix: &str,
        session_id: &str,
        state: &str,
    ) -> std::path::PathBuf {
        let tmp = dir.join(format!(".tmp.{}", suffix));
        let final_path = dir.join(format!("prs.{}.json", suffix));
        let json = serde_json::json!({
            "session_id": session_id,
            "state": state,
            "tool_name": "",
            "cwd": "/tmp",
            "transcript_path": "/tmp/transcript.jsonl"
        });
        fs::write(&tmp, serde_json::to_vec(&json).unwrap()).unwrap();
        fs::rename(&tmp, &final_path).unwrap();
        final_path
    }

    /// Spawn a minimal HTTP server that returns `status` for every request.
    /// Returns the bound address, shared request log, and a task handle (call .abort() when done).
    /// Uses Arc<Mutex> for paths so the test can inspect them without awaiting the server task.
    async fn spawn_http_server(
        status: u16,
    ) -> (
        std::net::SocketAddr,
        std::sync::Arc<std::sync::Mutex<Vec<String>>>,
        tokio::task::JoinHandle<()>,
    ) {
        use std::sync::{Arc, Mutex};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let paths: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let paths_clone = paths.clone();

        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };

                // Read until end of HTTP headers
                let mut buf = vec![0u8; 4096];
                let mut total = 0;
                loop {
                    let n = socket.read(&mut buf[total..]).await.unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    total += n;
                    if buf[..total].windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }

                // Extract path from request line
                let head = String::from_utf8_lossy(&buf[..total]).into_owned();
                let path = head
                    .lines()
                    .next()
                    .and_then(|l| l.split_whitespace().nth(1))
                    .unwrap_or("/")
                    .to_string();
                paths_clone.lock().unwrap().push(path);

                // Drain body so reqwest doesn't hang waiting for it to be consumed
                let content_len = head
                    .lines()
                    .find(|l| l.to_ascii_lowercase().starts_with("content-length:"))
                    .and_then(|l| l.split(':').nth(1))
                    .and_then(|v| v.trim().parse::<usize>().ok())
                    .unwrap_or(0);
                let header_end = buf[..total]
                    .windows(4)
                    .position(|w| w == b"\r\n\r\n")
                    .unwrap()
                    + 4;
                let mut body_read = total - header_end;
                while body_read < content_len {
                    let n = socket.read(&mut buf).await.unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    body_read += n;
                }

                let resp = format!(
                    "HTTP/1.1 {}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                    status
                );
                let _ = socket.write_all(resp.as_bytes()).await;
                let _ = socket.shutdown().await;
            }
        });

        (addr, paths, handle)
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_success_deletes_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Write one file using the exact hook rename pattern
        let f = write_hook_style(dir.path(), "OK1234", "sess-ok", "thinking");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 1, "one event should be sent");
        assert_eq!(kept, 0);
        assert!(!f.exists(), "file must be deleted after successful POST");

        // Verify the server received exactly 1 POST to the presence endpoint
        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 1);
        assert_eq!(logged[0], "/api/agents/presence");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_runtime_event_outbox_success_deletes_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();
        let f = write_runtime_event(dir.path(), "rte.ok.json", "sess-runtime-ok");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_runtime_event_outbox(dir.path(), &client).await;

        assert_eq!(sent, 1);
        assert_eq!(kept, 0);
        assert!(
            !f.exists(),
            "runtime event file must be deleted after successful POST"
        );

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 1);
        assert_eq!(logged[0], "/api/agents/runtime/events/batch");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_runtime_event_outbox_failure_keeps_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, _paths, server) = spawn_http_server(503).await;
        let dir = tempfile::tempdir().unwrap();
        let f = write_runtime_event(dir.path(), "rte.retry.json", "sess-runtime-retry");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_runtime_event_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0);
        assert_eq!(kept, 1);
        assert!(f.exists(), "runtime event file must remain for retry");

        server.abort();
    }

    #[test]
    fn test_collect_runtime_event_outbox_deletes_malformed_files() {
        let dir = tempfile::tempdir().unwrap();
        let bad = dir.path().join("rte.bad.json");
        let scalar = dir.path().join("rte.scalar.json");
        fs::write(&bad, b"not valid json").unwrap();
        fs::write(&scalar, b"[]").unwrap();

        let posts = collect_runtime_event_outbox(dir.path());

        assert!(posts.is_empty());
        assert!(
            !bad.exists(),
            "malformed runtime event file must be deleted"
        );
        assert!(
            !scalar.exists(),
            "non-object runtime event file must be deleted"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_result_returns_sent_phase_signal() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, _paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();

        write_hook_style(dir.path(), "SIG123", "sess-signal", "needs_user");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(
            Some(&url),
            None,
            Some(db.path()),
            None,
            None,
            None,
        );
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let result =
            drain_outbox_with_local_state_result(dir.path(), &client, Some(db.path())).await;

        assert_eq!(result.sent, 1);
        assert_eq!(result.kept, 0);
        assert_eq!(result.signals.len(), 1);
        assert_eq!(result.signals[0].session_id, "sess-signal");
        assert_eq!(result.signals[0].provider, "claude");
        assert_eq!(result.signals[0].phase, "needs_user");
        assert_eq!(
            result.signals[0].transcript_path,
            Some(PathBuf::from("/tmp/transcript.jsonl"))
        );

        server.abort();
    }

    #[test]
    fn test_collect_outbox_result_returns_signal_before_post() {
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();

        let f = write_hook_style(dir.path(), "FAST123", "sess-fast", "thinking");

        let result = collect_outbox_with_local_state_result(dir.path(), Some(db.path()));

        assert_eq!(result.signals.len(), 1);
        assert_eq!(result.signals[0].session_id, "sess-fast");
        assert_eq!(result.signals[0].phase, "thinking");
        assert_eq!(result.posts.len(), 1);
        assert!(
            f.exists(),
            "presence file should remain until the POST/delete phase completes"
        );
    }

    #[test]
    fn test_empty_outbox_does_not_open_local_phase_db() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("engine.db");

        let result = collect_outbox_with_local_state_result(dir.path(), Some(&db_path));

        assert!(result.signals.is_empty());
        assert!(result.posts.is_empty());
        assert!(
            !db_path.exists(),
            "empty outbox ticks should not touch SQLite on the daemon hot loop"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_network_error_keeps_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        use tokio::net::TcpListener;

        // Server that accepts then immediately drops the socket — reqwest gets
        // a connection-closed error without a response, which makes post_json return Err.
        // This is faster and more reliable than pointing at a closed port (avoids timeout).
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            while let Ok((socket, _)) = listener.accept().await {
                drop(socket); // close immediately, no response written
            }
        });

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let dir = tempfile::tempdir().unwrap();
        let f = write_hook_style(dir.path(), "ERR123", "sess-err", "running");

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0);
        assert_eq!(kept, 1, "file must be kept when POST fails");
        assert!(f.exists(), "file must not be deleted on network error");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_presence_post_timeout_keeps_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;
        use tokio::io::AsyncReadExt;
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            while let Ok((mut socket, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let mut buf = [0u8; 512];
                    let _ = socket.read(&mut buf).await;
                    tokio::time::sleep(Duration::from_secs(2)).await;
                });
            }
        });

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let dir = tempfile::tempdir().unwrap();
        let f = write_hook_style(dir.path(), "SLOW123", "sess-slow", "thinking");
        let posts = collect_outbox_with_local_state_result(dir.path(), None).posts;

        let (sent, kept) =
            post_pending_presence_files_with_timeout(&client, posts, Duration::from_millis(50))
                .await;

        assert_eq!(sent, 0);
        assert_eq!(kept, 1, "slow presence POST should be retried later");
        assert!(f.exists(), "file must remain after a presence POST timeout");

        server.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_result_returns_phase_signal_when_post_fails() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, _paths, server) = spawn_http_server(503).await;
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();

        let f = write_hook_style(dir.path(), "LOCAL123", "sess-local", "thinking");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(
            Some(&url),
            None,
            Some(db.path()),
            None,
            None,
            None,
        );
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let result =
            drain_outbox_with_local_state_result(dir.path(), &client, Some(db.path())).await;

        assert_eq!(result.sent, 0);
        assert_eq!(result.kept, 1);
        assert_eq!(result.signals.len(), 1);
        assert_eq!(result.signals[0].session_id, "sess-local");
        assert_eq!(result.signals[0].phase, "thinking");
        assert!(
            f.exists(),
            "file must still be retried for presence delivery"
        );

        server.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_dot_files_never_posted() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        // Server that accepts exactly 1 request (the real prs.* file)
        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Dot-prefixed files — must be skipped by drain
        let tmp_in_progress = dir.path().join(".tmp.ABC123");
        let old_bad_pattern = dir.path().join(".tmp.ABC123.json"); // the bug we fixed
        fs::write(&tmp_in_progress, b"{}").unwrap();
        fs::write(
            &old_bad_pattern,
            serde_json::to_vec(&serde_json::json!({
                "session_id": "sess-dot", "state": "thinking"
            }))
            .unwrap(),
        )
        .unwrap();

        // One real file using correct hook pattern
        let real = write_hook_style(dir.path(), "REAL01", "sess-real", "idle");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 1, "only the real file should be sent");
        assert_eq!(kept, 0);
        assert!(!real.exists(), "real file deleted");
        assert!(tmp_in_progress.exists(), ".tmp file must not be touched");
        assert!(
            old_bad_pattern.exists(),
            "old .tmp.*.json pattern must be skipped (the bug we fixed)"
        );

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(
            logged.len(),
            1,
            "only 1 POST — dot files must not be POSTed"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_coalesces_same_session() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Three files for the same session — only latest should be POSTed
        write_hook_style(dir.path(), "S1A", "sess-multi", "thinking");
        std::thread::sleep(Duration::from_millis(10));
        write_hook_style(dir.path(), "S1B", "sess-multi", "running");
        std::thread::sleep(Duration::from_millis(10));
        let latest = write_hook_style(dir.path(), "S1C", "sess-multi", "idle");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        let older_a = dir.path().join("prs.S1A.json");
        let older_b = dir.path().join("prs.S1B.json");

        assert_eq!(sent, 1, "3 files for same session → 1 POST");
        assert_eq!(kept, 0);
        assert!(!latest.exists(), "latest file deleted after send");
        assert!(
            !older_a.exists(),
            "older file S1A deleted during coalescing"
        );
        assert!(
            !older_b.exists(),
            "older file S1B deleted during coalescing"
        );

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 1, "only 1 POST despite 3 files");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_deletes_invalid_json() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        // Server that should NOT be called — malformed files get deleted, not POSTed.
        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Write a malformed JSON file using the prs.* naming (would be picked up)
        let bad = dir.path().join("prs.bad.json");
        fs::write(&bad, b"not valid json!!!").unwrap();

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0, "malformed file must not be POSTed");
        assert_eq!(kept, 0, "malformed file must not be kept for retry");
        assert!(!bad.exists(), "malformed file must be deleted");

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 0, "no POSTs for malformed file");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_deletes_stale_tmp_files() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        // Server that must NOT be called — stale dot-files get deleted, never POSTed.
        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Two stale dot-file variants the hook can produce:
        //   1. .tmp.XXXXXX          — orphaned before mv (killed mid-write)
        //   2. .tmp.XXXXXX.json     — old buggy hook pattern
        let stale_no_ext = dir.path().join(".tmp.STALE1");
        let stale_with_ext = dir.path().join(".tmp.STALE2.json");
        fs::write(&stale_no_ext, b"{}").unwrap();
        fs::write(&stale_with_ext, b"{}").unwrap();

        // Backdate both past STALE_SECS (600s) using touch(1).
        // Using a timestamp clearly in the past (1970-01-02 = +86400s epoch).
        for path in [&stale_no_ext, &stale_with_ext] {
            std::process::Command::new("touch")
                .args(["-t", "197001020000", path.to_str().unwrap()])
                .status()
                .expect("touch failed");
        }

        // One fresh in-progress temp — must NOT be touched (age ~0).
        let fresh_tmp = dir.path().join(".tmp.FRESH1");
        fs::write(&fresh_tmp, b"{}").unwrap();

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0, "stale dot-files must not be POSTed");
        assert_eq!(kept, 0, "stale dot-files must not be kept for retry");
        assert!(!stale_no_ext.exists(), ".tmp.STALE1 must be deleted");
        assert!(!stale_with_ext.exists(), ".tmp.STALE2.json must be deleted");
        assert!(
            fresh_tmp.exists(),
            "fresh in-progress .tmp must be left alone"
        );

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 0, "no POSTs — stale dot-files never sent");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_with_local_state_persists_latest_phase() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, _paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();

        write_hook_style(dir.path(), "PHASE1", "sess-phase", "thinking");
        std::thread::sleep(Duration::from_millis(10));
        write_hook_style(dir.path(), "PHASE2", "sess-phase", "running");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(
            Some(&url),
            None,
            Some(db.path()),
            None,
            None,
            None,
        );
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) =
            drain_outbox_with_local_state(dir.path(), &client, Some(db.path())).await;

        assert_eq!(sent, 1);
        assert_eq!(kept, 0);

        let conn = crate::state::db::open_db(Some(db.path())).unwrap();
        let row: (String, Option<String>, String) = conn
            .query_row(
                "SELECT phase, tool_name, source
                 FROM session_phase_state
                 WHERE session_id = 'sess-phase'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(row.0, "running");
        assert!(row.1.is_none());
        assert_eq!(row.2, "claude_hook");

        assert!(conn.prepare("SELECT 1 FROM managed_session_state").is_err());

        server.abort();
    }
}
