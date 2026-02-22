//! Outbox drain for presence events.
//!
//! Claude Code hooks write small JSON files to `~/.claude/outbox/` instead of
//! calling the API directly. This eliminates network I/O from the hook hot path,
//! allowing hooks to run as `async: false` without risking stalls.
//!
//! This module drains the outbox on a 1-second tick: reads all ready files,
//! coalesces by session_id (latest state wins), POSTs to `/api/agents/presence`,
//! and deletes files on success. Files are kept on failure and retried next tick.
//! Files older than `STALE_SECS` are deleted without posting (presence is ephemeral).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use crate::shipping::client::ShipperClient;

/// Maximum age for an outbox file before it is considered stale and deleted.
const STALE_SECS: u64 = 600; // 10 minutes

/// Drain all ready presence events from the outbox directory.
///
/// Returns `(sent, kept)`:
/// - `sent`: number of events successfully POSTed (files deleted)
/// - `kept`: number of files kept for retry (POST failed)
pub async fn drain_outbox(dir: &Path, client: &ShipperClient) -> (usize, usize) {
    // Nothing to do if outbox doesn't exist yet.
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return (0, 0),
    };

    let now = SystemTime::now();
    // session_id → (path, bytes) — latest file per session wins
    let mut by_session: HashMap<String, (PathBuf, Vec<u8>)> = HashMap::new();

    for entry in entries.flatten() {
        let path = entry.path();

        // Skip non-JSON and in-progress tmp files (start with '.')
        let file_name = match path.file_name().and_then(|n| n.to_str()) {
            Some(n) => n.to_owned(),
            None => continue,
        };
        if !file_name.ends_with(".json") || file_name.starts_with('.') {
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
        let val: serde_json::Value = match serde_json::from_slice(&bytes) {
            Ok(v) => v,
            Err(_) => {
                // Malformed JSON — delete to avoid indefinite retry.
                let _ = std::fs::remove_file(&path);
                continue;
            }
        };

        // Must have a non-empty session_id.
        let sid = match val.get("session_id").and_then(|v| v.as_str()) {
            Some(s) if !s.is_empty() => s.to_owned(),
            _ => {
                let _ = std::fs::remove_file(&path);
                continue;
            }
        };

        // Coalesce: keep the file with the latest mtime for each session.
        // If we can't get mtime, the last file iterated wins (good enough).
        let new_mtime = entry
            .metadata()
            .ok()
            .and_then(|m| m.modified().ok())
            .unwrap_or(SystemTime::UNIX_EPOCH);

        match by_session.get(&sid) {
            Some((existing_path, _)) => {
                let existing_mtime = existing_path
                    .metadata()
                    .ok()
                    .and_then(|m| m.modified().ok())
                    .unwrap_or(SystemTime::UNIX_EPOCH);
                if new_mtime > existing_mtime {
                    // Delete the older file; replace with newer.
                    let _ = std::fs::remove_file(existing_path);
                    by_session.insert(sid, (path, bytes));
                } else {
                    // New file is older; delete it.
                    let _ = std::fs::remove_file(&path);
                }
            }
            None => {
                by_session.insert(sid, (path, bytes));
            }
        }
    }

    // POST coalesced events.
    let mut sent = 0usize;
    let mut kept = 0usize;

    for (_sid, (path, bytes)) in by_session {
        match client.post_json("/api/agents/presence", bytes).await {
            Ok(_) => {
                let _ = std::fs::remove_file(&path);
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn make_outbox() -> TempDir {
        tempfile::tempdir().expect("tempdir")
    }

    fn write_presence(dir: &Path, name: &str, session_id: &str, state: &str) -> PathBuf {
        let path = dir.join(name);
        let json = serde_json::json!({
            "session_id": session_id,
            "state": state,
            "tool_name": "",
            "cwd": "/tmp"
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
        assert_eq!(ready.len(), 1, "only prs.*.json should be ready, not .tmp.* files");
        assert_eq!(ready[0].file_name().unwrap().to_str().unwrap(), "prs.ABC123.json");
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
        assert!(!final_name.starts_with('.'), "final name must not start with dot");

        write_presence(dir.path(), &final_name, "sess-hook", "idle");
        let ready = filter_ready(dir.path());
        assert_eq!(ready.len(), 1, "hook-produced filename must be picked up by drain");
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
    fn write_hook_style(dir: &std::path::Path, suffix: &str, session_id: &str, state: &str) -> std::path::PathBuf {
        let tmp = dir.join(format!(".tmp.{}", suffix));
        let final_path = dir.join(format!("prs.{}.json", suffix));
        let json = serde_json::json!({
            "session_id": session_id,
            "state": state,
            "tool_name": "",
            "cwd": "/tmp"
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
    ) -> (std::net::SocketAddr, std::sync::Arc<std::sync::Mutex<Vec<String>>>, tokio::task::JoinHandle<()>) {
        use std::sync::{Arc, Mutex};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let paths: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let paths_clone = paths.clone();

        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else { break };

                // Read until end of HTTP headers
                let mut buf = vec![0u8; 4096];
                let mut total = 0;
                loop {
                    let n = socket.read(&mut buf[total..]).await.unwrap_or(0);
                    if n == 0 { break; }
                    total += n;
                    if buf[..total].windows(4).any(|w| w == b"\r\n\r\n") { break; }
                }

                // Extract path from request line
                let head = String::from_utf8_lossy(&buf[..total]).into_owned();
                let path = head.lines().next()
                    .and_then(|l| l.split_whitespace().nth(1))
                    .unwrap_or("/")
                    .to_string();
                paths_clone.lock().unwrap().push(path);

                // Drain body so reqwest doesn't hang waiting for it to be consumed
                let content_len = head.lines()
                    .find(|l| l.to_ascii_lowercase().starts_with("content-length:"))
                    .and_then(|l| l.split(':').nth(1))
                    .and_then(|v| v.trim().parse::<usize>().ok())
                    .unwrap_or(0);
                let header_end = buf[..total].windows(4).position(|w| w == b"\r\n\r\n").unwrap() + 4;
                let mut body_read = total - header_end;
                while body_read < content_len {
                    let n = socket.read(&mut buf).await.unwrap_or(0);
                    if n == 0 { break; }
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
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None);
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
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let dir = tempfile::tempdir().unwrap();
        let f = write_hook_style(dir.path(), "ERR123", "sess-err", "running");

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0);
        assert_eq!(kept, 1, "file must be kept when POST fails");
        assert!(f.exists(), "file must not be deleted on network error");
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
        fs::write(&old_bad_pattern, serde_json::to_vec(&serde_json::json!({
            "session_id": "sess-dot", "state": "thinking"
        })).unwrap()).unwrap();

        // One real file using correct hook pattern
        let real = write_hook_style(dir.path(), "REAL01", "sess-real", "idle");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 1, "only the real file should be sent");
        assert_eq!(kept, 0);
        assert!(!real.exists(), "real file deleted");
        assert!(tmp_in_progress.exists(), ".tmp file must not be touched");
        assert!(old_bad_pattern.exists(), "old .tmp.*.json pattern must be skipped (the bug we fixed)");

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 1, "only 1 POST — dot files must not be POSTed");
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
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        let older_a = dir.path().join("prs.S1A.json");
        let older_b = dir.path().join("prs.S1B.json");

        assert_eq!(sent, 1, "3 files for same session → 1 POST");
        assert_eq!(kept, 0);
        assert!(!latest.exists(), "latest file deleted after send");
        assert!(!older_a.exists(), "older file S1A deleted during coalescing");
        assert!(!older_b.exists(), "older file S1B deleted during coalescing");

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
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0, "malformed file must not be POSTed");
        assert_eq!(kept, 0, "malformed file must not be kept for retry");
        assert!(!bad.exists(), "malformed file must be deleted");

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 0, "no POSTs for malformed file");
    }
}
