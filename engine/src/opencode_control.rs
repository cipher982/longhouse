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
const MAX_READABLE_STATE_SCHEMA_VERSION: u64 = 1;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
pub const OPENCODE_SERVER_BRIDGE_TRANSPORT: &str = "opencode_server_bridge";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenCodeControlResult {
    pub provider_session_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenCodeStopResult {
    pub pid: Option<u32>,
    pub stopped: bool,
}

#[derive(Debug, Clone)]
struct OpenCodeControlState {
    session_id: String,
    provider_session_id: String,
    server_url: String,
    cwd: Option<String>,
    username: String,
    password: String,
    pid: Option<u32>,
    process_start_time: String,
    process_command: String,
}

pub(crate) struct OpenCodeAttachState {
    pub provider_session_id: String,
    pub server_url: String,
    pub cwd: String,
    pub username: String,
    pub password: String,
}

pub(crate) fn read_for_bridge(session_id: &str) -> Result<OpenCodeAttachState> {
    let state = read_bridge_state(session_id, None)?;
    Ok(OpenCodeAttachState {
        provider_session_id: state.provider_session_id,
        server_url: state.server_url,
        cwd: state
            .cwd
            .context("OpenCode bridge state has no working directory")?,
        username: state.username,
        password: state.password,
    })
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct OpenCodeServerStateFile {
    schema_version: Option<u64>,
    session_id: Option<String>,
    provider_session_id: Option<String>,
    server_url: Option<String>,
    cwd: Option<String>,
    username: Option<String>,
    password: Option<String>,
    pid: Option<u32>,
    log_path: Option<String>,
    config_content_path: Option<String>,
    process_start_time: Option<String>,
    process_command: Option<String>,
    launch_mode: Option<String>,
    owner_wrapper_pid: Option<u32>,
    owner_wrapper_start_time: Option<String>,
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

pub fn stop_server_bridge(session_id: &str) -> Result<OpenCodeStopResult> {
    let state = read_bridge_state(session_id, None)?;
    let pid = state.pid;
    let stopped = terminate_recorded_opencode_server(&state)?;
    Ok(OpenCodeStopResult { pid, stopped })
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
    let Some(schema_version) = payload.schema_version else {
        bail!("OpenCode server bridge state is missing schema_version");
    };
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
        pid: payload.pid,
        process_start_time: payload
            .process_start_time
            .unwrap_or_default()
            .trim()
            .to_string(),
        process_command: payload
            .process_command
            .unwrap_or_default()
            .trim()
            .to_string(),
    })
}

fn pid_is_running(pid: u32) -> bool {
    if pid == 0 || pid > i32::MAX as u32 {
        return false;
    }
    let rc = unsafe { libc::kill(pid as i32, 0) };
    if rc == 0 {
        return true;
    }
    let err = std::io::Error::last_os_error();
    matches!(err.raw_os_error(), Some(code) if code == libc::EPERM)
}

fn process_identity(pid: u32) -> Option<(String, String)> {
    if pid == 0 {
        return None;
    }
    let output = std::process::Command::new("ps")
        .arg("-o")
        .arg("lstart=,command=")
        .arg("-p")
        .arg(pid.to_string())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let line = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if line.len() <= 24 {
        return None;
    }
    let (start, command) = line.split_at(24);
    let start = start.trim().to_string();
    let command = command.trim().to_string();
    if command.is_empty() {
        return None;
    }
    Some((start, command))
}

fn pid_matches_strict_opencode_identity(state: &OpenCodeControlState) -> bool {
    let Some(pid) = state.pid else {
        return false;
    };
    if !pid_is_running(pid) {
        return false;
    }
    let recorded_start = state.process_start_time.trim();
    let recorded_cmd = state.process_command.trim();
    if recorded_start.is_empty() || recorded_cmd.is_empty() {
        return false;
    }
    if !(recorded_cmd.contains("opencode") && recorded_cmd.contains(" serve")) {
        return false;
    }
    let Some((live_start, live_cmd)) = process_identity(pid) else {
        return false;
    };
    recorded_start == live_start && recorded_cmd == live_cmd
}

fn terminate_recorded_opencode_server(state: &OpenCodeControlState) -> Result<bool> {
    let Some(pid) = state.pid else {
        return Ok(false);
    };
    if pid == 0 || pid > i32::MAX as u32 {
        return Ok(false);
    }
    if !pid_matches_strict_opencode_identity(state) {
        return Ok(false);
    }
    terminate_recorded_process_group(pid)
}

#[cfg(unix)]
fn terminate_recorded_process_group(pid: u32) -> Result<bool> {
    let pid_i = pid as i32;
    unsafe {
        let pgid = libc::getpgid(pid_i);
        if pgid == -1 {
            let err = std::io::Error::last_os_error();
            if matches!(err.raw_os_error(), Some(code) if code == libc::ESRCH) {
                return Ok(false);
            }
            return Err(err).with_context(|| {
                format!("Could not inspect OpenCode server process group pid={pid}")
            });
        }
        if pgid != pid_i {
            return Ok(false);
        }
        let rc = libc::killpg(pid_i, libc::SIGTERM);
        if rc == 0 {
            return Ok(true);
        }
        let err = std::io::Error::last_os_error();
        if matches!(err.raw_os_error(), Some(code) if code == libc::ESRCH) {
            return Ok(false);
        }
        Err(err).with_context(|| format!("Could not terminate OpenCode server pid={pid}"))
    }
}

#[cfg(not(unix))]
fn terminate_recorded_process_group(_pid: u32) -> Result<bool> {
    Ok(false)
}

#[cfg(test)]
fn terminate_pid(pid: u32) -> Result<()> {
    if pid == 0 || pid > i32::MAX as u32 {
        return Ok(());
    }
    let pid = pid as i32;
    #[cfg(unix)]
    {
        let group_rc = unsafe { libc::killpg(pid, libc::SIGTERM) };
        if group_rc == 0 {
            return Ok(());
        }
        let group_err = std::io::Error::last_os_error();
        if matches!(group_err.raw_os_error(), Some(code) if code == libc::ESRCH) {
            return Ok(());
        }
    }
    let rc = unsafe { libc::kill(pid, libc::SIGTERM) };
    if rc == 0 {
        return Ok(());
    }
    let err = std::io::Error::last_os_error();
    if matches!(err.raw_os_error(), Some(code) if code == libc::ESRCH) {
        return Ok(());
    }
    Err(err).context("Could not terminate OpenCode server")
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
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    use base64::{engine::general_purpose, Engine as _};
    use tempfile::TempDir;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::oneshot;
    use tokio::time::sleep;

    use super::*;

    const SESSION_ID: &str = "11111111-1111-4111-8111-111111111111";

    #[derive(Debug, Clone)]
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

    #[cfg(unix)]
    #[tokio::test]
    async fn stop_server_bridge_stops_identity_matched_process_group() {
        let temp = TempDir::new().unwrap();
        let mut child = spawn_fake_opencode_stop_target(temp.path(), true);
        let pid = child.id();
        let (start, command) = wait_process_identity(pid);
        write_stop_state(temp.path(), pid, &start, &command);

        let result = stop_from_state_dir(temp.path(), SESSION_ID).unwrap();

        assert_eq!(result.pid, Some(pid));
        assert!(result.stopped);
        wait_until_pid_stops(pid).await;
        let _ = child.wait();
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn stop_server_bridge_refuses_missing_or_legacy_identity() {
        let temp = TempDir::new().unwrap();
        write_state_payload(
            temp.path(),
            SESSION_ID,
            base_state_payload("http://127.0.0.1:12345", Some("/tmp/project")),
        );
        let result = stop_from_state_dir(temp.path(), SESSION_ID).unwrap();
        assert_eq!(result.pid, None);
        assert!(!result.stopped);

        let mut child = spawn_fake_opencode_stop_target(temp.path(), true);
        let pid = child.id();
        let _ = wait_process_identity(pid);
        let mut legacy = base_state_payload("http://127.0.0.1:12345", Some("/tmp/project"));
        legacy["pid"] = json!(pid);
        write_state_payload(temp.path(), SESSION_ID, legacy);

        let result = stop_from_state_dir(temp.path(), SESSION_ID).unwrap();

        assert_eq!(result.pid, Some(pid));
        assert!(!result.stopped);
        assert!(pid_is_running(pid));
        terminate_pid(pid).unwrap();
        wait_until_pid_stops(pid).await;
        let _ = child.wait();
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn stop_server_bridge_refuses_mismatched_recorded_identity() {
        let temp = TempDir::new().unwrap();
        let mut child = spawn_fake_opencode_stop_target(temp.path(), true);
        let pid = child.id();
        let (start, command) = wait_process_identity(pid);

        write_stop_state(temp.path(), pid, "Sun Jan  1 00:00:00 2000", &command);
        let wrong_start = stop_from_state_dir(temp.path(), SESSION_ID).unwrap();
        assert!(!wrong_start.stopped);
        assert!(pid_is_running(pid));

        write_stop_state(
            temp.path(),
            pid,
            &start,
            "opencode serve --hostname 127.0.0.1 --different",
        );
        let wrong_command = stop_from_state_dir(temp.path(), SESSION_ID).unwrap();
        assert!(!wrong_command.stopped);
        assert!(pid_is_running(pid));

        terminate_pid(pid).unwrap();
        wait_until_pid_stops(pid).await;
        let _ = child.wait();
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn stop_server_bridge_refuses_non_group_leader_and_non_opencode_recorded_command() {
        let temp = TempDir::new().unwrap();
        let mut child = spawn_fake_opencode_stop_target(temp.path(), false);
        let pid = child.id();
        let (start, command) = wait_process_identity(pid);
        write_stop_state(temp.path(), pid, &start, &command);

        let result = stop_from_state_dir(temp.path(), SESSION_ID).unwrap();

        assert_eq!(result.pid, Some(pid));
        assert!(!result.stopped);
        assert!(pid_is_running(pid));
        child.kill().unwrap();
        let _ = child.wait();

        let mut child = spawn_fake_opencode_stop_target(temp.path(), true);
        let pid = child.id();
        let (start, _command) = wait_process_identity(pid);
        write_stop_state(temp.path(), pid, &start, "sleep 60");
        let result = stop_from_state_dir(temp.path(), SESSION_ID).unwrap();
        assert_eq!(result.pid, Some(pid));
        assert!(!result.stopped);
        assert!(pid_is_running(pid));
        terminate_pid(pid).unwrap();
        wait_until_pid_stops(pid).await;
        let _ = child.wait();
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
        newer_schema["schema_version"] = Value::Number(2.into());
        write_state_payload(temp.path(), SESSION_ID, newer_schema);
        let error = read_bridge_state(SESSION_ID, Some(temp.path())).unwrap_err();
        assert!(error
            .to_string()
            .contains("state schema 2 is newer than this Longhouse build"));

        let mut missing_schema = base_state_payload("http://127.0.0.1:12345", Some("/tmp/project"));
        missing_schema
            .as_object_mut()
            .unwrap()
            .remove("schema_version");
        write_state_payload(temp.path(), SESSION_ID, missing_schema);
        let error = read_bridge_state(SESSION_ID, Some(temp.path())).unwrap_err();
        assert!(error
            .to_string()
            .contains("OpenCode server bridge state is missing schema_version"));

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

    fn stop_from_state_dir(state_dir: &Path, session_id: &str) -> Result<OpenCodeStopResult> {
        let state = read_bridge_state(session_id, Some(state_dir))?;
        let pid = state.pid;
        Ok(OpenCodeStopResult {
            pid,
            stopped: terminate_recorded_opencode_server(&state)?,
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

    #[cfg(unix)]
    fn spawn_fake_opencode_stop_target(root: &Path, new_process_group: bool) -> TestChild {
        let script_dir = root.join("stop-bin");
        fs::create_dir_all(&script_dir).unwrap();
        let path = script_dir.join("opencode");
        fs::write(
            &path,
            "#!/bin/sh\ntrap 'exit 0' TERM\nwhile :; do sleep 1; done\n",
        )
        .unwrap();
        let mut perms = fs::metadata(&path).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&path, perms).unwrap();

        let mut command = std::process::Command::new(&path);
        command
            .arg("serve")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        if new_process_group {
            use std::os::unix::process::CommandExt;
            unsafe {
                command.pre_exec(|| {
                    if libc::setsid() == -1 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
        }
        TestChild::new(command.spawn().unwrap())
    }

    #[cfg(unix)]
    struct TestChild {
        child: std::process::Child,
    }

    #[cfg(unix)]
    impl TestChild {
        fn new(child: std::process::Child) -> Self {
            Self { child }
        }

        fn id(&self) -> u32 {
            self.child.id()
        }

        fn kill(&mut self) -> std::io::Result<()> {
            self.child.kill()
        }

        fn wait(&mut self) -> std::io::Result<std::process::ExitStatus> {
            self.child.wait()
        }
    }

    #[cfg(unix)]
    impl Drop for TestChild {
        fn drop(&mut self) {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }

    fn write_stop_state(
        state_dir: &Path,
        pid: u32,
        process_start_time: &str,
        process_command: &str,
    ) {
        let mut payload = base_state_payload("http://127.0.0.1:12345", Some("/tmp/project"));
        payload["pid"] = json!(pid);
        payload["process_start_time"] = json!(process_start_time);
        payload["process_command"] = json!(process_command);
        write_state_payload(state_dir, SESSION_ID, payload);
    }

    fn wait_process_identity(pid: u32) -> (String, String) {
        for _ in 0..30 {
            if let Some(identity) = process_identity(pid) {
                return identity;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        panic!("pid {pid} identity was not visible");
    }

    async fn wait_until_pid_stops(pid: u32) {
        for _ in 0..30 {
            if !pid_is_running(pid) {
                return;
            }
            sleep(Duration::from_millis(100)).await;
        }
        panic!("pid {pid} did not stop");
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
