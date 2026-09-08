//! Stock Pi TUI Helm launcher.
//!
//! Pi remains the terminal owner and the provider runtime. Longhouse owns only
//! the launch transaction, a private Unix channel, and the exact process group
//! it spawned. The extension is process-scoped and uses Pi's native
//! ExtensionAPI; no Pi settings or global extension directories are changed.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, IsTerminal, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::os::unix::process::CommandExt;
use uuid::Uuid;

use crate::managed_identity::ManagedIdentity;
use crate::managed_identity_contract::ManagedProvider;
use crate::managed_launch_lifecycle::{
    register_managed_launch_with_timeout, spawn_managed_registration_retry, DeferredNotices,
    ManagedLaunchTransaction, FOREGROUND_REGISTRATION_TIMEOUT,
};
use crate::managed_launch_payload::{
    ManagedLaunchProvenance, ManagedLaunchRegistration, PermissionMode,
};
use crate::managed_terminal::{terminal_state_for_exit, ManagedTerminalEvent};
use crate::pi_helm_control::PI_HELM_TRANSPORT;

const STATE_DIR_NAME: &str = "managed-local/pi-helm";
const EXTENSION_FILE_NAME: &str = "longhouse-pi-helm.ts";
const CONTROL_READY_NOTICE_DELAY: Duration = Duration::from_secs(10);
const SOCKET_READ_TIMEOUT: Duration = Duration::from_secs(8);
const COMMAND_TIMEOUT: Duration = Duration::from_secs(8);
const MAX_FRAME_BYTES: usize = 512 * 1024;
const MAX_PENDING_COMMANDS: usize = 64;

const EXTENSION_ASSET: &str = include_str!("../assets/longhouse-pi-helm.ts");

pub struct LaunchConfig {
    pub cwd: PathBuf,
    pub prompt: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub session_dir: Option<PathBuf>,
    pub resume_session: Option<String>,
    pub pi_bin: Option<String>,
    pub url: Option<String>,
    pub token: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct PiHelmStateFile {
    schema_version: u64,
    session_id: String,
    run_id: String,
    provider: String,
    native_provider: Option<String>,
    model: Option<String>,
    provider_session_id: String,
    session_file: Option<String>,
    cwd: String,
    provider_binary: String,
    session_dir: String,
    socket_path: String,
    channel_token: String,
    connection_id: String,
    lease_generation: String,
    launcher_pid: u32,
    launcher_process_start_time: Option<String>,
    provider_pid: Option<u32>,
    provider_process_start_time: Option<String>,
    status: String,
    phase: String,
    tool_name: Option<String>,
    ready: bool,
    terminal_state: Option<String>,
    terminal_reason: Option<String>,
    exit_code: Option<i32>,
    started_at: String,
    updated_at: String,
}

#[derive(Debug, Clone)]
struct SharedChannelState {
    state: PiHelmStateFile,
    extension_sender: Option<mpsc::Sender<Value>>,
    extension_connection_id: Option<String>,
    pending: HashMap<String, mpsc::Sender<Value>>,
}

#[derive(Clone)]
struct PiHelmServer {
    shared: Arc<Mutex<SharedChannelState>>,
    socket_path: PathBuf,
    state_path: PathBuf,
    persist_lock: Arc<Mutex<()>>,
    socket_dir: PathBuf,
    stop: Arc<AtomicBool>,
    terminate_requested: Arc<AtomicBool>,
}

impl PiHelmServer {
    fn start(
        mut state: PiHelmStateFile,
        socket_path: PathBuf,
        socket_dir: PathBuf,
        state_path: PathBuf,
    ) -> Result<Self> {
        ensure_private_owned_dir(&socket_dir)?;
        let _ = fs::remove_file(&socket_path);
        let listener = std::os::unix::net::UnixListener::bind(&socket_path)
            .with_context(|| format!("bind Pi Helm socket {}", socket_path.display()))?;
        set_private_file(&socket_path)?;
        state.socket_path = socket_path.display().to_string();
        let server = Self {
            shared: Arc::new(Mutex::new(SharedChannelState {
                state,
                extension_sender: None,
                extension_connection_id: None,
                pending: HashMap::new(),
            })),
            socket_path,
            state_path,
            persist_lock: Arc::new(Mutex::new(())),
            socket_dir,
            stop: Arc::new(AtomicBool::new(false)),
            terminate_requested: Arc::new(AtomicBool::new(false)),
        };
        server.persist_state()?;
        let acceptor = server.clone();
        thread::spawn(move || {
            for stream in listener.incoming() {
                if acceptor.stop.load(Ordering::Acquire) {
                    break;
                }
                let Ok(stream) = stream else { continue };
                let handler = acceptor.clone();
                thread::spawn(move || handler.handle_connection(stream));
            }
        });
        Ok(server)
    }

    fn persist_state(&self) -> Result<()> {
        let _persist = self
            .persist_lock
            .lock()
            .expect("Pi Helm state persist mutex poisoned");
        let state = self
            .shared
            .lock()
            .expect("Pi Helm state mutex poisoned")
            .state
            .clone();
        write_json_private(&self.state_path, &state)
    }

    fn handle_connection(&self, stream: std::os::unix::net::UnixStream) {
        let _ = stream.set_read_timeout(Some(SOCKET_READ_TIMEOUT));
        let _ = stream.set_write_timeout(Some(SOCKET_READ_TIMEOUT));
        let Ok(clone) = stream.try_clone() else {
            return;
        };
        let mut reader = BufReader::new(stream);
        let Ok(Some(frame)) = read_frame(&mut reader) else {
            return;
        };
        if frame.get("kind").and_then(Value::as_str) == Some("extension_hello") {
            let _ = reader.get_mut().set_read_timeout(None);
            self.handle_extension(clone, reader, frame);
        } else {
            let response = self.handle_remote_command(frame);
            let _ = reader
                .get_mut()
                .write_all(format!("{}\n", response).as_bytes());
        }
    }

