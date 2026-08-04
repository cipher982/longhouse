//! Native terminal owner for Cursor Helm.
//!
//! This module deliberately owns only the PTY and its on-disk control contract.
//! Machine Agent control remains in `cursor_helm_control`.

use std::ffi::CString;
use std::fs;
use std::io::{IsTerminal, Read, Write};
use std::os::fd::RawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread;
use std::{collections::BTreeMap, time::Duration};

use anyhow::Context;
use chrono::Utc;
use serde_json::{json, Value};
use sha2::Digest;
use uuid::Uuid;

use crate::managed_launch_lifecycle::{
    register_managed_launch_with_timeout, spawn_managed_launch_registration_retry,
    ManagedLaunchRegistrationRetry, ManagedLaunchResponse,
};

const STATE_DIR: &str = "managed-local/cursor-helm";

pub struct LaunchConfig {
    pub cwd: PathBuf,
    pub project: Option<String>,
    pub name: Option<String>,
    pub loop_mode: String,
    pub url: Option<String>,
    pub token: Option<String>,
    pub resume_session: Option<String>,
    pub cursor_bin: Option<String>,
    pub config_dir: Option<PathBuf>,
    pub permission_mode: Option<String>,
    pub verbose: bool,
    pub open: bool,
    pub cursor_args: Vec<String>,
}

fn home(config_dir: Option<&Path>) -> anyhow::Result<PathBuf> {
    Ok(config_dir
        .map(Path::to_path_buf)
        .or_else(|| std::env::var_os("LONGHOUSE_HOME").map(PathBuf::from))
        .unwrap_or(
            PathBuf::from(std::env::var("HOME").context("HOME not set")?).join(".longhouse"),
        ))
}
fn state_dir(config_dir: Option<&Path>) -> anyhow::Result<PathBuf> {
    Ok(home(config_dir)?.join(STATE_DIR))
}
fn socket_path(session_id: &str) -> anyhow::Result<PathBuf> {
    // macOS's per-user temporary directory and deeply nested HOME paths can
    // exceed sockaddr_un::sun_path. A private short directory keeps the wire
    // contract absolute while preserving same-user-only socket access.
    let dir =
        PathBuf::from("/tmp").join(format!("longhouse-cursor-{}", unsafe { libc::geteuid() }));
    fs::create_dir_all(&dir)?;
    fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))?;
    Ok(dir.join(format!("{session_id}.sock")))
}
fn write_json(path: &Path, value: &Value) -> anyhow::Result<()> {
    fs::create_dir_all(path.parent().context("state path has no parent")?)?;
    let tmp = path.with_extension(format!("json.tmp.{}", std::process::id()));
    fs::write(&tmp, serde_json::to_vec_pretty(value)?)?;
    fs::rename(tmp, path)?;
    Ok(())
}
fn process_start_time(pid: libc::pid_t) -> Option<String> {
    std::process::Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "lstart="])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}
fn phase(dir: &Path, session_id: &str, conversation: &str, launch_id: &str) -> Option<Value> {
    let value: Value =
        serde_json::from_slice(&fs::read(dir.join(format!("{session_id}.phase.json"))).ok()?)
            .ok()?;
    let active_conversation = fs::read(
        dir.join("binding-probes")
            .join(format!("{session_id}.json")),
    )
    .ok()
    .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
    .and_then(|claim| {
        (claim.get("launch_id")?.as_str()? == launch_id)
            .then(|| claim.get("conversation_uuid")?.as_str().map(str::to_string))
            .flatten()
    })
    .unwrap_or_else(|| conversation.to_string());
    (value.get("session_id")?.as_str()? == session_id
        && value.get("conversation_id")?.as_str()? == active_conversation
        && value.get("launch_id")?.as_str()? == launch_id)
        .then_some(value)
}
struct ResumeClaim {
    conversation: String,
    permission_mode: String,
}

fn normalize_permission_mode(value: &str) -> anyhow::Result<String> {
    match value.trim().to_ascii_lowercase().replace('-', "_").as_str() {
        "auto_approve" => Ok("auto_approve".into()),
        "provider_local" | "bypass" => Ok("provider_local".into()),
        "remote_approve" | "remote_human" => Ok("remote_human".into()),
        _ => anyhow::bail!("invalid --permission-mode"),
    }
}

fn permission_wire_mode(value: &str) -> &'static str {
    if value == "remote_human" {
        "remote_approve"
    } else if value == "provider_local" {
        "provider_local"
    } else {
        "bypass"
    }
}

fn read_claim(
    dir: &Path,
    session_id: &str,
    requested_permission_mode: Option<&str>,
    requested_cwd: Option<&Path>,
    cursor_bin: Option<&str>,
) -> anyhow::Result<ResumeClaim> {
    Uuid::parse_str(session_id).context("--resume-session must be a Longhouse session UUID")?;
    let value: Value = serde_json::from_slice(&fs::read(
        dir.join("binding-probes")
            .join(format!("{session_id}.json")),
    )?)?;
    if value.get("schema_version").and_then(Value::as_i64) != Some(2)
        || value.get("provider").and_then(Value::as_str) != Some("cursor")
        || value.get("session_id").and_then(Value::as_str) != Some(session_id)
        || value.get("status").and_then(Value::as_str) != Some("observed")
    {
        anyhow::bail!("no observed native Cursor identity claim exists for this Longhouse session");
    }
    let conversation = value
        .get("conversation_uuid")
        .and_then(Value::as_str)
        .filter(|s| Uuid::parse_str(s).is_ok())
        .map(str::to_owned)
        .context("no valid Cursor identity claim exists for this Longhouse session")?;
    let recorded = match value.get("permission_policy").and_then(Value::as_str) {
        Some(value) => normalize_permission_mode(value)?,
        None => "provider_local".into(),
    };
    if let Some(requested) = requested_permission_mode {
        let requested = normalize_permission_mode(requested)?;
        if requested != recorded {
            anyhow::bail!("resume policy conflict: session uses {recorded}, requested {requested}");
        }
    }
    if let Some(requested_cwd) = requested_cwd {
        let recorded_cwd = value
            .get("cwd")
            .and_then(Value::as_str)
            .map(PathBuf::from)
            .context("Cursor Resume is unavailable because its retained workspace is missing")?;
        if fs::canonicalize(recorded_cwd).ok() != fs::canonicalize(requested_cwd).ok() {
            anyhow::bail!("Cursor Resume must run from its retained workspace");
        }
    }
    if let Some(cursor_bin) = cursor_bin {
        let recorded_binary = value
            .get("provider_binary")
            .and_then(Value::as_str)
            .context(
                "Cursor Resume is unavailable because its retained provider binary is missing",
            )?;
        if fs::canonicalize(recorded_binary).ok() != fs::canonicalize(cursor_bin).ok() {
            anyhow::bail!("Cursor provider binary changed since this session ended");
        }
    }
    Ok(ResumeClaim {
        conversation,
        permission_mode: recorded,
    })
}

fn claim_path(dir: &Path, session_id: &str) -> PathBuf {
    dir.join("binding-probes")
        .join(format!("{session_id}.json"))
}

fn claim_backup_path(dir: &Path, session_id: &str) -> PathBuf {
    dir.join("binding-probes")
        .join(format!("{session_id}.observed-backup.json"))
}

