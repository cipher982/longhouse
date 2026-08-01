//! Native owner for a managed OpenCode localhost server bridge.
//!
//! This intentionally owns only the stock `opencode serve` lifecycle and its
//! private state. Runtime plugins and answerable permission pauses stay out of
//! this first native slice until their reply path is native too.

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use reqwest::Url;
use serde::Serialize;
use serde_json::{json, Value};
#[cfg(unix)]
use tokio::io::AsyncWriteExt;
use uuid::Uuid;

const USERNAME: &str = "opencode";
const READY_TIMEOUT: Duration = Duration::from_secs(20);

pub struct StartConfig {
    pub session_id: String,
    pub run_id: String,
    pub cwd: PathBuf,
    pub display_name: Option<String>,
    pub opencode_bin: Option<String>,
    pub claude_dir: Option<PathBuf>,
    pub launch_mode: String,
    pub coordination_token: String,
}

#[derive(Serialize)]
pub struct StartResult {
    pub session_id: String,
    pub provider_session_id: String,
    pub server_url: String,
}

#[derive(serde::Deserialize)]
struct ExistingState {
    run_id: String,
    pid: u32,
    provider_session_id: String,
    server_url: String,
}

pub fn start(config: StartConfig) -> Result<StartResult> {
    let session_id = normalize_uuid(&config.session_id, "session_id")?;
    let run_id = normalize_uuid(&config.run_id, "run_id")?;
    let cwd = fs::canonicalize(&config.cwd).with_context(|| {
        format!(
            "OpenCode workspace is unavailable: {}",
            config.cwd.display()
        )
    })?;
    if !cwd.is_dir() {
        bail!("OpenCode workspace is not a directory: {}", cwd.display());
    }
    if !matches!(
        config.launch_mode.as_str(),
        "attached_tui" | "detached" | "keep_server"
    ) {
        bail!("unsupported OpenCode launch mode");
    }
    let coordination_token = config.coordination_token.trim();
    if coordination_token.is_empty() {
        bail!("Longhouse did not issue coordination authority for this session");
    }
    let state_dir = state_dir(config.claude_dir.as_deref())?;
    let _start_lock = acquire_start_lock(&state_dir.join(format!("{session_id}.start.lock")))?;
    let state_path = state_dir.join(format!("{session_id}.json"));
    if state_path.exists() {
        let existing = fs::read(&state_path)
            .ok()
            .and_then(|raw| serde_json::from_slice::<ExistingState>(&raw).ok());
        if existing.as_ref().is_none_or(|state| !pid_alive(state.pid)) {
            fs::remove_file(&state_path)?;
        } else if let Some(existing) = existing {
            if existing.run_id == run_id {
                return Ok(StartResult {
                    session_id,
                    provider_session_id: existing.provider_session_id,
                    server_url: existing.server_url,
                });
            }
            bail!("managed OpenCode bridge already has a live state for {session_id}; stop it before starting again");
        } else {
            unreachable!("live OpenCode state was handled above");
        }
    }
    fs::create_dir_all(state_dir.join("logs"))?;
    let binary = resolve_binary(config.opencode_bin)?;
    let password = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let log_path = state_dir.join("logs").join(format!("{session_id}.log"));
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)?;
    let mut command = Command::new(&binary);
    let engine = std::env::current_exe().context("resolve native engine for OpenCode MCP")?;
    // This process is the paired engine binary, so the registered command is
    // absolute and remains valid even when the facade is not on PATH.
    let mcp_config = opencode_mcp_config(&engine, &session_id, coordination_token);
    command
        .args([
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
            "--print-logs",
        ])
        .current_dir(&cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log));
    configure_opencode_environment(&mut command, &session_id, &password, &mcp_config)?;
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn().context("start stock OpenCode server")?;
    let pid = child.id();
    let started = Instant::now();
    let server_url = loop {
        if let Some(url) = read_listen_url(&log_path)? {
            break url;
        }
        if let Some(status) = child.try_wait()? {
            bail!(
                "OpenCode server exited before readiness ({status}): {}",
                tail(&log_path)?
            );
        }
        if started.elapsed() > READY_TIMEOUT {
            let _ = stop_pid(pid);
            bail!(
                "timed out waiting for OpenCode server readiness: {}",
                tail(&log_path)?
            );
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    let result = (|| -> Result<StartResult> {
        let runtime = tokio::runtime::Runtime::new()?;
        let provider_session_id = runtime.block_on(create_session(
            &server_url,
            &password,
            &cwd,
            config.display_name.as_deref(),
        ))?;
        let (process_start_time, process_command) = process_identity(pid).unwrap_or_default();
        let now = chrono::Utc::now().to_rfc3339();
        let payload = json!({
            "schema_version": 1, "session_id": session_id, "run_id": run_id,
            "connection_id": Uuid::new_v4().to_string(), "lease_generation": Uuid::new_v4().to_string(),
            "provider_session_id": provider_session_id, "server_url": server_url,
            "pid": pid, "cwd": cwd, "username": USERNAME, "password": password,
            "log_path": log_path, "started_at": now, "updated_at": now,
            "process_start_time": process_start_time, "process_command": process_command,
            "launch_mode": config.launch_mode, "owner_wrapper_pid": 0, "owner_wrapper_start_time": ""
        });
        write_private_json(&state_path, &payload)?;
        write_provider_binding(&provider_binding_path(&session_id)?, &payload)?;
        Ok(StartResult {
            session_id,
            provider_session_id,
            server_url,
        })
    })();
    if result.is_err() {
        let _ = stop_pid(pid);
    }
    result
}

fn opencode_mcp_config(
    engine: &Path,
    session_id: &str,
    coordination_token: &str,
) -> serde_json::Value {
    json!({
        "mcp": {
            "longhouse": {
                "type": "local",
                "command": [engine, "claude-channel", "serve"],
                "environment": {
                    "LONGHOUSE_COORDINATION_TOKEN": coordination_token,
                    "LONGHOUSE_MANAGED_SESSION_ID": session_id,
                },
                "enabled": true,
            }
        }
    })
}

fn configure_opencode_environment(
    command: &mut Command,
    session_id: &str,
    password: &str,
    mcp_config: &serde_json::Value,
) -> Result<()> {
    command
        .env_remove("LONGHOUSE_COORDINATION_TOKEN")
        .env("LONGHOUSE_MANAGED_SESSION_ID", session_id)
        .env("OPENCODE_SERVER_USERNAME", USERNAME)
        .env("OPENCODE_SERVER_PASSWORD", password)
        .env(
            "OPENCODE_CONFIG_CONTENT",
            serde_json::to_string(mcp_config)?,
        );
    Ok(())
}

fn acquire_start_lock(lock_path: &Path) -> Result<fd_lock::RwLockWriteGuard<'static, fs::File>> {
    if let Some(parent) = lock_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(lock_path)
        .with_context(|| format!("open OpenCode start lock {}", lock_path.display()))?;
    let lock = Box::leak(Box::new(fd_lock::RwLock::new(file)));
    lock.try_write().map_err(|err| {
        if err.kind() == std::io::ErrorKind::WouldBlock {
            anyhow::anyhow!(
                "another OpenCode start is in progress for {}",
                lock_path.display()
            )
        } else {
            anyhow::Error::from(err).context(format!("lock OpenCode start {}", lock_path.display()))
        }
    })
}