    fn handle_extension(
        &self,
        mut writer_stream: std::os::unix::net::UnixStream,
        mut reader: BufReader<std::os::unix::net::UnixStream>,
        hello: Value,
    ) {
        let auth_ok = self.authority_matches(&hello)
            && hello
                .get("provider_session_id")
                .and_then(Value::as_str)
                .is_some();
        if !auth_ok {
            let _ = writer_stream.write_all(b"{\"ok\":false,\"error\":{\"code\":\"stale_channel\",\"message\":\"Pi Helm channel identity rejected\"}}\n");
            return;
        }
        let connection_id = Uuid::new_v4().to_string();
        let (command_tx, command_rx) = mpsc::channel::<Value>();
        {
            let mut guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
            fail_pending_locked(&mut guard, "Pi Helm extension connection replaced");
            guard.extension_sender = Some(command_tx);
            guard.extension_connection_id = Some(connection_id.clone());
            guard.state.connection_id = connection_id.clone();
            guard.state.lease_generation = Uuid::new_v4().to_string();
            guard.state.status = "starting".into();
            guard.state.ready = false;
            guard.state.updated_at = Utc::now().to_rfc3339();
        }
        let writer_connection = connection_id.clone();
        let writer = thread::spawn(move || {
            for command in command_rx {
                if writer_stream
                    .write_all(format!("{}\n", command).as_bytes())
                    .is_err()
                {
                    break;
                }
                let _ = writer_stream.flush();
            }
        });
        if let Err(error) = self.update_identity(&connection_id, &hello) {
            self.send_extension_frame(
                &connection_id,
                json!({
                    "kind": "extension_ready",
                    "ok": false,
                    "error": {"code": "source_binding_failed", "message": error.to_string()},
                }),
            );
            self.disconnect_extension(&connection_id);
            let _ = writer.join();
            return;
        }
        let state = self.current_state();
        self.send_extension_frame(
            &connection_id,
            json!({
                "kind": "extension_ready",
                "ok": true,
                "session_id": state.session_id,
                "provider_session_id": state.provider_session_id,
                "connection_id": state.connection_id,
                "lease_generation": state.lease_generation,
            }),
        );
        loop {
            let Ok(Some(frame)) = read_frame(&mut reader) else {
                break;
            };
            self.handle_extension_frame(&connection_id, frame);
        }
        self.disconnect_extension(&writer_connection);
        let _ = writer.join();
        let _ = self.persist_state();
    }

    fn disconnect_extension(&self, connection_id: &str) {
        let mut guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
        if guard.extension_connection_id.as_deref() != Some(connection_id) {
            return;
        }
        fail_pending_locked(&mut guard, "Pi Helm extension channel disconnected");
        guard.extension_sender = None;
        guard.extension_connection_id = None;
        guard.state.ready = false;
        if guard.state.status != "stopped" {
            guard.state.status = "degraded".into();
        }
        guard.state.updated_at = Utc::now().to_rfc3339();
        drop(guard);
        let _ = self.persist_state();
    }

    fn authority_matches(&self, frame: &Value) -> bool {
        let guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
        frame.get("auth_token").and_then(Value::as_str) == Some(guard.state.channel_token.as_str())
            && frame.get("session_id").and_then(Value::as_str)
                == Some(guard.state.session_id.as_str())
    }

    fn extension_authority_matches(&self, connection_id: &str, frame: &Value) -> bool {
        let guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
        guard.extension_connection_id.as_deref() == Some(connection_id)
            && frame.get("auth_token").and_then(Value::as_str)
                == Some(guard.state.channel_token.as_str())
            && frame.get("session_id").and_then(Value::as_str)
                == Some(guard.state.session_id.as_str())
            && frame.get("connection_id").and_then(Value::as_str)
                == Some(guard.state.connection_id.as_str())
            && frame.get("lease_generation").and_then(Value::as_str)
                == Some(guard.state.lease_generation.as_str())
    }

    fn send_extension_frame(&self, connection_id: &str, frame: Value) {
        let sender = {
            let guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
            (guard.extension_connection_id.as_deref() == Some(connection_id))
                .then(|| guard.extension_sender.clone())
                .flatten()
        };
        if let Some(sender) = sender {
            let _ = sender.send(frame);
        }
    }

    fn current_authority_frame(&self) -> Value {
        let guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
        json!({
            "auth_token": guard.state.channel_token,
            "session_id": guard.state.session_id,
            "provider_session_id": guard.state.provider_session_id,
            "connection_id": guard.state.connection_id,
            "lease_generation": guard.state.lease_generation,
        })
    }