fn write_pending_claim(
    dir: &Path,
    session_id: &str,
    conversation: &str,
    launch_id: &str,
    permission_mode: &str,
    registration: Option<&ManagedLaunchResponse>,
    cwd: &Path,
    cursor_bin: &str,
) -> anyhow::Result<()> {
    let target = claim_path(dir, session_id);
    let backup = claim_backup_path(dir, session_id);
    if fs::read(&target)
        .ok()
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
        .and_then(|value| {
            value
                .get("status")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .as_deref()
        == Some("observed")
    {
        let current: Value = serde_json::from_slice(&fs::read(&target)?)?;
        write_json(&backup, &current)?;
    }
    write_json(
        &target,
        &json!({
            "schema_version": 2,
            "provider": "cursor",
            "status": "pending",
            "session_id": session_id,
            "conversation_uuid": conversation,
            "launch_id": launch_id,
            "permission_policy": permission_mode,
            "cwd": cwd,
            "provider_binary": cursor_bin,
            "run_id": registration.map(|value| value.run_id.as_str()),
            "expires_at": (Utc::now() + chrono::Duration::minutes(10)).to_rfc3339(),
        }),
    )
}

fn rollback_pending_claim(dir: &Path, session_id: &str, launch_id: &str) {
    let target = claim_path(dir, session_id);
    let backup = claim_backup_path(dir, session_id);
    let pending_matches = fs::read(&target)
        .ok()
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
        .is_some_and(|claim| {
            claim.get("status").and_then(Value::as_str) == Some("pending")
                && claim.get("launch_id").and_then(Value::as_str) == Some(launch_id)
        });
    if !pending_matches {
        return;
    }
    let observed_backup = fs::read(&backup)
        .ok()
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
        .is_some_and(|claim| claim.get("status").and_then(Value::as_str) == Some("observed"));
    if observed_backup {
        let _ = fs::rename(&backup, &target);
    } else {
        let _ = fs::remove_file(&target);
        let _ = fs::remove_file(&backup);
    }
}

struct LaunchArtifacts {
    dir: PathBuf,
    socket: PathBuf,
    session_id: String,
    launch_id: String,
}

impl Drop for LaunchArtifacts {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.socket);
        let _ = fs::remove_file(self.dir.join(format!("{}.json", self.session_id)));
        let _ = fs::remove_file(self.dir.join(format!("{}.phase.json", self.session_id)));
        rollback_pending_claim(&self.dir, &self.session_id, &self.launch_id);
    }
}
fn resolve_bin(explicit: Option<String>) -> anyhow::Result<String> {
    let value = explicit
        .or_else(|| std::env::var("LONGHOUSE_CURSOR_BIN").ok())
        .unwrap_or_else(|| "cursor-agent".into());
    if value.contains('/') {
        return Path::new(&value)
            .is_file()
            .then_some(value)
            .context("--cursor-bin is not an executable file");
    }
    for path in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        let candidate = path.join(&value);
        if candidate.is_file() {
            return Ok(candidate.display().to_string());
        }
    }
    anyhow::bail!("cursor-agent executable not found. Install Cursor's CLI or set --cursor-bin.")
}
fn cursor_chat(bin: &str, cwd: &Path) -> anyhow::Result<String> {
    let output = std::process::Command::new(bin)
        .arg("create-chat")
        .current_dir(cwd)
        .output()?;
    if !output.status.success() {
        anyhow::bail!(
            "cursor-agent create-chat failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let id = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    Uuid::parse_str(&id).context("cursor-agent create-chat returned invalid id")?;
    Ok(id)
}
fn raw(fd: RawFd) -> anyhow::Result<libc::termios> {
    unsafe {
        let mut saved = std::mem::zeroed();
        if libc::tcgetattr(fd, &mut saved) != 0 {
            anyhow::bail!("read terminal state failed")
        };
        let mut next = saved;
        libc::cfmakeraw(&mut next);
        if libc::tcsetattr(fd, libc::TCSADRAIN, &next) != 0 {
            anyhow::bail!("set terminal raw mode failed")
        };
        Ok(saved)
    }
}
struct Terminal(RawFd, libc::termios);
impl Drop for Terminal {
    fn drop(&mut self) {
        unsafe {
            libc::tcsetattr(self.0, libc::TCSADRAIN, &self.1);
        }
    }
}
fn sync_winsize(master: RawFd) {
    copy_winsize(1, master);
}
fn copy_winsize(source: RawFd, target: RawFd) -> bool {
    unsafe {
        let mut size: libc::winsize = std::mem::zeroed();
        if libc::ioctl(source, libc::TIOCGWINSZ, &mut size) == 0 {
            return libc::ioctl(target, libc::TIOCSWINSZ, &size) == 0;
        }
    }
    false
}
fn terminal_winsize(fd: RawFd) -> libc::winsize {
    unsafe {
        let mut size: libc::winsize = std::mem::zeroed();
        if libc::ioctl(fd, libc::TIOCGWINSZ, &mut size) != 0 || size.ws_row == 0 || size.ws_col == 0
        {
            size.ws_row = 24;
            size.ws_col = 80;
        }
        size
    }
}
fn write_all(fd: RawFd, mut bytes: &[u8]) -> std::io::Result<()> {
    while !bytes.is_empty() {
        let written = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if written > 0 {
            bytes = &bytes[written as usize..];
        } else if written < 0
            && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted
        {
            continue;
        } else {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}
struct CursorMcpConfig {
    path: PathBuf,
    state_path: PathBuf,
    session_id: String,
}

impl Drop for CursorMcpConfig {
    fn drop(&mut self) {
        let Ok(lock) = mcp_config_lock(&self.state_path) else {
            return;
        };
        let _lock = lock;
        let Ok(mut state) = read_mcp_config_state(&self.state_path) else {
            return;
        };
        state.sessions.remove(&self.session_id);
        state.sessions.retain(|_, owner| owner.is_live());
        if state.sessions.is_empty() {
            let _ = restore_mcp_config(&self.path, state.original.as_deref());
            let _ = fs::remove_file(&self.state_path);
        } else {
            let _ = write_json(
                &self.state_path,
                &serde_json::to_value(state).unwrap_or_default(),
            );
        }
    }
}

#[derive(serde::Deserialize, serde::Serialize)]
struct CursorMcpConfigOwner {
    pid: libc::pid_t,
    process_start_time: String,
}

impl CursorMcpConfigOwner {
    fn is_live(&self) -> bool {
        process_start_time(self.pid).as_deref() == Some(&self.process_start_time)
    }
}

#[derive(serde::Deserialize, serde::Serialize)]
struct CursorMcpConfigState {
    original: Option<String>,
    sessions: BTreeMap<String, CursorMcpConfigOwner>,
}

fn coordination_token(
    config: &LaunchConfig,
    registration: Option<&ManagedLaunchResponse>,
    session_id: &str,
) -> anyhow::Result<Option<String>> {
    if let Some(token) = registration.and_then(ManagedLaunchResponse::coordination_token) {
        return Ok(Some(token.to_owned()));
    }
    if config.resume_session.is_none() {
        return Ok(None);
    }
    let machine = home(config.config_dir.as_deref())?.join("machine");
    let state: Value = serde_json::from_slice(&fs::read(machine.join("state.json"))?)?;
    let url = config
        .url
        .as_deref()
        .or_else(|| state.get("runtime_url").and_then(Value::as_str))
        .filter(|value| !value.trim().is_empty())
        .context("No Longhouse URL configured. Run `longhouse auth` first.")?;
    let device_token = config
        .token
        .as_deref()
        .map(str::to_owned)
        .or_else(|| {
            fs::read_to_string(machine.join("device-token"))
                .ok()
                .map(|value| value.trim().to_owned())
                .filter(|value| !value.is_empty())
        })
        .context("No device token found. Run `longhouse auth` first.")?;
    let endpoint = format!(
        "{}/api/agents/sessions/{session_id}/coordination-token",
        url.trim_end_matches('/')
    );
    tokio::runtime::Runtime::new()?.block_on(async {
        let response = reqwest::Client::new()
            .post(endpoint)
            .header("X-Agents-Token", device_token)
            .send()
            .await?;
        if !response.status().is_success() {
            anyhow::bail!(
                "Could not issue coordination authority for session {session_id}: HTTP {}",
                response.status()
            );
        }
        let payload: Value = response.json().await?;
        payload
            .get("coordination_token")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .map(Some)
            .context("Longhouse returned empty coordination authority")
    })
}

fn write_cursor_mcp_config(
    state_root: &Path,
    cwd: &Path,
    session_id: &str,
) -> anyhow::Result<CursorMcpConfig> {
    let path = cwd.join(".cursor/mcp.json");
    let state_path = state_root.join("mcp-configs").join(format!(
        "{:x}.json",
        sha2::Sha256::digest(cwd.as_os_str().as_bytes())
    ));
    let lock = mcp_config_lock(&state_path)?;
    let _lock = lock;
    let mut state = match read_mcp_config_state(&state_path) {
        Ok(mut state) => {
            state.sessions.retain(|_, owner| owner.is_live());
            if state.sessions.is_empty() {
                restore_mcp_config(&path, state.original.as_deref())?;
                fs::remove_file(&state_path)?;
                new_mcp_config_state(&path)?
            } else {
                state
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => new_mcp_config_state(&path)?,
        Err(error) => return Err(error.into()),
    };
    let mut config: Value = state
        .original
        .as_deref()
        .map(serde_json::from_str)
        .transpose()
        .context("Cursor MCP config is not valid JSON")?
        .unwrap_or_else(|| json!({}));
    let servers = config
        .as_object_mut()
        .context("Cursor MCP config must be an object")?
        .entry("mcpServers")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .context("Cursor MCP servers must be an object")?;
    servers.insert(
        "longhouse-coordination".into(),
        json!({
            "command": std::env::current_exe()?,
            "args": ["cursor-helm", "coordination-mcp"],
        }),
    );
    let parent = path.parent().context("Cursor MCP config has no parent")?;
    fs::create_dir_all(parent)?;
    let temporary = path.with_extension(format!("tmp.{}", std::process::id()));
    fs::write(&temporary, serde_json::to_vec_pretty(&config)?)?;
    #[cfg(unix)]
    fs::set_permissions(
        &temporary,
        std::os::unix::fs::PermissionsExt::from_mode(0o600),
    )?;
    fs::rename(&temporary, &path)?;
    state.sessions.insert(
        session_id.into(),
        CursorMcpConfigOwner {
            pid: std::process::id() as libc::pid_t,
            process_start_time: process_start_time(std::process::id() as libc::pid_t)
                .context("could not capture Cursor Helm launcher process identity")?,
        },
    );
    write_json(&state_path, &serde_json::to_value(&state)?)?;
    Ok(CursorMcpConfig {
        path,
        state_path,
        session_id: session_id.into(),
    })
}

fn mcp_config_lock(state_path: &Path) -> anyhow::Result<fs::File> {
    let lock_path = state_path.with_extension("lock");
    fs::create_dir_all(
        lock_path
            .parent()
            .context("Cursor MCP state has no parent")?,
    )?;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .open(lock_path)?;
    fs2_lock(&lock)?;
    Ok(lock)
}

fn read_mcp_config_state(path: &Path) -> std::io::Result<CursorMcpConfigState> {
    serde_json::from_slice(&fs::read(path)?)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
}

fn new_mcp_config_state(path: &Path) -> anyhow::Result<CursorMcpConfigState> {
    let original = match fs::read_to_string(path) {
        Ok(contents) => Some(contents),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(error.into()),
    };
    Ok(CursorMcpConfigState {
        original,
        sessions: BTreeMap::new(),
    })
}

fn restore_mcp_config(path: &Path, original: Option<&str>) -> std::io::Result<()> {
    match original {
        Some(original) => fs::write(path, original),
        None => match fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
        },
    }
}

pub fn serve_coordination_mcp() -> anyhow::Result<()> {
    let session_id = std::env::var("LONGHOUSE_SESSION_ID")
        .or_else(|_| std::env::var("LONGHOUSE_MANAGED_SESSION_ID"))
        .context("Cursor MCP did not inherit a Longhouse session ID")?;
    Uuid::parse_str(&session_id).context("Cursor MCP inherited an invalid Longhouse session ID")?;
    let mut stream = UnixStream::connect(socket_path(&session_id)?)?;
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.write_all(b"{\"kind\":\"coordination-token\"}\n")?;
    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    let payload = serde_json::from_str::<Value>(response.trim())?;
    let token = payload
        .get("coordination_token")
        .and_then(Value::as_str)
        .filter(|token| !token.is_empty())
        .context("Cursor Helm coordination authority is unavailable")?;
    std::env::set_var("LONGHOUSE_COORDINATION_TOKEN", token);
    let runtime = tokio::runtime::Runtime::new()?;
    runtime.block_on(crate::claude_channel_server::run(
        crate::claude_channel_server::ClaudeChannelServeConfig {
            session_id: Some(session_id),
            run_id: None,
            provider_session_id: None,
            state_root: None,
            port: 0,
            auth_token: None,
            claude_pid: None,
            cwd: None,
        },
    ))
}

fn registration_payload(
    config: &LaunchConfig,
    cwd: &Path,
    session_id: &str,
    permission_mode: &str,
    resume_provider_thread_id: Option<&str>,
) -> anyhow::Result<Value> {
    let machine_name = registration_credentials(config)
        .map(|(_, _, machine_name)| machine_name)
        .unwrap_or_else(|_| std::env::var("HOSTNAME").unwrap_or_else(|_| "unknown".into()));
    let mut payload = crate::managed_launch_payload::ManagedLaunchRegistration {
        provider: "cursor",
        cwd,
        project: config.project.as_deref(),
        display_name: config.name.as_deref(),
        loop_mode: &config.loop_mode,
        machine_name: &machine_name,
        permission_mode: match permission_wire_mode(permission_mode) {
            "remote_approve" => crate::managed_launch_payload::PermissionMode::RemoteApprove,
            "provider_local" => crate::managed_launch_payload::PermissionMode::ProviderLocal,
            _ => crate::managed_launch_payload::PermissionMode::Bypass,
        },
        // Cursor mints its own session id before registering, so the Runtime
        // Host must be told which one to bind rather than allocating its own.
        extra: match resume_provider_thread_id {
            Some(provider_thread_id) => vec![
                ("session_id", json!(session_id)),
                ("resume_attempt_id", json!(Uuid::new_v4().to_string())),
                ("provider_thread_id", json!(provider_thread_id)),
            ],
            None => vec![("session_id", json!(session_id))],
        },
    }
    .to_json();
    let (launch_actor, launch_surface) =
        crate::managed_launch_payload::interactive_human_shell_provenance();
    if let Some(actor) = launch_actor {
        payload["launch_actor"] = json!(actor);
    }
    if let Some(surface) = launch_surface {
        payload["launch_surface"] = json!(surface);
    }
    Ok(payload)
}

fn register(
    config: &LaunchConfig,
    cwd: &Path,
    session_id: &str,
    permission_mode: &str,
    resume_provider_thread_id: Option<&str>,
    timeout: Duration,
) -> anyhow::Result<ManagedLaunchResponse> {
    let (url, token, _) = registration_credentials(config)?;
    let payload = registration_payload(
        config,
        cwd,
        session_id,
        permission_mode,
        resume_provider_thread_id,
    )?;
    let runtime = tokio::runtime::Runtime::new()?;
    let response = register_managed_launch_with_timeout(
        &runtime,
        &url,
        &token,
        if resume_provider_thread_id.is_some() {
            "Cursor resume"
        } else {
            "Cursor"
        },
        &payload,
        Some(session_id),
        timeout,
    )?;
    Ok(response)
}

fn registration_credentials(config: &LaunchConfig) -> anyhow::Result<(String, String, String)> {
    let machine = home(config.config_dir.as_deref())?.join("machine");
    let state: Value = fs::read(machine.join("state.json"))
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_else(|| json!({}));
    let url = config.url.clone().or_else(|| {
        state
            .get("runtime_url")
            .and_then(Value::as_str)
            .map(str::to_owned)
    });
    let token = config.token.clone().or_else(|| {
        fs::read_to_string(machine.join("device-token"))
            .ok()
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
    });
    let url = url.context("No Longhouse URL configured. Run `longhouse auth` first.")?;
    let token = token.context("No device token found. Run `longhouse auth` first.")?;
    let machine_name = state
        .get("machine_name")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| std::env::var("HOSTNAME").unwrap_or_else(|_| "unknown".into()));
    Ok((url, token, machine_name))
}
fn enqueue_terminal_event(
    config: &LaunchConfig,
    session_id: &str,
    run_id: Option<&str>,
    exit_code: i32,
    requested_session_end: bool,
) {
    let Ok(root) = home(config.config_dir.as_deref()) else {
        return;
    };
    let Some(run_id) = run_id.filter(|value| !value.trim().is_empty()) else {
        eprintln!(
            "Longhouse warning: Cursor stopped without a run identity; terminal state was not queued"
        );
        return;
    };
    let runtime_key = format!("cursor:{session_id}");
    let device_id = std::env::var("HOSTNAME").ok();
    let event = crate::managed_terminal::ManagedTerminalEvent {
        runtime_key: &runtime_key,
        session_id,
        run_id,
        provider: "cursor",
        managed_transport: crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
        provider_session_id: None,
        device_id: device_id.as_deref(),
        source: "cursor_helm",
        dedupe_prefix: "cursor-helm-terminal",
        terminal_state: if requested_session_end {
            "session_ended"
        } else {
            crate::managed_terminal::terminal_state_for_exit(exit_code)
        },
        terminal_reason: if requested_session_end {
            "remote_terminate"
        } else {
            "provider_exit"
        },
        exit_code: Some(exit_code),
    }
    .to_json();
    if let Err(error) =
        crate::managed_terminal::enqueue(&root.join("agent/runtime-events-outbox"), &event)
    {
        eprintln!("Longhouse warning: could not queue Cursor terminal event: {error:#}");
    }
}
fn response(stream: &mut UnixStream, value: Value) {
    let _ = stream.write_all(format!("{}\n", value).as_bytes());
}
fn serve(
    mut stream: UnixStream,
    master: RawFd,
    child: libc::pid_t,
    stop: &AtomicBool,
    requested_session_end: &AtomicBool,
    pty_lock: &Mutex<()>,
    dir: &Path,
    session_id: &str,
    conversation: &str,
    launch_id: &str,
    coordination_token: Option<&str>,
) {
    // Accepted sockets inherit nonblocking mode on macOS. Read one bounded
    // newline-framed message with a deadline; partial reads are never commands.
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(8)));
    let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(8)));
    let mut bytes = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(count) => {
                bytes.extend_from_slice(&chunk[..count]);
                if bytes.len() > 65_536 {
                    return response(
                        &mut stream,
                        json!({"ok":false,"error":{"code":"bad_request","message":"request too large"}}),
                    );
                }
                if bytes.contains(&b'\n') {
                    break;
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"bad_request","message":"incomplete request"}}),
                );
            }
        }
    }
    let line = bytes.split(|b| *b == b'\n').next().unwrap_or_default();
    let Ok(request) = serde_json::from_slice::<Value>(line) else {
        return response(
            &mut stream,
            json!({"ok":false,"error":{"code":"bad_request","message":"invalid JSON"}}),
        );
    };
    match request.get("kind").and_then(Value::as_str) {
        Some("coordination-token") => match coordination_token {
            Some(token) => response(&mut stream, json!({"ok":true,"coordination_token":token})),
            None => response(
                &mut stream,
                json!({"ok":false,"error":{"code":"session_not_attached","message":"coordination authority is unavailable"}}),
            ),
        },
        Some("send")
            if request
                .get("text")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty()) =>
        {
            if phase(dir, session_id, conversation, launch_id)
                .and_then(|value| {
                    value
                        .get("phase")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
                .as_deref()
                != Some("idle")
            {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"provider_not_idle","message":"Cursor provider is not idle; send was not injected"}}),
                );
            }
            let _hold = pty_lock.lock().unwrap();
            let text = request["text"].as_str().unwrap().as_bytes();
            if let Err(error) = write_all(master, text) {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"session_not_attached","message":error.to_string()}}),
                );
            }
            thread::sleep(std::time::Duration::from_millis(
                std::env::var("LH_CURSOR_HELM_TEXT_SETTLE_MS")
                    .ok()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(300),
            ));
            if let Err(error) = write_all(master, b"\x1b") {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"session_not_attached","message":error.to_string()}}),
                );
            }
            thread::sleep(std::time::Duration::from_millis(
                std::env::var("LH_CURSOR_HELM_ESCAPE_SETTLE_MS")
                    .ok()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(100),
            ));
            if let Err(error) = write_all(master, b"\r") {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"session_not_attached","message":error.to_string()}}),
                );
            }
            response(
                &mut stream,
                json!({"ok":true,"exit_code":0,"stdout":"","stderr":""}),
            );
        }
        Some("interrupt") => {
            let expected = request.get("generation_id").and_then(Value::as_str);
            let generation = phase(dir, session_id, conversation, launch_id)
                .filter(|value| value.get("phase").and_then(Value::as_str) == Some("active"))
                .and_then(|value| {
                    value
                        .get("generation_id")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                });
            if expected.is_none() || generation.as_deref() != expected {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"provider_generation_mismatch","message":"Cursor active generation changed; cancel was not injected"}}),
                );
            }
            let _hold = pty_lock.lock().unwrap();
            if let Err(error) = write_all(master, b"\x03") {
                return response(
                    &mut stream,
                    json!({"ok":false,"error":{"code":"session_not_attached","message":error.to_string()}}),
                );
            }
            response(
                &mut stream,
                json!({"ok":true,"exit_code":0,"stdout":"","stderr":""}),
            );
        }
        Some("terminate") => {
            requested_session_end.store(true, Ordering::Relaxed);
            unsafe {
                libc::kill(child, libc::SIGKILL);
            }
            stop.store(true, Ordering::Relaxed);
            response(
                &mut stream,
                json!({"ok":true,"exit_code":0,"stdout":"","stderr":""}),
            );
        }
        Some("ping") => response(
            &mut stream,
            json!({"ok":true,"exit_code":0,"stdout":"","stderr":""}),
        ),
        _ => response(
            &mut stream,
            json!({"ok":false,"error":{"code":"bad_request","message":"unknown or malformed command"}}),
        ),
    }
}