pub fn stop(
    session_id: &str,
    claude_dir: Option<PathBuf>,
) -> Result<crate::opencode_control::OpenCodeStopResult> {
    let state_dir = claude_dir.map(|path| path.join("managed-local/opencode-server"));
    crate::opencode_control::stop_server_bridge_at(session_id, state_dir.as_deref())
}

pub fn attach(
    session_id: &str,
    opencode_bin: Option<String>,
    claude_dir: Option<PathBuf>,
) -> Result<i32> {
    let state_dir = state_dir(claude_dir.as_deref())?;
    let state_path = state_dir.join(format!(
        "{}.json",
        normalize_uuid(session_id, "session_id")?
    ));
    let state = crate::opencode_control::read_for_bridge(session_id, Some(&state_dir))?;
    let binary = resolve_binary(opencode_bin)?;
    let runtime = tokio::runtime::Runtime::new()?;
    runtime.block_on(assert_health(&state.server_url, &state.password))?;
    let mut child = Command::new(binary)
        .args([
            "attach",
            &state.server_url,
            "--session",
            &state.provider_session_id,
        ])
        .current_dir(&state.cwd)
        .env("OPENCODE_SERVER_USERNAME", &state.username)
        .env("OPENCODE_SERVER_PASSWORD", &state.password)
        .spawn()
        .context("attach stock OpenCode TUI")?;
    let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
    let monitor_server_url = state.server_url.clone();
    let monitor_username = state.username.clone();
    let monitor_password = state.password.clone();
    let monitor_cwd = state.cwd.clone();
    let monitor_session_id = normalize_uuid(session_id, "session_id")?;
    let monitor = thread::spawn(move || {
        monitor_opencode_session_rollovers(
            &monitor_server_url,
            &monitor_username,
            &monitor_password,
            &monitor_cwd,
            &state_path,
            &monitor_session_id,
            stop_rx,
        )
    });
    let status = child.wait().context("wait for stock OpenCode TUI")?;
    let _ = stop_tx.send(true);
    match monitor.join() {
        Ok(Ok(())) => {}
        Ok(Err(error)) => {
            tracing::warn!(error = %error, "OpenCode session rollover monitor stopped with an error")
        }
        Err(_) => tracing::warn!("OpenCode session rollover monitor panicked"),
    }
    Ok(status.code().unwrap_or(1))
}