    fn update_identity(&self, connection_id: &str, frame: &Value) -> Result<()> {
        let provider_session_id = frame
            .get("provider_session_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.trim().is_empty())
            .context("Pi extension frame has no provider session id")?;
        let session_file = frame
            .get("session_file")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .context("Pi extension frame has no native session file")?;
        let (session_id, previous_provider_session_id) = {
            let guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
            anyhow::ensure!(
                guard.extension_connection_id.as_deref() == Some(connection_id),
                "Pi Helm extension connection was replaced"
            );
            (
                guard.state.session_id.clone(),
                guard.state.provider_session_id.clone(),
            )
        };
        let db_path = crate::config::get_agent_db_path()?;
        let conn = crate::state::db::open_client_connection(&db_path, Duration::from_millis(500))?;
        crate::pi_session::bind_source_for_thread(
            &conn,
            Path::new(session_file),
            &session_id,
            provider_session_id,
        )?;
        let source_path = PathBuf::from(session_file);
        let mut guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
        anyhow::ensure!(
            guard.extension_connection_id.as_deref() == Some(connection_id),
            "Pi Helm extension connection was replaced during source binding"
        );
        guard.state.provider_session_id = provider_session_id.to_string();
        guard.state.session_file = Some(source_path.display().to_string());
        guard.state.updated_at = Utc::now().to_rfc3339();
        drop(guard);
        self.persist_state()?;
        self.publish_binding(
            &source_path,
            provider_session_id,
            previous_provider_session_id != provider_session_id,
        )?;
        Ok(())
    }

    fn handle_extension_frame(&self, connection_id: &str, frame: Value) {
        let kind = frame
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if !self.extension_authority_matches(connection_id, &frame) {
            return;
        }
        if kind != "session_start" {
            let provider_matches = {
                let guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
                frame.get("provider_session_id").and_then(Value::as_str)
                    == Some(guard.state.provider_session_id.as_str())
            };
            if !provider_matches {
                return;
            }
        }
        match kind {
            "extension_hello" | "session_start" => {
                let session_matches = {
                    let guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
                    frame.get("session_id").and_then(Value::as_str)
                        == Some(guard.state.session_id.as_str())
                };
                if session_matches {
                    if self.update_identity(connection_id, &frame).is_ok() {
                        let mut guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
                        guard.state.status = "ready".into();
                        guard.state.ready = true;
                        guard.state.updated_at = Utc::now().to_rfc3339();
                        drop(guard);
                        let _ = self.persist_state();
                        if kind == "session_start" {
                            let mut ack = self.current_authority_frame();
                            ack["kind"] = json!("session_start_ack");
                            ack["ok"] = json!(true);
                            if let Some(request_id) = frame.get("request_id") {
                                ack["request_id"] = request_id.clone();
                            }
                            self.send_extension_frame(connection_id, ack);
                        }
                    }
                }
            }
            "command_result" => {
                let request_id = frame
                    .get("request_id")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if let Some(sender) = self
                    .shared
                    .lock()
                    .expect("Pi Helm state mutex poisoned")
                    .pending
                    .remove(request_id)
                {
                    let _ = sender.send(frame);
                }
            }
            "activity" => {
                let phase = frame
                    .get("phase")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown");
                let tool_name = frame
                    .get("tool_name")
                    .and_then(Value::as_str)
                    .map(str::to_string);
                let mut guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
                if guard.state.phase == phase
                    && guard.state.tool_name.as_deref() == tool_name.as_deref()
                {
                    return;
                }
                guard.state.phase = phase.to_string();
                guard.state.tool_name = tool_name.clone();
                guard.state.updated_at = Utc::now().to_rfc3339();
                drop(guard);
                let _ = self.persist_state();
                self.publish_phase(phase, tool_name);
            }
            "session_shutdown" => self.disconnect_extension(connection_id),
            _ => {}
        }
    }

    fn handle_remote_command(&self, frame: Value) -> Value {
        let kind = frame
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if !matches!(kind.as_str(), "send" | "steer" | "abort" | "terminate") {
            return channel_error("bad_request", "unknown Pi Helm command");
        }
        let request_id = Uuid::new_v4().to_string();
        let (sender, receiver) = mpsc::channel();
        {
            let mut guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
            if !remote_authority_matches_locked(&guard, &frame) {
                return channel_error("stale_channel", "Pi Helm channel identity rejected");
            }
            let Some(extension) = guard.extension_sender.clone() else {
                return channel_error(
                    "session_not_attached",
                    "Pi extension channel is not connected",
                );
            };
            if guard.pending.len() >= MAX_PENDING_COMMANDS {
                return channel_error("command_busy", "Pi Helm command queue is full");
            }
            guard.pending.insert(request_id.clone(), sender);
            let mut command = frame;
            command["request_id"] = json!(request_id);
            if extension.send(command).is_err() {
                guard.pending.remove(&request_id);
                return channel_error("session_not_attached", "Pi extension channel is closed");
            }
        }
        match receiver.recv_timeout(COMMAND_TIMEOUT) {
            Ok(response) => {
                if kind == "terminate" && response.get("ok").and_then(Value::as_bool) == Some(true)
                {
                    self.terminate_requested.store(true, Ordering::Release);
                }
                response
            }
            Err(_) => {
                self.shared
                    .lock()
                    .expect("Pi Helm state mutex poisoned")
                    .pending
                    .remove(&request_id);
                channel_error(
                    "command_failed",
                    "Pi extension did not acknowledge the command",
                )
            }
        }
    }

    fn publish_binding(
        &self,
        source_path: &Path,
        provider_session_id: &str,
        replacement: bool,
    ) -> Result<()> {
        let state = self.current_state();
        let outbox = crate::config::get_agent_runtime_events_outbox_dir()?;
        crate::outbox::enqueue_runtime_event(
            &outbox,
            &json!({
                "runtime_key": format!("pi:{}", state.session_id),
                "session_id": state.session_id,
                "provider": "pi",
                "run_id": state.run_id,
                "source": "pi_helm_channel",
                "kind": "binding_signal",
                "occurred_at": Utc::now().to_rfc3339(),
                "dedupe_key": format!("pi-helm:{}:{}:{}", state.session_id, state.run_id, provider_session_id),
                "payload": {
                    "provider_session_id": provider_session_id,
                    "source_path": source_path,
                    "managed_transport": PI_HELM_TRANSPORT,
                    "execution_lifetime": "interactive",
                    "conversation_reset": replacement,
                }
            }),
        )?;
        wake_transcript_shipper(&state, source_path, provider_session_id, "binding");
        Ok(())
    }

    fn publish_phase(&self, phase: &str, tool_name: Option<String>) {
        let state = self.current_state();
        if let Ok(db_path) = crate::config::get_agent_db_path() {
            if let Ok(conn) =
                crate::state::db::open_client_connection(&db_path, Duration::from_millis(250))
            {
                let signal = crate::state::session_phase::SessionPhaseSignal {
                    session_id: state.session_id.clone(),
                    provider: "pi".into(),
                    phase: phase.into(),
                    tool_name: tool_name.clone(),
                    source: PI_HELM_TRANSPORT.into(),
                    observed_at: Utc::now(),
                };
                let _ = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal);
            }
        }
        if let Ok(outbox) = crate::config::get_agent_runtime_events_outbox_dir() {
            let _ = crate::outbox::enqueue_runtime_event(
                &outbox,
                &json!({
                    "runtime_key": format!("pi:{}", state.session_id),
                    "session_id": state.session_id,
                    "provider": "pi",
                    "run_id": state.run_id,
                    "source": "pi_helm_channel",
                    "kind": "phase_signal",
                    "phase": phase,
                    "tool_name": tool_name,
                    "occurred_at": Utc::now().to_rfc3339(),
                    "dedupe_key": format!("pi-helm:{}:{}:phase:{}:{}", state.session_id, state.run_id, phase, state.updated_at),
                    "payload": {
                        "managed_transport": PI_HELM_TRANSPORT,
                        "execution_lifetime": "interactive",
                        "structured_remote_approval": false,
                    }
                }),
            );
        }
        wake_transcript_shipper(
            &state,
            Path::new(&state.session_file.clone().unwrap_or_default()),
            &state.provider_session_id,
            "phase",
        );
    }

