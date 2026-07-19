use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use base64::Engine as _;
use chrono::{DateTime, Utc};
use rand::RngCore;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::mpsc;
use uuid::Uuid;

const CLAUDE_CHANNEL_CAPABILITY: &str = "claude/channel";
const DEFAULT_HTTP_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Clone, Debug)]
pub struct ClaudeChannelServeConfig {
    pub session_id: Option<String>,
    pub provider_session_id: Option<String>,
    pub state_root: Option<PathBuf>,
    pub port: u16,
    pub auth_token: Option<String>,
    pub claude_pid: Option<i32>,
    pub cwd: Option<String>,
}

#[derive(Debug)]
struct BridgeStateInner {
    session_id: Option<String>,
    run_id: String,
    connection_id: String,
    lease_generation: String,
    provider_session_id: Option<String>,
    state_root: PathBuf,
    auth_token: String,
    port: u16,
    claude_pid: Option<i32>,
    bridge_pid: u32,
    cwd: Option<String>,
    ready: bool,
    started_at: DateTime<Utc>,
}

#[derive(Clone, Debug)]
struct BridgeState {
    inner: Arc<Mutex<BridgeStateInner>>,
}

#[derive(Debug, Serialize)]
struct BridgeStatePayload {
    session_id: Option<String>,
    run_id: String,
    connection_id: String,
    lease_generation: String,
    provider_session_id: Option<String>,
    state_root: String,
    auth_token: String,
    port: u16,
    claude_pid: Option<i32>,
    bridge_pid: u32,
    cwd: Option<String>,
    ready: bool,
    started_at: String,
    updated_at: String,
}

#[derive(Debug, Serialize)]
struct BridgeHealthPayload {
    session_id: Option<String>,
    run_id: String,
    connection_id: String,
    lease_generation: String,
    provider_session_id: Option<String>,
    state_root: String,
    port: u16,
    claude_pid: Option<i32>,
    bridge_pid: u32,
    cwd: Option<String>,
    ready: bool,
    started_at: String,
    updated_at: String,
}

pub async fn run(config: ClaudeChannelServeConfig) -> Result<()> {
    let stdin = tokio::io::stdin();
    let stdout = tokio::io::stdout();
    run_with_io(stdin, stdout, config).await
}

async fn run_with_io<R, W>(reader: R, writer: W, config: ClaudeChannelServeConfig) -> Result<()>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let state = BridgeState::new(config)?;
    let (outbound_tx, mut outbound_rx) = mpsc::unbounded_channel::<Value>();
    let mut http = if state.has_managed_session() {
        let http = HttpServerHandle::start(state.clone(), outbound_tx.clone())?;
        state.set_port(http.port())?;
        state.write_state()?;
        Some(http)
    } else {
        None
    };

    let mut lines = BufReader::new(reader).lines();
    let mut writer = writer;
    let mut loop_result: Result<()> = Ok(());
    loop {
        tokio::select! {
            line = lines.next_line() => {
                match line {
                    Err(err) => {
                        loop_result = Err(err.into());
                        break;
                    }
                    Ok(None) => break,
                    Ok(Some(line)) => {
                        let trimmed = line.trim();
                        if trimmed.is_empty() {
                            continue;
                        }
                        let response = match handle_rpc_line(trimmed, &state) {
                            Ok(response) => response,
                            Err(err) => {
                                loop_result = Err(err);
                                break;
                            }
                        };
                        if let Some(response) = response {
                            if outbound_tx.send(response).is_err() {
                                loop_result = Err(anyhow!("Claude channel stdio writer is closed"));
                                break;
                            }
                        }
                    }
                }
            }
            message = outbound_rx.recv() => {
                match message {
                    Some(message) => {
                        if let Err(err) = write_json_line(&mut writer, &message).await {
                            loop_result = Err(err);
                            break;
                        }
                    }
                    None => break,
                }
            }
        }
    }

    if let Some(http) = http.take() {
        http.shutdown();
    }
    drop(outbound_tx);
    if loop_result.is_ok() {
        while let Some(message) = outbound_rx.recv().await {
            if let Err(err) = write_json_line(&mut writer, &message).await {
                loop_result = Err(err);
                break;
            }
        }
    }
    let cleanup_result = state.remove_state();
    loop_result?;
    cleanup_result?;
    Ok(())
}