pub fn launch(config: LaunchConfig) -> anyhow::Result<i32> {
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        anyhow::bail!("longhouse cursor Helm needs an interactive terminal");
    }
    let cwd = fs::canonicalize(&config.cwd)
        .with_context(|| format!("resolve {}", config.cwd.display()))?;
    let bin = resolve_bin(config.cursor_bin.clone())?;
    let dir = state_dir(config.config_dir.as_deref())?;
    let session_id = config
        .resume_session
        .clone()
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let (resume_conversation, permission_mode) = if config.resume_session.is_some() {
        let claim = read_claim(
            &dir,
            &session_id,
            config.permission_mode.as_deref(),
            Some(&cwd),
            Some(&bin),
        )?;
        (Some(claim.conversation), claim.permission_mode)
    } else {
        (
            None,
            normalize_permission_mode(config.permission_mode.as_deref().unwrap_or("auto_approve"))?,
        )
    };
    fs::create_dir_all(&dir)?;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .open(dir.join(format!("{session_id}.lock")))?;
    if fs2_lock(&lock).is_err() {
        anyhow::bail!("Cursor Helm session {session_id} is already attached");
    }
    let launch_id = Uuid::new_v4().to_string();
    let payload = registration_payload(
        &config,
        &cwd,
        &session_id,
        &permission_mode,
        resume_conversation.as_deref(),
    )?;
    let mut degraded_registration: Option<ManagedLaunchRegistrationRetry> = None;
    let registered = if config.resume_session.is_some() {
        register(
            &config,
            &cwd,
            &session_id,
            &permission_mode,
            resume_conversation.as_deref(),
            Duration::from_secs(10),
        )?
    } else {
        match registration_credentials(&config) {
            Ok((url, token, _)) => match register(
                &config,
                &cwd,
                &session_id,
                &permission_mode,
                resume_conversation.as_deref(),
                Duration::from_millis(750),
            ) {
                Ok(response) => response,
                Err(error) => {
                    degraded_registration = Some(spawn_managed_launch_registration_retry(
                        &url,
                        &token,
                        "Cursor",
                        payload.clone(),
                        &session_id,
                        crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                    ));
                    eprintln!(
                        "Longhouse warning: starting Cursor in degraded Helm mode; local provider ownership is active and registration will retry while this Helm wrapper remains alive ({error:#})"
                    );
                    ManagedLaunchResponse::degraded_from_payload(
                        &payload,
                        "Cursor",
                        crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                    )?
                }
            },
            Err(error) => {
                eprintln!(
                    "Longhouse warning: starting Cursor in degraded Helm mode; local provider ownership is active and registration is unavailable ({error:#})"
                );
                ManagedLaunchResponse::degraded_from_payload(
                    &payload,
                    "Cursor",
                    crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                )?
            }
        }
    };
    registered.validate_transport("Cursor", crate::cursor_helm_control::CURSOR_HELM_TRANSPORT)?;
    let lifecycle_credentials = registration_credentials(&config).ok();
    let lifecycle_runtime = tokio::runtime::Runtime::new()?;
    let mut launch_transaction = lifecycle_credentials.as_ref().and_then(|(url, token, _)| {
        degraded_registration.is_none().then(|| {
            crate::managed_launch_lifecycle::ManagedLaunchTransaction::new(
                &lifecycle_runtime,
                url,
                token,
                &registered.session_id,
                &registered.run_id,
            )
        })
    });
    let hook_url = config.url.clone().or_else(|| {
        home(config.config_dir.as_deref()).ok().and_then(|root| {
            fs::read(root.join("machine/state.json"))
                .ok()
                .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
                .and_then(|state| {
                    state
                        .get("runtime_url")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
        })
    });
    if config.verbose {
        eprintln!("Longhouse Cursor session: {session_id}");
        if let Some(url) = hook_url.as_deref() {
            eprintln!(
                "Timeline: {}/sessions/{session_id}",
                url.trim_end_matches('/')
            );
        }
    }
    let conversation = match resume_conversation.as_deref() {
        Some(value) => value.to_owned(),
        None => cursor_chat(&bin, &cwd)?,
    };
    if matches!(permission_mode.as_str(), "remote_human")
        && registered
            .hook_token
            .as_deref()
            .filter(|value| !value.is_empty())
            .is_none()
    {
        anyhow::bail!("Cursor remote approval could not be enforced; Cursor was not launched");
    }
    let coordination_token = coordination_token(&config, Some(&registered), &session_id)?;
    let _mcp_config = write_cursor_mcp_config(&dir, &cwd, &session_id)?;
    write_pending_claim(
        &dir,
        &session_id,
        &conversation,
        &launch_id,
        &permission_mode,
        Some(&registered),
        &cwd,
        &bin,
    )?;
    let socket = match socket_path(&session_id) {
        Ok(socket) => socket,
        Err(error) => {
            rollback_pending_claim(&dir, &session_id, &launch_id);
            return Err(error);
        }
    };
    let _artifacts = LaunchArtifacts {
        dir: dir.clone(),
        socket: socket.clone(),
        session_id: session_id.clone(),
        launch_id: launch_id.clone(),
    };
    let _ = fs::remove_file(&socket);
    let listener = UnixListener::bind(&socket)?;
    fs::set_permissions(&socket, std::os::unix::fs::PermissionsExt::from_mode(0o600))?;
    // Build all exec data before forkpty. The parent is multi-threaded by this
    // point, so the child may only call async-signal-safe libc functions.
    let mut argv = vec![bin.clone(), "--resume".into(), conversation.clone()];
    if permission_mode == "auto_approve" {
        argv.extend(["--force".into(), "--approve-mcps".into()]);
    }
    argv.extend(config.cursor_args.clone());
    let argv: Vec<CString> = argv
        .iter()
        .map(|value| CString::new(value.as_str()))
        .collect::<Result<_, _>>()
        .context("Cursor arguments cannot contain NUL")?;
    let mut env_pairs: Vec<(Vec<u8>, Vec<u8>)> = std::env::vars_os()
        .filter_map(|(key, value)| {
            let key = key.as_os_str().as_bytes().to_vec();
            (!matches!(
                key.as_slice(),
                b"LONGHOUSE_SESSION_ID"
                    | b"LONGHOUSE_CURSOR_LAUNCH_ID"
                    | b"LONGHOUSE_PERMISSION_HOOK_ENABLED"
                    | b"LONGHOUSE_HOOK_URL"
                    | b"LONGHOUSE_HOOK_TOKEN"
                    | b"LONGHOUSE_COORDINATION_TOKEN"
                    | b"LONGHOUSE_MANAGED_SESSION_ID"
            ))
            .then(|| (key, value.as_os_str().as_bytes().to_vec()))
        })
        .collect();
    env_pairs.extend([
        (
            b"LONGHOUSE_SESSION_ID".to_vec(),
            session_id.as_bytes().to_vec(),
        ),
        (
            b"LONGHOUSE_CURSOR_LAUNCH_ID".to_vec(),
            launch_id.as_bytes().to_vec(),
        ),
        (
            b"LONGHOUSE_PERMISSION_HOOK_ENABLED".to_vec(),
            if matches!(permission_mode.as_str(), "remote_human") {
                b"1".to_vec()
            } else {
                b"0".to_vec()
            },
        ),
    ]);
    env_pairs.push((
        b"LONGHOUSE_CURSOR_REGISTRATION_READY".to_vec(),
        b"1".to_vec(),
    ));
    for key in [
        b"CI".as_slice(),
        b"CONTINUOUS_INTEGRATION",
        b"GITHUB_ACTIONS",
        b"GITLAB_CI",
        b"CIRCLECI",
        b"TRAVIS",
        b"BUILDKITE",
        b"TEAMCITY_VERSION",
        b"BUILD_NUMBER",
        b"BUILD_ID",
        b"BITBUCKET_BUILD_NUMBER",
        b"JENKINS_URL",
    ] {
        env_pairs.retain(|(name, _)| name.as_slice() != key);
    }
    let mut size = terminal_winsize(1);
    env_pairs.retain(|(name, _)| !matches!(name.as_slice(), b"LINES" | b"COLUMNS"));
    env_pairs.push((b"LINES".to_vec(), size.ws_row.to_string().into_bytes()));
    env_pairs.push((b"COLUMNS".to_vec(), size.ws_col.to_string().into_bytes()));
    let term_is_usable = env_pairs.iter().any(|(name, value)| {
        name.as_slice() == b"TERM" && !value.is_empty() && value.as_slice() != b"dumb"
    });
    if !term_is_usable {
        env_pairs.retain(|(name, _)| name.as_slice() != b"TERM");
        env_pairs.push((b"TERM".to_vec(), b"xterm-256color".to_vec()));
    }
    if let Some(url) = hook_url.as_deref() {
        env_pairs.push((b"LONGHOUSE_HOOK_URL".to_vec(), url.as_bytes().to_vec()));
    }
    if let Some(token) = registered.hook_token.as_deref() {
        env_pairs.push((b"LONGHOUSE_HOOK_TOKEN".to_vec(), token.as_bytes().to_vec()));
    }
    let env: Vec<CString> = env_pairs
        .into_iter()
        .map(|(key, value)| {
            let mut pair = key;
            pair.push(b'=');
            pair.extend(value);
            CString::new(pair)
        })
        .collect::<Result<_, _>>()
        .context("Cursor environment cannot contain NUL")?;
    let mut argv_ptrs: Vec<*const libc::c_char> = argv.iter().map(|value| value.as_ptr()).collect();
    argv_ptrs.push(std::ptr::null());
    let mut env_ptrs: Vec<*const libc::c_char> = env.iter().map(|value| value.as_ptr()).collect();
    env_ptrs.push(std::ptr::null());
    let cwd_c = CString::new(cwd.as_os_str().as_bytes())
        .context("Cursor working directory cannot contain NUL")?;
    let mut master = -1;
    let mut slave_name = [0 as libc::c_char; 1024];
    let slave_name_ptr = if cfg!(target_os = "macos") {
        slave_name.as_mut_ptr()
    } else {
        std::ptr::null_mut()
    };
    let pid =
        unsafe { libc::forkpty(&mut master, slave_name_ptr, std::ptr::null_mut(), &mut size) };
    if pid < 0 {
        anyhow::bail!("forkpty failed: {}", std::io::Error::last_os_error());
    }
    if pid == 0 {
        unsafe {
            libc::chdir(cwd_c.as_ptr());
            libc::execve(argv[0].as_ptr(), argv_ptrs.as_ptr(), env_ptrs.as_ptr());
            libc::_exit(127)
        }
    }
    // XNU can discard queued PTY output or hold a session-leader child in the
    // exiting state until the master reads it. Keeping the slave open in the
    // parent makes POLLIN observable; the relay loop drains it and reaps with
    // WNOHANG before releasing this hold.
    #[cfg(target_os = "macos")]
    let slave_hold = unsafe { libc::open(slave_name.as_ptr(), libc::O_RDWR | libc::O_NOCTTY) };
    #[cfg(not(target_os = "macos"))]
    let slave_hold = -1;
    if cfg!(target_os = "macos") && slave_hold < 0 {
        unsafe {
            libc::kill(pid, libc::SIGKILL);
            libc::close(master);
            libc::waitpid(pid, std::ptr::null_mut(), 0);
        }
        anyhow::bail!(
            "open Cursor PTY slave hold failed: {}",
            std::io::Error::last_os_error()
        );
    }
    let stop = Arc::new(AtomicBool::new(false));
    let requested_session_end = Arc::new(AtomicBool::new(false));
    let resized = Arc::new(AtomicBool::new(true));
    let setup = (|| -> anyhow::Result<Terminal> {
        let launcher_pid = std::process::id();
        let launcher_start = process_start_time(launcher_pid as libc::pid_t)
            .context("could not capture Cursor Helm launcher process identity")?;
        let cursor_start =
            process_start_time(pid).context("could not capture cursor-agent process identity")?;
        let now = Utc::now().to_rfc3339();
        write_json(
            &dir.join(format!("{session_id}.json")),
            &json!({"schema_version":1,"session_id":session_id,"provider_session_id":conversation,"run_id":registered.run_id,"connection_id":Uuid::new_v4().to_string(),"lease_generation":Uuid::new_v4().to_string(),"provider":"cursor","control_plane":"cursor_helm","socket_path":socket,"launcher_pid":launcher_pid,"launcher_process_start_time":launcher_start,"cursor_pid":pid,"cursor_process_start_time":cursor_start,"cwd":cwd,"ready":true,"registration":if degraded_registration.is_some() { "degraded" } else { "registered" },"started_at":now,"updated_at":now}),
        )?;
        let terminal = Terminal(0, raw(0)?);
        signal_hook::flag::register(libc::SIGWINCH, resized.clone())?;
        signal_hook::flag::register(libc::SIGTERM, stop.clone())?;
        signal_hook::flag::register(libc::SIGHUP, stop.clone())?;
        listener.set_nonblocking(true)?;
        Ok(terminal)
    })();
    let terminal = match setup {
        Ok(terminal) => terminal,
        Err(error) => {
            unsafe {
                libc::kill(pid, libc::SIGKILL);
                if slave_hold >= 0 {
                    libc::close(slave_hold);
                }
                libc::close(master);
                libc::waitpid(pid, std::ptr::null_mut(), 0);
            }
            if let Some(registration) = &degraded_registration {
                registration.provider_alive.store(false, Ordering::Release);
            }
            return Err(error);
        }
    };
    if let Some(registration) = &degraded_registration {
        registration.provider_alive.store(true, Ordering::Release);
    }
    if let Some(transaction) = launch_transaction.as_mut() {
        transaction.confirm_in_background();
    }
    sync_winsize(master);
    let socket_stop = stop.clone();
    let socket_requested_session_end = requested_session_end.clone();
    let guard = Arc::new(Mutex::new(()));
    let socket_guard = guard.clone();
    let server_dir = dir.clone();
    let server_session = session_id.clone();
    let server_conversation = conversation.clone();
    let server_launch = launch_id.clone();
    let server_coordination_token = coordination_token.clone();
    let server = thread::spawn(move || {
        while !socket_stop.load(Ordering::Relaxed) {
            match listener.accept() {
                Ok((stream, _)) => serve(
                    stream,
                    master,
                    pid,
                    &socket_stop,
                    &socket_requested_session_end,
                    &socket_guard,
                    &server_dir,
                    &server_session,
                    &server_conversation,
                    &server_launch,
                    server_coordination_token.as_deref(),
                ),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(std::time::Duration::from_millis(25))
                }
                Err(_) => break,
            }
        }
    });
    let mut input = [0u8; 8192];
    let mut output = [0u8; 65536];
    let mut reaped_status = None;
    loop {
        if stop.load(Ordering::Relaxed) {
            break;
        }
        if resized.swap(false, Ordering::Relaxed) {
            sync_winsize(master);
        }
        let mut fds = [
            libc::pollfd {
                fd: 0,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: master,
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        if unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as _, 250) } < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            break;
        }
        if fds[0].revents & libc::POLLIN != 0 {
            let count = unsafe { libc::read(0, input.as_mut_ptr().cast(), input.len()) };
            if count > 0 {
                let _hold = guard.lock().unwrap();
                if write_all(master, &input[..count as usize]).is_err() {
                    break;
                }
            }
        }
        if fds[1].revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0 {
            let count = unsafe { libc::read(master, output.as_mut_ptr().cast(), output.len()) };
            if count > 0 {
                if write_all(1, &output[..count as usize]).is_err() {
                    break;
                }
            } else {
                break;
            }
        }
        let mut status = 0;
        if cfg!(target_os = "macos")
            && unsafe { libc::waitpid(pid, &mut status, libc::WNOHANG) } == pid
        {
            reaped_status = Some(status);
            break;
        }
    }
    let launcher_requested_stop = stop.load(Ordering::Relaxed);
    drop(terminal);
    stop.store(true, Ordering::Relaxed);
    let _ = server.join();
    if slave_hold >= 0 {
        unsafe {
            libc::close(slave_hold);
        }
    }
    let exit_code = unsafe {
        let mut status = reaped_status.unwrap_or(0);
        if reaped_status.is_none() {
            if launcher_requested_stop {
                let mut observed = libc::waitpid(pid, &mut status, libc::WNOHANG);
                for _ in 0..25 {
                    if observed != 0 {
                        break;
                    }
                    thread::sleep(std::time::Duration::from_millis(10));
                    observed = libc::waitpid(pid, &mut status, libc::WNOHANG);
                }
                if observed == 0 {
                    libc::kill(pid, libc::SIGKILL);
                    libc::waitpid(pid, &mut status, 0);
                }
            } else {
                libc::waitpid(pid, &mut status, 0);
            }
        }
        if libc::WIFEXITED(status) {
            libc::WEXITSTATUS(status)
        } else {
            128 + libc::WTERMSIG(status)
        }
    };
    enqueue_terminal_event(
        &config,
        &session_id,
        Some(&registered.run_id),
        exit_code,
        requested_session_end.load(Ordering::Relaxed),
    );
    unsafe {
        libc::close(master);
    }
    if let Some(registration) = &degraded_registration {
        registration.provider_alive.store(false, Ordering::Release);
    }
    if config.open {
        if let Some(url) = hook_url.as_deref() {
            println!(
                "Timeline: {}/sessions/{session_id}",
                url.trim_end_matches('/')
            );
        }
    }
    Ok(exit_code)
}

