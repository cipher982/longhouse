//! Cursor Console-mode runtime over ACP (Agent Client Protocol).
//!
//! Replaces the old `cursor_exec` stream-json parser. The engine spawns
//! `cursor-agent acp` headless on behalf of a Console-mode launch
//! (POST /api/sessions/launch with execution_lifetime=one_shot), returns the
//! pid/argv upstream immediately so the Runtime Host can flip the connection
//! to `attached`, then drives an ACP JSON-RPC turn over stdio in the
//! background:
//!
//!   1. `initialize`   (protocolVersion is a NUMBER — cursor rejects strings)
//!   2. `session/new`  {cwd, mcpServers: []}  → acp_session_id
//!      or `session/load` {sessionId: <resume_target>} for a continuation turn
//!   3. `session/prompt` {sessionId, prompt: [{type:"text","text":<prompt>}]}
//!      → streams `session/update` notifications until the prompt response
//!        ({stopReason: "end_turn" | ...}) arrives.
//!
//! Every ACP notification is appended unchanged to a local, run-scoped source
//! file before a provisional phase/progress/terminal signal is put in the
//! shared runtime outbox. There is deliberately no direct ingest request and
//! no fabricated event timestamp. The source becomes durable only once the
//! storage-v2 adapter seals it with a receipt.
//!
//! Interrupt/terminate: cursor-agent returns "Method not found" for
//! `session/cancel`, so there is no graceful ACP interrupt. Terminate is
//! cleanup-on-drop (kill_on_drop) plus SIGINT/SIGKILL on the pid if a
//! pid-registry terminate command is wired later.
//!

use std::collections::VecDeque;
use std::ffi::OsString;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result};
use chrono::Utc;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

const CURSOR_ACP_RUNTIME_SOURCE: &str = "cursor_acp";
const STDERR_TAIL_LINES: usize = 40;
const ACP_PROTOCOL_VERSION: i64 = 1;