fn monitor_opencode_session_rollovers(
    server_url: &str,
    username: &str,
    password: &str,
    expected_directory: &str,
    state_path: &Path,
    longhouse_session_id: &str,
    mut stop: tokio::sync::watch::Receiver<bool>,
) -> Result<()> {
    let runtime = tokio::runtime::Runtime::new()?;
    runtime.block_on(async {
        while !*stop.borrow() {
            if let Err(error) = monitor_opencode_events_once(
                server_url,
                username,
                password,
                expected_directory,
                state_path,
                longhouse_session_id,
                &mut stop,
            )
            .await
            {
                if *stop.borrow() {
                    break;
                }
                tracing::warn!(error = %error, "OpenCode event monitor disconnected; retrying")
            }
            if !*stop.borrow() {
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(250)) => {}
                    changed = stop.changed() => {
                        if changed.is_err() || *stop.borrow() {
                            break;
                        }
                    }
                }
            }
        }
        Ok(())
    })
}

async fn monitor_opencode_events_once(
    server_url: &str,
    username: &str,
    password: &str,
    expected_directory: &str,
    state_path: &Path,
    longhouse_session_id: &str,
    stop: &mut tokio::sync::watch::Receiver<bool>,
) -> Result<()> {
    let mut response = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(5))
        .build()?
        .get(format!("{server_url}/global/event"))
        .basic_auth(username, Some(password))
        .send()
        .await?;
    if !response.status().is_success() {
        bail!("OpenCode event stream failed ({})", response.status());
    }
    let mut pending = String::new();
    while !*stop.borrow() {
        let chunk = tokio::select! {
            chunk = response.chunk() => chunk?,
            changed = stop.changed() => {
                if changed.is_err() || *stop.borrow() {
                    return Ok(());
                }
                continue;
            },
        };
        let Some(chunk) = chunk else {
            break;
        };
        pending.push_str(&String::from_utf8_lossy(&chunk));
        while let Some((boundary, delimiter_len)) = sse_frame_boundary(&pending) {
            let frame = pending[..boundary].to_string();
            pending.drain(..boundary + delimiter_len);
            for line in frame.lines() {
                let Some(data) = line.strip_prefix("data:") else {
                    continue;
                };
                let Ok(event) = serde_json::from_str::<Value>(data.trim()) else {
                    continue;
                };
                if !event_matches_directory(&event, expected_directory) {
                    continue;
                }
                if let Some(provider_session_id) = top_level_created_session_id(&event) {
                    update_provider_session_id(
                        state_path,
                        longhouse_session_id,
                        provider_session_id,
                        Some(&provider_binding_path(longhouse_session_id)?),
                    )?;
                }
                if let Some(provider_session_id) = idle_session_id(&event) {
                    let longhouse_session_id = longhouse_session_id.to_string();
                    let provider_session_id = provider_session_id.to_string();
                    tokio::spawn(async move {
                        wake_transcript_shipper(&longhouse_session_id, &provider_session_id).await;
                    });
                }
            }
        }
    }
    Ok(())
}

