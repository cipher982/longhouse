//! Native control adapter for managed OpenCode server-bridge sessions.

use std::env;
use std::ffi::OsStr;
use std::fs;
use std::fs::File;
use std::fs::OpenOptions;
use std::io::Write;
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;
use std::time::Instant;

use anyhow::{anyhow, bail, Context, Result};
use base64::{engine::general_purpose, Engine as _};
use rand::rngs::OsRng;
use rand::RngCore;
use reqwest::{Client, Method, Url};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::process::Command;
use tokio::time::sleep;
use uuid::Uuid;

const DEFAULT_USERNAME: &str = "opencode";
const MAX_READABLE_STATE_SCHEMA_VERSION: u64 = 1;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
const STATE_SCHEMA_VERSION: u64 = 1;
const LAUNCH_MODE_DETACHED: &str = "detached";
const OPENCODE_BIN_ENV: &str = "LONGHOUSE_OPENCODE_BIN";
const OPENCODE_RUNTIME_PLUGIN_FILENAME: &str = "longhouse-opencode-runtime.mjs";
const OPENCODE_RUNTIME_PLUGIN_POST_TIMEOUT_MS: u64 = 2_000;
const SERVER_LOG_MARKER: &str = "opencode server listening on ";

pub const OPENCODE_SERVER_BRIDGE_TRANSPORT: &str = "opencode_server_bridge";

const OPENCODE_RUNTIME_PLUGIN_TEMPLATE: &str = r#"
const SOURCE = "opencode_event"
const POST_TIMEOUT_MS = __LONGHOUSE_POST_TIMEOUT_MS__

function requireOption(options, name) {
  const value = options && typeof options[name] === "string" ? options[name].trim() : ""
  if (!value) throw new Error(`Longhouse OpenCode plugin missing ${name}`)
  return value
}

function phaseForStatus(status) {
  const type = status && typeof status.type === "string" ? status.type : ""
  if (type === "busy") return { phase: "running" }
  if (type === "retry") return { phase: "blocked", toolName: "retry" }
  return { phase: "idle" }
}

function buildEvent(ctx, kind, phase, toolName, payload) {
  const occurredAt = new Date().toISOString()
  ctx.seq += 1
  return {
    runtime_key: `opencode:${ctx.sessionID}`,
    session_id: ctx.sessionID,
    provider: "opencode",
    device_id: ctx.deviceID,
    source: SOURCE,
    kind,
    phase,
    tool_name: toolName || null,
    occurred_at: occurredAt,
    dedupe_key: `${ctx.sessionID}:${SOURCE}:${ctx.seq}:${payload && payload.eventID ? payload.eventID : occurredAt}`,
    payload: payload || {},
  }
}

async function postEvents(ctx, events) {
  if (!events.length) return
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), POST_TIMEOUT_MS)
  try {
    const response = await fetch(ctx.runtimeEventsUrl, {
      method: "POST",
      signal: controller.signal,
      headers: {
        "content-type": "application/json",
        "x-agents-token": ctx.token,
      },
      body: JSON.stringify({ events }),
    })
    if (!response.ok) {
      const body = await response.text().catch(() => "")
      console.warn(`Longhouse runtime ingest failed: ${response.status} ${body.slice(0, 200)}`)
    }
  } catch (error) {
    console.warn(`Longhouse runtime ingest failed: ${error && error.message ? error.message : error}`)
  } finally {
    clearTimeout(timeout)
  }
}

export default {
  id: "longhouse-runtime",
  async server(_input, options) {
    const ctx = {
      runtimeEventsUrl: requireOption(options, "runtimeEventsUrl"),
      token: requireOption(options, "token"),
      sessionID: requireOption(options, "longhouseSessionID"),
      deviceID: requireOption(options, "deviceID"),
      seq: 0,
    }

    return {
      async event({ event }) {
        const type = event && event.type
        const props = (event && event.properties) || {}
        if (type === "session.status") {
          const mapped = phaseForStatus(props.status)
          await postEvents(ctx, [
            buildEvent(ctx, "phase_signal", mapped.phase, mapped.toolName, {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
              opencodeStatus: props.status,
            }),
          ])
        }
        if (type === "session.idle") {
          await postEvents(ctx, [
            buildEvent(ctx, "phase_signal", "idle", null, {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
            }),
          ])
        }
        if (type === "permission.asked") {
          const requestID = (props && (props.id || props.requestID || props.permissionID)) || null
          const toolName = (props && (props.tool || props.toolName)) || null
          await postEvents(ctx, [
            buildEvent(ctx, "phase_signal", "blocked", "permission", {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
              permission: props,
            }),
            buildEvent(ctx, "pause_request", null, toolName, {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
              request_id: requestID,
              provider_request_id: requestID,
              kind: "permission_prompt",
              can_respond: requestID ? true : false,
              provider_ref: { source: "opencode_bridge", reply_transport: "managed_push", opencode_request_id: requestID },
              tool_name: toolName,
              title: toolName ? ("Permission: " + toolName) : "Tool permission",
              summary: toolName ? ("OpenCode wants to use " + toolName) : "OpenCode is requesting tool permission.",
              permission: props,
            }),
          ])
        }
        if (type === "permission.replied") {
          await postEvents(ctx, [
            buildEvent(ctx, "phase_signal", "running", null, {
              eventID: event.id,
              opencodeSessionID: props.sessionID,
              permission: props,
            }),
          ])
        }
      },
      async "chat.message"(input) {
        await postEvents(ctx, [
          buildEvent(ctx, "phase_signal", "running", null, {
            hook: "chat.message",
            opencodeSessionID: input.sessionID,
            opencodeMessageID: input.messageID,
            agent: input.agent,
            model: input.model,
          }),
        ])
      },
      async "tool.execute.before"(input) {
        await postEvents(ctx, [
          buildEvent(ctx, "phase_signal", "running", input.tool, {
            hook: "tool.execute.before",
            opencodeSessionID: input.sessionID,
            opencodeCallID: input.callID,
            tool: input.tool,
          }),
        ])
      },
      async "tool.execute.after"(input) {
        await postEvents(ctx, [
          buildEvent(ctx, "phase_signal", "running", null, {
            hook: "tool.execute.after",
            opencodeSessionID: input.sessionID,
            opencodeCallID: input.callID,
            tool: input.tool,
          }),
        ])
      },
    }
  },
}
"#;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenCodeControlResult {
    pub provider_session_id: String,
}