fn handle_rpc_line(raw: &str, state: &BridgeState) -> Result<Option<Value>> {
    let message: Value = match serde_json::from_str(raw) {
        Ok(message) => message,
        Err(_) => {
            return Ok(Some(json!({
                "jsonrpc": "2.0",
                "id": Value::Null,
                "error": {
                    "code": -32700,
                    "message": "parse error"
                }
            })));
        }
    };
    let method = message.get("method").and_then(Value::as_str).unwrap_or("");
    let id = message.get("id").cloned();
    if method == "notifications/initialized" || method == "initialized" {
        state.set_ready(true)?;
        return Ok(None);
    }
    let Some(id) = id else {
        return Ok(None);
    };
    let response = match method {
        "initialize" => json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": {
                "protocolVersion": message
                    .get("params")
                    .and_then(|params| params.get("protocolVersion"))
                    .and_then(Value::as_str)
                    .unwrap_or("2025-11-25"),
                "capabilities": {
                    "experimental": {
                        CLAUDE_CHANNEL_CAPABILITY: {}
                    }
                },
                "serverInfo": {
                    "name": "longhouse-channel",
                    "version": env!("CARGO_PKG_VERSION")
                },
                "instructions": "Longhouse native Claude channel bridge. Claude may receive channel notifications from this local server."
            }
        }),
        "tools/list" => json!({"jsonrpc": "2.0", "id": id, "result": {"tools": []}}),
        "resources/list" => json!({"jsonrpc": "2.0", "id": id, "result": {"resources": []}}),
        "prompts/list" => json!({"jsonrpc": "2.0", "id": id, "result": {"prompts": []}}),
        "ping" => json!({"jsonrpc": "2.0", "id": id, "result": {}}),
        _ => json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {
                "code": -32601,
                "message": format!("method not found: {method}")
            }
        }),
    };
    Ok(Some(response))
}

async fn write_json_line<W>(writer: &mut W, value: &Value) -> Result<()>
where
    W: AsyncWrite + Unpin,
{
    let raw = serde_json::to_vec(value)?;
    writer.write_all(&raw).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;
    Ok(())
}

impl BridgeState {
    fn new(config: ClaudeChannelServeConfig) -> Result<Self> {
        let session_id = normalize_optional(config.session_id);
        if let Some(session_id) = session_id.as_deref() {
            Uuid::parse_str(session_id).context("session_id must be a UUID")?;
        }
        let provider_session_id = normalize_optional(config.provider_session_id);
        let state_root = config.state_root.unwrap_or_else(default_state_root);
        let auth_token = normalize_optional(config.auth_token).unwrap_or_else(random_token);
        let cwd = normalize_optional(config.cwd);
        Ok(Self {
            inner: Arc::new(Mutex::new(BridgeStateInner {
                session_id,
                run_id: Uuid::new_v4().to_string(),
                connection_id: Uuid::new_v4().to_string(),
                lease_generation: Uuid::new_v4().to_string(),
                provider_session_id,
                state_root,
                auth_token,
                port: config.port,
                claude_pid: config.claude_pid.or_else(parent_pid),
                bridge_pid: std::process::id(),
                cwd,
                ready: false,
                started_at: Utc::now(),
            })),
        })
    }

    fn has_managed_session(&self) -> bool {
        self.inner
            .lock()
            .expect("bridge state mutex poisoned")
            .session_id
            .is_some()
    }

    fn auth_token(&self) -> String {
        self.inner
            .lock()
            .expect("bridge state mutex poisoned")
            .auth_token
            .clone()
    }

    fn set_port(&self, port: u16) -> Result<()> {
        let mut inner = self.inner.lock().expect("bridge state mutex poisoned");
        inner.port = port;
        Ok(())
    }