fn sse_frame_boundary(pending: &str) -> Option<(usize, usize)> {
    let lf = pending.find("\n\n").map(|offset| (offset, 2));
    let crlf = pending.find("\r\n\r\n").map(|offset| (offset, 4));
    match (lf, crlf) {
        (Some(left), Some(right)) => Some(if left.0 <= right.0 { left } else { right }),
        (Some(boundary), None) | (None, Some(boundary)) => Some(boundary),
        (None, None) => None,
    }
}

fn top_level_created_session_id(event: &Value) -> Option<&str> {
    let payload = event.get("payload")?;
    if payload.get("type").and_then(Value::as_str) != Some("session.created") {
        return None;
    }
    let properties = payload.get("properties")?;
    let info = properties.get("info")?;
    if info
        .get("parentID")
        .and_then(Value::as_str)
        .is_some_and(|value| !value.trim().is_empty())
    {
        return None;
    }
    properties
        .get("sessionID")
        .and_then(Value::as_str)
        .or_else(|| info.get("id").and_then(Value::as_str))
        .filter(|value| !value.trim().is_empty())
}

fn idle_session_id(event: &Value) -> Option<&str> {
    let payload = event.get("payload")?;
    let event_type = payload.get("type").and_then(Value::as_str)?;
    let properties = payload.get("properties")?;
    let idle = event_type == "session.idle"
        || (event_type == "session.status"
            && properties
                .get("status")
                .and_then(|status| status.get("type"))
                .and_then(Value::as_str)
                == Some("idle"));
    idle.then(|| properties.get("sessionID").and_then(Value::as_str))
        .flatten()
        .filter(|value| !value.trim().is_empty())
}

#[cfg(unix)]
async fn wake_transcript_shipper(longhouse_session_id: &str, provider_session_id: &str) {
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else {
        return;
    };
    let database_path = opencode_database_path(&home);
    if !database_path.is_file() {
        return;
    }
    let Ok(socket_path) = crate::config::get_agent_transcript_wake_socket_path() else {
        return;
    };
    if !socket_path.exists() {
        return;
    }
    let _ = send_transcript_wake(
        &socket_path,
        &database_path,
        longhouse_session_id,
        provider_session_id,
    )
    .await;
}

fn opencode_database_path(home: &Path) -> PathBuf {
    home.join(".local/share/opencode/opencode.db")
}