#[derive(Debug, Clone)]
pub struct OpenCodeLaunchConfig {
    pub session_id: String,
    pub cwd: PathBuf,
    pub api_url: String,
    pub api_token: String,
    pub device_id: String,
    pub display_name: Option<String>,
    pub wait_ready: Duration,
    pub config_dir: Option<PathBuf>,
    pub opencode_bin: Option<PathBuf>,
    pub opencode_config_content: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenCodeLaunchResult {
    pub session_id: String,
    pub provider_session_id: String,
    pub server_url: String,
    pub pid: u32,
    pub log_path: PathBuf,
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
    log_path: Option<String>,
    process_start_time: String,
    process_command: String,
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

#[derive(Debug, Serialize)]
struct OpenCodeServerStateWrite {
    schema_version: u64,
    session_id: String,
    provider_session_id: String,
    server_url: String,
    pid: u32,
    cwd: String,
    username: String,
    password: String,
    log_path: String,
    config_content_path: String,
    started_at: String,
    updated_at: String,
    process_start_time: String,
    process_command: String,
    launch_mode: String,
    owner_wrapper_pid: u32,
    owner_wrapper_start_time: String,
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

pub async fn launch_server_bridge(config: OpenCodeLaunchConfig) -> Result<OpenCodeLaunchResult> {
    let normalized_session_id = normalize_session_id(&config.session_id)?;
    if !config.cwd.is_absolute() || !config.cwd.is_dir() {
        bail!("cwd must be an existing absolute directory");
    }
    if config.api_token.trim().is_empty() {
        bail!("api token is required");
    }
    if config.device_id.trim().is_empty() {
        bail!("device id is required");
    }

    let state_dir = opencode_server_state_dir(config.config_dir.as_deref())?;
    let _lock = acquire_launch_lock(&state_dir, &normalized_session_id)?;
    if let Some(existing) = existing_live_state_result(&normalized_session_id, &state_dir).await? {
        return Ok(existing);
    }

    let resolved_bin = resolve_opencode_binary(config.opencode_bin.as_deref())?;
    let logs_dir = state_dir.join("logs");
    fs::create_dir_all(&logs_dir)
        .with_context(|| format!("creating OpenCode server logs dir {}", logs_dir.display()))?;
    let log_path = logs_dir.join(format!("{normalized_session_id}.log"));
    let runtime_events_url = managed_runtime_events_url(&config.api_url);
    let config_content = write_opencode_runtime_config_content(
        config.config_dir.as_deref(),
        &runtime_events_url,
        &config.api_token,
        &normalized_session_id,
        &config.device_id,
        config.opencode_config_content.as_deref(),
    )?;
    let username = DEFAULT_USERNAME.to_string();
    let password = generate_server_password();
    let log_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .with_context(|| format!("opening OpenCode server log {}", log_path.display()))?;
    let log_file_for_stderr = log_file
        .try_clone()
        .with_context(|| format!("cloning OpenCode server log {}", log_path.display()))?;

    let mut command = Command::new(&resolved_bin);
    command
        .arg("serve")
        .arg("--hostname")
        .arg("127.0.0.1")
        .arg("--port")
        .arg("0")
        .arg("--print-logs")
        .current_dir(&config.cwd)
        .env("LONGHOUSE_MANAGED_SESSION_ID", &normalized_session_id)
        .env("LONGHOUSE_DEVICE_ID", &config.device_id)
        .env("OPENCODE_CONFIG_CONTENT", &config_content.content)
        .env("OPENCODE_SERVER_USERNAME", &username)
        .env("OPENCODE_SERVER_PASSWORD", &password)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(log_file_for_stderr));
    #[cfg(unix)]
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }

    let mut child = command.spawn().with_context(|| {
        format!(
            "failed to start OpenCode executable {}",
            resolved_bin.display()
        )
    })?;
    let pid = child
        .id()
        .ok_or_else(|| anyhow!("OpenCode server pid was not available after spawn"))?;

    let launched = async {
        let server_url = wait_for_server_url(&log_path, &mut child, config.wait_ready).await?;
        assert_health_ready(&server_url, &username, &password).await?;
        let title = config
            .display_name
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .or_else(|| {
                config
                    .cwd
                    .file_name()
                    .and_then(OsStr::to_str)
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
                    .map(str::to_string)
            })
            .unwrap_or_else(|| normalized_session_id.clone());
        let provider_session_id =
            create_opencode_session(&server_url, &username, &password, &config.cwd, &title).await?;
        let now = utc_now();
        let identity = process_identity(pid);
        let state = OpenCodeServerStateWrite {
            schema_version: STATE_SCHEMA_VERSION,
            session_id: normalized_session_id.clone(),
            provider_session_id: provider_session_id.clone(),
            server_url: server_url.clone(),
            pid,
            cwd: config.cwd.display().to_string(),
            username,
            password,
            log_path: log_path.display().to_string(),
            config_content_path: config_content.path.display().to_string(),
            started_at: now.clone(),
            updated_at: now,
            process_start_time: identity
                .as_ref()
                .map(|value| value.0.clone())
                .unwrap_or_default(),
            process_command: identity.map(|value| value.1).unwrap_or_default(),
            launch_mode: LAUNCH_MODE_DETACHED.to_string(),
            owner_wrapper_pid: 0,
            owner_wrapper_start_time: String::new(),
        };
        let state_path = state_dir.join(format!("{normalized_session_id}.json"));
        write_private_json(&state_path, &state)?;
        Ok(OpenCodeLaunchResult {
            session_id: normalized_session_id.clone(),
            provider_session_id,
            server_url,
            pid,
            log_path,
        })
    }
    .await;

    if launched.is_err() {
        let _ = terminate_pid(pid);
    }
    launched
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
        log_path: payload
            .log_path
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty()),
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

fn opencode_server_state_dir(config_dir: Option<&Path>) -> Result<PathBuf> {
    if let Some(config_dir) = config_dir {
        return Ok(config_dir.join("managed-local").join("opencode-server"));
    }
    crate::managed_opencode_scan::default_opencode_server_state_dir()
        .ok_or_else(|| anyhow!("OpenCode server bridge state directory could not be resolved"))
}

fn opencode_runtime_dir(config_dir: Option<&Path>) -> Result<PathBuf> {
    let provider_home = match config_dir {
        Some(path) => path.to_path_buf(),
        None => env::var_os("CLAUDE_CONFIG_DIR")
            .map(PathBuf::from)
            .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".claude")))
            .ok_or_else(|| anyhow!("OpenCode provider config directory could not be resolved"))?,
    };
    Ok(provider_home.join("managed-local").join("opencode"))
}

