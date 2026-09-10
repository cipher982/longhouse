//! Native OMP Helm launch and its launch-scoped extension channel.
//!
//! OMP owns the TUI and native JSONL. Longhouse owns only this process group,
//! the exact source binding, and the authenticated local control socket.

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
use crate::managed_terminal::{terminal_state_for_exit, ForegroundTerminal, ManagedTerminalEvent};
use crate::omp_helm_control::OMP_HELM_TRANSPORT;

const STATE_DIR_NAME: &str = "managed-local/omp-helm";
const EXTENSION_FILE_NAME: &str = "longhouse-omp-helm.ts";
const SOCKET_TIMEOUT: Duration = Duration::from_secs(8);
const COMMAND_TIMEOUT: Duration = Duration::from_secs(8);
const MAX_FRAME_BYTES: usize = 512 * 1024;
const MAX_PENDING_COMMANDS: usize = 64;
const EXTENSION_ASSET: &str = include_str!("../assets/longhouse-omp-helm.ts");

pub struct LaunchConfig {
    pub cwd: PathBuf,
    pub prompt: Option<String>,
    pub model: Option<String>,
    pub profile: Option<String>,
    pub session_dir: Option<PathBuf>,
    pub resume_session: Option<String>,
    pub omp_bin: Option<String>,
    pub url: Option<String>,
    pub token: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct OmpHelmStateFile {
    schema_version: u64,
    session_id: String,
    run_id: String,
    provider: String,
    model: Option<String>,
    profile: Option<String>,
    native_session_id: String,
    session_file: String,
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
    title: Option<String>,
    ready: bool,
    pending_transition: bool,
    #[serde(default)]
    initial_prompt_delivered: bool,
    terminal_state: Option<String>,
    terminal_reason: Option<String>,
    exit_code: Option<i32>,
    started_at: String,
    updated_at: String,
}

#[derive(Debug, Clone)]
struct SharedState {
    state: OmpHelmStateFile,
    extension_sender: Option<mpsc::Sender<Value>>,
    extension_connection_id: Option<String>,
    pending: HashMap<String, mpsc::Sender<Value>>,
}

#[derive(Clone)]
struct OmpHelmServer {
    shared: Arc<Mutex<SharedState>>,
    socket_path: PathBuf,
    state_path: PathBuf,
    persist_lock: Arc<Mutex<()>>,
    socket_dir: PathBuf,
    stop: Arc<AtomicBool>,
    terminate_requested: Arc<AtomicBool>,
}

impl OmpHelmServer {
    fn start(
        state: OmpHelmStateFile,
        socket_path: PathBuf,
        socket_dir: PathBuf,
        state_path: PathBuf,
    ) -> Result<Self> {
        ensure_private_owned_dir(&socket_dir)?;
        let _ = fs::remove_file(&socket_path);
        let listener = std::os::unix::net::UnixListener::bind(&socket_path)?;
        set_private_file(&socket_path)?;
        let server = Self {
            shared: Arc::new(Mutex::new(SharedState {
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
        let _lock = self
            .persist_lock
            .lock()
            .expect("OMP state persist mutex poisoned");
        let state = self
            .shared
            .lock()
            .expect("OMP state mutex poisoned")
            .state
            .clone();
        write_json_private(&self.state_path, &state)
    }

    fn handle_connection(&self, stream: std::os::unix::net::UnixStream) {
        let _ = stream.set_read_timeout(Some(SOCKET_TIMEOUT));
        let _ = stream.set_write_timeout(Some(SOCKET_TIMEOUT));
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
            let _ = clone
                .try_clone()
                .and_then(|mut writer| writer.write_all(format!("{}\n", response).as_bytes()));
        }
    }

    fn handle_extension(
        &self,
        mut writer: std::os::unix::net::UnixStream,
        mut reader: BufReader<std::os::unix::net::UnixStream>,
        hello: Value,
    ) {
        if !self.base_authority_matches(&hello) {
            let _ = writer.write_all(b"{\"ok\":false,\"error\":{\"code\":\"stale_channel\"}}\n");
            return;
        }
        let connection_id = Uuid::new_v4().to_string();
        let (sender, receiver) = mpsc::channel::<Value>();
        {
            let mut state = self.shared.lock().expect("OMP state mutex poisoned");
            fail_pending_locked(&mut state, "OMP extension connection replaced");
            state.extension_sender = Some(sender);
            state.extension_connection_id = Some(connection_id.clone());
            state.state.connection_id = connection_id.clone();
            state.state.lease_generation = Uuid::new_v4().to_string();
            state.state.ready = false;
            state.state.status = "starting".into();
            state.state.updated_at = Utc::now().to_rfc3339();
        }
        let writer_connection = connection_id.clone();
        let writer_thread = thread::spawn(move || {
            for frame in receiver {
                if writer.write_all(format!("{}\n", frame).as_bytes()).is_err() {
                    break;
                }
                let _ = writer.flush();
            }
        });
        self.send_extension_frame(
            &connection_id,
            json!({
                "kind": "extension_ready",
                "ok": true,
                "connection_id": connection_id,
                "lease_generation": self.current_state().lease_generation,
            }),
        );
        loop {
            let Ok(Some(frame)) = read_frame(&mut reader) else {
                break;
            };
            self.handle_extension_frame(&writer_connection, frame);
        }
        self.disconnect_extension(&writer_connection);
        let _ = writer_thread.join();
    }

    fn base_authority_matches(&self, frame: &Value) -> bool {
        let state = self.shared.lock().expect("OMP state mutex poisoned");
        frame.get("auth_token").and_then(Value::as_str) == Some(state.state.channel_token.as_str())
            && frame.get("session_id").and_then(Value::as_str)
                == Some(state.state.session_id.as_str())
    }

    fn extension_authority_matches(&self, connection_id: &str, frame: &Value) -> bool {
        let kind = frame.get("kind").and_then(Value::as_str).unwrap_or_default();
        let state = self.shared.lock().expect("OMP state mutex poisoned");
        let base = state.extension_connection_id.as_deref() == Some(connection_id)
            && frame.get("auth_token").and_then(Value::as_str)
                == Some(state.state.channel_token.as_str())
            && frame.get("session_id").and_then(Value::as_str)
                == Some(state.state.session_id.as_str())
            && frame.get("connection_id").and_then(Value::as_str)
                == Some(state.state.connection_id.as_str())
            && frame.get("lease_generation").and_then(Value::as_str)
                == Some(state.state.lease_generation.as_str());
        if !base {
            return false;
        }
        if matches!(kind, "session_start" | "session_reconnect") {
            return true;
        }
        if matches!(kind, "session_switch" | "session_branch") && state.state.pending_transition {
            return true;
        }
        state.state.native_session_id.is_empty()
            || (frame.get("native_session_id").and_then(Value::as_str)
                == Some(state.state.native_session_id.as_str())
                && frame.get("session_file").and_then(Value::as_str)
                    == Some(state.state.session_file.as_str()))
    }

    fn disconnect_extension(&self, connection_id: &str) {
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        if state.extension_connection_id.as_deref() != Some(connection_id) {
            return;
        }
        fail_pending_locked(&mut state, "OMP extension channel disconnected");
        state.extension_sender = None;
        state.extension_connection_id = None;
        state.state.ready = false;
        if state.state.status != "stopped" {
            state.state.status = "degraded".into();
        }
        state.state.updated_at = Utc::now().to_rfc3339();
        drop(state);
        let _ = self.persist_state();
    }

    fn send_extension_frame(&self, connection_id: &str, frame: Value) {
        let sender = self
            .shared
            .lock()
            .expect("OMP state mutex poisoned")
            .extension_connection_id
            .as_deref()
            == Some(connection_id);
        if sender {
            if let Some(channel) = self
                .shared
                .lock()
                .expect("OMP state mutex poisoned")
                .extension_sender
                .clone()
            {
                let _ = channel.send(frame);
            }
        }
    }

    fn update_identity(&self, connection_id: &str, frame: &Value, replacement: bool) -> Result<()> {
        let native_id = frame
            .get("native_session_id")
            .and_then(Value::as_str)
            .context("OMP extension frame has no native session id")?;
        let source = frame
            .get("session_file")
            .and_then(Value::as_str)
            .context("OMP extension frame has no native session file")?;
        let deadline = Instant::now() + SOCKET_TIMEOUT;
        let header = loop {
            match crate::omp_session::read_session_header(Path::new(source)) {
                Ok(header) => break header,
                Err(error) if Instant::now() < deadline => {
                    thread::sleep(Duration::from_millis(25));
                    drop(error);
                }
                Err(error) => return Err(error).context("waiting for OMP native session header"),
            }
        };
        anyhow::ensure!(
            header.native_id == native_id,
            "OMP extension native identity does not match its source header"
        );
        let (session_id, previous) = {
            let state = self.shared.lock().expect("OMP state mutex poisoned");
            anyhow::ensure!(
                state.extension_connection_id.as_deref() == Some(connection_id),
                "OMP extension connection was replaced"
            );
            (
                state.state.session_id.clone(),
                state.state.native_session_id.clone(),
            )
        };
        if !previous.is_empty() && previous != native_id && !replacement {
            anyhow::bail!("OMP native session changed without a replacement fence");
        }
        let db_path = crate::config::get_agent_db_path()?;
        let conn = crate::state::db::open_client_connection(&db_path, Duration::from_millis(500))?;
        crate::omp_session::bind_source_for_thread(
            &conn,
            Path::new(source),
            &session_id,
            native_id,
        )?;
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        anyhow::ensure!(
            state.extension_connection_id.as_deref() == Some(connection_id),
            "OMP extension connection was replaced during binding"
        );
        state.state.native_session_id = native_id.to_string();
        state.state.session_file = source.to_string();
        state.state.pending_transition = false;
        state.state.ready = true;
        state.state.status = "ready".into();
        state.state.updated_at = Utc::now().to_rfc3339();
        drop(state);
        self.persist_state()?;
        self.publish_binding(Path::new(source), native_id, previous != native_id)?;
        Ok(())
    }

    fn mark_degraded(&self, error: &anyhow::Error) {
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        state.state.ready = false;
        state.state.status = "degraded".into();
        state.state.terminal_reason = Some(error.to_string());
        state.state.updated_at = Utc::now().to_rfc3339();
        drop(state);
        let _ = self.persist_state();
    }

    fn handle_extension_frame(&self, connection_id: &str, frame: Value) {
        let kind = frame
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if !self.extension_authority_matches(connection_id, &frame) {
            return;
        }
        if matches!(kind, "session_before_switch" | "session_before_branch") {
            let mut state = self.shared.lock().expect("OMP state mutex poisoned");
            fail_pending_locked(&mut state, "OMP native session replacement started");
            state.state.ready = false;
            state.state.pending_transition = true;
            state.state.lease_generation = Uuid::new_v4().to_string();
            state.state.status = "switching".into();
            state.state.updated_at = Utc::now().to_rfc3339();
            let generation = state.state.lease_generation.clone();
            let connection = state.state.connection_id.clone();
            drop(state);
            let _ = self.persist_state();
            self.send_extension_frame(
                connection_id,
                json!({"kind": "extension_generation", "connection_id": connection, "lease_generation": generation}),
            );
            return;
        }
        match kind {
            "session_start" | "session_reconnect" => {
                if let Err(error) = self.update_identity(connection_id, &frame, false) {
                    self.mark_degraded(&error);
                    eprintln!("Longhouse: OMP native identity binding failed: {error:#}");
                }
            }
            "initial_prompt_request" => {
                let granted = {
                    let mut state = self.shared.lock().expect("OMP state mutex poisoned");
                    if !state.state.ready || state.state.initial_prompt_delivered {
                        false
                    } else {
                        state.state.initial_prompt_delivered = true;
                        state.state.updated_at = Utc::now().to_rfc3339();
                        true
                    }
                };
                let _ = self.persist_state();
                self.send_extension_frame(
                    connection_id,
                    json!({"kind": "initial_prompt_grant", "granted": granted}),
                );
            }
            "session_switch" | "session_branch" => {
                let replacement = self.current_state().pending_transition;
                if let Err(error) = self.update_identity(connection_id, &frame, replacement) {
                    self.mark_degraded(&error);
                    eprintln!("Longhouse: OMP native replacement binding failed: {error:#}");
                }
            }
            "command_result" => {
                if let Some(request_id) = frame.get("request_id").and_then(Value::as_str) {
                    if let Some(sender) = self
                        .shared
                        .lock()
                        .expect("OMP state mutex poisoned")
                        .pending
                        .remove(request_id)
                    {
                        let _ = sender.send(frame);
                    }
                }
            }
            "title_change" => self.publish_title(
                frame
                    .get("event")
                    .and_then(|value| value.get("title"))
                    .and_then(Value::as_str),
            ),
            "activity" | "agent_start" | "tool_execution_start" | "tool_execution_end" => {
                self.record_activity(kind, &frame)
            }
            "agent_end" => {
                // OMP's session event is the authoritative distinction between
                // an intermediate agent loop and a terminal settle. `session_stop`
                // can occur while the session is being torn down or replaced; it
                // must never manufacture an idle observation.
                self.record_activity(kind, &frame)
            }
            "session_shutdown" => self.disconnect_extension(connection_id),
            _ => {}
        }
    }

    fn record_activity(&self, kind: &str, frame: &Value) {
        let event = frame.get("event");
        let phase = match kind {
            "agent_end" => match event.and_then(|value| value.get("isTerminal")).and_then(Value::as_bool) {
                Some(true) => "idle",
                Some(false) => "running",
                None => return,
            },
            "agent_start" | "activity" | "tool_execution_start" | "tool_execution_end" => "running",
            _ => return,
        };
        let tool = event
            .and_then(|value| value.get("toolName"))
            .and_then(Value::as_str)
            .map(str::to_string);
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        state.state.phase = phase.into();
        state.state.tool_name = tool.clone();
        state.state.updated_at = Utc::now().to_rfc3339();
        let current = state.state.clone();
        drop(state);
        let _ = self.persist_state();
        if let Ok(conn) = crate::state::db::open_client_connection(
            &crate::config::get_agent_db_path().unwrap_or_default(),
            Duration::from_millis(250),
        ) {
            let signal = crate::state::session_phase::SessionPhaseSignal {
                session_id: current.session_id.clone(),
                provider: "omp".into(),
                phase: phase.into(),
                tool_name: tool.clone(),
                source: OMP_HELM_TRANSPORT.into(),
                observed_at: Utc::now(),
            };
            let _ = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal);
        }
        self.publish_phase(phase, tool);
    }


    fn publish_binding(&self, source: &Path, native_id: &str, replacement: bool) -> Result<()> {
        let state = self.current_state();
        let outbox = crate::config::get_agent_runtime_events_outbox_dir()?;
        crate::outbox::enqueue_runtime_event(
            &outbox,
            &json!({
                "runtime_key": format!("omp:{}", state.session_id), "session_id": state.session_id,
                "provider": "omp", "run_id": state.run_id, "source": OMP_HELM_TRANSPORT,
                "kind": "binding_signal", "occurred_at": Utc::now().to_rfc3339(),
                "dedupe_key": format!("omp-helm:{}:{}:{}", state.session_id, state.run_id, native_id),
                "payload": {"provider_session_id": native_id, "source_path": source, "managed_transport": OMP_HELM_TRANSPORT, "execution_lifetime": "interactive", "conversation_reset": replacement}
            }),
        )?;
        wake_transcript_shipper(&state, source, native_id, "binding");
        Ok(())
    }

    fn publish_title(&self, title: Option<&str>) {
        let Some(title) = title.map(str::trim).filter(|title| !title.is_empty()) else {
            return;
        };
        let state = self.current_state();
        if let Ok(outbox) = crate::config::get_agent_runtime_events_outbox_dir() {
            let _ = crate::outbox::enqueue_runtime_event(
                &outbox,
                &json!({
                    "runtime_key": format!("omp:{}", state.session_id), "session_id": state.session_id,
                    "provider": "omp", "run_id": state.run_id, "source": OMP_HELM_TRANSPORT,
                    "kind": "title_signal", "occurred_at": Utc::now().to_rfc3339(),
                    "dedupe_key": format!("omp-title:{}:{}:{}", state.session_id, state.run_id, title),
                    "payload": {"title": title, "provider_session_id": state.native_session_id}
                }),
            );
        }
        let mut guard = self.shared.lock().expect("OMP state mutex poisoned");
        guard.state.title = Some(title.to_string());
        guard.state.updated_at = Utc::now().to_rfc3339();
        drop(guard);
        let _ = self.persist_state();
    }

    fn publish_phase(&self, phase: &str, tool: Option<String>) {
        let state = self.current_state();
        if let Ok(outbox) = crate::config::get_agent_runtime_events_outbox_dir() {
            let _ = crate::outbox::enqueue_runtime_event(
                &outbox,
                &json!({
                    "runtime_key": format!("omp:{}", state.session_id), "session_id": state.session_id,
                    "provider": "omp", "run_id": state.run_id, "source": OMP_HELM_TRANSPORT,
                    "kind": "phase_signal", "phase": phase, "tool_name": tool,
                    "occurred_at": Utc::now().to_rfc3339(),
                    "dedupe_key": format!("omp-phase:{}:{}:{}:{}", state.session_id, state.run_id, phase, state.updated_at),
                    "payload": {"managed_transport": OMP_HELM_TRANSPORT, "execution_lifetime": "interactive", "structured_remote_approval": false}
                }),
            );
        }
        wake_transcript_shipper(
            &state,
            Path::new(&state.session_file),
            &state.native_session_id,
            "phase",
        );
    }

    fn handle_remote_command(&self, frame: Value) -> Value {
        let kind = frame
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if !matches!(kind.as_str(), "send" | "steer" | "abort" | "terminate") {
            return channel_error("bad_request", "unknown OMP Helm command");
        }
        let request_id = Uuid::new_v4().to_string();
        let (sender, receiver) = mpsc::channel();
        {
            let mut state = self.shared.lock().expect("OMP state mutex poisoned");
            if !remote_authority_matches_locked(&state, &frame) {
                return channel_error("stale_channel", "OMP Helm channel identity rejected");
            }
            let Some(extension) = state.extension_sender.clone() else {
                return channel_error(
                    "session_not_attached",
                    "OMP extension channel is not connected",
                );
            };
            if state.pending.len() >= MAX_PENDING_COMMANDS {
                return channel_error("command_busy", "OMP Helm command queue is full");
            }
            state.pending.insert(request_id.clone(), sender);
            let mut command = frame;
            command["request_id"] = json!(request_id);
            command["connection_id"] = json!(state.state.connection_id);
            command["lease_generation"] = json!(state.state.lease_generation);
            command["native_session_id"] = json!(state.state.native_session_id);
            command["session_file"] = json!(state.state.session_file);
            command["auth_token"] = json!(state.state.channel_token);
            command["session_id"] = json!(state.state.session_id);
            if extension.send(command).is_err() {
                state.pending.remove(&request_id);
                return channel_error("session_not_attached", "OMP extension channel is closed");
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
                    .expect("OMP state mutex poisoned")
                    .pending
                    .remove(&request_id);
                channel_error(
                    "command_failed",
                    "OMP extension did not acknowledge the command",
                )
            }
        }
    }

    fn mark_stopped(&self, exit_code: Option<i32>, reason: &str) -> Result<()> {
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        state.state.status = "stopped".into();
        state.state.ready = false;
        state.state.phase = "idle".into();
        state.state.terminal_state = Some(
            exit_code
                .map(terminal_state_for_exit)
                .unwrap_or("process_gone")
                .into(),
        );
        state.state.terminal_reason = Some(reason.into());
        state.state.exit_code = exit_code;
        state.state.updated_at = Utc::now().to_rfc3339();
        drop(state);
        self.persist_state()
    }

    fn shutdown(&self) {
        self.stop.store(true, Ordering::Release);
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        fail_pending_locked(&mut state, "OMP Helm server is shutting down");
        state.extension_sender = None;
        state.extension_connection_id = None;
        drop(state);
        let _ = std::os::unix::net::UnixStream::connect(&self.socket_path);
        let _ = fs::remove_file(&self.socket_path);
        let _ = fs::remove_dir(&self.socket_dir);
    }

    fn current_state(&self) -> OmpHelmStateFile {
        self.shared
            .lock()
            .expect("OMP state mutex poisoned")
            .state
            .clone()
    }
}

fn remote_authority_matches_locked(state: &SharedState, frame: &Value) -> bool {
    state.state.ready
        && state.extension_sender.is_some()
        && frame.get("auth_token").and_then(Value::as_str)
            == Some(state.state.channel_token.as_str())
        && frame.get("session_id").and_then(Value::as_str) == Some(state.state.session_id.as_str())
        && frame.get("native_session_id").and_then(Value::as_str)
            == Some(state.state.native_session_id.as_str())
        && frame.get("connection_id").and_then(Value::as_str)
            == Some(state.state.connection_id.as_str())
        && frame.get("lease_generation").and_then(Value::as_str)
            == Some(state.state.lease_generation.as_str())
}

fn fail_pending_locked(state: &mut SharedState, message: &str) {
    for (_, sender) in std::mem::take(&mut state.pending) {
        let _ = sender.send(channel_error("stale_channel", message));
    }
}

fn channel_error(code: &str, message: &str) -> Value {
    json!({"ok": false, "error": {"code": code, "message": message}})
}

fn read_frame(reader: &mut BufReader<std::os::unix::net::UnixStream>) -> Result<Option<Value>> {
    let mut bytes = Vec::new();
    let mut one = [0_u8; 1];
    loop {
        let count = reader.read(&mut one)?;
        if count == 0 {
            return Ok(if bytes.is_empty() {
                None
            } else {
                Some(serde_json::from_slice(&bytes)?)
            });
        }
        if one[0] == b'\n' {
            if bytes.is_empty() {
                continue;
            }
            return Ok(Some(serde_json::from_slice(&bytes)?));
        }
        bytes.push(one[0]);
        if bytes.len() > MAX_FRAME_BYTES {
            anyhow::bail!("OMP Helm frame exceeds limit");
        }
    }
}

fn home_state() -> Result<PathBuf> {
    crate::config::get_longhouse_home()
}
fn state_dir() -> Result<PathBuf> {
    Ok(home_state()?.join(STATE_DIR_NAME))
}

fn launch_lock(session_id: &str) -> Result<File> {
    let path = state_dir()?.join(format!("{session_id}.lock"));
    fs::create_dir_all(path.parent().context("OMP Helm lock has no parent")?)?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .open(path)?;
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            anyhow::bail!("OMP Helm session {session_id} is already attached or resuming");
        }
    }
    Ok(file)
}

fn process_start_time(pid: Option<u32>) -> Option<String> {
    crate::turn_claims::process_start_time_for_pid(pid)
}

pub fn resolve_binary(explicit: Option<String>) -> Result<String> {
    let candidate = explicit
        .or_else(|| std::env::var("LONGHOUSE_OMP_BIN").ok())
        .unwrap_or_else(|| "omp".into());
    let path = PathBuf::from(&candidate);
    if path.components().count() > 1 {
        return path
            .is_file()
            .then(|| path.display().to_string())
            .context("--omp-bin is not a file");
    }
    for directory in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        let found = directory.join(&candidate);
        if found.is_file() {
            return Ok(found.display().to_string());
        }
    }
    anyhow::bail!("OMP executable not found. Install stock `omp` or set --omp-bin.")
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
    let machine = home_state()?.join("machine");
    let state: Value = fs::read(machine.join("state.json"))
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_else(|| json!({}));
    let url = config
        .url
        .clone()
        .or_else(|| std::env::var("LONGHOUSE_OMP_HELM_URL").ok())
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
        .or_else(|| std::env::var("LONGHOUSE_OMP_HELM_TOKEN").ok())
        .or_else(|| fs::read_to_string(machine.join("device-token")).ok())
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .context("No device token found. Run `longhouse auth` first.")?;
    let machine_name = state
        .get("machine_name")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .unwrap_or("unknown")
        .to_string();
    Ok((url, token, machine_name))
}