#[cfg(unix)]
async fn send_transcript_wake(
    socket_path: &Path,
    database_path: &Path,
    longhouse_session_id: &str,
    provider_session_id: &str,
) -> std::io::Result<()> {
    let file_len_hint = database_path.metadata().ok().map(|value| value.len());
    let payload = json!({
        "provider": "opencode",
        "path": database_path,
        "phase": "idle",
        "session_id": longhouse_session_id,
        "turn_id": provider_session_id,
        "wake_reason": "turn_completed",
        "observed_at_ms": chrono::Utc::now().timestamp_millis(),
        "file_len_hint": file_len_hint,
    })
    .to_string()
    .into_bytes();
    let mut stream = tokio::time::timeout(
        Duration::from_millis(75),
        tokio::net::UnixStream::connect(socket_path),
    )
    .await
    .map_err(|_| {
        std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "transcript wake connect timed out",
        )
    })??;
    tokio::time::timeout(Duration::from_millis(75), stream.write_all(&payload))
        .await
        .map_err(|_| {
            std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "transcript wake write timed out",
            )
        })??;
    Ok(())
}

#[cfg(not(unix))]
async fn wake_transcript_shipper(_longhouse_session_id: &str, _provider_session_id: &str) {}

fn update_provider_session_id(
    state_path: &Path,
    longhouse_session_id: &str,
    provider_session_id: &str,
    binding_path: Option<&Path>,
) -> Result<()> {
    let raw = fs::read(state_path)
        .with_context(|| format!("read OpenCode bridge state {}", state_path.display()))?;
    let mut state: Value = serde_json::from_slice(&raw)?;
    if state.get("session_id").and_then(Value::as_str) != Some(longhouse_session_id) {
        bail!("OpenCode bridge state changed ownership while attached");
    }
    if state.get("provider_session_id").and_then(Value::as_str) == Some(provider_session_id) {
        return Ok(());
    }
    if let Some(previous) = state
        .get("provider_session_id")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_string)
    {
        let history = state
            .as_object_mut()
            .and_then(|object| {
                object
                    .entry("previous_provider_session_ids")
                    .or_insert_with(|| json!([]))
                    .as_array_mut()
            })
            .context("OpenCode bridge provider identity history is invalid")?;
        if !history
            .iter()
            .any(|value| value.as_str() == Some(&previous))
        {
            history.push(Value::String(previous));
        }
    }
    state["provider_session_id"] = Value::String(provider_session_id.to_string());
    state["updated_at"] = Value::String(chrono::Utc::now().to_rfc3339());
    write_private_json(state_path, &state)?;
    if let Some(path) = binding_path {
        write_provider_binding(path, &state)?;
    }
    Ok(())
}

fn provider_binding_path(longhouse_session_id: &str) -> Result<PathBuf> {
    Ok(crate::config::get_longhouse_home()?
        .join("managed-local/opencode/bridge/sessions")
        .join(format!("{longhouse_session_id}.json")))
}

fn write_provider_binding(path: &Path, state: &Value) -> Result<()> {
    let longhouse_session_id = state
        .get("session_id")
        .and_then(Value::as_str)
        .context("OpenCode bridge state has no Longhouse session id")?;
    let provider_session_id = state
        .get("provider_session_id")
        .and_then(Value::as_str)
        .context("OpenCode bridge state has no provider session id")?;
    write_private_json(
        path,
        &json!({
            "schema_version": 1,
            "provider": "opencode",
            "adapter": "opencode_server_bridge",
            "longhouse_session_id": longhouse_session_id,
            "provider_session_id": provider_session_id,
            "previous_provider_session_ids": state
                .get("previous_provider_session_ids")
                .cloned()
                .unwrap_or_else(|| json!([])),
            "cwd": state.get("cwd").cloned().unwrap_or(Value::Null),
            "updated_at": state.get("updated_at").cloned().unwrap_or(Value::Null),
        }),
    )
}

fn paths_resolve_equal(left: &str, right: &str) -> bool {
    match (fs::canonicalize(left), fs::canonicalize(right)) {
        (Ok(left), Ok(right)) => left == right,
        _ => Path::new(left) == Path::new(right),
    }
}

fn event_matches_directory(event: &Value, expected_directory: &str) -> bool {
    event
        .get("directory")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .is_some_and(|value| paths_resolve_equal(value, expected_directory))
}

