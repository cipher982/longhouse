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
use sha2::{Digest, Sha256};
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
const NATIVE_SHUTDOWN_GRACE: Duration = Duration::from_secs(2);
const TRANSITION_RECONCILE_GRACE: Duration = Duration::from_secs(5);
const MAX_FRAME_BYTES: usize = 512 * 1024;
const MAX_PENDING_COMMANDS: usize = 64;
const MAX_LIVE_TEXT_BYTES: usize = 16 * 1024;
/// The daemon writes the agent DB continuously, and every statement on this
/// connection — the open, the source reservation, and the final bind — shares
/// this window. Measured on `cinder` with five managed sessions live:
/// write-lock waits up to 3.7 s, so the 2 s this used to allow lost often
/// enough to leave live sessions marked `degraded` with an empty native
/// identity. A reservation is a launch-gap guard rather than a precondition,
/// so a real budget costs latency only when the database is genuinely busy.
/// How long a preview-only update waits before the slot is rewritten. A token
/// stream restates the preview every few milliseconds; the reader only ever
/// wants the newest one.
const STATUS_SLOT_COALESCE: Duration = Duration::from_millis(100);
const SOURCE_BINDING_BUSY_TIMEOUT: Duration = Duration::from_secs(5);
/// Attempts and spacing for the *required* native identity binding: a busy DB
/// must not become a permanently degraded session.
const SOURCE_BINDING_ATTEMPTS: usize = 3;
const SOURCE_BINDING_RETRY_DELAY: Duration = Duration::from_millis(250);

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
    #[serde(default)]
    provider_binary_sha256: Option<String>,
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
    #[serde(default)]
    agent_end_observed: bool,
    #[serde(default)]
    agent_end_is_terminal: Option<bool>,
    #[serde(default)]
    agent_end_will_continue: Option<bool>,
    #[serde(default)]
    agent_end_is_terminal_present: bool,
    #[serde(default)]
    agent_end_will_continue_present: bool,
    #[serde(default)]
    live_turn_seq: u64,
    #[serde(default)]
    live_message_seq: u64,
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
    /// Request id of an in-flight terminate. Native OMP `ctx.shutdown()` exits
    /// the process before it writes a command_result, so the channel closing is
    /// that command's success, not its failure.
    pending_terminate: Option<String>,
    live_assistant_text: String,
    live_text_seq: u64,
    /// OMP increments this when a new provider turn starts. If the start frame
    /// is lost during a channel outage, the reconnect snapshot still lets the
    /// launcher open the new turn without admitting old drain frames.
    observed_turn_generation: u64,
    live_turn_seq: u64,
    live_message_seq: u64,
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
    status: Arc<StatusPublisher>,
}

/// Writes this session's one status slot.
///
/// Status is replaceable, so it is overwritten rather than queued: a streamed
/// turn used to leave three fsynced files per token in a shared directory, and
/// the Machine Agent then had to guess which of them still mattered.
struct StatusPublisher {
    dir: PathBuf,
    epoch: String,
    state: Mutex<StatusPublisherState>,
}

#[derive(Default)]
struct StatusPublisherState {
    preview: Option<crate::status_slot::StatusPreview>,
    last_written: Option<Instant>,
    last_phase: Option<(String, Option<String>)>,
    seq: u64,
    /// A retired session has no current status. Nothing may recreate its slot,
    /// including a frame that was already in flight when the run ended.
    retired: bool,
}

/// Where this machine's status slots live. A launcher that cannot resolve the
/// agent directory writes nowhere rather than guessing at a path.
fn status_slot_dir_or_default() -> PathBuf {
    crate::config::get_agent_dir()
        .map(|agent| crate::status_slot::status_slot_dir(&agent))
        .unwrap_or_else(|_| PathBuf::from("/dev/null/longhouse-status"))
}

impl StatusPublisher {
    fn new(dir: PathBuf) -> Self {
        Self {
            dir,
            epoch: uuid::Uuid::new_v4().to_string(),
            state: Mutex::new(StatusPublisherState::default()),
        }
    }

    /// Publish the current phase, and the newest preview if there is one.
    ///
    /// A phase or tool change is published immediately; a preview-only update
    /// is coalesced, because a token stream restates it every few
    /// milliseconds and the reader only ever wants the latest.
    fn publish(
        &self,
        state: &OmpHelmStateFile,
        phase: &str,
        tool: Option<&str>,
        preview: Option<crate::status_slot::StatusPreview>,
    ) {
        // The lock is held across the write. Dropping it first let two frames
        // race and land out of order, so the slot could end up holding the
        // older of two states with the newer sequence.
        let mut guard = self.state.lock().expect("OMP status publisher mutex poisoned");
        if guard.retired {
            return;
        }
        let completes_turn = preview.as_ref().is_some_and(|preview| preview.turn_completed);
        if let Some(preview) = preview {
            guard.preview = Some(preview);
        }
        let phase_key = (phase.to_string(), tool.map(str::to_string));
        let transition = guard.last_phase.as_ref() != Some(&phase_key);
        let due = guard
            .last_written
            .map(|written| written.elapsed() >= STATUS_SLOT_COALESCE)
            .unwrap_or(true);
        // A turn's last preview is the one a reader keeps until the next turn
        // starts. Coalescing it away leaves the finished answer truncated
        // until the 20s keepalive, or forever if the run ends first.
        if !transition && !due && !completes_turn {
            return;
        }
        guard.last_phase = Some(phase_key);
        guard.last_written = Some(Instant::now());
        guard.seq += 1;
        let seq = guard.seq;
        let slot = crate::status_slot::StatusSlot {
            schema: crate::status_slot::STATUS_SLOT_SCHEMA,
            session_id: state.session_id.clone(),
            provider: "omp".into(),
            runtime_key: format!("omp:{}", state.session_id),
            run_id: state.run_id.clone(),
            source: OMP_HELM_TRANSPORT.into(),
            phase: phase.to_string(),
            tool_name: tool.map(str::to_string),
            observed_at: state.updated_at.clone(),
            payload: json!({
                "managed_transport": OMP_HELM_TRANSPORT,
                "execution_lifetime": "interactive",
                "structured_remote_approval": false,
            }),
            preview: guard.preview.clone(),
            producer_epoch: self.epoch.clone(),
            seq,
        };
        if let Err(error) = crate::status_slot::publish(&self.dir, &slot) {
            eprintln!(
                "[omp-helm] status slot publish failed for {}: {error}",
                slot.session_id
            );
        }
    }

    /// A new turn starts with no preview. Without this the next phase snapshot
    /// carries the previous turn's text.
    fn clear_preview(&self) {
        let mut guard = self.state.lock().expect("OMP status publisher mutex poisoned");
        guard.preview = None;
    }