    fn is_ready(&self) -> bool {
        self.shared
            .lock()
            .expect("Pi Helm state mutex poisoned")
            .state
            .ready
    }

    fn mark_stopped(&self, exit_code: Option<i32>, reason: &str) -> Result<()> {
        let mut guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
        guard.state.status = "stopped".into();
        guard.state.ready = false;
        guard.state.phase = "idle".into();
        guard.state.terminal_state = Some(
            exit_code
                .map(terminal_state_for_exit)
                .unwrap_or("process_gone")
                .into(),
        );
        guard.state.terminal_reason = Some(reason.into());
        guard.state.exit_code = exit_code;
        guard.state.updated_at = Utc::now().to_rfc3339();
        drop(guard);
        self.persist_state()
    }

    fn shutdown(&self) {
        self.stop.store(true, Ordering::Release);
        let mut guard = self.shared.lock().expect("Pi Helm state mutex poisoned");
        fail_pending_locked(&mut guard, "Pi Helm server is shutting down");
        guard.extension_sender = None;
        guard.extension_connection_id = None;
        drop(guard);
        let _ = std::os::unix::net::UnixStream::connect(&self.socket_path);
        let _ = fs::remove_file(&self.socket_path);
        let _ = fs::remove_dir(&self.socket_dir);
    }