    fn set_ready(&self, ready: bool) -> Result<()> {
        {
            let mut inner = self.inner.lock().expect("bridge state mutex poisoned");
            inner.ready = ready;
        }
        self.write_state()
    }

    fn payload(&self) -> BridgeStatePayload {
        let inner = self.inner.lock().expect("bridge state mutex poisoned");
        BridgeStatePayload {
            session_id: inner.session_id.clone(),
            run_id: inner.run_id.clone(),
            connection_id: inner.connection_id.clone(),
            lease_generation: inner.lease_generation.clone(),
            provider_session_id: inner.provider_session_id.clone(),
            state_root: inner.state_root.display().to_string(),
            auth_token: inner.auth_token.clone(),
            port: inner.port,
            claude_pid: inner.claude_pid,
            bridge_pid: inner.bridge_pid,
            cwd: inner.cwd.clone(),
            ready: inner.ready,
            started_at: inner.started_at.to_rfc3339(),
            updated_at: Utc::now().to_rfc3339(),
        }
    }

    fn health_payload(&self) -> BridgeHealthPayload {
        let inner = self.inner.lock().expect("bridge state mutex poisoned");
        BridgeHealthPayload {
            session_id: inner.session_id.clone(),
            run_id: inner.run_id.clone(),
            connection_id: inner.connection_id.clone(),
            lease_generation: inner.lease_generation.clone(),
            provider_session_id: inner.provider_session_id.clone(),
            state_root: inner.state_root.display().to_string(),
            port: inner.port,
            claude_pid: inner.claude_pid,
            bridge_pid: inner.bridge_pid,
            cwd: inner.cwd.clone(),
            ready: inner.ready,
            started_at: inner.started_at.to_rfc3339(),
            updated_at: Utc::now().to_rfc3339(),
        }
    }

    fn state_file(&self) -> Result<Option<PathBuf>> {
        let inner = self.inner.lock().expect("bridge state mutex poisoned");
        let Some(session_id) = inner.session_id.as_deref() else {
            return Ok(None);
        };
        let normalized = Uuid::parse_str(session_id).context("session_id must be a UUID")?;
        Ok(Some(
            inner
                .state_root
                .join("sessions")
                .join(format!("{normalized}.json")),
        ))
    }

    fn write_state(&self) -> Result<()> {
        let Some(path) = self.state_file()? else {
            return Ok(());
        };
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating {}", parent.display()))?;
        }
        let raw = serde_json::to_vec_pretty(&self.payload())?;
        let tmp = path.with_extension(format!("json.tmp.{}", std::process::id()));
        write_private_file(&tmp, [&raw[..], b"\n"].concat().as_slice())
            .with_context(|| format!("writing {}", tmp.display()))?;
        std::fs::rename(&tmp, &path)
            .with_context(|| format!("renaming {} to {}", tmp.display(), path.display()))?;
        set_private_file_mode(&path);
        Ok(())
    }

    fn remove_state(&self) -> Result<()> {
        let Some(path) = self.state_file()? else {
            return Ok(());
        };
        match std::fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(err) => Err(err).with_context(|| format!("removing {}", path.display())),
        }
    }
}