#[cfg(unix)]
struct LaunchLock {
    file: File,
}

#[cfg(unix)]
impl Drop for LaunchLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.file.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

#[cfg(unix)]
fn acquire_launch_lock(state_dir: &Path, session_id: &str) -> Result<LaunchLock> {
    fs::create_dir_all(state_dir)
        .with_context(|| format!("creating OpenCode server state dir {}", state_dir.display()))?;
    let lock_path = state_dir.join(format!("{session_id}.lock"));
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(&lock_path)
        .with_context(|| format!("opening OpenCode launch lock {}", lock_path.display()))?;
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) };
    if rc != 0 {
        return Err(std::io::Error::last_os_error())
            .with_context(|| format!("acquiring OpenCode launch lock {}", lock_path.display()));
    }
    Ok(LaunchLock { file })
}

#[cfg(not(unix))]
fn acquire_launch_lock(_state_dir: &Path, _session_id: &str) -> Result<()> {
    Ok(())
}

async fn existing_live_state_result(
    session_id: &str,
    state_dir: &Path,
) -> Result<Option<OpenCodeLaunchResult>> {
    let state_path = state_dir.join(format!("{session_id}.json"));
    let state = match read_bridge_state_from_path(session_id, &state_path) {
        Ok(state) => state,
        Err(_) => return Ok(None),
    };
    if !pid_matches_recorded_identity(&state) {
        return Ok(None);
    }
    if assert_health_ready(&state.server_url, &state.username, &state.password)
        .await
        .is_err()
    {
        if pid_matches_recorded_identity(&state) {
            if let Some(pid) = state.pid {
                let _ = terminate_pid(pid);
            }
        }
        return Ok(None);
    }
    let Some(pid) = state.pid else {
        return Ok(None);
    };
    Ok(Some(OpenCodeLaunchResult {
        session_id: state.session_id,
        provider_session_id: state.provider_session_id,
        server_url: state.server_url,
        pid,
        log_path: state
            .log_path
            .map(PathBuf::from)
            .unwrap_or_else(|| state_dir.join("logs").join(format!("{session_id}.log"))),
    }))
}

struct RuntimeConfigContent {
    path: PathBuf,
    content: String,
}

fn managed_runtime_events_url(base_url: &str) -> String {
    format!(
        "{}/api/agents/runtime/events/batch",
        base_url.trim_end_matches('/')
    )
}

fn write_opencode_runtime_config_content(
    config_dir: Option<&Path>,
    runtime_events_url: &str,
    token: &str,
    session_id: &str,
    device_id: &str,
    existing_content: Option<&str>,
) -> Result<RuntimeConfigContent> {
    let runtime_dir = opencode_runtime_dir(config_dir)?;
    fs::create_dir_all(&runtime_dir)
        .with_context(|| format!("creating OpenCode runtime dir {}", runtime_dir.display()))?;
    let plugin_path = runtime_dir.join(OPENCODE_RUNTIME_PLUGIN_FILENAME);
    let plugin = OPENCODE_RUNTIME_PLUGIN_TEMPLATE.trim().replace(
        "__LONGHOUSE_POST_TIMEOUT_MS__",
        &OPENCODE_RUNTIME_PLUGIN_POST_TIMEOUT_MS.to_string(),
    );
    fs::write(&plugin_path, format!("{plugin}\n"))
        .with_context(|| format!("writing OpenCode runtime plugin {}", plugin_path.display()))?;

    let content = opencode_config_content_with_longhouse_plugin(
        existing_content,
        &plugin_path,
        runtime_events_url,
        token,
        session_id,
        device_id,
    )?;
    let config_path = runtime_dir.join(format!("{session_id}.config-content.json"));
    write_private_text(&config_path, &format!("{content}\n"))?;
    Ok(RuntimeConfigContent {
        path: config_path,
        content,
    })
}

