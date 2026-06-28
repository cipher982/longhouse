//! Native control adapter for managed OpenCode server-bridge sessions.

use std::fs;
use std::path::Path;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use reqwest::{Client, Method, Url};
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

const DEFAULT_USERNAME: &str = "opencode";
const MAX_READABLE_STATE_SCHEMA_VERSION: u64 = 2;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);

pub const OPENCODE_SERVER_BRIDGE_TRANSPORT: &str = "opencode_server_bridge";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenCodeControlResult {
    pub provider_session_id: String,
}

#[derive(Debug, Clone)]
struct OpenCodeControlState {
    session_id: String,
    provider_session_id: String,
    server_url: String,
    cwd: Option<String>,
    username: String,
    password: String,
}

#[derive(Debug, Deserialize)]
struct OpenCodeServerStateFile {
    schema_version: Option<u64>,
    session_id: Option<String>,
    provider_session_id: Option<String>,
    server_url: Option<String>,
    cwd: Option<String>,
    username: Option<String>,
    password: Option<String>,
}

pub async fn send_text(session_id: &str, text: &str) -> Result<OpenCodeControlResult> {
    let state = read_bridge_state(session_id, None)?;
    post_prompt_async(&state, text).await?;
    Ok(OpenCodeControlResult {
        provider_session_id: state.provider_session_id,
    })
}

pub async fn interrupt(session_id: &str) -> Result<OpenCodeControlResult> {
    let state = read_bridge_state(session_id, None)?;
    post_abort(&state).await?;
    Ok(OpenCodeControlResult {
        provider_session_id: state.provider_session_id,
    })
}

fn read_bridge_state(session_id: &str, state_dir: Option<&Path>) -> Result<OpenCodeControlState> {
    let normalized_session_id = normalize_session_id(session_id)?;
    let state_dir =
        match state_dir {
            Some(path) => path.to_path_buf(),
            None => crate::managed_opencode_scan::default_opencode_server_state_dir().ok_or_else(
                || anyhow!("OpenCode server bridge state directory could not be resolved"),
            )?,
        };
    read_bridge_state_from_path(
        &normalized_session_id,
        &state_dir.join(format!("{normalized_session_id}.json")),
    )
}

fn normalize_session_id(session_id: &str) -> Result<String> {
    let trimmed = session_id.trim();
    let uuid = Uuid::parse_str(trimmed).context("session_id must be a UUID")?;
    Ok(uuid.to_string())
}

fn read_bridge_state_from_path(
    normalized_session_id: &str,
    path: &Path,
) -> Result<OpenCodeControlState> {
    let bytes = fs::read(path).with_context(|| {
        format!("OpenCode server bridge state not found for {normalized_session_id}")
    })?;
    let payload: OpenCodeServerStateFile = serde_json::from_slice(&bytes).with_context(|| {
        format!(
            "OpenCode server bridge state is not valid JSON: {}",
            path.display()
        )
    })?;
    let schema_version = payload.schema_version.unwrap_or(0);
    if schema_version > MAX_READABLE_STATE_SCHEMA_VERSION {
        bail!("OpenCode server bridge state schema {schema_version} is newer than this Longhouse build");
    }

    let state_session_id = payload.session_id.unwrap_or_default().trim().to_string();
    if state_session_id != normalized_session_id {
        bail!("OpenCode server bridge state session_id mismatch");
    }
    let provider_session_id = payload
        .provider_session_id
        .unwrap_or_default()
        .trim()
        .to_string();
    let server_url = payload.server_url.unwrap_or_default().trim().to_string();
    let password = payload.password.unwrap_or_default().trim().to_string();
    if provider_session_id.is_empty() || server_url.is_empty() || password.is_empty() {
        bail!("OpenCode server bridge state is incomplete");
    }
    let username = payload
        .username
        .unwrap_or_else(|| DEFAULT_USERNAME.to_string())
        .trim()
        .to_string();
    let username = if username.is_empty() {
        DEFAULT_USERNAME.to_string()
    } else {
        username
    };
    let cwd = payload
        .cwd
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());

    Ok(OpenCodeControlState {
        session_id: state_session_id,
        provider_session_id,
        server_url,
        cwd,
        username,
        password,
    })
}

async fn post_prompt_async(state: &OpenCodeControlState, text: &str) -> Result<()> {
    request_opencode_json(
        state,
        Method::POST,
        "prompt_async",
        Some(json!({
            "noReply": true,
            "parts": [{"type": "text", "text": text}],
        })),
    )
    .await
}