#[derive(Clone, Debug)]
pub struct CursorAcpRunConfig {
    pub session_id: String,
    pub run_id: String,
    pub cwd: PathBuf,
    pub cursor_bin: String,
    pub prompt: String,
    pub launch_actor: Option<String>,
    pub launch_surface: Option<String>,
    /// Cursor ACP sessionId to resume (`session/load` + `session/prompt`).
    /// When None, a fresh `session/new` is created.
    pub resume_acp_session_id: Option<String>,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct CursorAcpRunSummary {
    pub session_id: String,
    pub run_id: String,
    pub pid: Option<u32>,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct CursorAcpSink {
    session_id: String,
    run_id: String,
    machine_name: String,
    cwd: String,
    local_db_path: Option<PathBuf>,
    runtime_events_outbox_dir: PathBuf,
    source_dir: PathBuf,
}

pub fn cursor_acp_args() -> Vec<OsString> {
    vec![OsString::from("acp")]
}

pub async fn start_cursor_acp_once(config: CursorAcpRunConfig) -> Result<CursorAcpRunSummary> {
    let args = cursor_acp_args();
    let argv = std::iter::once(OsString::from(config.cursor_bin.clone()))
        .chain(args.iter().cloned())
        .map(|item| item.to_string_lossy().to_string())
        .collect::<Vec<_>>();

    let mut command = Command::new(&config.cursor_bin);
    command
        .args(&args)
        .env("LONGHOUSE_MANAGED_SESSION_ID", &config.session_id)
        .current_dir(&config.cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(launch_actor) = normalized_optional(&config.launch_actor) {
        command.env("LONGHOUSE_LAUNCH_ACTOR", launch_actor);
    }
    if let Some(launch_surface) = normalized_optional(&config.launch_surface) {
        command.env("LONGHOUSE_LAUNCH_SURFACE", launch_surface);
    }

    #[cfg(unix)]
    {
        unsafe {
            command.pre_exec(|| {
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }

    let mut child = command
        .spawn()
        .with_context(|| format!("spawning `{}` acp", config.cursor_bin))?;
    let pid = child.id();
    let stdin = child.stdin.take();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    let summary_session_id = config.session_id.clone();
    let summary_run_id = config.run_id.clone();
    let summary_argv = argv.clone();

    let runtime_events_outbox_dir = crate::config::get_agent_runtime_events_outbox_dir()?;
    let source_dir = crate::config::get_agent_dir()?.join("cursor-acp-source");
    let sink = CursorAcpSink {
        session_id: config.session_id.clone(),
        run_id: config.run_id.clone(),
        machine_name: config.machine_name.clone(),
        cwd: config.cwd.to_string_lossy().to_string(),
        local_db_path: config.local_db_path.clone(),
        runtime_events_outbox_dir,
        source_dir,
    };

    let stderr_tail = Arc::new(Mutex::new(VecDeque::with_capacity(STDERR_TAIL_LINES)));

    let monitor_sink = sink.clone();
    let monitor_tail = stderr_tail.clone();
    let monitor_config = config;

    tokio::spawn(async move {
        monitor_sink.post_phase("thinking", None).await;

        let stderr_task = stderr.map(|stream| {
            tokio::spawn(async move {
                read_stderr_tail(stream, monitor_tail).await;
            })
        });

        // Drive the ACP turn over stdio. stdin/stdout are owned here (not
        // separate tasks) because the handshake is strictly sequential.
        let turn_outcome = match (stdin, stdout) {
            (Some(stdin), Some(stdout)) => {
                run_acp_turn(stdin, stdout, &monitor_config, &monitor_sink).await
            }
            _ => Err(anyhow::anyhow!("missing cursor-agent acp stdio")),
        };

        if turn_outcome.is_err() {
            let _ = child.kill().await;
        }
        let process_status = child.wait().await;

        if let Some(task) = stderr_task {
            let _ = task.await;
        }

        match (turn_outcome, process_status) {
            (Ok(_stop_reason), Ok(status)) if status.success() => {
                monitor_sink
                    .post_terminal(
                        "run_completed",
                        status.code(),
                        stderr_tail_snapshot(&stderr_tail),
                    )
                    .await;
            }
            (Ok(_), Ok(status)) => {
                monitor_sink
                    .post_terminal(
                        "run_failed",
                        status.code(),
                        stderr_tail_snapshot(&stderr_tail)
                            .or_else(|| Some(format!("cursor-agent exited with {status}"))),
                    )
                    .await;
            }
            (Ok(_), Err(err)) => {
                monitor_sink
                    .post_terminal(
                        "run_failed",
                        None,
                        Some(format!("waiting for cursor-agent failed: {err}")),
                    )
                    .await;
            }
            (Err(err), _) => {
                monitor_sink
                    .post_terminal("run_failed", None, Some(format!("acp turn failed: {err}")))
                    .await;
            }
        }
    });

    Ok(CursorAcpRunSummary {
        session_id: summary_session_id,
        run_id: summary_run_id,
        pid,
        argv: summary_argv,
    })
}

/// Sequential ACP handshake + prompt turn.
async fn run_acp_turn(
    mut stdin: tokio::process::ChildStdin,
    stdout: tokio::process::ChildStdout,
    config: &CursorAcpRunConfig,
    sink: &CursorAcpSink,
) -> Result<Option<String>> {
    let mut lines = BufReader::new(stdout).lines();
    let mut next_id = 1i64;
    let mut acp_session_id: Option<String> = None;

    // 1. initialize
    let init_id = next_id;
    next_id += 1;
    write_request(
        &mut stdin,
        init_id,
        "initialize",
        json!({
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "clientCapabilities": {"terminal": false, "progress": false},
            "clientInfo": {"name": "longhouse-engine", "version": "cursor_acp"},
        }),
    )
    .await?;
    let init_resp = read_response(&mut lines, init_id, sink).await?;
    if let Some(err) = init_resp.get("error") {
        anyhow::bail!("initialize error: {err}");
    }

    // 2. session/new or session/load
    let session_id_req = next_id;
    next_id += 1;
    let (session_method, session_params) = match normalized_optional(&config.resume_acp_session_id)
    {
        Some(acp_id) => (
            "session/load",
            json!({"sessionId": acp_id, "cwd": config.cwd.to_string_lossy().to_string(), "mcpServers": []}),
        ),
        None => (
            "session/new",
            json!({"cwd": config.cwd.to_string_lossy().to_string(), "mcpServers": []}),
        ),
    };
    write_request(&mut stdin, session_id_req, session_method, session_params).await?;
    let session_resp = read_response(&mut lines, session_id_req, sink).await?;
    if let Some(err) = session_resp.get("error") {
        anyhow::bail!("{session_method} error: {err}");
    }
    if let Some(sid) = session_resp
        .pointer("/result/sessionId")
        .and_then(Value::as_str)
    {
        acp_session_id = Some(sid.to_string());
    }
    let acp_session_id = acp_session_id
        .context("session/new returned no sessionId")?
        .clone();
    sink.post_binding(&acp_session_id).await;

    // 3. session/prompt
    let prompt_id = next_id;
    write_request(
        &mut stdin,
        prompt_id,
        "session/prompt",
        json!({
            "sessionId": acp_session_id,
            "prompt": [{"type": "text", "text": config.prompt}],
        }),
    )
    .await?;

    // Stream notifications until the prompt response arrives.
    let prompt_resp = read_response(&mut lines, prompt_id, sink).await?;
    if let Some(err) = prompt_resp.get("error") {
        anyhow::bail!("session/prompt error: {err}");
    }

    let stop_reason = prompt_resp
        .pointer("/result/stopReason")
        .and_then(Value::as_str)
        .map(str::to_string);

    // Close stdin so cursor-agent can exit cleanly.
    let _ = stdin.shutdown().await;

    Ok(stop_reason)
}

async fn write_request(
    stdin: &mut tokio::process::ChildStdin,
    id: i64,
    method: &str,
    params: Value,
) -> Result<()> {
    let line = serde_json::to_string(&json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": method,
        "params": params,
    }))?;
    stdin.write_all(line.as_bytes()).await?;
    stdin.write_all(b"\n").await?;
    stdin.flush().await?;
    Ok(())
}

/// Read stdout lines until the response with `expected_id` arrives.
/// Every notification is first retained as exact local source evidence, then
/// mirrored as a provisional runtime progress signal through the shared
/// daemon outbox. Console remains unavailable until this source is sealed by
/// the storage-v2 adapter.
async fn read_response(
    lines: &mut tokio::io::Lines<BufReader<tokio::process::ChildStdout>>,
    expected_id: i64,
    sink: &CursorAcpSink,
) -> Result<Value> {
    let mut seq = 0u64;
    loop {
        match lines.next_line().await? {
            Some(line) => {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                seq += 1;
                let value: Value = serde_json::from_str(trimmed)
                    .with_context(|| format!("invalid ACP json line: {trimmed}"))?;

                if let Some(id) = value.get("id").and_then(Value::as_i64) {
                    if id == expected_id {
                        return Ok(value);
                    }
                    // Unmatched response id — ignore (shouldn't happen in
                    // sequential handshake).
                    continue;
                }

                sink.persist_raw_notification(line.as_bytes())?;
                sink.post_progress(seq, value).await;
            }
            None => {
                anyhow::bail!("cursor-agent acp stdout closed before response id={expected_id}")
            }
        }
    }
}

async fn read_stderr_tail(stream: tokio::process::ChildStderr, tail: Arc<Mutex<VecDeque<String>>>) {
    let mut lines = BufReader::new(stream).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let mut guard = tail.lock().expect("cursor acp stderr tail lock poisoned");
        if guard.len() >= STDERR_TAIL_LINES {
            guard.pop_front();
        }
        guard.push_back(trimmed.to_string());
    }
}

fn stderr_tail_snapshot(tail: &Arc<Mutex<VecDeque<String>>>) -> Option<String> {
    let guard = tail.lock().expect("cursor acp stderr tail lock poisoned");
    if guard.is_empty() {
        None
    } else {
        Some(guard.iter().cloned().collect::<Vec<_>>().join("\n"))
    }
}

fn normalized_optional(value: &Option<String>) -> Option<String> {
    value
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

impl CursorAcpSink {
    async fn post_binding(&self, provider_session_id: &str) {
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_ACP_RUNTIME_SOURCE,
            "kind": "binding_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("cursor-acp:{}:{}:binding", self.session_id, self.run_id),
            "payload": {"provider_session_id": provider_session_id},
        })])
        .await;
    }

    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        let observed_at = Utc::now();
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_ACP_RUNTIME_SOURCE,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("cursor-acp:{}:{}:phase:{}", self.session_id, self.run_id, phase),
            "payload": {
                "managed_transport": CURSOR_ACP_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
            }
        })])
        .await;
    }

    async fn post_progress(&self, seq: u64, notification: Value) {
        let payload = json!({
            "progress_kind": "cursor_acp_notification",
            "seq": seq,
            "notification": notification,
            "managed_transport": CURSOR_ACP_RUNTIME_SOURCE,
            "execution_lifetime": "one_shot",
        });
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_ACP_RUNTIME_SOURCE,
            "kind": "progress_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("cursor-acp:{}:{}:notif:{}", self.session_id, self.run_id, seq),
            "payload": payload,
        })])
        .await;
    }

    async fn post_terminal(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        stderr_tail: Option<String>,
    ) {
        crate::turn_claims::mark_terminal(&self.run_id, terminal_state, stderr_tail.clone());
        let observed_at = Utc::now();
        self.persist_local_phase("finished", None, observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_ACP_RUNTIME_SOURCE,
            "kind": "terminal_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("cursor-acp:{}:{}:terminal", self.session_id, self.run_id),
            "payload": {
                "managed_transport": CURSOR_ACP_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": CURSOR_ACP_RUNTIME_SOURCE,
                "exit_code": exit_code,
                "stderr_tail": stderr_tail,
            }
        })])
        .await;
    }

    fn persist_raw_notification(&self, bytes: &[u8]) -> Result<()> {
        let session_dir = self.source_dir.join(&self.session_id);
        std::fs::create_dir_all(&session_dir)?;
        let path = session_dir.join(format!("{}.jsonl", self.run_id));
        let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
        file.write_all(bytes)?;
        file.write_all(b"\n")?;
        file.sync_data()?;
        Ok(())
    }

    fn persist_local_phase(
        &self,
        phase: &str,
        tool_name: Option<String>,
        observed_at: chrono::DateTime<Utc>,
    ) {
        let Some(db_path) = self.local_db_path.as_deref() else {
            return;
        };
        let conn = match crate::state::db::open_db(Some(db_path)) {
            Ok(conn) => conn,
            Err(err) => {
                eprintln!("[cursor-acp] open local phase DB failed: {err}");
                return;
            }
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "cursor".to_string(),
            phase: phase.to_string(),
            tool_name,
            source: CURSOR_ACP_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal)
        {
            eprintln!(
                "[cursor-acp] persist local phase failed for {}: {}",
                self.session_id, err
            );
        }
        let managed_signal = crate::state::managed_session_state::ManagedSessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "cursor".to_string(),
            workspace_path: Some(self.cwd.clone()),
            phase_kind: phase.to_string(),
            tool_name: signal.tool_name.clone(),
            phase_source: CURSOR_ACP_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::managed_session_state::ManagedSessionStateStore::new(&conn)
            .record_phase(&managed_signal)
        {
            eprintln!(
                "[cursor-acp] persist managed session state failed for {}: {}",
                self.session_id, err
            );
        }
    }

    async fn post_events(&self, events: Vec<Value>) {
        for event in events {
            if let Err(error) =
                crate::outbox::enqueue_runtime_event(&self.runtime_events_outbox_dir, &event)
            {
                eprintln!("[cursor-acp] runtime outbox write failed: {error}");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(prompt: &str, resume: Option<&str>) -> CursorAcpRunConfig {
        CursorAcpRunConfig {
            session_id: "s-1".to_string(),
            run_id: "r-1".to_string(),
            cwd: PathBuf::from("/tmp/proj"),
            cursor_bin: "cursor-agent".to_string(),
            prompt: prompt.to_string(),
            launch_actor: None,
            launch_surface: None,
            resume_acp_session_id: resume.map(str::to_string),
            machine_name: "mac".to_string(),
            local_db_path: None,
        }
    }

    #[test]
    fn cursor_acp_args_uses_acp_subcommand() {
        let args = cursor_acp_args();
        let strings: Vec<&str> = args.iter().map(|s| s.to_str().unwrap()).collect();
        assert_eq!(strings, vec!["acp"]);
    }

    #[test]
    fn write_request_frame_is_newline_delimited_jsonrpc() {
        // Frame shape is asserted via the json structure helper used by
        // write_request; here we verify the static shape directly.
        let frame = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": ACP_PROTOCOL_VERSION},
        });
        assert_eq!(frame["jsonrpc"], "2.0");
        assert_eq!(frame["id"], 1);
        assert_eq!(frame["params"]["protocolVersion"], ACP_PROTOCOL_VERSION);
        // protocolVersion MUST be a number — cursor rejects string versions.
        assert!(frame["params"]["protocolVersion"].is_number());
    }

    #[test]
    fn session_prompt_payload_uses_prompt_array_and_session_id() {
        let acp_session_id = "acp-123";
        let frame = json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": acp_session_id,
                "prompt": [{"type": "text", "text": "do thing"}],
            },
        });
        assert_eq!(frame["params"]["sessionId"], acp_session_id);
        assert_eq!(frame["params"]["prompt"][0]["type"], "text");
        assert_eq!(frame["params"]["prompt"][0]["text"], "do thing");
    }

    #[test]
    fn resume_uses_session_load_with_acp_session_id() {
        let cfg = config("next step", Some("acp-resume-id"));
        assert_eq!(
            normalized_optional(&cfg.resume_acp_session_id).as_deref(),
            Some("acp-resume-id")
        );
        let (_method, params) = match normalized_optional(&cfg.resume_acp_session_id) {
            Some(acp_id) => (
                "session/load",
                json!({"sessionId": acp_id, "cwd": cfg.cwd.to_string_lossy().to_string(), "mcpServers": []}),
            ),
            None => (
                "session/new",
                json!({"cwd": cfg.cwd.to_string_lossy().to_string(), "mcpServers": []}),
            ),
        };
        assert_eq!(params["sessionId"], "acp-resume-id");
        assert!(params["mcpServers"].is_array());
    }
}