fn read_resume_state(session_id: &str, cwd: &Path, binary: &str) -> Result<OmpHelmStateFile> {
    let path = state_dir()?.join(format!("{session_id}.json"));
    let state: OmpHelmStateFile = serde_json::from_slice(
        &fs::read(&path)
            .with_context(|| format!("OMP Resume contract is missing: {}", path.display()))?,
    )?;
    anyhow::ensure!(
        state.provider == "omp"
            && state.session_id == session_id
            && matches!(state.status.as_str(), "ready" | "degraded" | "stopped"),
        "OMP retained launch contract is not resumable"
    );
    anyhow::ensure!(
        fs::canonicalize(&state.cwd).ok() == fs::canonicalize(cwd).ok(),
        "OMP Resume must run from its retained workspace"
    );
    anyhow::ensure!(
        fs::canonicalize(&state.provider_binary).ok() == fs::canonicalize(binary).ok(),
        "OMP Resume binary does not match the retained launch"
    );
    anyhow::ensure!(
        !state.native_session_id.is_empty(),
        "OMP retained launch contract has no native session identity"
    );
    crate::omp_session::verify_exact_session_file(
        Path::new(&state.session_file),
        &state.native_session_id,
        Some(&state.cwd),
    )?;
    let facts = crate::process_identity::try_collect_process_facts_by_pid()
        .context("OMP Resume cannot verify prior process identities")?;
    for (label, pid, birth) in [
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
        if let (Some(pid), Some(birth)) = (pid, birth) {
            if facts.get(&pid).is_some_and(|fact| fact.lstart == birth) {
                anyhow::bail!(
                    "OMP Resume refused while the previous {label} execution owner is still alive"
                );
            }
        }
    }
    Ok(state)
}