struct HttpServerHandle {
    port: u16,
    shutdown: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl HttpServerHandle {
    fn start(state: BridgeState, outbound: mpsc::UnboundedSender<Value>) -> Result<Self> {
        let requested_port = state
            .inner
            .lock()
            .expect("bridge state mutex poisoned")
            .port;
        let listener = TcpListener::bind(("127.0.0.1", requested_port))
            .with_context(|| format!("binding Claude channel HTTP port {requested_port}"))?;
        listener
            .set_nonblocking(true)
            .context("setting Claude channel HTTP listener nonblocking")?;
        let port = listener.local_addr()?.port();
        let shutdown = Arc::new(AtomicBool::new(false));
        let thread_shutdown = shutdown.clone();
        let thread = thread::spawn(move || {
            while !thread_shutdown.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let _ = stream.set_read_timeout(Some(DEFAULT_HTTP_TIMEOUT));
                        let _ = stream.set_write_timeout(Some(DEFAULT_HTTP_TIMEOUT));
                        handle_http_stream(&mut stream, &state, &outbound);
                    }
                    Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(_) => break,
                }
            }
        });
        Ok(Self {
            port,
            shutdown,
            thread: Some(thread),
        })
    }

    fn port(&self) -> u16 {
        self.port
    }

    fn shutdown(mut self) {
        self.shutdown.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(("127.0.0.1", self.port));
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn handle_http_stream(
    stream: &mut TcpStream,
    state: &BridgeState,
    outbound: &mpsc::UnboundedSender<Value>,
) {
    match read_http_request(stream) {
        Ok(request) => {
            let response = handle_http_request(request, state, outbound);
            let _ = write_http_response(stream, response);
        }
        Err(status) => {
            let _ = write_http_response(stream, HttpResponse::empty(status));
        }
    }
}

#[derive(Debug)]
struct HttpRequest {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

#[derive(Debug)]
struct HttpResponse {
    status: u16,
    reason: &'static str,
    content_type: Option<&'static str>,
    body: Vec<u8>,
}

impl HttpResponse {
    fn empty(status: u16) -> Self {
        Self {
            status,
            reason: reason_phrase(status),
            content_type: None,
            body: Vec::new(),
        }
    }

    fn json(status: u16, body: Value) -> Self {
        Self {
            status,
            reason: reason_phrase(status),
            content_type: Some("application/json"),
            body: serde_json::to_vec(&body).unwrap_or_else(|_| b"{}".to_vec()),
        }
    }
}

fn read_http_request(stream: &mut TcpStream) -> std::result::Result<HttpRequest, u16> {
    let mut raw = Vec::new();
    let mut buffer = [0_u8; 4096];
    let header_end = loop {
        let n = stream.read(&mut buffer).map_err(|_| 400_u16)?;
        if n == 0 {
            return Err(400);
        }
        raw.extend_from_slice(&buffer[..n]);
        if raw.len() > 1024 * 1024 {
            return Err(413);
        }
        if let Some(index) = find_header_end(&raw) {
            break index;
        }
    };
    let header_text = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = header_text.lines();
    let request_line = lines.next().ok_or(400_u16)?;
    let mut request_parts = request_line.split_whitespace();
    let method = request_parts.next().unwrap_or("").to_string();
    let path = request_parts.next().unwrap_or("").to_string();
    if method.is_empty() || path.is_empty() {
        return Err(400);
    }
    let headers = lines
        .filter_map(|line| {
            let (key, value) = line.split_once(':')?;
            Some((key.trim().to_ascii_lowercase(), value.trim().to_string()))
        })
        .collect::<Vec<_>>();
    let content_length = headers
        .iter()
        .find(|(key, _)| key == "content-length")
        .and_then(|(_, value)| value.parse::<usize>().ok())
        .unwrap_or(0);
    if content_length > 1024 * 1024 {
        return Err(413);
    }
    let body_start = header_end + 4;
    while raw.len() < body_start + content_length {
        let n = stream.read(&mut buffer).map_err(|_| 400_u16)?;
        if n == 0 {
            return Err(400);
        }
        raw.extend_from_slice(&buffer[..n]);
    }
    Ok(HttpRequest {
        method,
        path,
        headers,
        body: raw[body_start..body_start + content_length].to_vec(),
    })
}

fn handle_http_request(
    request: HttpRequest,
    state: &BridgeState,
    outbound: &mpsc::UnboundedSender<Value>,
) -> HttpResponse {
    match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/health") => {
            HttpResponse::json(200, serde_json::to_value(state.health_payload()).unwrap())
        }
        ("POST", "/inject") => handle_inject_request(request, state, outbound),
        _ => HttpResponse::empty(404),
    }
}