async fn post_abort(state: &OpenCodeControlState) -> Result<()> {
    request_opencode_json(state, Method::POST, "abort", None).await
}

async fn request_opencode_json(
    state: &OpenCodeControlState,
    method: Method,
    action: &str,
    payload: Option<Value>,
) -> Result<()> {
    let url = opencode_action_url(state, action)?;
    let client = Client::builder()
        .timeout(REQUEST_TIMEOUT)
        .build()
        .context("failed to build OpenCode control HTTP client")?;
    let mut request = client
        .request(method, url)
        .basic_auth(&state.username, Some(&state.password))
        .header("Accept", "application/json");
    if let Some(payload) = payload {
        request = request.json(&payload);
    }
    let response = request.send().await.with_context(|| {
        format!(
            "OpenCode server request failed for session {}",
            state.session_id
        )
    })?;
    let status = response.status();
    let body = response
        .text()
        .await
        .context("OpenCode server response body could not be read")?;
    if !status.is_success() {
        bail!("OpenCode server request failed: HTTP {status}; body={body}");
    }
    if !body.trim().is_empty() {
        serde_json::from_str::<Value>(&body).context("OpenCode server returned invalid JSON")?;
    }
    Ok(())
}

fn opencode_action_url(state: &OpenCodeControlState, action: &str) -> Result<Url> {
    let mut url = Url::parse(state.server_url.trim())
        .with_context(|| format!("OpenCode server URL is invalid: {}", state.server_url))?;
    validate_local_server_url(&url)?;
    {
        let mut segments = url
            .path_segments_mut()
            .map_err(|_| anyhow!("OpenCode server URL cannot be used as a base URL"))?;
        segments
            .clear()
            .push("session")
            .push(&state.provider_session_id)
            .push(action);
    }
    url.set_fragment(None);
    url.set_query(None);
    if let Some(cwd) = state.cwd.as_deref() {
        url.query_pairs_mut().append_pair("directory", cwd);
    }
    Ok(url)
}