fn effective_resume_settings(
    config: &LaunchConfig,
    resume_state: Option<&OmpHelmStateFile>,
) -> (Option<String>, Option<String>) {
    match resume_state {
        Some(state) => (state.model.clone(), state.profile.clone()),
        None => (config.model.clone(), config.profile.clone()),
    }
}

fn select_session_storage(
    resume_state: Option<&OmpHelmStateFile>,
    configured_session_dir: Option<&Path>,
    profile: Option<&str>,
    cwd: &Path,
) -> Result<(PathBuf, PathBuf)> {
    let session_dir = resume_state
        .map(|state| PathBuf::from(&state.session_dir))
        .or_else(|| configured_session_dir.map(Path::to_path_buf))
        .or_else(|| crate::omp_session::session_dir_for_launch(cwd, profile).ok())
        .context("OMP has no session directory")?;
    let session_file = match resume_state {
        Some(state) => PathBuf::from(&state.session_file),
        None => crate::omp_session::reserve_session_path(&session_dir)?,
    };
    Ok((session_dir, session_file))
}

fn run_provider(
    command: &mut Command,
    server: &OmpHelmServer,
    runtime: &tokio::runtime::Runtime,
    after_spawn: impl FnOnce(u32) -> Result<()>,
) -> Result<i32> {
    let signal = Arc::new(AtomicUsize::new(0));
    for number in [libc::SIGHUP, libc::SIGTERM, libc::SIGINT] {
        signal_hook::flag::register_usize(number, signal.clone(), number as usize)?;
    }
    let terminal = ForegroundTerminal::capture()?;
    command.process_group(0);
    let mut child = command.spawn().context("spawn stock OMP Helm TUI")?;
    let pid = child.id();
    if let Err(error) = terminal.give_to(pid as libc::pid_t) {
        crate::managed_terminal::terminate_owned_group(&mut child, pid as libc::pid_t);
        let _ = runtime.block_on(crate::process_group::shutdown_group(
            pid as i32,
            crate::process_group::DEFAULT_GRACE,
        ));
        return Err(error).context("give terminal to stock OMP");
    }
    if let Err(error) = after_spawn(pid) {
        crate::managed_terminal::terminate_owned_group(&mut child, pid as libc::pid_t);
        let _ = runtime.block_on(crate::process_group::shutdown_group(
            pid as i32,
            crate::process_group::DEFAULT_GRACE,
        ));
        return Err(error);
    }
    let mut sent = false;
    let started = Instant::now();
    let mut status = None;
    loop {
        if (server.terminate_requested.load(Ordering::Acquire)
            || signal.load(Ordering::Acquire) != 0)
            && !sent
        {
            crate::managed_terminal::terminate_owned_group(&mut child, pid as libc::pid_t);
            sent = true;
        }
        match child.try_wait() {
            Ok(Some(exit)) => {
                status = Some(exit);
                break;
            }
            Ok(None) => {}
            Err(error) => {
                crate::managed_terminal::terminate_owned_group(&mut child, pid as libc::pid_t);
                return Err(error).context("waiting for stock OMP");
            }
        }
        if sent && started.elapsed() > Duration::from_secs(2) {
            break;
        }
        thread::sleep(Duration::from_millis(25));
    }
    if status.is_none() {
        crate::managed_terminal::terminate_owned_group(&mut child, pid as libc::pid_t);
        status = child.try_wait()?;
    }
    let outcome = runtime.block_on(crate::process_group::shutdown_group(
        pid as i32,
        crate::process_group::DEFAULT_GRACE,
    ));
    if !outcome.is_gone() {
        eprintln!("Longhouse: OMP process group {pid} survived cleanup");
    }
    let exit = status
        .map(|status| status.code().unwrap_or(128))
        .unwrap_or(1);
    Ok(exit)
}