fn opencode_config_content_with_longhouse_plugin(
    existing_content: Option<&str>,
    plugin_path: &Path,
    runtime_events_url: &str,
    token: &str,
    session_id: &str,
    device_id: &str,
) -> Result<String> {
    let mut config = match existing_content
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        Some(existing) => {
            let value: Value = serde_json::from_str(existing)
                .context("OPENCODE_CONFIG_CONTENT is set but is not valid JSON")?;
            value
                .as_object()
                .cloned()
                .ok_or_else(|| anyhow!("OPENCODE_CONFIG_CONTENT must be a JSON object"))?
        }
        None => serde_json::Map::new(),
    };
    let plugins = config.remove("plugin");
    let mut plugin_items = match plugins {
        Some(Value::Array(items)) => items,
        Some(_) => bail!("OPENCODE_CONFIG_CONTENT plugin field must be an array"),
        None => Vec::new(),
    };
    let plugin_url = Url::from_file_path(
        plugin_path
            .canonicalize()
            .with_context(|| format!("resolving plugin path {}", plugin_path.display()))?,
    )
    .map_err(|_| anyhow!("OpenCode runtime plugin path could not be converted to a file URL"))?;
    plugin_items.push(json!([
        plugin_url.as_str(),
        {
            "runtimeEventsUrl": runtime_events_url,
            "token": token,
            "longhouseSessionID": session_id,
            "deviceID": device_id,
        }
    ]));
    config.insert("plugin".to_string(), Value::Array(plugin_items));
    serde_json::to_string(&Value::Object(config))
        .context("serializing OpenCode runtime config content")
}

fn generate_server_password() -> String {
    let mut bytes = [0u8; 24];
    OsRng.fill_bytes(&mut bytes);
    general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

fn resolve_opencode_binary(explicit: Option<&Path>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return resolve_command_candidate(path.as_os_str(), "opencode binary override");
    }
    if let Some(value) = env::var_os(OPENCODE_BIN_ENV).filter(|value| !value.is_empty()) {
        return resolve_command_candidate(&value, OPENCODE_BIN_ENV);
    }
    resolve_command_candidate(OsStr::new("opencode"), "opencode")
}

fn resolve_command_candidate(candidate: &OsStr, source: &str) -> Result<PathBuf> {
    let candidate_path = PathBuf::from(candidate);
    let candidate_text = candidate.to_string_lossy();
    let looks_like_path = candidate_path.is_absolute()
        || candidate_text.starts_with('.')
        || candidate_text.starts_with('~')
        || candidate_text.contains(std::path::MAIN_SEPARATOR);
    if looks_like_path {
        let expanded = expand_tilde(&candidate_text);
        if is_executable_file(&expanded) {
            return expanded
                .canonicalize()
                .with_context(|| format!("resolving {source} {}", expanded.display()));
        }
        bail!("{source} points to `{candidate_text}`, but it is not an executable file");
    }
    let path = env::var_os("PATH").unwrap_or_default();
    for dir in env::split_paths(&path) {
        let path = dir.join(Path::new(candidate));
        if is_executable_file(&path) {
            return path
                .canonicalize()
                .with_context(|| format!("resolving {source} {}", path.display()));
        }
    }
    bail!("OpenCode executable not found");
}

fn expand_tilde(candidate: &str) -> PathBuf {
    if candidate == "~" {
        if let Some(home) = env::var_os("HOME") {
            return PathBuf::from(home);
        }
    }
    if let Some(rest) = candidate.strip_prefix("~/") {
        if let Some(home) = env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(candidate)
}

fn is_executable_file(path: &Path) -> bool {
    let Ok(metadata) = fs::metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

async fn wait_for_server_url(
    log_path: &Path,
    child: &mut tokio::process::Child,
    timeout: Duration,
) -> Result<String> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        let tail = tail_text(log_path, 4000);
        if let Some(url) = parse_server_url_from_text(&tail) {
            return Ok(url);
        }
        if let Some(status) = child
            .try_wait()
            .context("checking OpenCode server process status")?
        {
            let detail = tail.trim();
            bail!("OpenCode server exited before it became ready: {status}; {detail}");
        }
        sleep(Duration::from_millis(100)).await;
    }
    let detail = tail_text(log_path, 4000);
    bail!(
        "Timed out waiting for OpenCode server URL after {}s: {}",
        timeout.as_secs(),
        detail.trim()
    );
}

fn parse_server_url_from_text(text: &str) -> Option<String> {
    let start = text.rfind(SERVER_LOG_MARKER)? + SERVER_LOG_MARKER.len();
    let candidate = text[start..]
        .split_whitespace()
        .next()
        .unwrap_or_default()
        .trim();
    if !candidate.starts_with("http://127.0.0.1:") {
        return None;
    }
    let url = Url::parse(candidate).ok()?;
    validate_local_server_url(&url).ok()?;
    Some(url.to_string().trim_end_matches('/').to_string())
}

fn tail_text(path: &Path, max_chars: usize) -> String {
    let Ok(text) = fs::read_to_string(path) else {
        return String::new();
    };
    if text.chars().count() <= max_chars {
        return text;
    }
    text.chars()
        .rev()
        .take(max_chars)
        .collect::<String>()
        .chars()
        .rev()
        .collect()
}

async fn assert_health_ready(server_url: &str, username: &str, password: &str) -> Result<()> {
    let mut url = Url::parse(server_url)
        .with_context(|| format!("OpenCode server URL is invalid: {server_url}"))?;
    validate_local_server_url(&url)?;
    url.set_path("/global/health");
    url.set_query(None);
    url.set_fragment(None);
    let client = Client::builder()
        .timeout(REQUEST_TIMEOUT)
        .build()
        .context("failed to build OpenCode health HTTP client")?;
    let response = client
        .get(url)
        .basic_auth(username, Some(password))
        .header("Accept", "application/json")
        .send()
        .await
        .context("OpenCode server health request failed")?;
    let status = response.status();
    let body = response
        .text()
        .await
        .context("OpenCode server health response body could not be read")?;
    if !status.is_success() {
        bail!("OpenCode server health failed: HTTP {status}; body={body}");
    }
    let payload: Value =
        serde_json::from_str(&body).context("OpenCode server health returned invalid JSON")?;
    if payload.get("healthy").and_then(Value::as_bool) != Some(true) {
        bail!("OpenCode server health check did not report healthy");
    }
    Ok(())
}