fn handle_inject_request(
    request: HttpRequest,
    state: &BridgeState,
    outbound: &mpsc::UnboundedSender<Value>,
) -> HttpResponse {
    let expected = state.auth_token();
    let provided = request
        .headers
        .iter()
        .find(|(key, _)| key == "x-longhouse-channel-token")
        .map(|(_, value)| value.as_str())
        .unwrap_or("");
    if !expected.is_empty() && provided != expected {
        return HttpResponse::empty(403);
    }
    let payload: Value = match serde_json::from_slice(&request.body) {
        Ok(value) => value,
        Err(_) => return HttpResponse::empty(400),
    };
    let content = payload
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if content.trim().is_empty() {
        return HttpResponse::empty(400);
    }
    let meta = match payload.get("meta") {
        Some(Value::Object(map)) => Some(
            map.iter()
                .map(|(key, value)| {
                    (
                        key.clone(),
                        Value::String(
                            value
                                .as_str()
                                .map(str::to_string)
                                .unwrap_or_else(|| value.to_string()),
                        ),
                    )
                })
                .collect::<serde_json::Map<String, Value>>(),
        ),
        Some(_) => return HttpResponse::empty(400),
        None => None,
    };
    let mut params = json!({ "content": content });
    if let Some(meta) = meta {
        params["meta"] = Value::Object(meta);
    }
    let notification = json!({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": params,
    });
    if outbound.send(notification).is_err() {
        return HttpResponse::empty(500);
    }
    HttpResponse::empty(204)
}

fn write_http_response(stream: &mut TcpStream, response: HttpResponse) -> std::io::Result<()> {
    let mut headers = format!(
        "HTTP/1.1 {} {}\r\nContent-Length: {}\r\nConnection: close\r\n",
        response.status,
        response.reason,
        response.body.len()
    );
    if let Some(content_type) = response.content_type {
        headers.push_str(&format!("Content-Type: {content_type}\r\n"));
    }
    headers.push_str("\r\n");
    stream.write_all(headers.as_bytes())?;
    stream.write_all(&response.body)?;
    stream.flush()
}

fn find_header_end(raw: &[u8]) -> Option<usize> {
    raw.windows(4).position(|window| window == b"\r\n\r\n")
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        204 => "No Content",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        413 => "Payload Too Large",
        500 => "Internal Server Error",
        _ => "Error",
    }
}

fn default_state_root() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".claude/channels/longhouse")
}