    fn current_state(&self) -> PiHelmStateFile {
        self.shared
            .lock()
            .expect("Pi Helm state mutex poisoned")
            .state
            .clone()
    }
}

fn channel_error(code: &str, message: &str) -> Value {
    json!({"ok": false, "error": {"code": code, "message": message}})
}

fn read_frame(reader: &mut BufReader<std::os::unix::net::UnixStream>) -> Result<Option<Value>> {
    let mut bytes = Vec::with_capacity(4096);
    let mut one = [0_u8; 1];
    loop {
        let count = reader.read(&mut one)?;
        if count == 0 {
            return Ok(if bytes.is_empty() {
                None
            } else {
                Some(
                    serde_json::from_slice(&bytes)
                        .context("Pi Helm frame ended without a newline")?,
                )
            });
        }
        if one[0] == b'\n' {
            if bytes.is_empty() {
                continue;
            }
            return Ok(Some(
                serde_json::from_slice(&bytes).context("invalid Pi Helm frame")?,
            ));
        }
        bytes.push(one[0]);
        if bytes.len() > MAX_FRAME_BYTES {
            anyhow::bail!("Pi Helm frame exceeds {} bytes", MAX_FRAME_BYTES);
        }
    }
}

fn remote_authority_matches_locked(state: &SharedChannelState, frame: &Value) -> bool {
    state.state.ready
        && state.extension_sender.is_some()
        && frame.get("auth_token").and_then(Value::as_str)
            == Some(state.state.channel_token.as_str())
        && frame.get("session_id").and_then(Value::as_str) == Some(state.state.session_id.as_str())
        && frame.get("provider_session_id").and_then(Value::as_str)
            == Some(state.state.provider_session_id.as_str())
        && frame.get("connection_id").and_then(Value::as_str)
            == Some(state.state.connection_id.as_str())
        && frame.get("lease_generation").and_then(Value::as_str)
            == Some(state.state.lease_generation.as_str())
}

fn fail_pending_locked(state: &mut SharedChannelState, message: &str) {
    let pending = std::mem::take(&mut state.pending);
    for (_, sender) in pending {
        let _ = sender.send(channel_error("stale_channel", message));
    }
}

fn wake_transcript_shipper(
    state: &PiHelmStateFile,
    source_path: &Path,
    provider_session_id: &str,
    wake_reason: &str,
) {
    #[cfg(unix)]
    {
        let Ok(socket_path) = crate::config::get_agent_transcript_wake_socket_path() else {
            return;
        };
        if !socket_path.exists() || !source_path.is_absolute() {
            return;
        }
        let payload = json!({
            "provider": "pi",
            "path": source_path,
            "phase": state.phase,
            "session_id": state.session_id,
            "run_id": state.run_id,
            "provider_turn_id": provider_session_id,
            "wake_reason": wake_reason,
            "observed_at_ms": Utc::now().timestamp_millis(),
            "file_len_hint": fs::metadata(source_path).ok().map(|metadata| metadata.len()),
        });
        if let Ok(mut stream) = std::os::unix::net::UnixStream::connect(socket_path) {
            let _ = stream.set_write_timeout(Some(Duration::from_millis(50)));
            let _ = stream.write_all(payload.to_string().as_bytes());
        }
    }
}

fn set_private_dir(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn ensure_private_owned_dir(path: &Path) -> Result<()> {
    fs::create_dir(path)
        .with_context(|| format!("create private Pi Helm socket directory {}", path.display()))?;
    set_private_dir(path)?;
    let metadata = fs::symlink_metadata(path)?;
    anyhow::ensure!(
        metadata.file_type().is_dir(),
        "Pi Helm socket directory is not a directory"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        anyhow::ensure!(
            metadata.uid() == unsafe { libc::geteuid() },
            "Pi Helm socket directory is not owned by the current user"
        );
    }
    Ok(())
}

fn set_private_file(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn write_json_private<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let parent = path.parent().context("Pi Helm state has no parent")?;
    fs::create_dir_all(parent)?;
    set_private_dir(parent)?;
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("state");
    let temporary = path.with_file_name(format!(".{name}.tmp.{}", Uuid::new_v4()));
    fs::write(
        &temporary,
        format!("{}\n", serde_json::to_string_pretty(value)?),
    )?;
    set_private_file(&temporary)?;
    fs::rename(temporary, path)?;
    set_private_file(path)?;
    Ok(())
}

fn process_start_time(pid: Option<u32>) -> Option<String> {
    crate::turn_claims::process_start_time_for_pid(pid)
}

fn state_dir() -> Result<PathBuf> {
    crate::config::get_longhouse_home().map(|home| home.join(STATE_DIR_NAME))
}

fn socket_path(session_id: &str) -> Result<(PathBuf, PathBuf)> {
    let suffix = session_id.split('-').next().unwrap_or("session");
    #[cfg(unix)]
    let user_id = unsafe { libc::geteuid() };
    #[cfg(not(unix))]
    let user_id = 0;
    #[cfg(unix)]
    let temp_root = PathBuf::from("/tmp");
    #[cfg(not(unix))]
    let temp_root = std::env::temp_dir();
    let directory = temp_root.join(format!(
        "longhouse-pi-{}-{suffix}-{}",
        user_id,
        Uuid::new_v4().simple()
    ));
    Ok((directory.join("channel.sock"), directory))
}

pub(crate) fn resolve_binary(explicit: Option<String>) -> Result<String> {
    let candidate = explicit
        .or_else(|| std::env::var("LONGHOUSE_PI_BIN").ok())
        .unwrap_or_else(|| "pi".into());
    let path = PathBuf::from(&candidate);
    if path.components().count() > 1 {
        return path
            .is_file()
            .then(|| path.display().to_string())
            .context("--pi-bin is not a file");
    }
    for directory in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        let found = directory.join(&candidate);
        if found.is_file() {
            return Ok(found.display().to_string());
        }
    }
    anyhow::bail!("Pi executable not found. Install stock `pi` or set --pi-bin.")
}

fn launch_lock(session_id: &str) -> Result<File> {
    let path = state_dir()?.join(format!("{session_id}.lock"));
    fs::create_dir_all(path.parent().context("Pi Helm lock has no parent")?)?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .open(path)?;
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            anyhow::bail!("Pi Helm session {session_id} is already attached or resuming");
        }
    }
    Ok(file)
}

fn read_resume_state(session_id: &str, cwd: &Path, _binary: &str) -> Result<PiHelmStateFile> {
    let path = state_dir()?.join(format!("{session_id}.json"));
    let state: PiHelmStateFile = serde_json::from_slice(&fs::read(&path).with_context(|| {
        format!(
            "Pi Resume is unavailable because its retained launch contract is missing: {}",
            path.display()
        )
    })?)?;
    if state.provider != "pi"
        || state.session_id != session_id
        || !matches!(
            state.status.as_str(),
            "running" | "ready" | "degraded" | "stopped"
        )
    {
        anyhow::bail!("Pi retained launch contract is not resumable");
    }
    let recorded_cwd = PathBuf::from(&state.cwd);
    if fs::canonicalize(recorded_cwd).ok() != fs::canonicalize(cwd).ok() {
        anyhow::bail!("Pi Resume must run from its retained workspace");
    }
    let inventory = crate::process_identity::try_collect_process_facts_by_pid()
        .context("Pi Resume cannot verify prior process identities")?;
    for (label, pid, recorded) in [
        (
            "launcher",
            Some(state.launcher_pid),
            state.launcher_process_start_time.as_deref(),
        ),
        (
            "provider",
            state.provider_pid,
            state.provider_process_start_time.as_deref(),
        ),
    ] {
        let pid = pid.context("Pi retained launch contract has no process identity")?;
        let recorded =
            recorded.context("Pi retained launch contract has no process birth identity")?;
        if let Some(fact) = inventory.get(&pid) {
            if fact.lstart == recorded {
                anyhow::bail!(
                    "Pi Resume refused while the previous {label} execution owner is still alive"
                );
            }
        }
    }
    Ok(state)
}

struct PiTerminal {
    fd: libc::c_int,
    parent_pgrp: libc::pid_t,
    attributes: libc::termios,
    old_sigttou: libc::sighandler_t,
}