async fn create_opencode_session(
    server_url: &str,
    username: &str,
    password: &str,
    cwd: &Path,
    title: &str,
) -> Result<String> {
    let mut url = Url::parse(server_url)
        .with_context(|| format!("OpenCode server URL is invalid: {server_url}"))?;
    validate_local_server_url(&url)?;
    url.set_path("/session");
    url.set_query(None);
    url.set_fragment(None);
    url.query_pairs_mut()
        .append_pair("directory", &cwd.display().to_string());
    let client = Client::builder()
        .timeout(REQUEST_TIMEOUT)
        .build()
        .context("failed to build OpenCode session.create HTTP client")?;
    let response = client
        .post(url)
        .basic_auth(username, Some(password))
        .header("Accept", "application/json")
        .json(&json!({"title": title}))
        .send()
        .await
        .context("OpenCode session.create request failed")?;
    let status = response.status();
    let body = response
        .text()
        .await
        .context("OpenCode session.create response body could not be read")?;
    if !status.is_success() {
        bail!("OpenCode session.create failed: HTTP {status}; body={body}");
    }
    let payload: Value =
        serde_json::from_str(&body).context("OpenCode session.create returned invalid JSON")?;
    let id = payload
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if id.is_empty() {
        bail!("OpenCode session.create returned no session id");
    }
    Ok(id.to_string())
}

fn write_private_json<T: Serialize>(path: &Path, payload: &T) -> Result<()> {
    let text = format!(
        "{}\n",
        serde_json::to_string_pretty(payload).context("serializing OpenCode bridge state")?
    );
    write_private_text(path, &text)
}

fn write_private_text(path: &Path, text: &str) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("path has no parent: {}", path.display()))?;
    fs::create_dir_all(parent)
        .with_context(|| format!("creating private parent dir {}", parent.display()))?;
    #[cfg(unix)]
    {
        let _ = fs::set_permissions(parent, fs::Permissions::from_mode(0o700));
    }
    let tmp_path = path.with_file_name(format!(
        "{}.{}.tmp",
        path.file_name()
            .and_then(OsStr::to_str)
            .unwrap_or("opencode-state"),
        std::process::id()
    ));
    let mut options = OpenOptions::new();
    options.create(true).write(true).truncate(true);
    #[cfg(unix)]
    {
        options.mode(0o600);
    }
    let write_result = (|| -> Result<()> {
        let mut file = options
            .open(&tmp_path)
            .with_context(|| format!("opening private temp file {}", tmp_path.display()))?;
        file.write_all(text.as_bytes())
            .with_context(|| format!("writing private temp file {}", tmp_path.display()))?;
        file.sync_all()
            .with_context(|| format!("fsync private temp file {}", tmp_path.display()))?;
        fs::rename(&tmp_path, path).with_context(|| {
            format!(
                "replacing private file {} with {}",
                path.display(),
                tmp_path.display()
            )
        })?;
        Ok(())
    })();
    if write_result.is_err() {
        let _ = fs::remove_file(&tmp_path);
    }
    write_result
}