fn state_dir(claude_dir: Option<&Path>) -> Result<PathBuf> {
    match claude_dir {
        Some(path) => Ok(path.join("managed-local/opencode-server")),
        None => crate::managed_opencode_scan::default_opencode_server_state_dir()
            .context("could not resolve OpenCode provider state directory"),
    }
}

fn normalize_uuid(value: &str, name: &str) -> Result<String> {
    Uuid::parse_str(value.trim())
        .with_context(|| format!("{name} must be a UUID"))
        .map(|id| id.to_string())
}

fn resolve_binary(explicit: Option<String>) -> Result<String> {
    let candidate = explicit
        .or_else(|| std::env::var("LONGHOUSE_OPENCODE_BIN").ok())
        .unwrap_or_else(|| "opencode".into());
    if candidate.contains('/') {
        let path = PathBuf::from(&candidate);
        if path.is_file() {
            return Ok(path.to_string_lossy().into_owned());
        }
        bail!("OpenCode executable is not a file: {candidate}");
    }
    let path = std::env::var_os("PATH")
        .into_iter()
        .flat_map(|value| std::env::split_paths(&value).collect::<Vec<_>>())
        .map(|dir| dir.join(&candidate))
        .find(|path| path.is_file())
        .context("OpenCode executable not found; install `opencode` or pass --opencode-bin")?;
    Ok(path.to_string_lossy().into_owned())
}

fn read_listen_url(path: &Path) -> Result<Option<String>> {
    let text = fs::read_to_string(path).unwrap_or_default();
    Ok(text
        .lines()
        .rev()
        .find_map(|line| line.split("opencode server listening on ").nth(1))
        .map(str::trim)
        .filter(|url| url.starts_with("http://127.0.0.1:"))
        .map(str::to_owned))
}

fn tail(path: &Path) -> Result<String> {
    let mut text = String::new();
    OpenOptions::new()
        .read(true)
        .open(path)?
        .read_to_string(&mut text)?;
    Ok(text
        .chars()
        .rev()
        .take(2000)
        .collect::<String>()
        .chars()
        .rev()
        .collect())
}

async fn create_session(
    server_url: &str,
    password: &str,
    cwd: &Path,
    title: Option<&str>,
) -> Result<String> {
    let base = Url::parse(server_url)?;
    if base.host_str() != Some("127.0.0.1") {
        bail!("OpenCode server must listen on localhost");
    }
    assert_health(server_url, password).await?;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?;
    let mut session_url = Url::parse(&format!("{server_url}/session"))?;
    session_url
        .query_pairs_mut()
        .append_pair("directory", &cwd.to_string_lossy());
    let response = client.post(session_url)
        .basic_auth(USERNAME, Some(password)).json(&json!({"title": title.unwrap_or_else(|| cwd.file_name().and_then(|v| v.to_str()).unwrap_or("Longhouse"))})).send().await?;
    if !response.status().is_success() {
        bail!("OpenCode session creation failed ({})", response.status());
    }
    response
        .json::<serde_json::Value>()
        .await?
        .get("id")
        .and_then(serde_json::Value::as_str)
        .filter(|id| !id.trim().is_empty())
        .map(str::to_owned)
        .context("OpenCode session creation returned no id")
}

async fn assert_health(server_url: &str, password: &str) -> Result<()> {
    let health = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?
        .get(format!("{server_url}/global/health"))
        .basic_auth(USERNAME, Some(password))
        .send()
        .await?;
    if !health.status().is_success() {
        bail!("OpenCode server health check failed ({})", health.status());
    }
    if health
        .json::<serde_json::Value>()
        .await?
        .get("healthy")
        .and_then(serde_json::Value::as_bool)
        != Some(true)
    {
        bail!("OpenCode server health check did not report healthy");
    }
    Ok(())
}