pub fn launch(config: LaunchConfig) -> Result<i32> {
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        anyhow::bail!(
            "longhouse omp Helm needs an interactive terminal; use Console for headless turns"
        );
    }
    let cwd = fs::canonicalize(&config.cwd)
        .with_context(|| format!("resolve OMP workspace {}", config.cwd.display()))?;
    let binary = resolve_binary(config.omp_bin.clone())?;
    let state_root = state_dir()?;
    fs::create_dir_all(&state_root)?;
    let (resume_state, session_id) = if let Some(session_id) = config.resume_session.as_deref() {
        (
            Some(read_resume_state(session_id, &cwd, &binary)?),
            session_id.to_string(),
        )
    } else {
        (None, Uuid::new_v4().to_string())
    };
    let _owner_lock = launch_lock(&session_id)?;
    let (model, profile) = effective_resume_settings(&config, resume_state.as_ref());
    let (session_dir, session_file) = select_session_storage(
        resume_state.as_ref(),
        config.session_dir.as_deref(),
        profile.as_deref(),
        &cwd,
    )?;
    let native_id = resume_state
        .as_ref()
        .map(|state| state.native_session_id.clone())
        .unwrap_or_default();
    // Claim the exact path before stock OMP can materialize its header. The
    // parser will keep the path pending until the native identity appears,
    // while the reservation prevents a launch-gap Shadow session.
    let db_path = crate::config::get_agent_db_path()?;
    let conn = crate::state::db::open_client_connection(&db_path, Duration::from_millis(500))?;
    crate::omp_session::reserve_source_for_thread(
        &conn,
        &session_file,
        &session_id,
        (!native_id.is_empty()).then_some(native_id.as_str()),
    )?;
    let (url, token, machine_name) = registration_credentials(&config)?;
    let run_id = Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("longhouse:managed-local-run:{session_id}").as_bytes(),
    )
    .to_string();
    let connection_id = Uuid::new_v4().to_string();
    let lease_generation = Uuid::new_v4().to_string();
    let channel_token = Uuid::new_v4().to_string();
    let provider_config = json!({"session_dir": session_dir, "session_file": session_file, "profile": profile, "model": model, "native_session_id": native_id});
    let registration = ManagedLaunchRegistration {
        provider: "omp",
        cwd: &cwd,
        project: None,
        display_name: None,
        machine_name: &machine_name,
        permission_mode: PermissionMode::ProviderLocal,
        provenance: ManagedLaunchProvenance::interactive_helm(),
        extra: vec![
            ("session_id", json!(session_id)),
            ("run_id", json!(run_id)),
            ("connection_id", json!(connection_id)),
            ("lease_generation", json!(lease_generation)),
            ("managed_transport", json!(OMP_HELM_TRANSPORT)),
            (
                "provider_session_id",
                json!(if native_id.is_empty() {
                    Value::Null
                } else {
                    json!(native_id)
                }),
            ),
            ("provider_config", provider_config),
        ],
    }
    .to_json();
    let mut registration = registration;
    if resume_state.is_some() {
        registration["resume_attempt_id"] = json!(Uuid::new_v4().to_string());
        registration["provider_thread_id"] = json!(native_id);
    }
    let runtime = tokio::runtime::Runtime::new()?;
    let response = match register_managed_launch_with_timeout(
        &runtime,
        &url,
        &token,
        "OMP",
        &registration,
        Some(&session_id),
        FOREGROUND_REGISTRATION_TIMEOUT,
    ) {
        Ok(response) => Some(response),
        Err(error) if resume_state.is_some() => return Err(error),
        Err(error) => {
            eprintln!(
                "Longhouse: OMP registration degraded; starting stock OMP and retrying: {error:#}"
            );
            None
        }
    };
    if let Some(response) = response.as_ref() {
        response.validate_transport("OMP", OMP_HELM_TRANSPORT)?;
    }
    let mut transaction = response.as_ref().map(|response| {
        ManagedLaunchTransaction::new(&runtime, &url, &token, &session_id, &response.run_id)
    });
    let deferred = DeferredNotices::default();
    let degraded = response.is_none().then(|| {
        spawn_managed_registration_retry(
            &url,
            &token,
            "OMP",
            registration.clone(),
            &session_id,
            deferred.clone(),
            crate::config::get_agent_dir().unwrap_or_else(|_| PathBuf::from(".")),
        )
    });
    let socket_dir = std::env::temp_dir().join(format!(
        "longhouse-omp-{}-{}",
        std::process::id(),
        Uuid::new_v4().simple()
    ));
    let socket = socket_dir.join("channel.sock");
    let now = Utc::now().to_rfc3339();
    let state = OmpHelmStateFile {
        schema_version: 1,
        session_id: session_id.clone(),
        run_id: response
            .as_ref()
            .map(|value| value.run_id.clone())
            .unwrap_or(run_id),
        provider: "omp".into(),
        model: model.clone(),
        profile: profile.clone(),
        native_session_id: native_id,
        session_file: session_file.display().to_string(),
        cwd: cwd.display().to_string(),
        provider_binary: fs::canonicalize(&binary)
            .unwrap_or_else(|_| PathBuf::from(&binary))
            .display()
            .to_string(),
        session_dir: session_dir.display().to_string(),
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
        title: None,
        ready: false,
        pending_transition: false,
        initial_prompt_delivered: resume_state.is_some(),
        terminal_state: None,
        terminal_reason: None,
        exit_code: None,
        started_at: now.clone(),
        updated_at: now,
    };
    let server = OmpHelmServer::start(
        state,
        socket,
        socket_dir,
        state_root.join(format!("{session_id}.json")),
    )?;
    let extension = write_extension_file(&state_root.join("extensions").join(&session_id))?;
    let mut command = Command::new(&binary);
    command
        .arg("--session-dir")
        .arg(&session_dir)
        .arg("--resume")
        .arg(&session_file)
        .arg("-e")
        .arg(&extension)
        .current_dir(&cwd)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .env("LONGHOUSE_OMP_HELM_CHANNEL_PATH", &server.socket_path)
        .env(
            "LONGHOUSE_OMP_HELM_CHANNEL_TOKEN",
            &server.current_state().channel_token,
        )
        .env(
            "LONGHOUSE_OMP_HELM_INITIAL_PROMPT",
            config.prompt.as_deref().unwrap_or(""),
        );
    if let Some(profile) = profile.as_deref() {
        command.arg("--profile").arg(profile);
    }
    if let Some(model) = model.as_deref() {
        command.arg("--model").arg(model);
    }
    ManagedIdentity::new(ManagedProvider::Omp, &session_id)
        .with_run_id(&server.current_state().run_id)
        .apply(
            &mut command,
            &[
                (
                    "LONGHOUSE_OMP_HELM_CHANNEL_PATH",
                    server.socket_path.to_string_lossy().as_ref(),
                ),
                (
                    "LONGHOUSE_OMP_HELM_CHANNEL_TOKEN",
                    server.current_state().channel_token.as_str(),
                ),
                (
                    "LONGHOUSE_OMP_HELM_INITIAL_PROMPT",
                    config.prompt.as_deref().unwrap_or(""),
                ),
            ],
        );
    let server_for_spawn = server.clone();
    let exit = match run_provider(&mut command, &server, &runtime, |pid| {
        let mut state = server_for_spawn
            .shared
            .lock()
            .expect("OMP state mutex poisoned");
        state.state.provider_pid = Some(pid);
        state.state.provider_process_start_time = process_start_time(Some(pid));
        state.state.status = "running".into();
        state.state.updated_at = Utc::now().to_rfc3339();
        drop(state);
        server_for_spawn.persist_state()?;
        if let Some(transaction) = transaction.as_mut() {
            transaction.confirm_or_degrade("OMP", &crate::config::get_agent_dir()?, &deferred);
        }
        Ok(())
    }) {
        Ok(exit) => exit,
        Err(error) => {
            let _ = server.mark_stopped(None, "launcher_error");
            server.shutdown();
            return Err(error);
        }
    };
    let reason = if server.terminate_requested.load(Ordering::Acquire) {
        "remote_terminate"
    } else {
        "provider_exit"
    };
    let stop_result = server.mark_stopped(Some(exit), reason);
    let current = server.current_state();
    let root = home_state()?;
    let runtime_key = format!("omp:{}", current.session_id);
    let event = ManagedTerminalEvent {
        runtime_key: &runtime_key,
        session_id: &current.session_id,
        run_id: &current.run_id,
        provider: "omp",
        managed_transport: OMP_HELM_TRANSPORT,
        provider_session_id: (!current.native_session_id.is_empty())
            .then_some(current.native_session_id.as_str()),
        device_id: Some(&machine_name),
        source: "omp_helm_launcher",
        dedupe_prefix: "omp-helm-terminal",
        terminal_state: terminal_state_for_exit(exit),
        terminal_reason: reason,
        exit_code: Some(exit),
    }
    .to_json();
    let _ = crate::managed_terminal::enqueue(&root.join("agent/runtime-events-outbox"), &event);
    server.shutdown();
    stop_result?;
    drop(degraded);
    for notice in deferred.drain() {
        eprintln!("{notice}");
    }
    Ok(exit)
}