fn normalize_optional(value: Option<String>) -> Option<String> {
    value
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn random_token() -> String {
    let mut bytes = [0_u8; 24];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

fn parent_pid() -> Option<i32> {
    #[cfg(unix)]
    unsafe {
        Some(libc::getppid())
    }
    #[cfg(not(unix))]
    {
        None
    }
}

fn set_private_file_mode(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(metadata) = std::fs::metadata(path) {
            let mut permissions = metadata.permissions();
            permissions.set_mode(0o600);
            let _ = std::fs::set_permissions(path, permissions);
        }
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
}

fn write_private_file(path: &Path, contents: &[u8]) -> Result<()> {
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path)?;
    file.write_all(contents)?;
    file.sync_all()?;
    set_private_file_mode(path);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tokio::io::{duplex, AsyncBufReadExt, AsyncWriteExt};

    const SESSION_ID: &str = "11111111-1111-4111-8111-111111111111";

    fn test_config(state_root: &Path) -> ClaudeChannelServeConfig {
        ClaudeChannelServeConfig {
            session_id: Some(SESSION_ID.to_string()),
            provider_session_id: Some("provider-123".to_string()),
            state_root: Some(state_root.to_path_buf()),
            port: 0,
            auth_token: Some("bridge-test-token".to_string()),
            claude_pid: Some(std::process::id() as i32),
            cwd: Some("/tmp/demo".to_string()),
        }
    }

    fn state_path(root: &Path) -> PathBuf {
        root.join("sessions").join(format!("{SESSION_ID}.json"))
    }

    async fn read_json_line<R>(reader: &mut R) -> Value
    where
        R: tokio::io::AsyncBufRead + Unpin,
    {
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        serde_json::from_str(&line).unwrap()
    }

    #[tokio::test]
    async fn bridge_handshake_state_inject_and_shutdown_match_python_contract() {
        let temp = tempfile::tempdir().unwrap();
        let (mut stdin_client, stdin_server) = duplex(8192);
        let (stdout_server, stdout_client) = duplex(8192);
        let mut stdout = BufReader::new(stdout_client);
        let task = tokio::spawn(run_with_io(
            stdin_server,
            stdout_server,
            test_config(temp.path()),
        ));

        stdin_client
            .write_all(
                (serde_json::to_string(&json!({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"}
                    }
                }))
                .unwrap()
                    + "\n")
                    .as_bytes(),
            )
            .await
            .unwrap();
        stdin_client
            .write_all(br#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#)
            .await
            .unwrap();
        stdin_client.write_all(b"\n").await.unwrap();
        stdin_client.write_all(b"{not-json}\n").await.unwrap();
        stdin_client
            .write_all(br#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#)
            .await
            .unwrap();
        stdin_client.write_all(b"\n").await.unwrap();
        stdin_client.flush().await.unwrap();

        let init = read_json_line(&mut stdout).await;
        assert_eq!(init["id"], 1);
        assert_eq!(
            init["result"]["capabilities"]["experimental"],
            json!({"claude/channel": {}})
        );
        let parse_error = read_json_line(&mut stdout).await;
        assert_eq!(parse_error["id"], Value::Null);
        assert_eq!(parse_error["error"]["code"], -32700);
        let tools = read_json_line(&mut stdout).await;
        assert_eq!(
            tools,
            json!({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
        );

        let state: Value =
            serde_json::from_slice(&std::fs::read(state_path(temp.path())).unwrap()).unwrap();
        assert_eq!(state["session_id"], SESSION_ID);
        assert_eq!(state["provider_session_id"], "provider-123");
        assert_eq!(state["auth_token"], "bridge-test-token");
        assert_eq!(state["cwd"], "/tmp/demo");
        assert_eq!(state["ready"], true);
        let port = state["port"].as_u64().unwrap() as u16;
        assert!(port > 0);

        let client = reqwest::Client::new();
        let health = client
            .get(format!("http://127.0.0.1:{port}/health"))
            .send()
            .await
            .unwrap();
        assert_eq!(health.status(), reqwest::StatusCode::OK);
        let health_text = health.text().await.unwrap();
        assert!(!health_text.contains("bridge-test-token"));
        let health_json: Value = serde_json::from_str(&health_text).unwrap();
        assert!(health_json.get("auth_token").is_none());
        assert_eq!(health_json["ready"], true);

        let rejected = client
            .post(format!("http://127.0.0.1:{port}/inject"))
            .header("X-Longhouse-Channel-Token", "wrong-token")
            .json(&json!({"content": "hello"}))
            .send()
            .await
            .unwrap();
        assert_eq!(rejected.status(), reqwest::StatusCode::FORBIDDEN);
        assert!(!rejected.text().await.unwrap().contains("bridge-test-token"));

        let accepted = client
            .post(format!("http://127.0.0.1:{port}/inject"))
            .header("X-Longhouse-Channel-Token", "bridge-test-token")
            .json(&json!({"content": "hello from rust", "meta": {"user": "pm"}}))
            .send()
            .await
            .unwrap();
        assert_eq!(accepted.status(), reqwest::StatusCode::NO_CONTENT);

        let notification = read_json_line(&mut stdout).await;
        assert_eq!(notification["method"], "notifications/claude/channel");
        assert_eq!(notification["params"]["content"], "hello from rust");
        assert_eq!(notification["params"]["meta"]["user"], "pm");

        drop(stdin_client);
        task.await.unwrap().unwrap();
        assert!(!state_path(temp.path()).exists());
    }

    #[test]
    fn state_file_is_private_when_written() {
        let temp = tempfile::tempdir().unwrap();
        let state = BridgeState::new(test_config(temp.path())).unwrap();
        state.set_port(1234).unwrap();
        state.write_state().unwrap();
        let payload: Value =
            serde_json::from_slice(&std::fs::read(state_path(temp.path())).unwrap()).unwrap();
        assert!(Uuid::parse_str(payload["connection_id"].as_str().unwrap()).is_ok());
        assert!(Uuid::parse_str(payload["lease_generation"].as_str().unwrap()).is_ok());

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(state_path(temp.path()))
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600);
        }
    }
}