fn fs2_lock(file: &fs::File) -> std::io::Result<()> {
    unsafe {
        if libc::flock(
            std::os::fd::AsRawFd::as_raw_fd(file),
            libc::LOCK_EX | libc::LOCK_NB,
        ) == 0
        {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::fd::FromRawFd;

    fn serve_request(
        dir: &Path,
        master: RawFd,
        child: libc::pid_t,
        request: Value,
    ) -> (Value, Arc<AtomicBool>, Arc<AtomicBool>) {
        let (mut client, server) = UnixStream::pair().unwrap();
        client.write_all(format!("{request}\n").as_bytes()).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let requested_session_end = Arc::new(AtomicBool::new(false));
        serve(
            server,
            master,
            child,
            &stop,
            &requested_session_end,
            &Mutex::new(()),
            dir,
            "session-id",
            "conversation-id",
            "launch-id",
            None,
        );
        let mut response = String::new();
        client.read_to_string(&mut response).unwrap();
        (
            serde_json::from_str(response.trim()).unwrap(),
            stop,
            requested_session_end,
        )
    }

    fn observed_claim(dir: &Path, policy: Option<&str>) -> String {
        let session_id = Uuid::new_v4().to_string();
        let mut claim = json!({
            "schema_version": 2,
            "provider": "cursor",
            "status": "observed",
            "session_id": session_id,
            "conversation_uuid": Uuid::new_v4().to_string(),
            "hook_observed_at": Utc::now().to_rfc3339(),
        });
        if let Some(policy) = policy {
            claim["permission_policy"] = json!(policy);
        }
        write_json(&claim_path(dir, &session_id), &claim).unwrap();
        session_id
    }

    #[test]
    fn terminal_event_carries_the_registered_run_identity() {
        let root = tempfile::tempdir().unwrap();
        let config = LaunchConfig {
            cwd: PathBuf::new(),
            project: None,
            name: None,
            loop_mode: "interactive".into(),
            url: None,
            token: None,
            resume_session: None,
            cursor_bin: None,
            config_dir: Some(root.path().to_path_buf()),
            permission_mode: None,
            verbose: false,
            open: false,
            cursor_args: vec![],
        };

        enqueue_terminal_event(&config, "session-1", Some("run-1"), 137, true);

        let event_path = fs::read_dir(root.path().join("agent/runtime-events-outbox"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        let event: Value = serde_json::from_slice(&fs::read(event_path).unwrap()).unwrap();
        assert_eq!(event["run_id"], "run-1");
        assert_eq!(event["dedupe_key"], "cursor-helm-terminal:session-1:run-1");
        assert_eq!(event["payload"]["terminal_state"], "session_ended");
        assert_eq!(event["payload"]["terminal_reason"], "remote_terminate");
    }

    #[test]
    fn mcp_config_scopes_coordination_authority_to_the_server() {
        let root = tempfile::tempdir().unwrap();
        let cursor_dir = root.path().join(".cursor");
        fs::create_dir(&cursor_dir).unwrap();
        let path = cursor_dir.join("mcp.json");
        let original = br#"{"mcpServers":{"existing":{"command":"existing"}}}"#;
        fs::write(&path, original).unwrap();

        let config = write_cursor_mcp_config(
            &root.path().join("state"),
            root.path(),
            "11111111-1111-4111-8111-111111111111",
        )
        .unwrap();
        let payload: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        let server = &payload["mcpServers"]["longhouse-coordination"];
        assert_eq!(
            server["command"],
            std::env::current_exe().unwrap().display().to_string()
        );
        assert_eq!(server["args"], json!(["cursor-helm", "coordination-mcp"]));
        assert!(server.get("env").is_none());
        assert!(payload["mcpServers"]["existing"].is_object());

        drop(config);
        assert_eq!(fs::read(path).unwrap(), original);
    }

    #[test]
    fn concurrent_mcp_configs_do_not_remove_live_authority_or_leave_tokens() {
        let root = tempfile::tempdir().unwrap();
        let first = write_cursor_mcp_config(
            &root.path().join("state"),
            root.path(),
            "11111111-1111-4111-8111-111111111111",
        )
        .unwrap();
        let second = write_cursor_mcp_config(
            &root.path().join("state"),
            root.path(),
            "22222222-2222-4222-8222-222222222222",
        )
        .unwrap();
        let path = root.path().join(".cursor/mcp.json");

        drop(first);
        let while_second_is_live = fs::read_to_string(&path)
            .expect("the second session must retain its MCP configuration");
        assert!(while_second_is_live.contains("coordination-mcp"));
        assert!(!while_second_is_live.contains("first-session-secret"));
        assert!(!while_second_is_live.contains("second-session-secret"));

        drop(second);
        assert!(
            !path.exists(),
            "the injected config must be removed after both sessions exit"
        );
    }

    #[test]
    fn resumed_session_requests_fresh_coordination_authority() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(1)))
                .unwrap();
            let mut request = [0; 4096];
            let count = stream.read(&mut request).unwrap();
            let request = String::from_utf8_lossy(&request[..count]);
            assert!(request.starts_with(
                "POST /api/agents/sessions/11111111-1111-4111-8111-111111111111/coordination-token"
            ));
            assert!(request
                .to_ascii_lowercase()
                .contains("x-agents-token: device-token"));
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 39\r\n\r\n{\"coordination_token\":\"resumed-secret\"}",
                )
                .unwrap();
        });
        let config = LaunchConfig {
            cwd: PathBuf::new(),
            project: None,
            name: None,
            loop_mode: "interactive".into(),
            url: Some(format!("http://{address}")),
            token: Some("device-token".into()),
            resume_session: Some("11111111-1111-4111-8111-111111111111".into()),
            cursor_bin: None,
            config_dir: None,
            permission_mode: None,
            verbose: false,
            open: false,
            cursor_args: vec![],
        };

        assert_eq!(
            coordination_token(&config, None, "11111111-1111-4111-8111-111111111111").unwrap(),
            Some("resumed-secret".to_string())
        );
        server.join().unwrap();
    }

    #[test]
    fn resume_retains_recorded_policy_and_rejects_conflicts() {
        let root = tempfile::tempdir().unwrap();
        let session_id = observed_claim(root.path(), Some("remote_human"));
        let claim = read_claim(root.path(), &session_id, None, None, None).unwrap();
        assert_eq!(claim.permission_mode, "remote_human");
        assert!(read_claim(root.path(), &session_id, Some("auto_approve"), None, None).is_err());
        assert!(read_claim(root.path(), &session_id, Some("remote_approve"), None, None).is_ok());
    }

    #[test]
    fn legacy_resume_defaults_to_provider_local() {
        let root = tempfile::tempdir().unwrap();
        let session_id = observed_claim(root.path(), None);
        assert_eq!(
            read_claim(root.path(), &session_id, None, None, None)
                .unwrap()
                .permission_mode,
            "provider_local"
        );
    }

    #[test]
    fn failed_resume_restores_observed_claim_and_new_launch_removes_pending() {
        let root = tempfile::tempdir().unwrap();
        let session_id = observed_claim(root.path(), Some("provider_local"));
        let original = fs::read(claim_path(root.path(), &session_id)).unwrap();
        write_pending_claim(
            root.path(),
            &session_id,
            &Uuid::new_v4().to_string(),
            "resume-launch",
            "provider_local",
            None,
            root.path(),
            "/bin/cursor-agent",
        )
        .unwrap();
        rollback_pending_claim(root.path(), &session_id, "resume-launch");
        assert_eq!(
            fs::read(claim_path(root.path(), &session_id)).unwrap(),
            original
        );

        let new_session = Uuid::new_v4().to_string();
        write_pending_claim(
            root.path(),
            &new_session,
            &Uuid::new_v4().to_string(),
            "new-launch",
            "auto_approve",
            None,
            root.path(),
            "/bin/cursor-agent",
        )
        .unwrap();
        rollback_pending_claim(root.path(), &new_session, "new-launch");
        assert!(!claim_path(root.path(), &new_session).exists());
    }

    #[test]
    fn socket_rejects_malformed_and_stale_send_then_relays_idle_send() {
        let root = tempfile::tempdir().unwrap();
        let mut pipe = [0; 2];
        assert_eq!(unsafe { libc::pipe(pipe.as_mut_ptr()) }, 0);
        let (mut client, server) = UnixStream::pair().unwrap();
        client.write_all(b"not-json\n").unwrap();
        let stop = AtomicBool::new(false);
        let requested_session_end = AtomicBool::new(false);
        serve(
            server,
            pipe[1],
            -1,
            &stop,
            &requested_session_end,
            &Mutex::new(()),
            root.path(),
            "session-id",
            "conversation-id",
            "launch-id",
            None,
        );
        let mut malformed = String::new();
        client.read_to_string(&mut malformed).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(malformed.trim()).unwrap()["error"]["code"],
            "bad_request"
        );

        let (stale, _, _) = serve_request(
            root.path(),
            pipe[1],
            -1,
            json!({"kind":"send","text":"hello"}),
        );
        assert_eq!(stale["error"]["code"], "provider_not_idle");
        write_json(
            &root.path().join("session-id.phase.json"),
            &json!({"session_id":"session-id","conversation_id":"conversation-id","launch_id":"launch-id","phase":"idle"}),
        )
        .unwrap();
        let (sent, _, _) = serve_request(
            root.path(),
            pipe[1],
            -1,
            json!({"kind":"send","text":"hello"}),
        );
        assert_eq!(sent["ok"], true);
        unsafe { libc::close(pipe[1]) };
        let mut reader = unsafe { fs::File::from_raw_fd(pipe[0]) };
        let mut relayed = Vec::new();
        reader.read_to_end(&mut relayed).unwrap();
        assert_eq!(relayed, b"hello\x1b\r");
    }

    #[test]
    fn socket_interrupt_checks_generation_and_terminate_kills_child() {
        let root = tempfile::tempdir().unwrap();
        let mut pipe = [0; 2];
        assert_eq!(unsafe { libc::pipe(pipe.as_mut_ptr()) }, 0);
        write_json(
            &root.path().join("session-id.phase.json"),
            &json!({"session_id":"session-id","conversation_id":"conversation-id","launch_id":"launch-id","phase":"active","generation_id":"turn-1"}),
        )
        .unwrap();
        let (mismatch, _, _) = serve_request(
            root.path(),
            pipe[1],
            -1,
            json!({"kind":"interrupt","generation_id":"other"}),
        );
        assert_eq!(mismatch["error"]["code"], "provider_generation_mismatch");
        let (interrupted, _, _) = serve_request(
            root.path(),
            pipe[1],
            -1,
            json!({"kind":"interrupt","generation_id":"turn-1"}),
        );
        assert_eq!(interrupted["ok"], true);
        unsafe { libc::close(pipe[1]) };
        let mut reader = unsafe { fs::File::from_raw_fd(pipe[0]) };
        let mut relayed = Vec::new();
        reader.read_to_end(&mut relayed).unwrap();
        assert_eq!(relayed, b"\x03");

        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            unsafe { libc::pause() };
            unsafe { libc::_exit(0) };
        }
        let (terminated, stop, requested_session_end) =
            serve_request(root.path(), -1, child, json!({"kind":"terminate"}));
        assert_eq!(terminated["ok"], true);
        assert!(stop.load(Ordering::Relaxed));
        assert!(requested_session_end.load(Ordering::Relaxed));
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert!(libc::WIFSIGNALED(status));
        assert_eq!(libc::WTERMSIG(status), libc::SIGKILL);
    }

    #[test]
    fn launch_lock_resize_and_terminal_restoration_are_real_os_contracts() {
        let root = tempfile::tempdir().unwrap();
        let lock_path = root.path().join("launch.lock");
        let first = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .open(&lock_path)
            .unwrap();
        let second = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&lock_path)
            .unwrap();
        fs2_lock(&first).unwrap();
        assert!(fs2_lock(&second).is_err());

        let mut source_master = 0;
        let mut source_slave = 0;
        let mut target_master = 0;
        let mut target_slave = 0;
        assert_eq!(
            unsafe {
                libc::openpty(
                    &mut source_master,
                    &mut source_slave,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            },
            0
        );
        assert_eq!(
            unsafe {
                libc::openpty(
                    &mut target_master,
                    &mut target_slave,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            },
            0
        );
        let expected = libc::winsize {
            ws_row: 41,
            ws_col: 133,
            ws_xpixel: 0,
            ws_ypixel: 0,
        };
        assert_eq!(
            unsafe { libc::ioctl(source_slave, libc::TIOCSWINSZ, &expected) },
            0
        );
        assert!(copy_winsize(source_slave, target_slave));
        let actual = terminal_winsize(target_slave);
        assert_eq!((actual.ws_row, actual.ws_col), (41, 133));

        let mut before: libc::termios = unsafe { std::mem::zeroed() };
        assert_eq!(unsafe { libc::tcgetattr(target_slave, &mut before) }, 0);
        let guard = Terminal(target_slave, raw(target_slave).unwrap());
        drop(guard);
        let mut after: libc::termios = unsafe { std::mem::zeroed() };
        assert_eq!(unsafe { libc::tcgetattr(target_slave, &mut after) }, 0);
        assert_eq!(before.c_iflag, after.c_iflag);
        assert_eq!(before.c_oflag, after.c_oflag);
        assert_eq!(before.c_cflag, after.c_cflag);
        let user_mode_flags = libc::ECHO | libc::ICANON | libc::IEXTEN | libc::ISIG;
        assert_eq!(
            before.c_lflag & user_mode_flags,
            after.c_lflag & user_mode_flags
        );
        for fd in [source_master, source_slave, target_master, target_slave] {
            unsafe { libc::close(fd) };
        }
    }
}
