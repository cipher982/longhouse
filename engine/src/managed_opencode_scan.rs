//! Managed-local OpenCode server-bridge scanner.
//!
//! `longhouse opencode` starts stock `opencode serve` and writes a private
//! state file under the provider config home. The Machine Agent must include
//! that bridge in its complete managed-session heartbeat; otherwise the Runtime
//! Host correctly interprets the missing session as detached.

use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::path::PathBuf;
use std::time::Duration;

use base64::{engine::general_purpose, Engine as _};
use reqwest::Url;
use serde::Deserialize;
use serde_json::Value;

const HEALTH_CHECK_TIMEOUT: Duration = Duration::from_millis(750);
const DEFAULT_USERNAME: &str = "opencode";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenCodeServerObservation {
    pub session_id: String,
    pub provider_session_id: String,
    pub state_file: PathBuf,
    pub cwd: Option<String>,
    pub server_url: Option<String>,
    pub pid: Option<u32>,
    pub started_at: String,
    pub updated_at: String,
    pub server_alive: bool,
}

#[derive(Debug, Deserialize)]
struct OpenCodeServerStateFile {
    session_id: Option<String>,
    provider_session_id: Option<String>,
    server_url: Option<String>,
    pid: Option<u32>,
    cwd: Option<String>,
    username: Option<String>,
    password: Option<String>,
    started_at: Option<String>,
    updated_at: Option<String>,
}

pub fn collect_observations() -> Vec<OpenCodeServerObservation> {
    let Some(state_dir) = default_opencode_server_state_dir() else {
        return Vec::new();
    };
    collect_observations_from(&state_dir)
}

pub fn default_opencode_server_state_dir() -> Option<PathBuf> {
    let provider_home = std::env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".claude")))?;
    Some(provider_home.join("managed-local").join("opencode-server"))
}

pub fn collect_observations_from(state_dir: &Path) -> Vec<OpenCodeServerObservation> {
    let mut out = Vec::new();
    let Ok(entries) = fs::read_dir(state_dir) else {
        return out;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(state) = serde_json::from_slice::<OpenCodeServerStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.unwrap_or_default().trim().to_string();
        let provider_session_id = state
            .provider_session_id
            .unwrap_or_default()
            .trim()
            .to_string();
        if session_id.is_empty() || provider_session_id.is_empty() {
            continue;
        }
        let pid_alive = state
            .pid
            .and_then(|pid| i32::try_from(pid).ok())
            .map(crate::managed_bridge_scan::pid_alive)
            .unwrap_or(false);
        let server_url = state.server_url.filter(|value| !value.trim().is_empty());
        let server_alive = pid_alive
            && opencode_health_ready(
                server_url.as_deref(),
                state.username.as_deref(),
                state.password.as_deref(),
            );

        out.push(OpenCodeServerObservation {
            session_id,
            provider_session_id,
            state_file: path,
            cwd: state.cwd.filter(|value| !value.trim().is_empty()),
            server_url,
            pid: state.pid,
            started_at: state.started_at.unwrap_or_default(),
            updated_at: state.updated_at.unwrap_or_default(),
            server_alive,
        });
    }
    out.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    out
}

fn opencode_health_ready(
    server_url: Option<&str>,
    username: Option<&str>,
    password: Option<&str>,
) -> bool {
    let Some(server_url) = server_url.map(str::trim).filter(|value| !value.is_empty()) else {
        return false;
    };
    let Some(password) = password.map(str::trim).filter(|value| !value.is_empty()) else {
        return false;
    };
    let username = username
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or(DEFAULT_USERNAME);
    let Ok(mut url) = Url::parse(server_url) else {
        return false;
    };
    if url.scheme() != "http" || !is_localhost_url(&url) {
        return false;
    }
    url.set_path("/global/health");
    url.set_query(None);
    url.set_fragment(None);

    let Some(addr) = socket_addr_for_url(&url) else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, HEALTH_CHECK_TIMEOUT) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(HEALTH_CHECK_TIMEOUT));
    let _ = stream.set_write_timeout(Some(HEALTH_CHECK_TIMEOUT));

    let host = match url.port() {
        Some(port) => format!("{}:{port}", url.host_str().unwrap_or("127.0.0.1")),
        None => url.host_str().unwrap_or("127.0.0.1").to_string(),
    };
    let auth = general_purpose::STANDARD.encode(format!("{username}:{password}"));
    let request = format!(
        "GET /global/health HTTP/1.1\r\nHost: {host}\r\nAccept: application/json\r\nAuthorization: Basic {auth}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }

    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    let Some((head, body)) = response.split_once("\r\n\r\n") else {
        return false;
    };
    if !head
        .lines()
        .next()
        .is_some_and(|line| line.contains(" 200 "))
    {
        return false;
    }
    serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|payload| payload.get("healthy").and_then(Value::as_bool))
        == Some(true)
}