impl PiTerminal {
    fn capture() -> Result<Self> {
        use std::os::unix::io::AsRawFd;
        let fd = std::io::stdin().as_raw_fd();
        let mut attributes = std::mem::MaybeUninit::<libc::termios>::uninit();
        if unsafe { libc::tcgetattr(fd, attributes.as_mut_ptr()) } != 0 {
            return Err(std::io::Error::last_os_error()).context("read invoking terminal state");
        }
        Ok(Self {
            fd,
            parent_pgrp: unsafe { libc::tcgetpgrp(fd) },
            attributes: unsafe { attributes.assume_init() },
            old_sigttou: unsafe { libc::signal(libc::SIGTTOU, libc::SIG_IGN) },
        })
    }

    fn give_to(&self, pgid: libc::pid_t) -> Result<()> {
        if self.parent_pgrp >= 0 && unsafe { libc::tcsetpgrp(self.fd, pgid) } != 0 {
            return Err(std::io::Error::last_os_error()).context("hand terminal to stock Pi");
        }
        // The child may already have stopped on its first background TTY read.
        unsafe { libc::kill(-pgid, libc::SIGCONT) };
        Ok(())
    }
}

impl Drop for PiTerminal {
    fn drop(&mut self) {
        unsafe {
            if self.parent_pgrp >= 0 {
                libc::tcsetpgrp(self.fd, self.parent_pgrp);
            }
            libc::tcsetattr(self.fd, libc::TCSANOW, &self.attributes);
            libc::signal(libc::SIGTTOU, self.old_sigttou);
        }
    }
}

fn run_pi_provider(
    command: &mut Command,
    server: &PiHelmServer,
    after_spawn: impl FnOnce(u32) -> Result<()>,
) -> Result<i32> {
    let signal = Arc::new(AtomicUsize::new(0));
    for signal_number in [libc::SIGHUP, libc::SIGTERM, libc::SIGINT] {
        signal_hook::flag::register_usize(signal_number, signal.clone(), signal_number as usize)
            .context("install Pi Helm signal cleanup")?;
    }
    let terminal = PiTerminal::capture()?;
    #[cfg(unix)]
    command.process_group(0);
    let mut child = command.spawn().context("spawn stock Pi Helm TUI")?;
    let pid = child.id();
    let pgid = pid as libc::pid_t;
    if let Err(error) = terminal.give_to(pgid).and_then(|()| after_spawn(pid)) {
        terminate_pi_group(pgid, &mut child);
        return Err(error);
    }

    let startup_started = Instant::now();
    let mut startup_observed = false;
    let mut terminate_sent_at = None;
    let mut raw_status = 0;
    loop {
        if !startup_observed {
            if server.is_ready() {
                startup_observed = true;
            } else if startup_started.elapsed() >= CONTROL_READY_NOTICE_DELAY {
                eprintln!(
                    "Longhouse: Pi control is not ready yet; native startup remains interactive."
                );
                startup_observed = true;
            }
        }
        let externally_requested = server.terminate_requested.load(Ordering::Acquire);
        let signal_requested = signal.load(Ordering::Acquire) != 0;
        if (externally_requested || signal_requested) && terminate_sent_at.is_none() {
            terminate_pi_group_term(pgid);
            terminate_sent_at = Some(Instant::now());
        } else if terminate_sent_at.is_some_and(|sent| sent.elapsed() >= Duration::from_millis(500))
        {
            unsafe { libc::kill(-pgid, libc::SIGKILL) };
        }

        let waited = unsafe {
            libc::waitpid(
                pid as libc::pid_t,
                &mut raw_status,
                libc::WNOHANG | libc::WUNTRACED,
            )
        };
        if waited == pid as libc::pid_t {
            if libc::WIFSTOPPED(raw_status) {
                unsafe { libc::kill(-pgid, libc::SIGCONT) };
            } else {
                break;
            }
        } else if waited < 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::EINTR) {
                terminate_pi_group(pgid, &mut child);
                return Err(error).context("wait for stock Pi Helm TUI");
            }
        }
        thread::sleep(Duration::from_millis(25));
    }

    #[cfg(unix)]
    if unsafe { libc::kill(-pgid, 0) } == 0 {
        terminate_pi_group(pgid, &mut child);
    }

    let _ = child.try_wait();
    if libc::WIFEXITED(raw_status) {
        Ok(libc::WEXITSTATUS(raw_status) as i32)
    } else if libc::WIFSIGNALED(raw_status) {
        Ok(128 + libc::WTERMSIG(raw_status) as i32)
    } else {
        Ok(1)
    }
}

fn terminate_pi_group_term(pgid: libc::pid_t) {
    #[cfg(unix)]
    unsafe {
        libc::kill(-pgid, libc::SIGCONT);
        libc::kill(-pgid, libc::SIGTERM);
    }
}

fn terminate_pi_group(pgid: libc::pid_t, child: &mut std::process::Child) {
    terminate_pi_group_term(pgid);
    let deadline = Instant::now() + Duration::from_millis(500);
    while Instant::now() < deadline {
        let child_dead = child.try_wait().ok().flatten().is_some();
        if child_dead && unsafe { libc::kill(-pgid, 0) } != 0 {
            return;
        }
        thread::sleep(Duration::from_millis(25));
    }
    #[cfg(unix)]
    unsafe {
        libc::kill(-pgid, libc::SIGKILL);
    }
    let _ = child.try_wait();
}

fn write_extension_file(dir: &Path) -> Result<PathBuf> {
    fs::create_dir_all(dir)?;
    set_private_dir(dir)?;
    let path = dir.join(EXTENSION_FILE_NAME);
    fs::write(&path, EXTENSION_ASSET)?;
    set_private_file(&path)?;
    Ok(path)
}