    fn retire(&self, session_id: &str) {
        let mut guard = self.state.lock().expect("OMP status publisher mutex poisoned");
        guard.retired = true;
        guard.preview = None;
        crate::status_slot::retire(&self.dir, session_id);
    }
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
                pending_terminate: None,
                live_assistant_text: String::new(),
                live_text_seq: 0,
                observed_turn_generation: 0,
                live_turn_seq: 0,
                live_message_seq: 0,
            })),
            socket_path,
            state_path,
            persist_lock: Arc::new(Mutex::new(())),
            socket_dir,
            stop: Arc::new(AtomicBool::new(false)),
            terminate_requested: Arc::new(AtomicBool::new(false)),
            status: Arc::new(StatusPublisher::new(status_slot_dir_or_default())),
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
            // An unbounded read turns a silent channel into a permanent one: on
            // 2026-09-13 both live sessions' state files froze for nine hours
            // while the provider kept working, because a connected-but-silent
            // endpoint never produced an error and so never reconnected. An
            // extension that advertises a keepalive gets a deadline; one that
            // does not keeps today's behaviour, so a running old extension is
            // never disconnected by a newer launcher.
            let _ = reader
                .get_mut()
                .set_read_timeout(extension_read_timeout(&frame));
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
    fn extension_base_authority_matches(&self, connection_id: &str, frame: &Value) -> bool {
        let state = self.shared.lock().expect("OMP state mutex poisoned");
        extension_base_authority_matches_locked(&state, connection_id, frame)
    }

    fn extension_identity_matches(&self, frame: &Value) -> bool {
        let state = self.shared.lock().expect("OMP state mutex poisoned");
        extension_identity_matches_locked(&state, frame)
    }

    fn activity_identity_reconciliation_allowed(&self) -> bool {
        let state = self.shared.lock().expect("OMP state mutex poisoned");
        !state.state.pending_transition
            && state.state.status != "stopped"
            && state.state.terminal_state.is_none()
            && state.state.terminal_reason.as_deref()
                != Some("native_session_transition_not_committed")
    }
    fn terminal_turn_is_latched(&self) -> bool {
        self.shared
            .lock()
            .expect("OMP state mutex poisoned")
            .state
            .agent_end_is_terminal
            == Some(true)
    }
    fn start_new_turn_locked(shared: &mut SharedState) {
        shared.live_turn_seq = shared.live_turn_seq.saturating_add(1);
        shared.live_message_seq = 0;
        shared.state.live_turn_seq = shared.live_turn_seq;
        shared.state.live_message_seq = 0;
        shared.state.agent_end_observed = false;
        shared.state.agent_end_is_terminal = None;
        shared.state.agent_end_will_continue = None;
        shared.state.agent_end_is_terminal_present = false;
        shared.state.agent_end_will_continue_present = false;
        shared.state.phase = "running".into();
        shared.state.tool_name = None;
        shared.live_assistant_text.clear();
        shared.live_text_seq = 0;
    }

    fn reconcile_turn_generation(&self, frame: &Value) {
        let Some(generation) = frame.get("turn_generation").and_then(Value::as_u64) else {
            return;
        };
        let mut shared = self.shared.lock().expect("OMP state mutex poisoned");
        if shared.state.status == "stopped"
            || shared.state.terminal_state.is_some()
            || generation <= shared.observed_turn_generation
        {
            return;
        }
        shared.observed_turn_generation = generation;
        Self::start_new_turn_locked(&mut shared);
        drop(shared);
        // A new turn starts with no preview. Without this the next phase
        // snapshot carries the previous turn's text.
        self.status.clear_preview();
    }

    fn reconcile_terminal_snapshot(&self, frame: &Value) {
        let Some(terminal) = frame.get("agent_end_terminal").and_then(Value::as_bool) else {
            return;
        };
        let generation = frame.get("turn_generation").and_then(Value::as_u64);
        let mut shared = self.shared.lock().expect("OMP state mutex poisoned");
        if shared.state.status == "stopped"
            || shared.state.terminal_state.is_some()
            || generation.is_some_and(|value| value < shared.observed_turn_generation)
        {
            return;
        }
        shared.state.agent_end_observed = true;
        shared.state.agent_end_is_terminal = Some(terminal);
    }

    fn extension_authority_matches(&self, connection_id: &str, frame: &Value) -> bool {
        let kind = frame
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let state = self.shared.lock().expect("OMP state mutex poisoned");
        if !extension_base_authority_matches_locked(&state, connection_id, frame) {
            return false;
        }
        if matches!(kind, "session_start" | "session_reconnect") {
            return true;
        }
        if matches!(kind, "session_switch" | "session_branch") {
            return state.state.pending_transition;
        }
        if kind == "session_transition_cancelled" {
            return state.state.pending_transition
                && frame.get("native_session_id").and_then(Value::as_str)
                    == Some(state.state.native_session_id.as_str())
                && frame.get("session_file").and_then(Value::as_str)
                    == Some(state.state.session_file.as_str());
        }
        extension_identity_matches_locked(&state, frame)
    }

    fn disconnect_extension(&self, connection_id: &str) {
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        if state.extension_connection_id.as_deref() != Some(connection_id) {
            return;
        }
        settle_pending_terminate_locked(&mut state);
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
        let expected_frame_generation = frame
            .get("lease_generation")
            .and_then(Value::as_str)
            .context("OMP extension frame has no lease generation")?;
        let (session_id, previous, previous_source, expected_generation, expected_pending) = {
            let state = self.shared.lock().expect("OMP state mutex poisoned");
            anyhow::ensure!(
                state.extension_connection_id.as_deref() == Some(connection_id),
                "OMP extension connection was replaced"
            );
            anyhow::ensure!(
                state.state.lease_generation == expected_frame_generation,
                "OMP extension lease generation was replaced"
            );
            anyhow::ensure!(
                state.state.pending_transition == replacement,
                "OMP native session transition state changed"
            );
            (
                state.state.session_id.clone(),
                state.state.native_session_id.clone(),
                state.state.session_file.clone(),
                state.state.lease_generation.clone(),
                state.state.pending_transition,
            )
        };
        if !replacement {
            // OMP rewrites its transcript and, when it compacts or switches,
            // can report a different source or native id on a normal frame
            // without the `session_before_switch` fence. This frame is already
            // authenticated as this launch's extension connection under the
            // current lease generation, so a changed source is the provider
            // moving its own session. Refusing it marked a live session
            // degraded with no recovery path, and the Runtime Host then closed
            // a session whose terminal was still open.
            if source != previous_source || (!previous.is_empty() && previous != native_id) {
                tracing::info!(
                    session_id = %session_id,
                    launched_source = %previous_source,
                    reported_source = %source,
                    launched_native_id = %previous,
                    reported_native_id = %native_id,
                    "OMP reported a new native session without a switch fence; following it"
                );
            }
        }
        // Reserve the exact replacement path before waiting for OMP to finish
        // materializing its header. Discovery then keeps the path pending
        // instead of minting a Shadow session in this transition window.
        let db_path = crate::config::get_agent_db_path()?;
        let conn = open_agent_binding_connection(&db_path)?;
        {
            let state = self.shared.lock().expect("OMP state mutex poisoned");
            identity_commit_authority_matches_locked(
                &state,
                connection_id,
                &expected_generation,
                expected_pending,
            )?;
            crate::omp_session::reserve_source_for_thread(
                &conn,
                Path::new(source),
                &session_id,
                Some(native_id),
            )?;
        }
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
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        identity_commit_authority_matches_locked(
            &state,
            connection_id,
            &expected_generation,
            expected_pending,
        )?;
        // The lease check and the final source bind share one state lock. A
        // timeout can therefore either revoke the lease first, or linearize
        // after this commit, but it cannot turn a late replacement ready.
        crate::omp_session::bind_source_for_thread(
            &conn,
            Path::new(source),
            &session_id,
            native_id,
        )?;
        state.state.native_session_id = native_id.to_string();
        state.state.session_file = source.to_string();
        state.state.pending_transition = false;
        state.state.ready = true;
        state.state.status = "ready".into();
        if previous != native_id {
            state.live_assistant_text.clear();
            state.live_text_seq = 0;
        }
        if replacement {
            state.state.agent_end_observed = false;
            state.state.agent_end_is_terminal = None;
            state.state.agent_end_will_continue = None;
            state.state.agent_end_is_terminal_present = false;
            state.state.agent_end_will_continue_present = false;
            state.state.live_message_seq = 0;
            state.live_message_seq = 0;
            state.live_assistant_text.clear();
            state.live_text_seq = 0;
        }
        state.state.updated_at = Utc::now().to_rfc3339();
        drop(state);
        self.persist_state()?;
        self.publish_binding(Path::new(source), native_id, previous != native_id)?;
        Ok(())
    }

    fn identity_failure_is_current(
        &self,
        connection_id: &str,
        frame: &Value,
        replacement: bool,
    ) -> bool {
        let Some(expected_generation) = frame.get("lease_generation").and_then(Value::as_str)
        else {
            return false;
        };
        let state = self.shared.lock().expect("OMP state mutex poisoned");
        state.extension_connection_id.as_deref() == Some(connection_id)
            && state.state.lease_generation == expected_generation
            && state.state.pending_transition == replacement
    }

    fn mark_degraded(&self, error: &anyhow::Error) {
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        if state.state.status == "stopped" || state.state.terminal_state.is_some() {
            return;
        }
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
        let identity_drift =
            is_activity_frame_kind(kind) && !self.extension_identity_matches(&frame);
        if !self.extension_authority_matches(connection_id, &frame) {
            if !identity_drift
                || !self.extension_base_authority_matches(connection_id, &frame)
                || !self.activity_identity_reconciliation_allowed()
                || !has_native_session_identity(&frame)
            {
                return;
            }
            if let Err(error) = self.update_identity(connection_id, &frame, false) {
                eprintln!("Longhouse: OMP activity identity binding failed: {error:#}");
                return;
            }
            // The binding performs its own connection/lease checks. Recheck
            // the full frame authority before publishing activity in case the
            // extension was replaced while SQLite/header I/O was in flight.
            if !self.extension_authority_matches(connection_id, &frame) {
                return;
            }
        }
        if is_activity_frame_kind(kind) && kind != "agent_start" {
            self.reconcile_turn_generation(&frame);
        }
        if is_activity_frame_kind(kind)
            && !matches!(kind, "agent_start" | "extension_keepalive")
            && self.terminal_turn_is_latched()
        {
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
                json!({"kind": "extension_generation", "connection_id": connection, "lease_generation": generation.clone()}),
            );
            self.schedule_transition_reconcile(connection_id.to_string(), generation);
            return;
        }
        match kind {
            "session_start" | "session_reconnect" => {
                if let Err(error) = self.update_identity(connection_id, &frame, false) {
                    if self.identity_failure_is_current(connection_id, &frame, false) {
                        self.mark_degraded(&error);
                    }
                    eprintln!("Longhouse: OMP native identity binding failed: {error:#}");
                } else if kind == "session_reconnect" {
                    self.reconcile_turn_generation(&frame);
                    self.reconcile_terminal_snapshot(&frame);
                    if let Some(provider_idle) = frame
                        .get("event")
                        .and_then(|event| event.get("provider_idle"))
                        .and_then(Value::as_bool)
                    {
                        self.record_reconnect(provider_idle);
                    }
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
                    if self.identity_failure_is_current(connection_id, &frame, replacement) {
                        self.mark_degraded(&error);
                    }
                    eprintln!("Longhouse: OMP native replacement binding failed: {error:#}");
                }
            }
            "session_transition_cancelled" => {
                let mut state = self.shared.lock().expect("OMP state mutex poisoned");
                if state.extension_connection_id.as_deref() != Some(connection_id)
                    || frame.get("lease_generation").and_then(Value::as_str)
                        != Some(state.state.lease_generation.as_str())
                    || !state.state.pending_transition
                {
                    return;
                }
                if state.state.status == "stopped" || state.state.terminal_state.is_some() {
                    return;
                }
                fail_pending_locked(&mut state, "OMP native session transition cancelled");
                state.state.pending_transition = false;
                state.state.ready = true;
                state.state.status = "ready".into();
                state.state.terminal_reason = None;
                state.state.updated_at = Utc::now().to_rfc3339();
                let generation = state.state.lease_generation.clone();
                let connection = state.state.connection_id.clone();
                drop(state);
                let _ = self.persist_state();
                self.send_extension_frame(
                    connection_id,
                    json!({"kind": "extension_generation", "connection_id": connection, "lease_generation": generation}),
                );
            }
            "extension_keepalive" => {
                self.reconcile_terminal_snapshot(&frame);
                if let Some(provider_idle) = frame.get("provider_idle").and_then(Value::as_bool) {
                    self.record_keepalive(provider_idle);
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
            "activity"
            | "agent_start"
            | "tool_execution_start"
            | "tool_execution_update"
            | "tool_execution_end"
            | "message_start"
            | "message_end"
            | "message_update" => self.record_activity(kind, &frame),
            "agent_end" => {
                // OMP's session event is the authoritative distinction between
                // an intermediate agent loop and a terminal settle. `session_stop`
                // can occur while the session is being torn down or replaced; it
                // must never manufacture an idle observation.
                self.record_activity(kind, &frame)
            }
            "session_shutdown" => self.disconnect_extension(connection_id),
            "initial_prompt_failed" => {
                // The launcher path has no tracing subscriber, so this has to be
                // eprintln to reach the terminal running the session. A prompt
                // that never arrived used to look exactly like one that did.
                let message = frame
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown error");
                eprintln!("Longhouse: OMP initial prompt was not delivered: {message}");
            }
            _ => {}
        }
    }

    fn schedule_transition_reconcile(&self, connection_id: String, lease_generation: String) {
        let server = self.clone();
        thread::spawn(move || {
            thread::sleep(TRANSITION_RECONCILE_GRACE);
            server.reconcile_transition_timeout(&connection_id, &lease_generation);
        });
    }

    fn reconcile_transition_timeout(&self, connection_id: &str, lease_generation: &str) {
        let (connection, generation) = {
            let mut state = self.shared.lock().expect("OMP state mutex poisoned");
            if state.extension_connection_id.as_deref() != Some(connection_id)
                || !state.state.pending_transition
                || state.state.lease_generation != lease_generation
            {
                return;
            }

            // An ambiguous transition must not restore control for either source.
            // Revoke the transition lease so a late native event cannot bind.
            fail_pending_locked(&mut state, "OMP native session transition timed out");
            state.state.pending_transition = false;
            state.state.ready = false;
            state.state.status = "degraded".into();
            state.state.terminal_reason = Some("native_session_transition_not_committed".into());
            state.state.lease_generation = Uuid::new_v4().to_string();
            state.state.updated_at = Utc::now().to_rfc3339();
            let connection = state.state.connection_id.clone();
            let generation = state.state.lease_generation.clone();
            drop(state);
            let _ = self.persist_state();
            (connection, generation)
        };
        self.send_extension_frame(
            connection_id,
            json!({"kind": "extension_generation", "connection_id": connection, "lease_generation": generation}),
        );
    }

    fn append_live_text(target: &mut String, delta: &str) {
        for character in delta.chars() {
            if target.len() + character.len_utf8() > MAX_LIVE_TEXT_BYTES {
                break;
            }
            target.push(character);
        }
    }

    fn record_activity(&self, kind: &str, frame: &Value) {
        let event = frame.get("event");
        let phase = match kind {
            "agent_end" => {
                if is_terminal_agent_end(event) {
                    "idle"
                } else {
                    "running"
                }
            }
            "agent_start"
            | "activity"
            | "tool_execution_start"
            | "tool_execution_update"
            | "tool_execution_end" => "running",
            "message_start" | "message_end" | "message_update" => "thinking",
            _ => return,
        };
        let tool = event
            .and_then(|value| value.get("toolName"))
            .and_then(Value::as_str)
            .map(str::to_string);
        let turn_completed = kind == "agent_end" && is_terminal_agent_end(event);
        let live_delta = (kind == "message_update")
            .then(|| omp_live_text_delta(event).map(str::to_string))
            .flatten();
        let _persist_lock = self
            .persist_lock
            .lock()
            .expect("OMP state persist mutex poisoned");
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        if state.state.status == "stopped" || state.state.terminal_state.is_some() {
            return;
        }
        // A terminal agent_end is the settled boundary for this turn. OMP can
        // emit delayed activity/tool/message frames while the TUI is draining;
        // reject them before touching live-message state, persistence, or
        // publication. The next agent_start re-opens the latch below.
        if state.state.agent_end_is_terminal == Some(true) && kind != "agent_start" {
            return;
        }
        if kind == "agent_start" {
            if let Some(generation) = frame.get("turn_generation").and_then(Value::as_u64) {
                if generation <= state.observed_turn_generation {
                    return;
                }
                state.observed_turn_generation = generation;
            }
            Self::start_new_turn_locked(&mut state);
            self.status.clear_preview();
        } else if kind == "message_start" {
            state.live_message_seq = state.live_message_seq.saturating_add(1);
            state.state.live_message_seq = state.live_message_seq;
            state.live_assistant_text.clear();
            state.live_text_seq = 0;
        } else if kind == "agent_end" {
            let is_terminal = turn_completed;
            state.state.agent_end_observed = true;
            state.state.agent_end_is_terminal = Some(is_terminal);
            state.state.agent_end_is_terminal_present =
                event.and_then(|value| value.get("isTerminal")).is_some();
            state.state.agent_end_will_continue = event
                .and_then(|value| value.get("willContinue"))
                .and_then(Value::as_bool);
            state.state.agent_end_will_continue_present =
                event.and_then(|value| value.get("willContinue")).is_some();
        }
        if let Some(delta) = live_delta.as_deref() {
            Self::append_live_text(&mut state.live_assistant_text, delta);
            state.live_text_seq = state.live_text_seq.saturating_add(1);
        } else if turn_completed && !state.live_assistant_text.is_empty() {
            state.live_text_seq = state.live_text_seq.saturating_add(1);
        }
        state.state.phase = phase.into();
        state.state.tool_name = tool.clone();
        state.state.updated_at = Utc::now().to_rfc3339();
        let current = state.state.clone();
        let live_text = state.live_assistant_text.clone();
        let live_text_seq = state.live_text_seq;
        let live_turn_seq = state.live_turn_seq;
        let live_message_seq = state.live_message_seq;
        let turn_id = live_turn_id(&current.run_id, live_turn_seq, live_message_seq);
        let publish_live = live_delta.is_some() || (turn_completed && !live_text.is_empty());
        if let Err(error) = write_json_private(&self.state_path, &current) {
            eprintln!(
                "[omp-helm] state persistence failed for {}: {error}",
                current.session_id
            );
        }
        drop(state);
        drop(_persist_lock);
        // The local phase ledger is written by the daemon from this session's
        // status slot. A callback that wrote its own file per frame is exactly
        // the cost this lane exists to remove.
        self.publish_phase_snapshot(&current, phase, tool.as_deref());
        if publish_live {
            self.publish_live_text(
                &current,
                &turn_id,
                live_delta.as_deref().unwrap_or_default(),
                &live_text,
                live_text_seq,
                turn_completed,
            );
        }
        if kind == "agent_start" {
            crate::warp_cli_agent::emit_tty_event(
                "omp",
                "prompt_submit",
                &current.session_id,
                Path::new(&current.cwd),
                None,
            );
        }
        if turn_completed {
            crate::warp_cli_agent::emit_tty_event(
                "omp",
                "stop",
                &current.session_id,
                Path::new(&current.cwd),
                None,
            );
        }
    }

    /// Refresh canonical activity evidence from the extension without
    /// manufacturing a lifecycle event. Keepalive observations are frequent:
    /// they preserve the current phase and refresh the provider's live
    /// observation. A terminal latch settles only on an idle sample.
    fn record_keepalive(&self, provider_idle: bool) {
        self.record_keepalive_with_policy(provider_idle);
    }

    /// A reconnect re-samples the provider after the extension may have
    /// crossed a turn boundary while disconnected. Turn-generation
    /// reconciliation happens before this snapshot is recorded.
    fn record_reconnect(&self, provider_idle: bool) {
        self.record_keepalive_with_policy(provider_idle);
    }

    fn record_keepalive_with_policy(&self, provider_idle: bool) {
        let (state, phase, tool) = {
            let _persist_lock = self
                .persist_lock
                .lock()
                .expect("OMP state persist mutex poisoned");
            let mut shared = self.shared.lock().expect("OMP state mutex poisoned");
            if shared.state.status == "stopped" || shared.state.terminal_state.is_some() {
                return;
            }
            let terminal_turn = shared.state.agent_end_is_terminal == Some(true);
            let continuation_turn = shared.state.agent_end_is_terminal == Some(false);
            let settled = terminal_turn && provider_idle;
            shared.state.phase = if settled || (provider_idle && !continuation_turn) {
                "idle".into()
            } else if shared.state.phase == "thinking" {
                "thinking".into()
            } else {
                "running".into()
            };
            shared.state.tool_name = if settled || (provider_idle && !continuation_turn) {
                None
            } else {
                shared.state.tool_name.clone()
            };
            shared.state.updated_at = Utc::now().to_rfc3339();
            let snapshot = shared.state.clone();
            if let Err(error) = write_json_private(&self.state_path, &snapshot) {
                eprintln!(
                    "[omp-helm] state persistence failed for {}: {error}",
                    snapshot.session_id
                );
            }
            (
                snapshot,
                shared.state.phase.clone(),
                shared.state.tool_name.clone(),
            )
        };
        let phase = phase.as_str();
        let tool = tool.as_deref();
        self.publish_phase_snapshot(&state, phase, tool);
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

    fn publish_phase_snapshot(&self, state: &OmpHelmStateFile, phase: &str, tool: Option<&str>) {
        self.status.publish(state, phase, tool, None);
        wake_transcript_shipper(
            state,
            Path::new(&state.session_file),
            &state.native_session_id,
            "phase",
        );
    }
    fn publish_live_text(
        &self,
        state: &OmpHelmStateFile,
        turn_id: &str,
        _delta: &str,
        live_text: &str,
        seq: u64,
        turn_completed: bool,
    ) {
        // The preview rides the status slot. `live_text` is cumulative, so the
        // newest value says everything the deltas said, and the delta itself
        // has no reader on the Runtime Host.
        let preview = crate::status_slot::StatusPreview {
            turn_id: turn_id.to_string(),
            seq,
            live_text: live_text.to_string(),
            turn_completed,
            progress_kind: "omp_helm_stream".into(),
            provider_session_id: (!state.native_session_id.is_empty())
                .then(|| state.native_session_id.clone()),
        };
        let (phase, tool) = {
            let shared = self.shared.lock().expect("OMP state mutex poisoned");
            (shared.state.phase.clone(), shared.state.tool_name.clone())
        };
        self.status
            .publish(state, &phase, tool.as_deref(), Some(preview));
        wake_transcript_shipper(
            state,
            Path::new(&state.session_file),
            &state.native_session_id,
            "progress",
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
            let fault = crate::qa_fault::helm_extension_fault("omp");
            if kind == "abort" && fault == Some(crate::qa_fault::HelmExtensionFault::AbortNoop) {
                crate::qa_fault::record_fired_named(
                    "omp_abort_noop",
                    &state.state.session_id,
                    json!({"forwarded": false}),
                );
                return json!({"kind": "command_result", "ok": true, "status": "active"});
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
            if kind == "steer" && fault == Some(crate::qa_fault::HelmExtensionFault::SteerAsFollowUp) {
                crate::qa_fault::record_fired_named(
                    "omp_steer_as_follow_up",
                    &state.state.session_id,
                    json!({"forwarded_kind": "send"}),
                );
                command["kind"] = json!("send");
            }
            command["connection_id"] = json!(state.state.connection_id);
            command["lease_generation"] = json!(state.state.lease_generation);
            command["native_session_id"] = json!(state.state.native_session_id);
            command["session_file"] = json!(state.state.session_file);
            command["auth_token"] = json!(state.state.channel_token);
            command["session_id"] = json!(state.state.session_id);
            if kind == "terminate" {
                // The request already passed the authenticated ownership check.
                // Arm local group ownership before asking native OMP to drain so
                // a lost acknowledgement cannot leave the provider unowned.
                self.terminate_requested.store(true, Ordering::Release);
                state.pending_terminate = Some(request_id.clone());
            }
            if extension.send(command).is_err() {
                state.pending.remove(&request_id);
                return channel_error("session_not_attached", "OMP extension channel is closed");
            }
        }
        match receiver.recv_timeout(COMMAND_TIMEOUT) {
            Ok(response) => response,
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
        let _persist_lock = self
            .persist_lock
            .lock()
            .expect("OMP state persist mutex poisoned");
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
        let snapshot = state.state.clone();
        drop(state);
        write_json_private(&self.state_path, &snapshot)
    }

    fn shutdown(&self) {
        self.stop.store(true, Ordering::Release);
        let mut state = self.shared.lock().expect("OMP state mutex poisoned");
        settle_pending_terminate_locked(&mut state);
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

fn extension_base_authority_matches_locked(
    state: &SharedState,
    connection_id: &str,
    frame: &Value,
) -> bool {
    state.extension_connection_id.as_deref() == Some(connection_id)
        && frame.get("auth_token").and_then(Value::as_str)
            == Some(state.state.channel_token.as_str())
        && frame.get("session_id").and_then(Value::as_str) == Some(state.state.session_id.as_str())
        && frame.get("connection_id").and_then(Value::as_str)
            == Some(state.state.connection_id.as_str())
        && frame.get("lease_generation").and_then(Value::as_str)
            == Some(state.state.lease_generation.as_str())
}

fn extension_identity_matches_locked(state: &SharedState, frame: &Value) -> bool {
    state.state.native_session_id.is_empty()
        || (frame.get("native_session_id").and_then(Value::as_str)
            == Some(state.state.native_session_id.as_str())
            && frame.get("session_file").and_then(Value::as_str)
                == Some(state.state.session_file.as_str()))
}

fn is_activity_frame_kind(kind: &str) -> bool {
    matches!(
        kind,
        "activity"
            | "extension_keepalive"
            | "agent_start"
            | "tool_execution_start"
            | "tool_execution_update"
            | "tool_execution_end"
            | "message_start"
            | "message_end"
            | "message_update"
            | "agent_end"
    )
}

fn has_native_session_identity(frame: &Value) -> bool {
    frame
        .get("native_session_id")
        .and_then(Value::as_str)
        .is_some_and(|value| !value.trim().is_empty())
        && frame
            .get("session_file")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.trim().is_empty())
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

fn identity_commit_authority_matches_locked(
    state: &SharedState,
    connection_id: &str,
    expected_generation: &str,
    expected_pending: bool,
) -> Result<()> {
    anyhow::ensure!(
        state.extension_connection_id.as_deref() == Some(connection_id),
        "OMP extension connection was replaced during identity binding"
    );
    anyhow::ensure!(
        state.state.lease_generation == expected_generation,
        "OMP extension lease generation was replaced during identity binding"
    );
    anyhow::ensure!(
        state.state.pending_transition == expected_pending,
        "OMP native session transition changed during identity binding"
    );
    anyhow::ensure!(
        state.state.status != "stopped" && state.state.terminal_state.is_none(),
        "OMP execution stopped during identity binding"
    );
    Ok(())
}

fn omp_live_text_delta(event: Option<&Value>) -> Option<&str> {
    let event = event?;
    if event.get("message_event_type").and_then(Value::as_str) != Some("text_delta") {
        return None;
    }
    event
        .get("delta")
        .and_then(Value::as_str)
        .filter(|delta| !delta.is_empty())
}

fn live_turn_id(run_id: &str, turn_seq: u64, message_seq: u64) -> String {
    format!("{run_id}:{turn_seq}:{message_seq}")
}

fn is_terminal_agent_end(event: Option<&Value>) -> bool {
    for field in ["isTerminal", "willContinue"] {
        if event
            .and_then(|value| value.get(field))
            .is_some_and(|value| !value.is_boolean())
        {
            return false;
        }
    }
    event
        .and_then(|value| value.get("isTerminal"))
        .and_then(Value::as_bool)
        .or_else(|| {
            event
                .and_then(|value| value.get("willContinue"))
                .and_then(Value::as_bool)
                .map(|value| !value)
        })
        .unwrap_or(true)
}

fn settle_pending_terminate_locked(state: &mut SharedState) {
    let Some(request_id) = state.pending_terminate.take() else {
        return;
    };
    if let Some(sender) = state.pending.remove(&request_id) {
        let _ = sender.send(json!({
            "kind": "command_result",
            "request_id": request_id,
            "ok": true,
            "status": "stopped",
            "native_session_id": state.state.native_session_id,
        }));
    }
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
        "longhouse-omp-{}-{suffix}-{}",
        user_id,
        Uuid::new_v4().simple()
    ));
    Ok((directory.join("channel.sock"), directory))
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

pub(crate) fn provider_binary_sha256(path: &Path) -> Result<String> {
    let mut file = File::open(path).with_context(|| {
        format!(
            "open OMP executable for integrity check: {}",
            path.display()
        )
    })?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let bytes_read = file.read(&mut buffer)?;
        if bytes_read == 0 {
            break;
        }
        hasher.update(&buffer[..bytes_read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

pub(crate) fn verify_resume_binary_identity(
    retained_binary: &Path,
    current_binary: &Path,
    expected_sha256: &str,
) -> Result<()> {
    anyhow::ensure!(
        fs::canonicalize(retained_binary).ok() == fs::canonicalize(current_binary).ok(),
        "OMP Resume binary does not match the retained launch"
    );
    let current_sha256 = provider_binary_sha256(current_binary)?;
    anyhow::ensure!(
        current_sha256 == expected_sha256,
        "OMP Resume binary integrity does not match the retained launch"
    );
    Ok(())
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
    let expected_sha256 = state
        .provider_binary_sha256
        .as_deref()
        .context("OMP retained launch has no binary integrity identity")?;
    verify_resume_binary_identity(
        Path::new(&state.provider_binary),
        Path::new(binary),
        expected_sha256,
    )?;
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
    verify_resume_owner(
        &facts,
        "launcher",
        Some(state.launcher_pid),
        state.launcher_process_start_time.as_deref(),
        true,
    )?;
    verify_resume_owner(
        &facts,
        "provider",
        state.provider_pid,
        state.provider_process_start_time.as_deref(),
        false,
    )?;
    Ok(state)
}

fn verify_resume_owner(
    facts: &HashMap<u32, crate::process_identity::ProcessFact>,
    label: &str,
    pid: Option<u32>,
    birth: Option<&str>,
    required: bool,
) -> Result<()> {
    match (pid, birth.map(str::trim).filter(|value| !value.is_empty())) {
        (None, None) if !required => Ok(()),
        (Some(pid), Some(birth)) if pid > 0 => {
            if facts.get(&pid).is_some_and(|fact| fact.lstart == birth) {
                anyhow::bail!(
                    "OMP Resume refused while the previous {label} execution owner is still alive"
                );
            }
            Ok(())
        }
        _ => anyhow::bail!("OMP Resume cannot verify prior {label} process identity"),
    }
}

fn effective_profile(config: &LaunchConfig) -> Option<String> {
    let profile = config
        .profile
        .clone()
        .or_else(crate::omp_session::active_profile);
    profile
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn effective_resume_settings(
    config: &LaunchConfig,
    resume_state: Option<&OmpHelmStateFile>,
) -> (Option<String>, Option<String>) {
    match resume_state {
        Some(state) => (state.model.clone(), state.profile.clone()),
        None => (config.model.clone(), effective_profile(config)),
    }
}

fn initial_prompt_delivered(prompt: Option<&str>) -> bool {
    prompt.map(str::trim).is_none_or(str::is_empty)
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
    crate::omp_session::ensure_session_dir_is_disjoint_from_pi(cwd, &session_dir)?;
    let session_file = match resume_state {
        Some(state) => PathBuf::from(&state.session_file),
        None => crate::omp_session::reserve_session_path(&session_dir)?,
    };
    Ok((session_dir, session_file))
}

fn provisional_run_id(session_id: &str) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("longhouse:managed-local-run:{session_id}").as_bytes(),
    )
    .to_string()
}

fn resume_run_id(session_id: &str, resume_attempt_id: &str) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("longhouse:managed-local-resume:{session_id}:{resume_attempt_id}").as_bytes(),
    )
    .to_string()
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
    let mut escalation_sent = false;
    let mut native_shutdown_deadline = None;
    let status = loop {
        if server.terminate_requested.load(Ordering::Acquire) && native_shutdown_deadline.is_none()
        {
            native_shutdown_deadline = Some(Instant::now() + NATIVE_SHUTDOWN_GRACE);
        }
        let signal_requested = signal.load(Ordering::Acquire) != 0;
        let grace_expired =
            native_shutdown_deadline.is_some_and(|deadline| Instant::now() >= deadline);
        if (signal_requested || grace_expired) && !escalation_sent {
            crate::managed_terminal::terminate_owned_group(&mut child, pid as libc::pid_t);
            escalation_sent = true;
        }
        match child.try_wait() {
            Ok(Some(exit)) => {
                break Some(exit);
            }
            Ok(None) => {}
            Err(error) => {
                crate::managed_terminal::terminate_owned_group(&mut child, pid as libc::pid_t);
                return Err(error).context("waiting for stock OMP");
            }
        }
        thread::sleep(Duration::from_millis(25));
    };
    let outcome = runtime.block_on(crate::process_group::shutdown_group(
        pid as i32,
        crate::process_group::DEFAULT_GRACE,
    ));
    if !outcome.is_gone() {
        return Err(anyhow::anyhow!(
            "OMP process group {pid} survived owned cleanup"
        ));
    }
    let exit = status
        .map(|status| status.code().unwrap_or(128))
        .unwrap_or(1);
    Ok(exit)
}

/// Reserve a source path before stock OMP materializes its header.
///
/// Discovery then keeps the path pending instead of minting a Shadow session in
/// the transition window. The daemon writes the agent DB continuously, so a
/// transient busy open must not end the launch: this is a launch-gap guard, not
/// a precondition. The Codex launch path degrades the same way.
fn reserve_source_degrading(
    db_path: &Path,
    path: &Path,
    session_id: &str,
    native_id: Option<&str>,
) {
    let conn = match crate::state::db::open_client_connection(db_path, SOURCE_BINDING_BUSY_TIMEOUT)
    {
        Ok(conn) => conn,
        Err(error) => {
            eprintln!(
                "Longhouse: OMP agent DB unavailable; continuing without a source reservation: {error:#}"
            );
            return;
        }
    };
    if let Err(error) =
        crate::omp_session::reserve_source_for_thread(&conn, path, session_id, native_id)
    {
        eprintln!("Longhouse: OMP source reservation failed; continuing: {error:#}");
    }
}

/// Open the agent DB for a native identity binding.
///
/// The daemon writes this DB continuously, and the binding decides whether the
/// session is usable at all, so a transient busy open must be retried rather
/// than turned into a permanent `degraded` state. A lock held longer than every
/// attempt is still an error: the binding genuinely cannot be proven then.
fn open_agent_binding_connection(db_path: &Path) -> Result<rusqlite::Connection> {
    open_agent_binding_connection_with(
        db_path,
        SOURCE_BINDING_BUSY_TIMEOUT,
        SOURCE_BINDING_ATTEMPTS,
        SOURCE_BINDING_RETRY_DELAY,
    )
}

fn open_agent_binding_connection_with(
    db_path: &Path,
    busy_timeout: Duration,
    attempts: usize,
    retry_delay: Duration,
) -> Result<rusqlite::Connection> {
    let mut attempt = 0;
    loop {
        attempt += 1;
        match crate::state::db::open_client_connection(db_path, busy_timeout) {
            Ok(conn) => return Ok(conn),
            Err(error) => {
                if attempt >= attempts {
                    return Err(error);
                }
                // eprintln, not tracing: the launcher path has no subscriber, so
                // a tracing event here would be invisible in the terminal.
                eprintln!(
                    "Longhouse: OMP agent DB busy; retrying the native identity binding ({attempt}/{attempts})"
                );
            }
        }
        thread::sleep(retry_delay);
    }
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
    let binary_sha256 = provider_binary_sha256(Path::new(&binary))?;
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
    match crate::config::get_agent_db_path() {
        Ok(db_path) => reserve_source_degrading(
            &db_path,
            &session_file,
            &session_id,
            (!native_id.is_empty()).then_some(native_id.as_str()),
        ),
        Err(error) => {
            eprintln!("Longhouse: OMP agent DB path unavailable; continuing: {error:#}")
        }
    }
    let (url, token, machine_name) = registration_credentials(&config)?;
    let resume_attempt_id = resume_state.as_ref().map(|_| Uuid::new_v4().to_string());
    let run_id = resume_attempt_id
        .as_deref()
        .map(|attempt_id| resume_run_id(&session_id, attempt_id))
        .unwrap_or_else(|| provisional_run_id(&session_id));
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
    if let Some(resume_attempt_id) = resume_attempt_id.as_deref() {
        registration["resume_attempt_id"] = json!(resume_attempt_id);
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
    let (socket, socket_dir) = socket_path(&session_id)?;
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
        provider_binary_sha256: Some(binary_sha256),
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
        // A resume prompt is delivered by the extension through Pi's native
        // sendUserMessage path after the resumed session is bound.
        initial_prompt_delivered: initial_prompt_delivered(config.prompt.as_deref()),
        agent_end_observed: false,
        agent_end_is_terminal: None,
        agent_end_will_continue: None,
        agent_end_is_terminal_present: false,
        agent_end_will_continue_present: false,
        live_turn_seq: 0,
        live_message_seq: 0,
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
        )
        .env(
            "LONGHOUSE_OMP_HELM_INITIAL_PROMPT_DELIVERED",
            if server.current_state().initial_prompt_delivered {
                "1"
            } else {
                "0"
            },
        );
    if let Some(profile) = profile.as_deref() {
        command.env("OMP_PROFILE", profile);
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
                (
                    "LONGHOUSE_OMP_HELM_INITIAL_PROMPT_DELIVERED",
                    if server.current_state().initial_prompt_delivered {
                        "1"
                    } else {
                        "0"
                    },
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
        if let Some(registration) = degraded.as_ref() {
            registration.provider_alive.store(true, Ordering::Release);
        }
        let current = server_for_spawn.current_state();
        crate::warp_cli_agent::emit_session_event(
            "omp",
            "session_start",
            &current.session_id,
            Path::new(&current.cwd),
            None,
        );
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
    crate::warp_cli_agent::emit_session_event(
        "omp",
        "stop",
        &current.session_id,
        Path::new(&current.cwd),
        None,
    );
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
    // Retire the slot only once the terminal record is durable. Retiring first
    // and failing here would leave neither a current status nor the evidence
    // that the run ended.
    match crate::managed_terminal::enqueue(&root.join("agent/runtime-events-outbox"), &event) {
        Ok(()) => server.status.retire(&current.session_id),
        Err(error) => eprintln!(
            "[omp-helm] terminal record enqueue failed for {}: {error}; keeping the status slot",
            current.session_id
        ),
    }
    server.shutdown();
    stop_result?;
    drop(degraded);
    for notice in deferred.drain() {
        eprintln!("{notice}");
    }
    Ok(exit)
}

/// How long a keepalive-capable extension may be silent before the channel is
/// treated as dead.
///
/// The extension sends a keepalive on an interval far shorter than this, so
/// silence means the endpoint is gone rather than the provider being idle.
const EXTENSION_SILENCE_DEADLINE: Duration = Duration::from_secs(90);

/// The read deadline for an extension channel, from what the extension declared.
///
/// Only an extension that promises a keepalive may be held to one. Without that
/// declaration the launcher cannot tell an idle provider from a dead channel,
/// and guessing would disconnect healthy sessions.
fn extension_read_timeout(hello: &Value) -> Option<Duration> {
    if hello.get("keepalive").and_then(Value::as_bool) == Some(true) {
        Some(EXTENSION_SILENCE_DEADLINE)
    } else {
        None
    }
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
            provider_binary_sha256: None,
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
            agent_end_observed: false,
            agent_end_is_terminal: None,
            agent_end_will_continue: None,
            agent_end_is_terminal_present: false,
            agent_end_will_continue_present: false,
            live_turn_seq: 0,
            live_message_seq: 0,
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
    fn socket_path_uses_short_unix_temp_root() {
        let (socket, directory) = socket_path("12345678-1234-1234-1234-123456789012").unwrap();
        #[cfg(unix)]
        {
            assert_eq!(directory.parent(), Some(Path::new("/tmp")));
            assert!(socket.to_string_lossy().len() < 104);
        }
    }

    #[test]
    fn resume_run_matches_runtime_host_identity() {
        assert_eq!(
            resume_run_id(
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000002",
            ),
            "9103238b-f01a-5c1d-aef5-08c9296d6642"
        );
    }

    #[test]
    fn channel_loss_settles_an_in_flight_terminate_as_success() {
        let (terminate_tx, terminate_rx) = mpsc::channel();
        let (send_tx, send_rx) = mpsc::channel();
        let mut shared = SharedState {
            state: state(),
            extension_sender: None,
            extension_connection_id: None,
            pending: HashMap::from([
                ("terminate-1".to_string(), terminate_tx),
                ("send-1".to_string(), send_tx),
            ]),
            pending_terminate: Some("terminate-1".into()),
            live_assistant_text: String::new(),
            live_text_seq: 0,
            observed_turn_generation: 0,
            live_turn_seq: 0,
            live_message_seq: 0,
        };

        settle_pending_terminate_locked(&mut shared);
        fail_pending_locked(&mut shared, "OMP Helm server is shutting down");

        let terminate = terminate_rx.recv().unwrap();
        assert_eq!(terminate["ok"], json!(true));
        assert_eq!(terminate["status"], json!("stopped"));
        let send = send_rx.recv().unwrap();
        assert_eq!(send["ok"], json!(false));
        assert_eq!(send["error"]["code"], json!("stale_channel"));
        assert!(shared.pending.is_empty());
        assert!(shared.pending_terminate.is_none());
    }

    #[test]
    fn stale_generation_cannot_authorize_remote_control() {
        let shared = SharedState {
            state: state(),
            extension_sender: Some(mpsc::channel().0),
            extension_connection_id: Some("connection".into()),
            pending: HashMap::new(),
            pending_terminate: None,
            live_assistant_text: String::new(),
            live_text_seq: 0,
            observed_turn_generation: 0,
            live_turn_seq: 0,
            live_message_seq: 0,
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
    fn ordinary_activity_reconciles_native_drift_before_publishing_phase() {
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let source_a = temp.path().join("session-a.jsonl");
        let source_b = temp.path().join("session-b.jsonl");
        fs::write(
            &source_b,
            b"{\"type\":\"session\",\"id\":\"native-b\",\"cwd\":\"/tmp\"}\n",
        )
        .unwrap();
        let mut initial = state();
        initial.session_id = Uuid::new_v4().to_string();
        initial.native_session_id = "native-a".into();
        initial.session_file = source_a.display().to_string();
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");

        temp_env::with_var("LONGHOUSE_HOME", Some(&longhouse_home), || {
            let server =
                OmpHelmServer::start(initial, socket_path, socket_dir, state_path).unwrap();
            let (sender, _receiver) = mpsc::channel();
            {
                let mut shared = server.shared.lock().unwrap();
                shared.extension_sender = Some(sender);
                shared.extension_connection_id = Some("connection".into());
            }
            server.handle_extension_frame(
                "connection",
                json!({
                    "kind": "agent_start",
                    "event": {"type": "agent_start"},
                    "auth_token": "token",
                    "session_id": server.current_state().session_id,
                    "native_session_id": "native-b",
                    "session_file": source_b.display().to_string(),
                    "connection_id": "connection",
                    "lease_generation": "generation"
                }),
            );
            let current = server.current_state();
            assert_eq!(current.native_session_id, "native-b");
            assert_eq!(current.session_file, source_b.display().to_string());
            assert_eq!(current.phase, "running");
            assert!(current.ready);
            assert_eq!(current.status, "ready");
            server.shutdown();
        });
    }

    #[test]
    fn extension_keepalive_refreshes_activity_without_losing_detail_or_reviving_stopped_state() {
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");
        let persisted_state_path = state_path.clone();
        let mut initial = state();
        initial.phase = "thinking".into();
        initial.tool_name = Some("shell".into());

        temp_env::with_var("LONGHOUSE_HOME", Some(&longhouse_home), || {
            let server =
                OmpHelmServer::start(initial, socket_path, socket_dir, state_path).unwrap();
            let (sender, _receiver) = mpsc::channel();
            {
                let mut shared = server.shared.lock().unwrap();
                shared.extension_sender = Some(sender);
                shared.extension_connection_id = Some("connection".into());
            }
            let keepalive = |provider_idle| {
                json!({
                    "kind": "extension_keepalive",
                    "provider_idle": provider_idle,
                    "auth_token": "token",
                    "session_id": "session",
                    "native_session_id": "native",
                    "session_file": "/tmp/session.jsonl",
                    "connection_id": "connection",
                    "lease_generation": "generation"
                })
            };
            let read_json_files =
                |directory: &std::path::Path| -> Vec<(std::path::PathBuf, serde_json::Value)> {
                    let Ok(entries) = fs::read_dir(directory) else {
                        return Vec::new();
                    };
                    entries
                        .flatten()
                        .filter(|entry| {
                            entry.path().extension().and_then(|value| value.to_str())
                                == Some("json")
                        })
                        .filter_map(|entry| {
                            let path = entry.path();
                            let value = serde_json::from_slice(&fs::read(&path).ok()?).ok()?;
                            Some((path, value))
                        })
                        .collect()
                };
            let new_json_file =
                |before: &[(std::path::PathBuf, serde_json::Value)],
                 after: &[(std::path::PathBuf, serde_json::Value)]| {
                    after
                        .iter()
                        .find(|(path, _)| {
                            !before.iter().any(|(before_path, _)| before_path == path)
                        })
                        .map(|(_, value)| value.clone())
                        .unwrap()
                };
            let persisted = || {
                serde_json::from_slice::<serde_json::Value>(
                    &fs::read(&persisted_state_path).unwrap(),
                )
                .unwrap()
            };
            let status_dir = longhouse_home.join("agent/status");
            // Status lives in one slot per session now, so the assertions are
            // about what the slot says rather than how many files appeared.
            let slot = || {
                crate::status_slot::read_all(&status_dir)
                    .into_iter()
                    .find(|slot| slot.session_id == "session")
            };
            let slot_files = || {
                std::fs::read_dir(&status_dir)
                    .map(|entries| {
                        entries
                            .flatten()
                            .filter(|entry| {
                                entry
                                    .file_name()
                                    .to_str()
                                    .is_some_and(|name| name.ends_with(".json"))
                            })
                            .count()
                    })
                    .unwrap_or(0)
            };
            server.handle_extension_frame("connection", keepalive(false));
            let active = server.current_state();
            assert_eq!(active.phase, "thinking");
            assert_eq!(active.tool_name.as_deref(), Some("shell"));
            let persisted_active = persisted();
            assert_eq!(persisted_active["phase"], "thinking");
            assert_eq!(persisted_active["tool_name"], "shell");
            assert_eq!(persisted_active["updated_at"], active.updated_at);
            let active_slot = slot().expect("a live session has a status slot");
            assert_eq!(active_slot.phase, "thinking");
            assert_eq!(active_slot.tool_name.as_deref(), Some("shell"));
            assert_eq!(slot_files(), 1);

            // A second keepalive restating the same phase within the coalesce
            // window writes nothing: the slot already says it.
            server.handle_extension_frame("connection", keepalive(false));
            assert_eq!(slot_files(), 1, "one slot per session, always");
            assert_eq!(slot().expect("slot").seq, active_slot.seq);

            server.handle_extension_frame("connection", keepalive(true));
            let idle = server.current_state();
            assert_eq!(idle.phase, "idle");
            assert_eq!(idle.tool_name, None);
            let persisted_idle = persisted();
            assert_eq!(persisted_idle["phase"], "idle");
            assert_eq!(persisted_idle["tool_name"], serde_json::Value::Null);
            assert_eq!(persisted_idle["updated_at"], idle.updated_at);
            // A transition publishes immediately, coalescing or not.
            let idle_slot = slot().expect("slot");
            assert_eq!(idle_slot.phase, "idle");
            assert_eq!(idle_slot.tool_name, None);
            assert!(idle_slot.seq > active_slot.seq);
            assert_eq!(slot_files(), 1);

            {
                let mut shared = server.shared.lock().unwrap();
                shared.state.updated_at = "2000-01-01T00:00:00+00:00".into();
            }
            server.handle_extension_frame("connection", keepalive(true));
            let refreshed = server.current_state();
            assert_eq!(refreshed.phase, "idle");
            assert_ne!(refreshed.updated_at, "2000-01-01T00:00:00+00:00");
            assert_eq!(persisted()["updated_at"], refreshed.updated_at);

            {
                let mut shared = server.shared.lock().unwrap();
                shared.state.phase = "unknown".into();
                shared.state.tool_name = Some("stale".into());
            }
            server.handle_extension_frame("connection", keepalive(true));
            let unknown = server.current_state();
            assert_eq!(unknown.phase, "idle");
            assert_eq!(unknown.tool_name, None);
            assert_eq!(persisted()["phase"], "idle");
            assert_eq!(persisted()["tool_name"], serde_json::Value::Null);

            server.handle_extension_frame(
                "connection",
                json!({
                    "kind": "agent_end",
                    "event": {"type": "agent_end", "isTerminal": true},
                    "auth_token": "token",
                    "session_id": "session",
                    "native_session_id": "native",
                    "session_file": "/tmp/session.jsonl",
                    "connection_id": "connection",
                    "lease_generation": "generation"
                }),
            );
            let terminal = server.current_state();
            assert_eq!(terminal.phase, "idle");
            assert_eq!(terminal.tool_name, None);
            assert_eq!(terminal.agent_end_is_terminal, Some(true));
            let before_terminal_keepalive = terminal.clone();
            server.handle_extension_frame("connection", keepalive(false));
            let terminal_keepalive_active = server.current_state();
            assert_eq!(terminal_keepalive_active.phase, "running");
            assert_eq!(terminal_keepalive_active.tool_name, None);
            assert_eq!(terminal_keepalive_active.agent_end_is_terminal, Some(true));
            assert_ne!(
                terminal_keepalive_active.updated_at,
                before_terminal_keepalive.updated_at
            );
            assert_eq!(slot_files(), 1);
            assert_eq!(slot().expect("slot").phase, "running");
            assert_eq!(persisted()["phase"], "running");
            assert_eq!(
                persisted()["agent_end_is_terminal"],
                serde_json::Value::Bool(true)
            );
            server.handle_extension_frame("connection", keepalive(true));
            let terminal_keepalive = server.current_state();
            assert_eq!(terminal_keepalive.phase, "idle");
            assert_eq!(terminal_keepalive.agent_end_is_terminal, Some(true));
            let delayed_frame = |kind: &str, event: Value| {
                json!({
                    "kind": kind,
                    "event": event,
                    "auth_token": "token",
                    "session_id": "session",
                    "native_session_id": "native",
                    "session_file": "/tmp/session.jsonl",
                    "connection_id": "connection",
                    "lease_generation": "generation"
                })
            };
            let before_delayed = server.current_state();
            let before_delayed_live = {
                let shared = server.shared.lock().unwrap();
                (
                    shared.live_message_seq,
                    shared.live_text_seq,
                    shared.live_assistant_text.clone(),
                )
            };
            let slot_before_delayed = slot().map(|slot| slot.seq);
            for (kind, event) in [
                (
                    "message_start",
                    json!({"type": "message_start", "message_id": "late"}),
                ),
                (
                    "message_update",
                    json!({
                        "type": "message_update",
                        "message_event_type": "text_delta",
                        "delta": "late text"
                    }),
                ),
                (
                    "message_end",
                    json!({"type": "message_end", "message_id": "late"}),
                ),
                (
                    "agent_end",
                    json!({"type": "agent_end", "isTerminal": false, "willContinue": true}),
                ),
                (
                    "tool_execution_start",
                    json!({"type": "tool_execution_start", "toolName": "late_tool"}),
                ),
                (
                    "tool_execution_update",
                    json!({"type": "tool_execution_update", "toolName": "late_tool"}),
                ),
                (
                    "tool_execution_end",
                    json!({"type": "tool_execution_end", "toolName": "late_tool"}),
                ),
            ] {
                server.handle_extension_frame("connection", delayed_frame(kind, event));
            }
            let delayed_frames = server.current_state();
            assert_eq!(delayed_frames.phase, before_delayed.phase);
            assert_eq!(delayed_frames.tool_name, before_delayed.tool_name);
            assert_eq!(delayed_frames.updated_at, before_delayed.updated_at);
            assert_eq!(
                delayed_frames.agent_end_is_terminal,
                before_delayed.agent_end_is_terminal
            );
            let delayed_live = {
                let shared = server.shared.lock().unwrap();
                (
                    shared.live_message_seq,
                    shared.live_text_seq,
                    shared.live_assistant_text.clone(),
                )
            };
            assert_eq!(delayed_live, before_delayed_live);
            assert_eq!(
                slot().map(|slot| slot.seq),
                slot_before_delayed,
                "a delayed frame after a settled turn states nothing new"
            );
            assert_eq!(persisted()["phase"], "idle");
            assert_eq!(persisted()["updated_at"], before_delayed.updated_at);

            server.handle_extension_frame(
                "connection",
                json!({
                    "kind": "activity",
                    "event": {"type": "activity", "toolName": "late_tool"},
                    "auth_token": "token",
                    "session_id": "session",
                    "native_session_id": "native",
                    "session_file": "/tmp/session.jsonl",
                    "connection_id": "connection",
                    "lease_generation": "generation"
                }),
            );
            let delayed = server.current_state();
            assert_eq!(delayed.phase, "idle");
            assert_eq!(delayed.tool_name, None);
            assert_eq!(delayed.agent_end_is_terminal, Some(true));
            assert_eq!(persisted()["phase"], "idle");
            server.handle_extension_frame(
                "connection",
                json!({
                    "kind": "agent_start",
                    "event": {"type": "agent_start"},
                    "auth_token": "token",
                    "session_id": "session",
                    "native_session_id": "native",
                    "session_file": "/tmp/session.jsonl",
                    "connection_id": "connection",
                    "lease_generation": "generation"
                }),
            );
            let next_turn = server.current_state();
            assert_eq!(next_turn.phase, "running");
            assert_eq!(next_turn.agent_end_is_terminal, None);
            server.handle_extension_frame(
                "connection",
                json!({
                    "kind": "activity",
                    "event": {"type": "activity", "toolName": "next_tool"},
                    "auth_token": "token",
                    "session_id": "session",
                    "native_session_id": "native",
                    "session_file": "/tmp/session.jsonl",
                    "connection_id": "connection",
                    "lease_generation": "generation"
                }),
            );
            let next_activity = server.current_state();
            assert_eq!(next_activity.phase, "running");
            assert_eq!(next_activity.tool_name.as_deref(), Some("next_tool"));

            server.mark_stopped(None, "provider_exit").unwrap();
            // Retirement waits for the terminal record to be durable, so the
            // slot still holds the run's last status here. What a stopped run
            // must never do is state something new.
            let stopped_slot = slot().map(|slot| slot.seq);
            server.handle_extension_frame("connection", keepalive(false));
            let stopped = server.current_state();
            assert_eq!(stopped.status, "stopped");
            assert_eq!(stopped.phase, "idle");
            assert!(stopped.terminal_state.is_some());
            assert_eq!(slot().map(|slot| slot.seq), stopped_slot);

            // Once the terminal record is durable the slot goes, and nothing
            // that arrives afterwards recreates it.
            server.status.retire("session");
            assert!(slot().is_none(), "a retired run has no current status");
            server.handle_extension_frame("connection", keepalive(false));
            assert_eq!(slot_files(), 0, "a retired slot is never recreated");
            server.shutdown();
        });
    }

    /// A turn's final preview is what a reader keeps until the next turn
    /// starts, and a new turn must not inherit the previous one's text.
    #[test]
    fn preview_completion_publishes_and_a_new_turn_clears_it() {
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("home");
        let socket_dir = temp.path().join("sock");
        let socket_path = socket_dir.join("omp.sock");
        let state_path = temp.path().join("state.json");

        temp_env::with_var("LONGHOUSE_HOME", Some(&longhouse_home), || {
            let server =
                OmpHelmServer::start(state(), socket_path, socket_dir, state_path).unwrap();
            let status_dir = longhouse_home.join("agent/status");
            let slot = || {
                crate::status_slot::read_all(&status_dir)
                    .into_iter()
                    .find(|slot| slot.session_id == "session")
            };
            let current = server.current_state();

            server.status.publish(
                &current,
                "thinking",
                None,
                Some(crate::status_slot::StatusPreview {
                    turn_id: "turn-1".into(),
                    seq: 1,
                    live_text: "partial".into(),
                    turn_completed: false,
                    progress_kind: "omp_helm_stream".into(),
                    provider_session_id: None,
                }),
            );
            let opening = slot().expect("slot");
            assert_eq!(opening.preview.expect("preview").live_text, "partial");

            // Immediately after, inside the coalesce window: a completed turn
            // publishes anyway.
            server.status.publish(
                &current,
                "thinking",
                None,
                Some(crate::status_slot::StatusPreview {
                    turn_id: "turn-1".into(),
                    seq: 2,
                    live_text: "the whole answer".into(),
                    turn_completed: true,
                    progress_kind: "omp_helm_stream".into(),
                    provider_session_id: None,
                }),
            );
            let completed = slot().expect("slot").preview.expect("preview");
            assert!(completed.turn_completed);
            assert_eq!(completed.live_text, "the whole answer");

            server.status.clear_preview();
            server.status.publish(&current, "running", None, None);
            assert!(
                slot().expect("slot").preview.is_none(),
                "a new turn does not inherit the last turn's text"
            );
            server.shutdown();
        });
    }

    #[test]
    fn reconnect_resamples_active_provider_after_terminal_turn() {
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let source = temp.path().join("session.jsonl");
        fs::write(
            &source,
            b"{\"type\":\"session\",\"id\":\"native\",\"cwd\":\"/tmp\"}\n",
        )
        .unwrap();
        let session_id = Uuid::new_v4().to_string();
        let mut initial = state();
        initial.session_id = session_id.clone();
        initial.session_file = source.display().to_string();
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");

        temp_env::with_var("LONGHOUSE_HOME", Some(&longhouse_home), || {
            let server =
                OmpHelmServer::start(initial, socket_path, socket_dir, state_path).unwrap();
            let (sender, _receiver) = mpsc::channel();
            {
                let mut shared = server.shared.lock().unwrap();
                shared.extension_sender = Some(sender);
                shared.extension_connection_id = Some("connection".into());
            }
            let frame = |kind: &str, event: Value| {
                json!({
                    "kind": kind,
                    "event": event,
                    "auth_token": "token",
                    "session_id": session_id,
                    "native_session_id": "native",
                    "session_file": source,
                    "connection_id": "connection",
                    "lease_generation": "generation"
                })
            };
            let keepalive = |provider_idle: bool| {
                json!({
                    "kind": "extension_keepalive",
                    "provider_idle": provider_idle,
                    "auth_token": "token",
                    "session_id": session_id,
                    "native_session_id": "native",
                    "session_file": source,
                    "connection_id": "connection",
                    "lease_generation": "generation"
                })
            };
            let generation_frame =
                |kind: &str,
                 event: Value,
                 turn_generation: u64,
                 agent_end_terminal: Option<bool>| {
                    let mut frame = json!({
                        "kind": kind,
                        "event": event,
                        "auth_token": "token",
                        "session_id": session_id,
                        "native_session_id": "native",
                        "session_file": source,
                        "connection_id": "connection",
                        "lease_generation": "generation",
                        "turn_generation": turn_generation
                    });
                    if let Some(terminal) = agent_end_terminal {
                        frame["agent_end_terminal"] = json!(terminal);
                    }
                    frame
                };
            server.handle_extension_frame(
                "connection",
                frame(
                    "agent_end",
                    json!({"type": "agent_end", "isTerminal": true}),
                ),
            );
            assert_eq!(server.current_state().agent_end_is_terminal, Some(true));
            server.handle_extension_frame("connection", keepalive(false));
            let active_without_reconnect = server.current_state();
            assert_eq!(active_without_reconnect.phase, "running");
            assert_eq!(active_without_reconnect.agent_end_is_terminal, Some(true));
            server.handle_extension_frame("connection", keepalive(true));
            assert_eq!(server.current_state().phase, "idle");
            server.handle_extension_frame(
                "connection",
                frame(
                    "session_reconnect",
                    json!({"type": "session_reconnect", "provider_idle": true}),
                ),
            );
            let still_settled = server.current_state();
            assert_eq!(still_settled.phase, "idle");
            assert_eq!(still_settled.tool_name, None);
            assert_eq!(still_settled.agent_end_is_terminal, Some(true));
            server.handle_extension_frame(
                "connection",
                frame(
                    "activity",
                    json!({"type": "activity", "toolName": "late_tool"}),
                ),
            );
            let delayed_after_reconnect = server.current_state();
            assert_eq!(delayed_after_reconnect.phase, "idle");
            assert_eq!(delayed_after_reconnect.tool_name, None);
            assert_eq!(delayed_after_reconnect.agent_end_is_terminal, Some(true));

            server.handle_extension_frame(
                "connection",
                frame(
                    "session_reconnect",
                    json!({"type": "session_reconnect", "provider_idle": false}),
                ),
            );
            let reconnected = server.current_state();
            assert_eq!(reconnected.phase, "running");
            assert_eq!(reconnected.tool_name, None);
            assert_eq!(reconnected.agent_end_is_terminal, Some(true));
            assert!(reconnected.ready);
            assert_eq!(reconnected.status, "ready");
            assert_eq!(reconnected.live_turn_seq, still_settled.live_turn_seq);

            server.handle_extension_frame(
                "connection",
                frame(
                    "activity",
                    json!({"type": "activity", "toolName": "late_tool"}),
                ),
            );
            let delayed_during_provisional = server.current_state();
            assert_eq!(delayed_during_provisional.phase, "running");
            assert_eq!(delayed_during_provisional.tool_name, None);
            assert_eq!(delayed_during_provisional.agent_end_is_terminal, Some(true));

            {
                let mut shared = server.shared.lock().unwrap();
                shared.state.updated_at = "2000-01-01T00:00:00+00:00".into();
            }
            let before_keepalive = server.current_state();
            server.handle_extension_frame("connection", keepalive(false));
            let active_keepalive = server.current_state();
            assert_eq!(active_keepalive.phase, "running");
            assert_eq!(active_keepalive.agent_end_is_terminal, Some(true));
            assert_ne!(active_keepalive.updated_at, before_keepalive.updated_at);

            server.handle_extension_frame("connection", keepalive(true));
            let settled_again = server.current_state();
            assert_eq!(settled_again.phase, "idle");
            assert_eq!(settled_again.tool_name, None);
            assert_eq!(settled_again.agent_end_is_terminal, Some(true));
            // The new OMP turn starts while the channel is down, so no
            // agent_start frame reaches the launcher. The generation in the
            // reconnect snapshot must open it without reviving the old latch.
            server.handle_extension_frame(
                "connection",
                generation_frame(
                    "session_reconnect",
                    json!({"type": "session_reconnect", "provider_idle": false}),
                    2,
                    None,
                ),
            );
            let missed_start = server.current_state();
            assert_eq!(missed_start.phase, "running");
            assert_eq!(missed_start.agent_end_is_terminal, None);
            assert_eq!(
                missed_start.live_turn_seq,
                settled_again.live_turn_seq.saturating_add(1)
            );
            server.handle_extension_frame(
                "connection",
                generation_frame(
                    "activity",
                    json!({"type": "activity", "toolName": "reconnected_tool"}),
                    2,
                    None,
                ),
            );
            assert_eq!(
                server.current_state().tool_name.as_deref(),
                Some("reconnected_tool")
            );

            server.handle_extension_frame(
                "connection",
                frame("agent_start", json!({"type": "agent_start"})),
            );
            let next_turn = server.current_state();
            assert_eq!(next_turn.phase, "running");
            assert_eq!(next_turn.agent_end_is_terminal, None);
            server.handle_extension_frame(
                "connection",
                frame(
                    "activity",
                    json!({"type": "activity", "toolName": "next_tool"}),
                ),
            );
            let active = server.current_state();
            assert_eq!(active.phase, "running");
            assert_eq!(active.tool_name.as_deref(), Some("next_tool"));
            assert_eq!(active.agent_end_is_terminal, None);

            server.handle_extension_frame(
                "connection",
                frame(
                    "agent_end",
                    json!({
                        "type": "agent_end",
                        "isTerminal": false,
                        "willContinue": true
                    }),
                ),
            );
            assert_eq!(server.current_state().agent_end_is_terminal, Some(false));
            server.handle_extension_frame(
                "connection",
                generation_frame(
                    "session_reconnect",
                    json!({"type": "session_reconnect", "provider_idle": true}),
                    2,
                    None,
                ),
            );
            let continuation = server.current_state();
            assert_eq!(continuation.phase, "running");
            assert_eq!(continuation.agent_end_is_terminal, Some(false));
            server.handle_extension_frame(
                "connection",
                frame(
                    "activity",
                    json!({"type": "activity", "toolName": "continuation_tool"}),
                ),
            );
            let continuation_activity = server.current_state();
            assert_eq!(continuation_activity.phase, "running");
            assert_eq!(
                continuation_activity.tool_name.as_deref(),
                Some("continuation_tool")
            );
            assert_eq!(continuation_activity.agent_end_is_terminal, Some(false));
            // A final terminal event can be observed by OMP while the channel
            // is down. Its explicit snapshot supersedes the old continuation
            // latch and settles the served phase.
            server.handle_extension_frame(
                "connection",
                generation_frame(
                    "session_reconnect",
                    json!({"type": "session_reconnect", "provider_idle": true}),
                    2,
                    Some(true),
                ),
            );
            let settled_continuation = server.current_state();
            assert_eq!(settled_continuation.phase, "idle");
            assert_eq!(settled_continuation.agent_end_is_terminal, Some(true));
            server.handle_extension_frame(
                "connection",
                generation_frame(
                    "activity",
                    json!({"type": "activity", "toolName": "late_continuation_tool"}),
                    2,
                    None,
                ),
            );
            let delayed_continuation = server.current_state();
            assert_eq!(delayed_continuation.phase, "idle");
            assert_eq!(delayed_continuation.tool_name, None);

            server.shutdown();
        });
    }

    #[test]
    fn activity_identity_drift_is_refused_during_pending_transition() {
        let temp = tempfile::tempdir().unwrap();
        let mut initial = state();
        initial.session_id = Uuid::new_v4().to_string();
        initial.session_file = temp.path().join("session-a.jsonl").display().to_string();
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");
        let server = OmpHelmServer::start(initial, socket_path, socket_dir, state_path).unwrap();
        let (sender, _receiver) = mpsc::channel();
        {
            let mut shared = server.shared.lock().unwrap();
            shared.extension_sender = Some(sender);
            shared.extension_connection_id = Some("connection".into());
        }
        let before = server.current_state();
        server.handle_extension_frame(
            "connection",
            json!({
                "kind": "session_before_switch",
                "auth_token": "token",
                "session_id": before.session_id,
                "native_session_id": before.native_session_id,
                "session_file": before.session_file,
                "connection_id": "connection",
                "lease_generation": before.lease_generation
            }),
        );
        let switching = server.current_state();
        server.handle_extension_frame(
            "connection",
            json!({
                "kind": "agent_start",
                "event": {"type": "agent_start"},
                "auth_token": "token",
                "session_id": switching.session_id,
                "native_session_id": "late-native",
                "session_file": temp.path().join("late-session.jsonl"),
                "connection_id": "connection",
                "lease_generation": switching.lease_generation
            }),
        );
        let after = server.current_state();
        assert_eq!(after.native_session_id, before.native_session_id);
        assert_eq!(after.session_file, before.session_file);
        assert!(after.pending_transition);
        assert!(!after.ready);
        assert_eq!(after.phase, "idle");
        server.shutdown();
    }

    #[test]
    fn activity_snapshot_waits_for_persistence_lock_before_mutating() {
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");

        temp_env::with_var("LONGHOUSE_HOME", Some(&longhouse_home), || {
            let server =
                OmpHelmServer::start(state(), socket_path, socket_dir, state_path.clone()).unwrap();
            let persist_guard = server.persist_lock.lock().unwrap();
            let worker = {
                let server = server.clone();
                thread::spawn(move || {
                    server.record_activity("agent_start", &json!({"type": "agent_start"}));
                })
            };

            thread::sleep(Duration::from_millis(50));
            assert_eq!(server.current_state().phase, "idle");
            drop(persist_guard);
            worker.join().unwrap();

            server.mark_stopped(None, "provider_exit").unwrap();
            let persisted: OmpHelmStateFile =
                serde_json::from_slice(&fs::read(&state_path).unwrap()).unwrap();
            assert_eq!(persisted.status, "stopped");
            assert!(persisted.terminal_state.is_some());
            assert_eq!(persisted.phase, "idle");
            server.shutdown();
        });
    }
    #[test]
    fn ordinary_activity_rejects_native_drift_at_an_owned_source_path() {
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let source = temp.path().join("session.jsonl");
        fs::write(
            &source,
            b"{\"type\":\"session\",\"id\":\"native-a\",\"cwd\":\"/tmp\"}\n",
        )
        .unwrap();
        let session_id = Uuid::new_v4().to_string();
        let db_path = longhouse_home.join("agent/longhouse-shipper.db");
        let conn = open_agent_binding_connection(&db_path).unwrap();
        crate::omp_session::bind_source_for_thread(&conn, &source, &session_id, "native-a")
            .unwrap();
        drop(conn);

        let mut initial = state();
        initial.session_id = session_id.clone();
        initial.native_session_id = "native-a".into();
        initial.session_file = source.display().to_string();
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");

        temp_env::with_var("LONGHOUSE_HOME", Some(&longhouse_home), || {
            let server =
                OmpHelmServer::start(initial, socket_path, socket_dir, state_path).unwrap();
            let (sender, _receiver) = mpsc::channel();
            {
                let mut shared = server.shared.lock().unwrap();
                shared.extension_sender = Some(sender);
                shared.extension_connection_id = Some("connection".into());
            }
            server.handle_extension_frame(
                "connection",
                json!({
                    "kind": "agent_start",
                    "event": {"type": "agent_start"},
                    "auth_token": "token",
                    "session_id": session_id,
                    "native_session_id": "native-b",
                    "session_file": source.display().to_string(),
                    "connection_id": "connection",
                    "lease_generation": "generation"
                }),
            );
            let current = server.current_state();
            assert_eq!(current.native_session_id, "native-a");
            assert_eq!(current.session_file, source.display().to_string());
            assert_eq!(current.phase, "idle");
            assert!(current.ready);
            assert_eq!(current.status, "ready");
            server.shutdown();
        });
    }

    #[test]
    fn stopped_identity_reconciliation_cannot_restore_readiness() {
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let source_a = temp.path().join("session-a.jsonl");
        let source_b = temp.path().join("session-b.jsonl");
        let session_id = Uuid::new_v4().to_string();
        let mut initial = state();
        initial.session_id = session_id.clone();
        initial.native_session_id = "native-a".into();
        initial.session_file = source_a.display().to_string();
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");

        temp_env::with_var("LONGHOUSE_HOME", Some(&longhouse_home), || {
            let server =
                OmpHelmServer::start(initial, socket_path, socket_dir, state_path).unwrap();
            let (sender, _receiver) = mpsc::channel();
            {
                let mut shared = server.shared.lock().unwrap();
                shared.extension_sender = Some(sender);
                shared.extension_connection_id = Some("connection".into());
            }
            let frame = json!({
                "kind": "agent_start",
                "event": {"type": "agent_start"},
                "auth_token": "token",
                "session_id": session_id,
                "native_session_id": "native-b",
                "session_file": source_b.display().to_string(),
                "connection_id": "connection",
                "lease_generation": "generation"
            });
            let worker = {
                let server = server.clone();
                thread::spawn(move || server.update_identity("connection", &frame, false))
            };
            thread::sleep(Duration::from_millis(100));
            server.mark_stopped(None, "provider_exit").unwrap();
            fs::write(
                &source_b,
                b"{\"type\":\"session\",\"id\":\"native-b\",\"cwd\":\"/tmp\"}\n",
            )
            .unwrap();

            assert!(worker.join().unwrap().is_err());
            let after = server.current_state();
            assert_eq!(after.native_session_id, "native-a");
            assert_eq!(after.session_file, source_a.display().to_string());
            assert!(!after.ready);
            assert_eq!(after.status, "stopped");
            assert_eq!(after.phase, "idle");
            assert_eq!(after.terminal_reason.as_deref(), Some("provider_exit"));
            server.shutdown();
        });
    }

    #[test]
    fn replacement_fence_fails_queued_commands() {
        let (sender, receiver) = mpsc::channel();
        let mut shared = SharedState {
            state: state(),
            extension_sender: None,
            extension_connection_id: None,
            pending: HashMap::from([(String::from("request"), sender)]),
            pending_terminate: None,
            live_assistant_text: String::new(),
            live_text_seq: 0,
            observed_turn_generation: 0,
            live_turn_seq: 0,
            live_message_seq: 0,
        };
        fail_pending_locked(&mut shared, "replacement");
        let response = receiver.recv().unwrap();
        assert_eq!(response["error"]["code"], "stale_channel");
        assert!(shared.pending.is_empty());
    }

    #[test]
    fn cancelled_transition_restores_authority_for_unchanged_source() {
        let temp = tempfile::tempdir().unwrap();
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");
        let server = OmpHelmServer::start(state(), socket_path, socket_dir, state_path).unwrap();
        let (sender, _receiver) = mpsc::channel();
        {
            let mut shared = server.shared.lock().unwrap();
            shared.extension_sender = Some(sender);
            shared.extension_connection_id = Some("connection".into());
        }
        let before = json!({
            "kind": "session_before_switch",
            "auth_token": "token",
            "session_id": "session",
            "native_session_id": "native",
            "session_file": "/tmp/session.jsonl",
            "connection_id": "connection",
            "lease_generation": "generation"
        });
        server.handle_extension_frame("connection", before);
        let switching = server.current_state();
        assert!(switching.pending_transition);
        assert!(!switching.ready);
        let cancelled = json!({
            "kind": "session_transition_cancelled",
            "auth_token": "token",
            "session_id": "session",
            "native_session_id": "native",
            "session_file": "/tmp/session.jsonl",
            "connection_id": "connection",
            "lease_generation": switching.lease_generation
        });
        server.handle_extension_frame("connection", cancelled);
        let restored = server.current_state();
        assert!(restored.ready);
        assert!(!restored.pending_transition);
        assert_eq!(restored.status, "ready");
        server.shutdown();
    }

    #[test]
    fn timed_out_transition_rejects_late_replacement() {
        let temp = tempfile::tempdir().unwrap();
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");
        let server = OmpHelmServer::start(state(), socket_path, socket_dir, state_path).unwrap();
        let (sender, _receiver) = mpsc::channel();
        {
            let mut shared = server.shared.lock().unwrap();
            shared.extension_sender = Some(sender);
            shared.extension_connection_id = Some("connection".into());
        }
        server.handle_extension_frame(
            "connection",
            json!({
                "kind": "session_before_switch",
                "auth_token": "token",
                "session_id": "session",
                "native_session_id": "native",
                "session_file": "/tmp/session.jsonl",
                "connection_id": "connection",
                "lease_generation": "generation"
            }),
        );
        let switching = server.current_state();
        server.reconcile_transition_timeout("connection", &switching.lease_generation);
        let degraded = server.current_state();
        assert!(!degraded.ready);
        assert!(!degraded.pending_transition);
        assert_eq!(degraded.status, "degraded");

        server.handle_extension_frame(
            "connection",
            json!({
                "kind": "session_transition_cancelled",
                "auth_token": "token",
                "session_id": "session",
                "native_session_id": "native",
                "session_file": "/tmp/session.jsonl",
                "connection_id": "connection",
                "lease_generation": switching.lease_generation
            }),
        );
        let after_stale_cancel = server.current_state();
        assert!(!after_stale_cancel.ready);
        assert_eq!(after_stale_cancel.status, "degraded");

        for kind in ["session_switch", "session_branch"] {
            server.handle_extension_frame(
                "connection",
                json!({
                    "kind": kind,
                    "auth_token": "token",
                    "session_id": "session",
                    "native_session_id": "late-native",
                    "session_file": "/tmp/late-session.jsonl",
                    "connection_id": "connection",
                    "lease_generation": degraded.lease_generation
                }),
            );
            let after = server.current_state();
            assert_eq!(after.native_session_id, "native");
            assert_eq!(after.session_file, "/tmp/session.jsonl");
            assert!(!after.ready);
            assert_eq!(after.status, "degraded");
        }
        server.handle_extension_frame(
            "connection",
            json!({
                "kind": "agent_start",
                "event": {"type": "agent_start"},
                "auth_token": "token",
                "session_id": "session",
                "native_session_id": "late-native",
                "session_file": "/tmp/late-session.jsonl",
                "connection_id": "connection",
                "lease_generation": degraded.lease_generation
            }),
        );
        let after_activity = server.current_state();
        assert_eq!(after_activity.native_session_id, "native");
        assert_eq!(after_activity.session_file, "/tmp/session.jsonl");
        assert!(!after_activity.ready);
        assert_eq!(after_activity.status, "degraded");
        assert_eq!(after_activity.phase, "idle");
        server.shutdown();
    }

    #[test]
    fn in_flight_replacement_cannot_commit_after_lease_timeout() {
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let replacement_source = temp.path().join("late-replacement.jsonl");
        let mut initial = state();
        initial.session_id = Uuid::new_v4().to_string();
        initial.session_file = temp.path().join("original.jsonl").display().to_string();
        let socket_dir = temp.path().join("socket");
        let socket_path = socket_dir.join("channel.sock");
        let state_path = temp.path().join("state.json");

        temp_env::with_var("LONGHOUSE_HOME", Some(&longhouse_home), || {
            let server =
                OmpHelmServer::start(initial, socket_path, socket_dir, state_path).unwrap();
            let (sender, _receiver) = mpsc::channel();
            {
                let mut shared = server.shared.lock().unwrap();
                shared.extension_sender = Some(sender);
                shared.extension_connection_id = Some("connection".into());
            }
            server.handle_extension_frame(
                "connection",
                json!({
                    "kind": "session_before_switch",
                    "auth_token": "token",
                    "session_id": server.current_state().session_id,
                    "native_session_id": "native",
                    "session_file": server.current_state().session_file,
                    "connection_id": "connection",
                    "lease_generation": "generation"
                }),
            );
            let switching = server.current_state();
            let frame = json!({
                "kind": "session_switch",
                "auth_token": "token",
                "session_id": switching.session_id,
                "native_session_id": "late-native",
                "session_file": replacement_source,
                "connection_id": "connection",
                "lease_generation": switching.lease_generation
            });
            let worker = {
                let server = server.clone();
                std::thread::spawn(move || server.update_identity("connection", &frame, true))
            };
            std::thread::sleep(Duration::from_millis(100));
            server.reconcile_transition_timeout("connection", &switching.lease_generation);
            fs::write(
                &replacement_source,
                b"{\"type\":\"session\",\"id\":\"late-native\",\"cwd\":\"/tmp\"}\n",
            )
            .unwrap();

            assert!(worker.join().unwrap().is_err());
            let after = server.current_state();
            assert_eq!(after.native_session_id, "native");
            assert!(!after.ready);
            assert!(!after.pending_transition);
            assert_eq!(after.status, "degraded");
            server.shutdown();
        });
    }

    #[test]
    fn omp_extension_preserves_terminal_and_transition_events() {
        assert!(EXTENSION_ASSET.contains("const write ="));
        assert!(EXTENSION_ASSET.contains("const close ="));
        assert!(EXTENSION_ASSET.contains("const scheduleReconnect ="));
        assert!(EXTENSION_ASSET.contains("MAX_FRAME_BYTES"));
        assert!(EXTENSION_ASSET.contains("initial_prompt_request"));
        assert!(EXTENSION_ASSET.contains("LONGHOUSE_OMP_HELM_INITIAL_PROMPT_DELIVERED"));
        assert!(EXTENSION_ASSET.contains("initial_prompt_grant"));
        assert!(EXTENSION_ASSET.contains("session_stop"));
        assert!(EXTENSION_ASSET.contains("session_transition_cancelled"));
        assert!(EXTENSION_ASSET.contains("pi.on(\"agent_end\""));
        assert!(EXTENSION_ASSET.contains("compactLifecycleEvent"));
        assert!(!EXTENSION_ASSET.contains("event, ...session(ctx)"));
        assert!(EXTENSION_ASSET.contains("tool_execution_update"));
        assert!(EXTENSION_ASSET.contains("message_start"));
        assert!(EXTENSION_ASSET.contains("message_update"));
        assert!(EXTENSION_ASSET.contains("assistantMessageEvent"));
        assert!(EXTENSION_ASSET.contains("message_end"));
        assert!(EXTENSION_ASSET.contains("isTerminal"));
        assert!(EXTENSION_ASSET.contains("MALFORMED_BOOLEAN_MARKER"));
        assert!(EXTENSION_ASSET.contains("value !== null && value !== undefined"));
    }

    #[test]
    fn ordinary_agent_end_is_terminal_without_overriding_continuation() {
        assert!(is_terminal_agent_end(None));
        assert!(is_terminal_agent_end(Some(&json!({"type": "agent_end"}))));
        assert!(!is_terminal_agent_end(Some(
            &json!({"type": "agent_end", "willContinue": true})
        )));
        assert!(!is_terminal_agent_end(Some(
            &json!({"type": "agent_end", "isTerminal": false, "willContinue": false})
        )));
        assert!(is_terminal_agent_end(Some(
            &json!({"type": "agent_end", "isTerminal": true, "willContinue": true})
        )));
        assert!(!is_terminal_agent_end(Some(
            &json!({"type": "agent_end", "isTerminal": "true"})
        )));
        assert!(!is_terminal_agent_end(Some(
            &json!({"type": "agent_end", "willContinue": "false"})
        )));
    }
    #[test]
    fn live_preview_accepts_text_deltas_only() {
        let text_delta = json!({"message_event_type": "text_delta", "delta": "hello"});
        let thinking_delta =
            json!({"message_event_type": "thinking_delta", "delta": "secret reasoning"});
        let empty_delta = json!({"message_event_type": "text_delta", "delta": ""});

        assert_eq!(omp_live_text_delta(Some(&text_delta)), Some("hello"));
        assert_eq!(omp_live_text_delta(Some(&thinking_delta)), None);
        assert_eq!(omp_live_text_delta(Some(&empty_delta)), None);
    }

    #[test]
    fn live_preview_turn_id_changes_when_agent_starts_again() {
        assert_ne!(live_turn_id("run", 1, 1), live_turn_id("run", 2, 1));
        assert_ne!(live_turn_id("run", 2, 1), live_turn_id("run", 2, 2));
        assert_eq!(live_turn_id("run", 2, 2), "run:2:2");
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

    #[test]
    fn fresh_launch_captures_ambient_omp_profile_for_resume() {
        let temp = tempfile::tempdir().unwrap();
        let config = LaunchConfig {
            cwd: temp.path().to_path_buf(),
            prompt: None,
            model: None,
            profile: None,
            session_dir: None,
            resume_session: None,
            omp_bin: None,
            url: None,
            token: None,
        };

        temp_env::with_var("OMP_PROFILE", Some("work"), || {
            let (model, profile) = effective_resume_settings(&config, None);
            assert_eq!(model, None);
            assert_eq!(profile.as_deref(), Some("work"));
        });
    }

    #[test]
    fn supplied_resume_prompt_stays_pending_for_native_delivery() {
        assert!(!initial_prompt_delivered(Some(
            "continue from the retained session"
        )));
        assert!(initial_prompt_delivered(Some("  ")));
        assert!(initial_prompt_delivered(None));
    }

    #[test]
    fn resume_owner_birth_identity_is_required() {
        let facts = HashMap::new();
        assert!(verify_resume_owner(&facts, "launcher", Some(42), None, true).is_err());
        assert!(verify_resume_owner(&facts, "provider", Some(42), None, false).is_err());
        assert!(verify_resume_owner(&facts, "provider", None, None, false).is_ok());
    }

    #[test]
    fn only_a_keepalive_capable_extension_gets_a_read_deadline() {
        // An extension that does not promise a keepalive keeps the unbounded
        // read: a launcher upgrade must never disconnect a running old
        // extension, because silence is indistinguishable from an idle provider.
        assert_eq!(extension_read_timeout(&serde_json::json!({})), None);
        assert_eq!(
            extension_read_timeout(&serde_json::json!({ "keepalive": false })),
            None
        );
        assert_eq!(
            extension_read_timeout(&serde_json::json!({ "keepalive": true })),
            Some(EXTENSION_SILENCE_DEADLINE)
        );
        // The deadline must be comfortably longer than the extension's own
        // interval, or a healthy channel would be reconnected on a timer.
        assert!(EXTENSION_SILENCE_DEADLINE >= Duration::from_secs(60));
    }

    #[test]
    fn source_reservation_still_lands_on_a_healthy_agent_db() {
        // Degrading on a busy agent DB must not quietly retire the launch-gap
        // guard: a healthy DB still records the reservation.
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("agent/longhouse-shipper.db");
        let source = temp.path().join("session.jsonl");
        std::fs::write(&source, "{}\n").unwrap();
        let session_id = Uuid::new_v4().to_string();

        reserve_source_degrading(&db_path, &source, &session_id, None);

        let conn =
            crate::state::db::open_client_connection(&db_path, Duration::from_millis(500)).unwrap();
        let (bound_session, provider) = conn
            .query_row(
                "SELECT session_id, provider FROM session_binding",
                [],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .unwrap();
        assert_eq!(bound_session, session_id);
        assert_eq!(provider, "omp");
    }

    #[test]
    fn identity_binding_retries_a_transiently_locked_agent_db() {
        // A lock that outlives the busy timeout must be retried, not turned
        // into a permanently degraded session.
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("agent/longhouse-shipper.db");
        let holder =
            crate::state::db::open_client_connection(&db_path, Duration::from_millis(500)).unwrap();
        holder.execute_batch("BEGIN IMMEDIATE").unwrap();
        let releaser = thread::spawn(move || {
            thread::sleep(Duration::from_millis(200));
            holder.execute_batch("ROLLBACK").unwrap();
            holder
        });

        let opened = open_agent_binding_connection_with(
            &db_path,
            Duration::from_millis(20),
            10,
            Duration::from_millis(50),
        );

        let _holder = releaser.join().unwrap();
        assert!(
            opened.is_ok(),
            "a transient lock must be retried to success: {:?}",
            opened.err()
        );
    }

    #[test]
    fn identity_binding_gives_up_after_bounded_attempts() {
        // An agent DB that can never open must return, not retry forever.
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("agent/longhouse-shipper.db");
        std::fs::create_dir_all(&db_path).unwrap();

        let opened = open_agent_binding_connection_with(
            &db_path,
            Duration::from_millis(20),
            3,
            Duration::from_millis(1),
        );

        assert!(opened.is_err(), "an unopenable agent DB must not succeed");
    }
}