fn utc_now() -> String {
    chrono::Utc::now()
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
        .replace("+00:00", "Z")
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

fn pid_matches_recorded_identity(state: &OpenCodeControlState) -> bool {
    let Some(pid) = state.pid else {
        return false;
    };
    if !pid_is_running(pid) {
        return false;
    }
    let Some((live_start, live_cmd)) = process_identity(pid) else {
        return false;
    };
    let recorded_start = state.process_start_time.trim();
    let recorded_cmd = state.process_command.trim();
    if recorded_start.is_empty() && recorded_cmd.is_empty() {
        return live_cmd.contains("opencode") && live_cmd.contains(" serve");
    }
    if !recorded_start.is_empty() && recorded_start != live_start {
        return false;
    }
    if !recorded_cmd.is_empty() && recorded_cmd != live_cmd {
        return false;
    }
    true
}

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
    use std::sync::Arc;

    use base64::{engine::general_purpose, Engine as _};
    use tempfile::TempDir;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::mpsc;
    use tokio::sync::oneshot;
    use tokio::time::timeout;

    use super::*;

    const SESSION_ID: &str = "11111111-1111-4111-8111-111111111111";

    #[derive(Debug, Clone)]
    struct RecordedRequest {
        method: String,
        target: String,
        headers: HashMap<String, String>,
        body: String,
    }

    struct FakeOpenCodeScript {
        path: PathBuf,
        count_path: PathBuf,
        env_path: PathBuf,
        argv_path: PathBuf,
        pid_path: PathBuf,
    }

    #[test]
    fn parse_server_url_requires_expected_localhost_listen_line() {
        assert_eq!(
            parse_server_url_from_text(
                "noise\nopencode server listening on http://127.0.0.1:54321\n"
            )
            .as_deref(),
            Some("http://127.0.0.1:54321")
        );
        assert!(
            parse_server_url_from_text("opencode server listening on https://example.com:443")
                .is_none()
        );
    }

    #[test]
    fn runtime_config_content_appends_plugin_and_validates_existing_shape() {
        let temp = TempDir::new().unwrap();
        let plugin_path = temp.path().join("longhouse-opencode-runtime.mjs");
        fs::write(&plugin_path, "export default {}\n").unwrap();

        let content = opencode_config_content_with_longhouse_plugin(
            Some(r#"{"plugin":["existing"],"theme":"dark"}"#),
            &plugin_path,
            "https://longhouse.test/api/agents/runtime/events/batch",
            "zdt_test_token",
            SESSION_ID,
            "macbook",
        )
        .unwrap();
        let payload: Value = serde_json::from_str(&content).unwrap();

        assert_eq!(payload["theme"], "dark");
        let plugins = payload["plugin"].as_array().unwrap();
        assert_eq!(plugins.len(), 2);
        assert_eq!(plugins[0], "existing");
        assert_eq!(
            plugins[1][1]["runtimeEventsUrl"],
            "https://longhouse.test/api/agents/runtime/events/batch"
        );
        assert_eq!(plugins[1][1]["token"], "zdt_test_token");
        assert_eq!(plugins[1][1]["longhouseSessionID"], SESSION_ID);
        assert_eq!(plugins[1][1]["deviceID"], "macbook");

        let error = opencode_config_content_with_longhouse_plugin(
            Some(r#"{"plugin":{}}"#),
            &plugin_path,
            "https://longhouse.test/api/agents/runtime/events/batch",
            "token",
            SESSION_ID,
            "device",
        )
        .unwrap_err();
        assert!(error
            .to_string()
            .contains("OPENCODE_CONFIG_CONTENT plugin field must be an array"));
    }

    #[test]
    fn private_json_write_uses_private_modes_and_scanner_shape() {
        let temp = TempDir::new().unwrap();
        let path = temp
            .path()
            .join("managed-local/opencode-server")
            .join(format!("{SESSION_ID}.json"));
        let state = test_state_write("ses_written", "http://127.0.0.1:12345", 4242, temp.path());

        write_private_json(&path, &state).unwrap();

        let raw = fs::read_to_string(&path).unwrap();
        let payload: OpenCodeServerStateFile = serde_json::from_str(&raw).unwrap();
        assert_eq!(payload.schema_version, Some(STATE_SCHEMA_VERSION));
        assert_eq!(payload.session_id.as_deref(), Some(SESSION_ID));
        assert_eq!(payload.provider_session_id.as_deref(), Some("ses_written"));
        assert_eq!(payload.launch_mode.as_deref(), Some(LAUNCH_MODE_DETACHED));
        assert_eq!(
            payload.config_content_path.as_deref(),
            Some("/tmp/config.json")
        );
        assert_eq!(payload.owner_wrapper_pid, Some(0));

        #[cfg(unix)]
        {
            let dir_mode = fs::metadata(path.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            let file_mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(dir_mode, 0o700);
            assert_eq!(file_mode, 0o600);
        }
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
        newer_schema["schema_version"] = Value::Number(2.into());
        write_state_payload(temp.path(), SESSION_ID, newer_schema);
        let error = read_bridge_state(SESSION_ID, Some(temp.path())).unwrap_err();
        assert!(error
            .to_string()
            .contains("state schema 2 is newer than this Longhouse build"));

        let mut missing_schema = base_state_payload("http://127.0.0.1:12345", Some("/tmp/project"));
        missing_schema.as_object_mut().unwrap().remove("schema_version");
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

    #[tokio::test]
    async fn launch_server_bridge_starts_fake_opencode_and_writes_private_state() {
        let temp = TempDir::new().unwrap();
        let cwd = temp.path().join("project");
        fs::create_dir(&cwd).unwrap();
        let (server_url, mut requests) = spawn_launch_server("ses_launch", false).await;
        let fake = write_fake_opencode_script(temp.path(), &server_url);

        let result = launch_server_bridge(test_launch_config(
            temp.path(),
            cwd.clone(),
            fake.path.clone(),
            Some("Launch Title".to_string()),
            Duration::from_secs(5),
        ))
        .await
        .unwrap();

        assert_eq!(result.session_id, SESSION_ID);
        assert_eq!(result.provider_session_id, "ses_launch");
        assert_eq!(result.server_url, server_url);
        assert_eq!(fs::read_to_string(&fake.count_path).unwrap(), "spawn\n");

        let health = recv_request(&mut requests).await;
        let create = recv_request(&mut requests).await;
        assert_eq!(health.method, "GET");
        assert_eq!(health.target, "/global/health");
        assert_eq!(create.method, "POST");
        assert_eq!(
            create.target,
            format!(
                "/session?directory={}",
                urlencoding_like(&cwd.display().to_string())
            )
        );
        assert_eq!(
            serde_json::from_str::<Value>(&create.body).unwrap(),
            json!({"title": "Launch Title"})
        );

        let state_path = temp
            .path()
            .join("managed-local")
            .join("opencode-server")
            .join(format!("{SESSION_ID}.json"));
        let state: Value = serde_json::from_str(&fs::read_to_string(&state_path).unwrap()).unwrap();
        let password = state["password"].as_str().unwrap();
        assert!(!password.is_empty());
        let expected_auth = format!(
            "Basic {}",
            general_purpose::STANDARD.encode(format!("opencode:{password}"))
        );
        assert_eq!(health.headers.get("authorization"), Some(&expected_auth));
        assert_eq!(create.headers.get("authorization"), Some(&expected_auth));
        assert_eq!(state["schema_version"].as_u64(), Some(STATE_SCHEMA_VERSION));
        assert_eq!(state["launch_mode"].as_str(), Some(LAUNCH_MODE_DETACHED));
        let cwd_string = cwd.display().to_string();
        assert_eq!(state["cwd"].as_str(), Some(cwd_string.as_str()));
        assert_eq!(state["pid"].as_u64(), Some(result.pid as u64));
        assert_eq!(state["provider_session_id"].as_str(), Some("ses_launch"));
        assert!(state["process_start_time"].as_str().unwrap().len() >= 20);
        assert!(state["process_command"]
            .as_str()
            .unwrap()
            .contains(fake.path.to_str().unwrap()));
        assert_eq!(state["owner_wrapper_pid"].as_u64(), Some(0));
        assert_eq!(state["owner_wrapper_start_time"].as_str(), Some(""));
        assert_eq!(
            state["config_content_path"].as_str().unwrap(),
            temp.path()
                .join("managed-local/opencode")
                .join(format!("{SESSION_ID}.config-content.json"))
                .display()
                .to_string()
        );

        let config_content =
            fs::read_to_string(state["config_content_path"].as_str().unwrap()).unwrap();
        let config_json: Value = serde_json::from_str(&config_content).unwrap();
        assert_eq!(config_json["plugin"][0][1]["token"], "zdt_test_token");
        assert_eq!(
            config_json["plugin"][0][1]["longhouseSessionID"],
            SESSION_ID
        );
        assert_eq!(config_json["plugin"][0][1]["deviceID"], "test-device");

        let argv = fs::read_to_string(&fake.argv_path).unwrap();
        assert!(argv.contains("serve\n--hostname\n127.0.0.1\n--port\n0\n--print-logs"));
        assert!(!argv.contains("zdt_test_token"));
        assert!(!argv.contains(password));

        let env_dump = fs::read_to_string(&fake.env_path).unwrap();
        assert!(env_dump.contains(&format!("LONGHOUSE_MANAGED_SESSION_ID={SESSION_ID}")));
        assert!(env_dump.contains("LONGHOUSE_DEVICE_ID=test-device"));
        assert!(env_dump.contains("OPENCODE_SERVER_USERNAME=opencode"));
        assert!(env_dump.contains(&format!("OPENCODE_SERVER_PASSWORD={password}")));
        assert!(env_dump.contains("OPENCODE_CONFIG_CONTENT="));

        #[cfg(unix)]
        {
            let state_mode = fs::metadata(&state_path).unwrap().permissions().mode() & 0o777;
            let config_mode = fs::metadata(state["config_content_path"].as_str().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(state_mode, 0o600);
            assert_eq!(config_mode, 0o600);
        }

        terminate_pid(result.pid).unwrap();
        wait_until_pid_stops(result.pid).await;
    }

    #[tokio::test]
    async fn launch_server_bridge_reuses_existing_live_state_without_respawn() {
        let temp = TempDir::new().unwrap();
        let cwd = temp.path().join("project");
        fs::create_dir(&cwd).unwrap();
        let (server_url, mut requests) = spawn_launch_server("ses_reuse", false).await;
        let fake = write_fake_opencode_script(temp.path(), &server_url);
        let config = test_launch_config(
            temp.path(),
            cwd,
            fake.path.clone(),
            None,
            Duration::from_secs(5),
        );

        let first = launch_server_bridge(config.clone()).await.unwrap();
        let second = launch_server_bridge(config).await.unwrap();

        assert_eq!(first.provider_session_id, "ses_reuse");
        assert_eq!(second.provider_session_id, "ses_reuse");
        assert_eq!(second.pid, first.pid);
        assert_eq!(fs::read_to_string(&fake.count_path).unwrap(), "spawn\n");

        let _first_health = recv_request(&mut requests).await;
        let _create = recv_request(&mut requests).await;
        let second_health = recv_request(&mut requests).await;
        assert_eq!(second_health.target, "/global/health");

        terminate_pid(first.pid).unwrap();
        wait_until_pid_stops(first.pid).await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn concurrent_launches_serialize_to_one_fake_opencode_process() {
        let temp = TempDir::new().unwrap();
        let cwd = temp.path().join("project");
        fs::create_dir(&cwd).unwrap();
        let (server_url, _requests) = spawn_launch_server("ses_concurrent", false).await;
        let fake = write_fake_opencode_script(temp.path(), &server_url);
        let config = Arc::new(test_launch_config(
            temp.path(),
            cwd,
            fake.path.clone(),
            None,
            Duration::from_secs(5),
        ));

        let first_config = (*config).clone();
        let second_config = (*config).clone();
        let first_task = tokio::spawn(launch_server_bridge(first_config));
        let second_task = tokio::spawn(launch_server_bridge(second_config));
        let (first, second) = tokio::join!(first_task, second_task);
        let first = first.unwrap().unwrap();
        let second = second.unwrap().unwrap();

        assert_eq!(first.provider_session_id, "ses_concurrent");
        assert_eq!(second.provider_session_id, "ses_concurrent");
        assert_eq!(second.pid, first.pid);
        assert_eq!(fs::read_to_string(&fake.count_path).unwrap(), "spawn\n");

        terminate_pid(first.pid).unwrap();
        wait_until_pid_stops(first.pid).await;
    }

    #[tokio::test]
    async fn launch_failure_terminates_spawned_fake_opencode_process() {
        let temp = TempDir::new().unwrap();
        let cwd = temp.path().join("project");
        fs::create_dir(&cwd).unwrap();
        let (server_url, _requests) = spawn_launch_server("ses_fail", true).await;
        let fake = write_fake_opencode_script(temp.path(), &server_url);

        let error = launch_server_bridge(test_launch_config(
            temp.path(),
            cwd,
            fake.path,
            None,
            Duration::from_secs(5),
        ))
        .await
        .unwrap_err();

        assert!(error.to_string().contains("session.create"));
        let pid: u32 = fs::read_to_string(&fake.pid_path)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        wait_until_pid_stops(pid).await;
    }

    #[tokio::test]
    async fn launch_rejects_missing_binary_and_empty_token_before_spawn() {
        let temp = TempDir::new().unwrap();
        let cwd = temp.path().join("project");
        fs::create_dir(&cwd).unwrap();
        let missing = temp.path().join("missing-opencode");
        let mut config =
            test_launch_config(temp.path(), cwd, missing, None, Duration::from_millis(100));

        let error = launch_server_bridge(config.clone()).await.unwrap_err();
        assert!(error.to_string().contains("not an executable file"));

        let fake = write_fake_opencode_script(temp.path(), "http://127.0.0.1:9");
        config.opencode_bin = Some(fake.path);
        config.api_token = " ".to_string();
        let error = launch_server_bridge(config).await.unwrap_err();
        assert!(error.to_string().contains("api token is required"));
        assert!(!fake.count_path.exists());
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

    fn test_state_write(
        provider_session_id: &str,
        server_url: &str,
        pid: u32,
        temp_root: &Path,
    ) -> OpenCodeServerStateWrite {
        OpenCodeServerStateWrite {
            schema_version: STATE_SCHEMA_VERSION,
            session_id: SESSION_ID.to_string(),
            provider_session_id: provider_session_id.to_string(),
            server_url: server_url.to_string(),
            pid,
            cwd: temp_root.display().to_string(),
            username: DEFAULT_USERNAME.to_string(),
            password: "secret-password".to_string(),
            log_path: temp_root.join("server.log").display().to_string(),
            config_content_path: "/tmp/config.json".to_string(),
            started_at: "2026-06-28T00:00:00Z".to_string(),
            updated_at: "2026-06-28T00:00:00Z".to_string(),
            process_start_time: "Sun Jun 28 00:00:00 2026".to_string(),
            process_command: "opencode serve --hostname 127.0.0.1".to_string(),
            launch_mode: LAUNCH_MODE_DETACHED.to_string(),
            owner_wrapper_pid: 0,
            owner_wrapper_start_time: String::new(),
        }
    }

    fn test_launch_config(
        config_dir: &Path,
        cwd: PathBuf,
        opencode_bin: PathBuf,
        display_name: Option<String>,
        wait_ready: Duration,
    ) -> OpenCodeLaunchConfig {
        OpenCodeLaunchConfig {
            session_id: SESSION_ID.to_string(),
            cwd,
            api_url: "https://longhouse.test".to_string(),
            api_token: "zdt_test_token".to_string(),
            device_id: "test-device".to_string(),
            display_name,
            wait_ready,
            config_dir: Some(config_dir.to_path_buf()),
            opencode_bin: Some(opencode_bin),
            opencode_config_content: None,
        }
    }

    fn write_fake_opencode_script(root: &Path, server_url: &str) -> FakeOpenCodeScript {
        let script_dir = root.join("bin");
        fs::create_dir_all(&script_dir).unwrap();
        let path = script_dir.join("opencode");
        let count_path = root.join("spawn-count.txt");
        let env_path = root.join("opencode-env.txt");
        let argv_path = root.join("opencode-argv.txt");
        let pid_path = root.join("opencode-pid.txt");
        let script = format!(
            "#!/bin/sh\n\
             echo \"$$\" > {pid}\n\
             echo spawn >> {count}\n\
             printf '%s\\n' \"$@\" > {argv}\n\
             env | sort > {env}\n\
             echo {listen}\n\
             while :; do sleep 60; done\n",
            pid = shell_quote_path(&pid_path),
            count = shell_quote_path(&count_path),
            argv = shell_quote_path(&argv_path),
            env = shell_quote_path(&env_path),
            listen = shell_quote(&format!("{SERVER_LOG_MARKER}{server_url}")),
        );
        fs::write(&path, script).unwrap();
        #[cfg(unix)]
        {
            let mut perms = fs::metadata(&path).unwrap().permissions();
            perms.set_mode(0o755);
            fs::set_permissions(&path, perms).unwrap();
        }
        FakeOpenCodeScript {
            path,
            count_path,
            env_path,
            argv_path,
            pid_path,
        }
    }

    fn shell_quote(value: impl AsRef<str>) -> String {
        format!("'{}'", value.as_ref().replace('\'', "'\\''"))
    }

    fn shell_quote_path(path: &Path) -> String {
        shell_quote(path.display().to_string())
    }

    async fn spawn_launch_server(
        provider_session_id: &'static str,
        fail_create: bool,
    ) -> (String, mpsc::UnboundedReceiver<RecordedRequest>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = mpsc::unbounded_channel();
        tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
                let request = read_recorded_request(&mut stream).await;
                let _ = tx.send(request.clone());
                let response = if request.target.starts_with("/global/health") {
                    http_json_response(200, r#"{"healthy":true}"#)
                } else if request.target.starts_with("/session") && !fail_create {
                    http_json_response(200, &format!(r#"{{"id":"{provider_session_id}"}}"#))
                } else if request.target.starts_with("/session") {
                    http_json_response(500, r#"{"error":"create failed"}"#)
                } else {
                    http_json_response(404, r#"{"error":"missing"}"#)
                };
                stream.write_all(response.as_bytes()).await.unwrap();
            }
        });
        (format!("http://{addr}"), rx)
    }

    async fn read_recorded_request(stream: &mut tokio::net::TcpStream) -> RecordedRequest {
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
        parse_request(&bytes)
    }

    fn http_json_response(status: u16, body: &str) -> String {
        let reason = if status == 200 { "OK" } else { "ERROR" };
        format!(
            "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    }

    async fn recv_request(rx: &mut mpsc::UnboundedReceiver<RecordedRequest>) -> RecordedRequest {
        timeout(Duration::from_secs(3), rx.recv())
            .await
            .unwrap()
            .unwrap()
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

    fn urlencoding_like(value: &str) -> String {
        let mut url = Url::parse("http://example.test/session").unwrap();
        url.query_pairs_mut().append_pair("directory", value);
        url.query()
            .unwrap()
            .trim_start_matches("directory=")
            .to_string()
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