fn registration_credentials(config: &LaunchConfig) -> Result<(String, String, String)> {
    let home = crate::config::get_longhouse_home()?;
    let machine = home.join("machine");
    let state: Value = fs::read(machine.join("state.json"))
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_else(|| json!({}));
    let url = config
        .url
        .clone()
        .or_else(|| std::env::var("LONGHOUSE_PI_HELM_URL").ok())
        .or_else(|| {
            state
                .get("runtime_url")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .filter(|value| !value.trim().is_empty())
        .context("No Longhouse URL configured. Run `longhouse auth` first.")?;
    let token = config
        .token
        .clone()
        .or_else(|| std::env::var("LONGHOUSE_PI_HELM_TOKEN").ok())
        .or_else(|| fs::read_to_string(machine.join("device-token")).ok())
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .context("No device token found. Run `longhouse auth` first.")?;
    let machine_name = state
        .get("machine_name")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| "unknown".into());
    Ok((url, token, machine_name))
}

fn enqueue_terminal_event(
    state: &PiHelmStateFile,
    machine_name: &str,
    exit_code: Option<i32>,
    reason: &str,
) -> Result<()> {
    let root = crate::config::get_longhouse_home()?;
    let runtime_key = format!("pi:{}", state.provider_session_id);
    let event = ManagedTerminalEvent {
        runtime_key: &runtime_key,
        session_id: &state.session_id,
        run_id: &state.run_id,
        provider: "pi",
        managed_transport: PI_HELM_TRANSPORT,
        provider_session_id: Some(&state.provider_session_id),
        device_id: Some(machine_name),
        source: "pi_helm_launcher",
        dedupe_prefix: "pi-helm-terminal",
        terminal_state: exit_code
            .map(terminal_state_for_exit)
            .unwrap_or("process_gone"),
        terminal_reason: reason,
        exit_code,
    }
    .to_json();
    crate::managed_terminal::enqueue(&root.join("agent/runtime-events-outbox"), &event)
}

pub fn launch(config: LaunchConfig) -> Result<i32> {
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        anyhow::bail!(
            "longhouse pi Helm needs an interactive terminal; use Console for headless turns"
        );
    }
    let requested_cwd = fs::canonicalize(&config.cwd)
        .with_context(|| format!("resolve Pi workspace {}", config.cwd.display()))?;
    let binary = resolve_binary(config.pi_bin.clone())?;
    let state_root = state_dir()?;
    fs::create_dir_all(&state_root)?;

    let (resume_state, session_id, cwd) = if let Some(session_id) = config.resume_session.as_deref()
    {
        let old = read_resume_state(session_id, &requested_cwd, &binary)?;
        (Some(old), session_id.to_string(), requested_cwd)
    } else {
        (None, Uuid::new_v4().to_string(), requested_cwd)
    };
    let effective_provider = config.provider.clone().or_else(|| {
        resume_state
            .as_ref()
            .and_then(|state| state.native_provider.clone())
    });
    let effective_model = config
        .model
        .clone()
        .or_else(|| resume_state.as_ref().and_then(|state| state.model.clone()));
    let _owner_lock = launch_lock(&session_id)?;
    let target = if let Some(old) = &resume_state {
        let provider_id = old.provider_session_id.clone();
        let file = PathBuf::from(
            old.session_file
                .as_deref()
                .context("Pi retained launch contract has no exact session file")?,
        );
        crate::pi_session::prepare_session(
            &cwd,
            Some(Path::new(&old.session_dir)),
            Some(&provider_id),
            Some(&file),
        )?
    } else {
        crate::pi_session::prepare_session(&cwd, config.session_dir.as_deref(), None, None)?
    };
    let (url, token, machine_name) = registration_credentials(&config)?;
    let run_id = Uuid::new_v4().to_string();
    let connection_id = Uuid::new_v4().to_string();
    let lease_generation = Uuid::new_v4().to_string();
    let channel_token = Uuid::new_v4().to_string();
    let (socket, socket_dir) = socket_path(&session_id)?;
    let extension_dir = state_root.join("extensions").join(&session_id);
    let extension = write_extension_file(&extension_dir)?;
    let provider_config = json!({
        "pi_provider": effective_provider.clone(),
        "model": effective_model.clone(),
        "session_dir": target.session_dir.clone(),
        "provider_session_id": target.provider_thread_id.clone(),
        "session_file": target.session_file.clone(),
    });
    let mut payload = ManagedLaunchRegistration {
        provider: "pi",
        cwd: &cwd,
        project: None,
        display_name: None,
        machine_name: &machine_name,
        permission_mode: PermissionMode::ProviderLocal,
        provenance: ManagedLaunchProvenance::interactive_helm(),
        extra: vec![
            (
                "provider_session_id",
                json!(target.provider_thread_id.clone()),
            ),
            ("session_id", json!(session_id)),
            ("run_id", json!(run_id)),
            ("connection_id", json!(connection_id)),
            ("lease_generation", json!(lease_generation)),
            ("managed_transport", json!(PI_HELM_TRANSPORT)),
            ("provider_config", provider_config),
        ],
    }
    .to_json();
    payload["session_id"] = json!(session_id);
    if resume_state.is_some() {
        payload["resume_attempt_id"] = json!(Uuid::new_v4().to_string());
        payload["provider_thread_id"] = json!(target.provider_thread_id);
    }
    let runtime = tokio::runtime::Runtime::new()?;
    let response = match register_managed_launch_with_timeout(
        &runtime,
        &url,
        &token,
        if resume_state.is_some() {
            "Pi resume"
        } else {
            "Pi"
        },
        &payload,
        Some(&session_id),
        FOREGROUND_REGISTRATION_TIMEOUT,
    ) {
        Ok(response) => Some(response),
        Err(error) if resume_state.is_some() => return Err(error),
        Err(error) => {
            eprintln!("Longhouse: Pi registration degraded; starting stock Pi and retrying registration: {error:#}");
            None
        }
    };
    if let Some(response) = response.as_ref() {
        response.validate_transport("Pi", PI_HELM_TRANSPORT)?;
        if response
            .provider_session_id
            .as_deref()
            .is_some_and(|id| id != target.provider_thread_id.as_str())
        {
            anyhow::bail!("Runtime Host returned a different Pi provider session");
        }
    }
    let mut transaction = response.as_ref().map(|response| {
        ManagedLaunchTransaction::new(&runtime, &url, &token, &session_id, &response.run_id)
    });
    let degraded = response.is_none().then(|| {
        spawn_managed_registration_retry(
            &url,
            &token,
            "Pi",
            payload.clone(),
            &session_id,
            DeferredNotices::default(),
            crate::config::get_agent_dir().unwrap_or_else(|_| PathBuf::from(".")),
        )
    });

    let state = PiHelmStateFile {
        schema_version: 1,
        session_id: session_id.clone(),
        run_id: response
            .as_ref()
            .map(|value| value.run_id.clone())
            .unwrap_or(run_id),
        provider: "pi".into(),
        native_provider: effective_provider.clone(),
        model: effective_model.clone(),
        provider_session_id: target.provider_thread_id.clone(),
        session_file: target
            .session_file
            .as_ref()
            .map(|path| path.display().to_string()),
        cwd: cwd.display().to_string(),
        provider_binary: fs::canonicalize(&binary)
            .unwrap_or_else(|_| PathBuf::from(&binary))
            .display()
            .to_string(),
        session_dir: target.session_dir.display().to_string(),
        socket_path: socket.display().to_string(),
        channel_token,
        connection_id,
        lease_generation,
        launcher_pid: std::process::id(),
        launcher_process_start_time: process_start_time(Some(std::process::id())),
        provider_pid: None,
        provider_process_start_time: None,
        status: "starting".into(),
        phase: "unknown".into(),
        tool_name: None,
        ready: false,
        terminal_state: None,
        terminal_reason: None,
        exit_code: None,
        started_at: Utc::now().to_rfc3339(),
        updated_at: Utc::now().to_rfc3339(),
    };
    let state_path = state_root.join(format!("{session_id}.json"));
    let server = PiHelmServer::start(state, socket, socket_dir, state_path)?;
    let socket = &server.socket_path;
    let mut command = Command::new(&binary);
    command
        .arg("--session-dir")
        .arg(&target.session_dir)
        .arg("-e")
        .arg(&extension)
        .current_dir(&cwd)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .env("LONGHOUSE_PI_HELM_CHANNEL_PATH", &socket)
        .env(
            "LONGHOUSE_PI_HELM_CHANNEL_TOKEN",
            &server.current_state().channel_token,
        )
        .env(
            "LONGHOUSE_PI_HELM_INITIAL_PROMPT",
            config.prompt.as_deref().unwrap_or(""),
        );
    if let Some(model) = &effective_model {
        command.arg("--model").arg(model);
    }
    if let Some(provider) = &effective_provider {
        command.arg("--provider").arg(provider);
    }
    if let Some(file) = &target.session_file {
        command.arg("--session").arg(file);
    } else {
        command.arg("--session-id").arg(&target.provider_thread_id);
    }
    ManagedIdentity::new(ManagedProvider::Pi, &session_id)
        .with_run_id(&server.current_state().run_id)
        .apply(
            &mut command,
            &[
                (
                    "LONGHOUSE_PI_HELM_CHANNEL_PATH",
                    socket.to_string_lossy().as_ref(),
                ),
                (
                    "LONGHOUSE_PI_HELM_CHANNEL_TOKEN",
                    server.current_state().channel_token.as_str(),
                ),
                (
                    "LONGHOUSE_PI_HELM_INITIAL_PROMPT",
                    config.prompt.as_deref().unwrap_or(""),
                ),
            ],
        );
    let server_for_spawn = server.clone();
    let exit_code = match run_pi_provider(&mut command, &server, |pid| {
        let mut guard = server_for_spawn
            .shared
            .lock()
            .expect("Pi Helm state mutex poisoned");
        guard.state.provider_pid = Some(pid);
        guard.state.provider_process_start_time = process_start_time(Some(pid));
        guard.state.status = "running".into();
        guard.state.updated_at = Utc::now().to_rfc3339();
        drop(guard);
        server_for_spawn.persist_state()?;
        if let Some(transaction) = transaction.as_mut() {
            transaction.confirm_or_degrade(
                "Pi",
                &crate::config::get_agent_dir()?,
                &DeferredNotices::default(),
            );
        }
        if let Some(registration) = degraded.as_ref() {
            registration.provider_alive.store(true, Ordering::Release);
        }
        Ok(())
    }) {
        Ok(exit_code) => exit_code,
        Err(error) => {
            let _ = server.mark_stopped(None, "launch_failed");
            server.shutdown();
            return Err(error);
        }
    };
    let exit_code = Some(exit_code);
    let reason = if server.terminate_requested.load(Ordering::Acquire) {
        "remote_terminate"
    } else if exit_code.is_some_and(|code| code >= 128) {
        "terminal_signal"
    } else {
        "provider_exit"
    };
    server.mark_stopped(exit_code, reason)?;
    let final_state = server.current_state();
    let _ = enqueue_terminal_event(&final_state, &machine_name, exit_code, reason);
    server.shutdown();
    drop(degraded);
    if let Some(message) = exit_code.filter(|code| *code != 0) {
        eprintln!("Pi Helm exited with status {message}");
    }
    Ok(exit_code.unwrap_or(1))
}