fn wake_transcript_shipper(state: &OmpHelmStateFile, source: &Path, native_id: &str, reason: &str) {
    let Ok(path) = crate::config::get_agent_transcript_wake_socket_path() else {
        return;
    };
    if !path.exists() || !source.is_absolute() {
        return;
    }
    if let Ok(mut stream) = std::os::unix::net::UnixStream::connect(path) {
        let _ = stream.set_write_timeout(Some(Duration::from_millis(50)));
        let _ = stream.write_all(json!({"provider":"omp","path":source,"phase":state.phase,"session_id":state.session_id,"run_id":state.run_id,"provider_turn_id":native_id,"wake_reason":reason,"observed_at_ms":Utc::now().timestamp_millis(),"file_len_hint":fs::metadata(source).ok().map(|metadata| metadata.len())}).to_string().as_bytes());
    }
}

fn write_json_private<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let parent = path.parent().context("OMP state has no parent")?;
    fs::create_dir_all(parent)?;
    set_private_dir(parent)?;
    let tmp = path.with_file_name(format!(
        ".{}.tmp.{}",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("state"),
        Uuid::new_v4()
    ));
    fs::write(&tmp, format!("{}\n", serde_json::to_string_pretty(value)?))?;
    set_private_file(&tmp)?;
    fs::rename(tmp, path)?;
    set_private_file(path)?;
    Ok(())
}
fn set_private_dir(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
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
fn ensure_private_owned_dir(path: &Path) -> Result<()> {
    fs::create_dir(path)?;
    set_private_dir(path)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state() -> OmpHelmStateFile {
        OmpHelmStateFile {
            schema_version: 1,
            session_id: "session".into(),
            run_id: "run".into(),
            provider: "omp".into(),
            model: None,
            profile: None,
            native_session_id: "native".into(),
            session_file: "/tmp/session.jsonl".into(),
            cwd: "/tmp".into(),
            provider_binary: "/tmp/omp".into(),
            session_dir: "/tmp".into(),
            socket_path: "/tmp/socket".into(),
            channel_token: "token".into(),
            connection_id: "connection".into(),
            lease_generation: "generation".into(),
            launcher_pid: 1,
            launcher_process_start_time: None,
            provider_pid: None,
            provider_process_start_time: None,
            status: "ready".into(),
            phase: "idle".into(),
            tool_name: None,
            title: None,
            initial_prompt_delivered: false,
            ready: true,
            pending_transition: false,
            terminal_state: None,
            terminal_reason: None,
            exit_code: None,
            started_at: "now".into(),
            updated_at: "now".into(),
        }
    }

    #[test]
    fn stale_generation_cannot_authorize_remote_control() {
        let shared = SharedState {
            state: state(),
            extension_sender: Some(mpsc::channel().0),
            extension_connection_id: Some("connection".into()),
            pending: HashMap::new(),
        };
        let frame = json!({
            "auth_token": "token",
            "session_id": "session",
            "native_session_id": "native",
            "connection_id": "connection",
            "lease_generation": "old-generation"
        });
        assert!(!remote_authority_matches_locked(&shared, &frame));
    }

    #[test]
    fn replacement_fence_fails_queued_commands() {
        let (sender, receiver) = mpsc::channel();
        let mut shared = SharedState {
            state: state(),
            extension_sender: None,
            extension_connection_id: None,
            pending: HashMap::from([(String::from("request"), sender)]),
        };
        fail_pending_locked(&mut shared, "replacement");
        let response = receiver.recv().unwrap();
        assert_eq!(response["error"]["code"], "stale_channel");
        assert!(shared.pending.is_empty());
    }

    #[test]
    fn omp_extension_preserves_terminal_and_transition_events() {
        assert!(EXTENSION_ASSET.contains("const write ="));
        assert!(EXTENSION_ASSET.contains("const close ="));
        assert!(EXTENSION_ASSET.contains("initial_prompt_request"));
        assert!(EXTENSION_ASSET.contains("initial_prompt_grant"));
        assert!(EXTENSION_ASSET.contains("session_stop"));
        assert!(EXTENSION_ASSET.contains("pi.on(\"agent_end\""));
        assert!(EXTENSION_ASSET.contains("isTerminal"));
    }

    #[test]
    fn cold_resume_reuses_retained_storage_and_provider_settings() {
        let temp = tempfile::tempdir().unwrap();
        let mut retained = state();
        retained.session_dir = temp.path().join("original-sessions").display().to_string();
        retained.session_file = temp
            .path()
            .join("original-sessions/exact.jsonl")
            .display()
            .to_string();
        retained.profile = Some("original-profile".into());
        retained.model = Some("original-model".into());
        let config = LaunchConfig {
            cwd: temp.path().to_path_buf(),
            prompt: None,
            model: Some("new-model-must-not-win".into()),
            profile: Some("new-profile-must-not-win".into()),
            session_dir: Some(temp.path().join("new-sessions")),
            resume_session: None,
            omp_bin: None,
            url: None,
            token: None,
        };

        let (model, profile) = effective_resume_settings(&config, Some(&retained));
        let (session_dir, session_file) = select_session_storage(
            Some(&retained),
            config.session_dir.as_deref(),
            profile.as_deref(),
            temp.path(),
        )
        .unwrap();

        assert_eq!(model.as_deref(), Some("original-model"));
        assert_eq!(profile.as_deref(), Some("original-profile"));
        assert_eq!(session_dir, PathBuf::from(&retained.session_dir));
        assert_eq!(session_file, PathBuf::from(&retained.session_file));
        assert!(!temp.path().join("original-sessions").exists());
        assert!(!temp.path().join("new-sessions").exists());
    }
}