fn validate_local_server_url(url: &Url) -> Result<()> {
    if url.scheme() != "http" {
        bail!("OpenCode server URL must use http on localhost");
    }
    match url.host_str() {
        Some("127.0.0.1") | Some("localhost") | Some("::1") | Some("[::1]") => Ok(()),
        _ => bail!("OpenCode server URL must be localhost"),
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use base64::{engine::general_purpose, Engine as _};
    use tempfile::TempDir;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::oneshot;

    use super::*;

    const SESSION_ID: &str = "11111111-1111-4111-8111-111111111111";

    #[derive(Debug)]
    struct RecordedRequest {
        method: String,
        target: String,
        headers: HashMap<String, String>,
        body: String,
    }

    #[tokio::test]
    async fn send_text_posts_prompt_async_with_auth_and_directory() {
        let (server_url, request_rx) = spawn_single_request_server().await;
        let temp = TempDir::new().unwrap();
        write_state(temp.path(), &server_url, Some("/tmp/opencode work"));

        let result = send_text_from_state_dir(temp.path(), SESSION_ID, "hello opencode")
            .await
            .unwrap();
        let request = request_rx.await.unwrap();

        assert_eq!(result.provider_session_id, "ses_test123");
        assert_eq!(request.method, "POST");
        assert_eq!(
            request.target,
            "/session/ses_test123/prompt_async?directory=%2Ftmp%2Fopencode+work"
        );
        assert_eq!(
            request.headers.get("authorization").unwrap(),
            &format!(
                "Basic {}",
                general_purpose::STANDARD.encode("opencode:secret-password")
            )
        );
        assert_eq!(
            request.headers.get("content-type").unwrap(),
            "application/json"
        );
        assert_eq!(
            serde_json::from_str::<Value>(&request.body).unwrap(),
            json!({
                "noReply": true,
                "parts": [{"type": "text", "text": "hello opencode"}],
            })
        );
    }

    #[tokio::test]
    async fn interrupt_posts_abort_with_auth_and_directory() {
        let (server_url, request_rx) = spawn_single_request_server().await;
        let temp = TempDir::new().unwrap();
        write_state(temp.path(), &server_url, Some("/tmp/project"));

        let result = interrupt_from_state_dir(temp.path(), SESSION_ID)
            .await
            .unwrap();
        let request = request_rx.await.unwrap();

        assert_eq!(result.provider_session_id, "ses_test123");
        assert_eq!(request.method, "POST");
        assert_eq!(
            request.target,
            "/session/ses_test123/abort?directory=%2Ftmp%2Fproject"
        );
        assert_eq!(
            request.headers.get("authorization").unwrap(),
            &format!(
                "Basic {}",
                general_purpose::STANDARD.encode("opencode:secret-password")
            )
        );
        assert!(request.body.is_empty());
    }

    #[tokio::test]
    async fn send_text_omits_directory_query_when_cwd_is_empty() {
        let (server_url, request_rx) = spawn_single_request_server().await;
        let temp = TempDir::new().unwrap();
        write_state(temp.path(), &server_url, None);

        send_text_from_state_dir(temp.path(), SESSION_ID, "hello")
            .await
            .unwrap();
        let request = request_rx.await.unwrap();

        assert_eq!(request.target, "/session/ses_test123/prompt_async");
    }

    #[tokio::test]
    async fn send_text_defaults_missing_username_to_opencode() {
        let (server_url, request_rx) = spawn_single_request_server().await;
        let temp = TempDir::new().unwrap();
        let mut payload = base_state_payload(&server_url, Some("/tmp/project"));
        payload.as_object_mut().unwrap().remove("username");
        write_state_payload(temp.path(), SESSION_ID, payload);

        send_text_from_state_dir(temp.path(), SESSION_ID, "hello")
            .await
            .unwrap();
        let request = request_rx.await.unwrap();

        assert_eq!(
            request.headers.get("authorization").unwrap(),
            &format!(
                "Basic {}",
                general_purpose::STANDARD.encode("opencode:secret-password")
            )
        );
    }

    #[tokio::test]
    async fn send_text_encodes_provider_session_id_as_path_segment() {
        let (server_url, request_rx) = spawn_single_request_server().await;
        let temp = TempDir::new().unwrap();
        let mut payload = base_state_payload(&server_url, Some("/tmp/project"));
        payload["provider_session_id"] = Value::String("ses/test 123".to_string());
        write_state_payload(temp.path(), SESSION_ID, payload);

        send_text_from_state_dir(temp.path(), SESSION_ID, "hello")
            .await
            .unwrap();
        let request = request_rx.await.unwrap();

        assert_eq!(
            request.target,
            "/session/ses%2Ftest%20123/prompt_async?directory=%2Ftmp%2Fproject"
        );
    }

    #[tokio::test]
    async fn send_text_rejects_non_local_server_url_before_request() {
        let temp = TempDir::new().unwrap();
        write_state(temp.path(), "https://example.com", Some("/tmp/project"));

        let error = send_text_from_state_dir(temp.path(), SESSION_ID, "hello")
            .await
            .unwrap_err();

        assert!(error.to_string().contains("must use http on localhost"));
    }

    #[test]
    fn read_bridge_state_rejects_mismatched_session_id() {
        let temp = TempDir::new().unwrap();
        write_state_with_session_id(
            temp.path(),
            SESSION_ID,
            "22222222-2222-4222-8222-222222222222",
            "http://127.0.0.1:12345",
            Some("/tmp/project"),
        );

        let error = read_bridge_state(SESSION_ID, Some(temp.path())).unwrap_err();

        assert!(error
            .to_string()
            .contains("OpenCode server bridge state session_id mismatch"));
    }

    #[test]
    fn read_bridge_state_rejects_bad_or_incompatible_state_files() {
        let temp = TempDir::new().unwrap();

        let mut newer_schema = base_state_payload("http://127.0.0.1:12345", Some("/tmp/project"));
        newer_schema["schema_version"] = Value::Number(3.into());
        write_state_payload(temp.path(), SESSION_ID, newer_schema);
        let error = read_bridge_state(SESSION_ID, Some(temp.path())).unwrap_err();
        assert!(error
            .to_string()
            .contains("state schema 3 is newer than this Longhouse build"));

        let mut incomplete = base_state_payload("http://127.0.0.1:12345", Some("/tmp/project"));
        incomplete.as_object_mut().unwrap().remove("password");
        write_state_payload(temp.path(), SESSION_ID, incomplete);
        let error = read_bridge_state(SESSION_ID, Some(temp.path())).unwrap_err();
        assert!(error
            .to_string()
            .contains("OpenCode server bridge state is incomplete"));

        std::fs::write(temp.path().join(format!("{SESSION_ID}.json")), "{").unwrap();
        let error = read_bridge_state(SESSION_ID, Some(temp.path())).unwrap_err();
        assert!(error
            .to_string()
            .contains("OpenCode server bridge state is not valid JSON"));
    }

    async fn send_text_from_state_dir(
        state_dir: &Path,
        session_id: &str,
        text: &str,
    ) -> Result<OpenCodeControlResult> {
        let state = read_bridge_state(session_id, Some(state_dir))?;
        post_prompt_async(&state, text).await?;
        Ok(OpenCodeControlResult {
            provider_session_id: state.provider_session_id,
        })
    }

    async fn interrupt_from_state_dir(
        state_dir: &Path,
        session_id: &str,
    ) -> Result<OpenCodeControlResult> {
        let state = read_bridge_state(session_id, Some(state_dir))?;
        post_abort(&state).await?;
        Ok(OpenCodeControlResult {
            provider_session_id: state.provider_session_id,
        })
    }

    fn write_state(state_dir: &Path, server_url: &str, cwd: Option<&str>) {
        write_state_payload(state_dir, SESSION_ID, base_state_payload(server_url, cwd));
    }

    fn write_state_with_session_id(
        state_dir: &Path,
        filename_session_id: &str,
        state_session_id: &str,
        server_url: &str,
        cwd: Option<&str>,
    ) {
        let mut payload = base_state_payload(server_url, cwd);
        payload["session_id"] = Value::String(state_session_id.to_string());
        write_state_payload(state_dir, filename_session_id, payload);
    }

    fn base_state_payload(server_url: &str, cwd: Option<&str>) -> Value {
        json!({
            "schema_version": 1,
            "session_id": SESSION_ID,
            "provider_session_id": "ses_test123",
            "server_url": server_url,
            "cwd": cwd.unwrap_or(""),
            "username": "opencode",
            "password": "secret-password",
        })
    }

    fn write_state_payload(state_dir: &Path, filename_session_id: &str, payload: Value) {
        fs::create_dir_all(state_dir).unwrap();
        let path = state_dir.join(format!("{filename_session_id}.json"));
        fs::write(path, serde_json::to_string(&payload).unwrap()).unwrap();
    }

    async fn spawn_single_request_server() -> (String, oneshot::Receiver<RecordedRequest>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = oneshot::channel();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut bytes = Vec::new();
            let mut header_end = None;
            let mut content_length = 0usize;
            loop {
                let mut chunk = [0u8; 1024];
                let read = stream.read(&mut chunk).await.unwrap();
                if read == 0 {
                    break;
                }
                bytes.extend_from_slice(&chunk[..read]);
                if header_end.is_none() {
                    header_end = find_header_end(&bytes);
                    if let Some(end) = header_end {
                        let head = String::from_utf8_lossy(&bytes[..end]);
                        content_length = parse_content_length(&head);
                    }
                }
                if let Some(end) = header_end {
                    if bytes.len() >= end + 4 + content_length {
                        break;
                    }
                }
            }
            let request = parse_request(&bytes);
            let _ = tx.send(request);
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
                )
                .await
                .unwrap();
        });
        (format!("http://{addr}"), rx)
    }

    fn find_header_end(bytes: &[u8]) -> Option<usize> {
        bytes.windows(4).position(|window| window == b"\r\n\r\n")
    }

    fn parse_content_length(head: &str) -> usize {
        head.lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                if name.eq_ignore_ascii_case("content-length") {
                    value.trim().parse::<usize>().ok()
                } else {
                    None
                }
            })
            .unwrap_or(0)
    }

    fn parse_request(bytes: &[u8]) -> RecordedRequest {
        let text = String::from_utf8_lossy(bytes);
        let (head, body) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
        let mut lines = head.lines();
        let request_line = lines.next().unwrap();
        let mut request_parts = request_line.split_whitespace();
        let method = request_parts.next().unwrap().to_string();
        let target = request_parts.next().unwrap().to_string();
        let headers = lines
            .filter_map(|line| {
                let (name, value) = line.split_once(':')?;
                Some((name.to_ascii_lowercase(), value.trim().to_string()))
            })
            .collect();
        RecordedRequest {
            method,
            target,
            headers,
            body: body.to_string(),
        }
    }
}