fn write_private_json(path: &Path, payload: &serde_json::Value) -> Result<()> {
    let parent = path.parent().context("OpenCode state has no parent")?;
    fs::create_dir_all(parent)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
    }
    let temporary = path.with_extension(format!("json.tmp.{}", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(format!("{}\n", serde_json::to_string_pretty(payload)?).as_bytes())?;
    file.sync_all()?;
    drop(file);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))?;
    }
    fs::rename(temporary, path)?;
    Ok(())
}

fn process_identity(pid: u32) -> Option<(String, String)> {
    let output = Command::new("ps")
        .args(["-o", "lstart=,command=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    let line = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    (line.len() > 24).then(|| (line[..24].trim().to_owned(), line[24..].trim().to_owned()))
}

fn stop_pid(pid: u32) -> Result<()> {
    #[cfg(unix)]
    {
        if unsafe { libc::killpg(pid as i32, libc::SIGTERM) } == 0 {
            return Ok(());
        }
    }
    if unsafe { libc::kill(pid as i32, libc::SIGTERM) } == 0 {
        Ok(())
    } else {
        Ok(())
    }
}

fn pid_alive(pid: u32) -> bool {
    pid > 0 && unsafe { libc::kill(pid as i32, 0) == 0 }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn bridge_state_is_private() {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("private/state.json");
        write_private_json(&path, &json!({"password":"secret"})).unwrap();
        assert_eq!(
            fs::metadata(path.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[test]
    fn rejects_non_local_listen_urls() {
        assert!(read_listen_url(Path::new("/definitely/not/there"))
            .unwrap()
            .is_none());
    }

    #[test]
    fn mcp_config_scopes_coordination_authority_to_the_server() {
        let config = opencode_mcp_config(
            Path::new("/opt/longhouse-engine"),
            "11111111-1111-4111-8111-111111111111",
            "session-secret",
        );
        let server = &config["mcp"]["longhouse"];
        assert_eq!(server["command"][0], "/opt/longhouse-engine");
        assert_eq!(server["command"][1], "claude-channel");
        assert_eq!(server["command"][2], "serve");
        assert_eq!(
            server["environment"]["LONGHOUSE_COORDINATION_TOKEN"],
            "session-secret"
        );
        assert_eq!(
            server["environment"]["LONGHOUSE_MANAGED_SESSION_ID"],
            "11111111-1111-4111-8111-111111111111"
        );

        let mut command = Command::new("opencode");
        command.env("LONGHOUSE_COORDINATION_TOKEN", "ambient-parent-secret");
        configure_opencode_environment(
            &mut command,
            "11111111-1111-4111-8111-111111111111",
            "server-password",
            &config,
        )
        .unwrap();
        let envs = command
            .get_envs()
            .map(|(key, value)| {
                (
                    key.to_string_lossy().into_owned(),
                    value.map(|item| item.to_string_lossy().into_owned()),
                )
            })
            .collect::<std::collections::HashMap<_, _>>();
        assert_eq!(envs["LONGHOUSE_COORDINATION_TOKEN"], None);
        let embedded: serde_json::Value =
            serde_json::from_str(envs["OPENCODE_CONFIG_CONTENT"].as_deref().unwrap()).unwrap();
        assert_eq!(
            embedded["mcp"]["longhouse"]["environment"]["LONGHOUSE_COORDINATION_TOKEN"],
            "session-secret"
        );
    }

    #[test]
    fn extracts_only_top_level_created_sessions() {
        let top_level = json!({
            "payload": {
                "type": "session.created",
                "properties": {
                    "sessionID": "ses_new",
                    "info": {"id": "ses_new"}
                }
            }
        });
        let child = json!({
            "payload": {
                "type": "session.created",
                "properties": {
                    "sessionID": "ses_child",
                    "info": {"id": "ses_child", "parentID": "ses_parent"}
                }
            }
        });
        assert_eq!(top_level_created_session_id(&top_level), Some("ses_new"));
        assert_eq!(top_level_created_session_id(&child), None);
        assert_eq!(
            top_level_created_session_id(&json!({"payload": {"type": "session.updated"}})),
            None
        );
    }

    #[test]
    fn extracts_idle_sessions_from_current_and_legacy_events() {
        let legacy = json!({
            "payload": {
                "type": "session.idle",
                "properties": {"sessionID": "ses_legacy"}
            }
        });
        let current = json!({
            "payload": {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_current",
                    "status": {"type": "idle"}
                }
            }
        });
        let busy = json!({
            "payload": {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_busy",
                    "status": {"type": "busy"}
                }
            }
        });
        assert_eq!(idle_session_id(&legacy), Some("ses_legacy"));
        assert_eq!(idle_session_id(&current), Some("ses_current"));
        assert_eq!(idle_session_id(&busy), None);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn emits_turn_completed_wake_for_shared_opencode_database() {
        use tokio::io::AsyncReadExt;

        let temp = tempfile::tempdir().unwrap();
        let database_path = opencode_database_path(temp.path());
        fs::create_dir_all(database_path.parent().unwrap()).unwrap();
        fs::write(&database_path, b"opencode-db").unwrap();
        let socket_path = temp.path().join("wake.sock");
        let listener = tokio::net::UnixListener::bind(&socket_path).unwrap();

        let send = tokio::spawn({
            let socket_path = socket_path.clone();
            let database_path = database_path.clone();
            async move {
                send_transcript_wake(
                    &socket_path,
                    &database_path,
                    "longhouse-session",
                    "ses_after_reset",
                )
                .await
            }
        });
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut raw = Vec::new();
        stream.read_to_end(&mut raw).await.unwrap();
        send.await.unwrap().unwrap();

        let payload: Value = serde_json::from_slice(&raw).unwrap();
        assert_eq!(payload["provider"], "opencode");
        assert_eq!(payload["path"], database_path.to_string_lossy().as_ref());
        assert_eq!(payload["phase"], "idle");
        assert_eq!(payload["session_id"], "longhouse-session");
        assert_eq!(payload["turn_id"], "ses_after_reset");
        assert_eq!(payload["wake_reason"], "turn_completed");
        assert_eq!(payload["file_len_hint"], 11);
    }

    #[test]
    fn updates_active_provider_session_without_replacing_bridge_identity() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("state.json");
        write_private_json(
            &path,
            &json!({
                "session_id": "11111111-1111-4111-8111-111111111111",
                "provider_session_id": "ses_old",
                "password": "secret",
                "updated_at": "before"
            }),
        )
        .unwrap();

        let binding = temp.path().join("binding.json");
        update_provider_session_id(
            &path,
            "11111111-1111-4111-8111-111111111111",
            "ses_new",
            Some(&binding),
        )
        .unwrap();

        let state: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(state["provider_session_id"], "ses_new");
        assert_eq!(state["previous_provider_session_ids"], json!(["ses_old"]));
        assert_eq!(state["password"], "secret");
        assert_ne!(state["updated_at"], "before");
        let binding: Value = serde_json::from_slice(&fs::read(binding).unwrap()).unwrap();
        assert_eq!(binding["provider_session_id"], "ses_new");
        assert_eq!(binding["previous_provider_session_ids"], json!(["ses_old"]));
        assert!(binding.get("password").is_none());
    }

    #[test]
    fn accepts_lf_and_crlf_sse_boundaries() {
        assert_eq!(sse_frame_boundary("data: one\n\nnext"), Some((9, 2)));
        assert_eq!(sse_frame_boundary("data: one\r\n\r\nnext"), Some((9, 4)));
    }

    #[test]
    fn global_events_require_the_managed_workspace() {
        let temp = tempfile::tempdir().unwrap();
        let expected = temp.path().to_string_lossy();

        assert!(event_matches_directory(
            &json!({"directory": expected.as_ref()}),
            expected.as_ref()
        ));
        assert!(!event_matches_directory(
            &json!({"directory": "/different"}),
            expected.as_ref()
        ));
        assert!(!event_matches_directory(&json!({}), expected.as_ref()));
    }
}