fn is_localhost_url(url: &Url) -> bool {
    matches!(
        url.host_str(),
        Some("127.0.0.1") | Some("localhost") | Some("::1") | Some("[::1]")
    )
}

fn socket_addr_for_url(url: &Url) -> Option<SocketAddr> {
    let port = url.port_or_known_default()?;
    match url.host_str()? {
        "127.0.0.1" | "localhost" => Some(SocketAddr::from(([127, 0, 0, 1], port))),
        "::1" | "[::1]" => Some(SocketAddr::from(([0, 0, 0, 0, 0, 0, 0, 1], port))),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::sync::mpsc;
    use std::thread;

    #[test]
    fn default_state_dir_uses_provider_home() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().join("home");
        let claude_home = temp.path().join("claude-config");
        temp_env::with_vars(
            [
                ("HOME", Some(home.display().to_string())),
                ("CLAUDE_CONFIG_DIR", Some(claude_home.display().to_string())),
            ],
            || {
                assert_eq!(
                    default_opencode_server_state_dir().unwrap(),
                    claude_home.join("managed-local").join("opencode-server")
                );
            },
        );
    }

    #[test]
    fn scan_redacts_secret_state_to_public_observation() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            serde_json::json!({
                "schema_version": 1,
                "session_id": "longhouse-session",
                "provider_session_id": "opencode-session",
                "server_url": "http://127.0.0.1:12345",
                "pid": 999999,
                "cwd": "/Users/test/repo",
                "username": "opencode",
                "password": "secret",
                "started_at": "2026-06-17T10:00:00Z",
                "updated_at": "2026-06-17T10:00:01Z"
            })
            .to_string(),
        )
        .unwrap();

        let obs = collect_observations_from(tmp.path());

        assert_eq!(obs.len(), 1);
        assert_eq!(obs[0].session_id, "longhouse-session");
        assert_eq!(obs[0].provider_session_id, "opencode-session");
        assert_eq!(obs[0].cwd.as_deref(), Some("/Users/test/repo"));
        assert_eq!(obs[0].server_url.as_deref(), Some("http://127.0.0.1:12345"));
        assert!(!obs[0].server_alive);
    }

    #[test]
    fn scan_requires_authenticated_health_check_before_alive() {
        let tmp = tempfile::tempdir().unwrap();
        let (server_url, request_rx, handle) = spawn_health_server();
        fs::write(
            tmp.path().join("session.json"),
            serde_json::json!({
                "schema_version": 1,
                "session_id": "longhouse-session",
                "provider_session_id": "opencode-session",
                "server_url": server_url,
                "pid": std::process::id(),
                "cwd": "/Users/test/repo",
                "username": "opencode",
                "password": "secret",
                "started_at": "2026-06-17T10:00:00Z",
                "updated_at": "2026-06-17T10:00:01Z"
            })
            .to_string(),
        )
        .unwrap();

        let obs = collect_observations_from(tmp.path());

        assert_eq!(obs.len(), 1);
        assert!(obs[0].server_alive);
        let request = request_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(request.contains("GET /global/health HTTP/1.1"));
        assert!(request.contains("Authorization: Basic b3BlbmNvZGU6c2VjcmV0"));
        handle.join().unwrap();
    }

    #[test]
    fn scan_marks_alive_pid_dead_when_health_check_fails() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            serde_json::json!({
                "schema_version": 1,
                "session_id": "longhouse-session",
                "provider_session_id": "opencode-session",
                "server_url": "http://127.0.0.1:9",
                "pid": std::process::id(),
                "cwd": "/Users/test/repo",
                "username": "opencode",
                "password": "secret",
                "started_at": "2026-06-17T10:00:00Z",
                "updated_at": "2026-06-17T10:00:01Z"
            })
            .to_string(),
        )
        .unwrap();

        let obs = collect_observations_from(tmp.path());

        assert_eq!(obs.len(), 1);
        assert!(!obs[0].server_alive);
    }

    fn spawn_health_server() -> (String, mpsc::Receiver<String>, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = mpsc::channel();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buffer = [0_u8; 4096];
            let size = stream.read(&mut buffer).unwrap();
            let request = String::from_utf8_lossy(&buffer[..size]).to_string();
            tx.send(request).unwrap();
            let body = r#"{"healthy":true}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        (format!("http://{addr}"), rx, handle)
    }
}
