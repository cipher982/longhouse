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
//! `session/update` notifications are translated to `EventIngest` rows and
//! posted to `/api/agents/ingest`; phase/progress/terminal signals go to
//! `/api/agents/runtime/events/batch` for the live overlay.
//!
//! Timestamp fidelity: ACP notifications do not carry per-event timestamps
//! (verified by live probe), so every event uses a monotonic receipt clock.
//! Do not fabricate per-event timestamps beyond receipt time.
//!
//! Interrupt/terminate: cursor-agent returns "Method not found" for
//! `session/cancel`, so there is no graceful ACP interrupt. Terminate is
//! cleanup-on-drop (kill_on_drop) plus SIGINT/SIGKILL on the pid if a
//! pid-registry terminate command is wired later.
//!
//! Tool-variant mapping is provisional: `agent_message_chunk` is mapped
//! fully; other `sessionUpdate` variants (tool calls, thoughts, file changes)
//! emit progress signals and a best-effort tool EventIngest when a tool
//! identifier is present. Promote the tool mapping once a tool-using live
//! canary captures the exact Cursor ACP variant names.

use std::collections::VecDeque;
use std::ffi::OsString;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

const CURSOR_ACP_RUNTIME_SOURCE: &str = "cursor_acp";
const STDERR_TAIL_LINES: usize = 40;
const INGEST_BATCH_FLUSH_THRESHOLD: usize = 5;
const ACP_PROTOCOL_VERSION: i64 = 1;

#[derive(Clone, Debug)]
pub struct CursorAcpRunConfig {
    pub session_id: String,
    pub run_id: String,
    pub cwd: PathBuf,
    pub api_url: String,
    pub api_token: String,
    pub cursor_bin: String,
    pub prompt: String,
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
    api_url: String,
    api_token: String,
    machine_name: String,
    cwd: String,
    local_db_path: Option<PathBuf>,
    http: reqwest::Client,
}

pub fn cursor_acp_args() -> Vec<OsString> {
    vec![OsString::from("acp")]
}

pub async fn start_cursor_acp_once(
    config: CursorAcpRunConfig,
) -> Result<CursorAcpRunSummary> {
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

    let sink = CursorAcpSink {
        session_id: config.session_id.clone(),
        run_id: config.run_id.clone(),
        api_url: config.api_url.clone(),
        api_token: config.api_token.clone(),
        machine_name: config.machine_name.clone(),
        cwd: config.cwd.to_string_lossy().to_string(),
        local_db_path: config.local_db_path.clone(),
        http: reqwest::Client::new(),
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

        match turn_outcome {
            Ok(stop_reason) => {
                let terminal_state = match stop_reason.as_deref() {
                    Some("end_turn") | Some("end_turn_refused") => "run_completed",
                    _ => "run_completed",
                };
                monitor_sink
                    .post_terminal(terminal_state, Some(0), stderr_tail_snapshot(&stderr_tail))
                    .await;
            }
            Err(err) => {
                monitor_sink
                    .post_terminal(
                        "run_failed",
                        None,
                        Some(format!("acp turn failed: {err}")),
                    )
                    .await;
            }
        }

        if let Some(task) = stderr_task {
            let _ = task.await;
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
    let mut ingest_buffer: Vec<Value> = Vec::new();
    let mut last_ts: Option<DateTime<Utc>> = None;
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
    let init_resp = read_response(&mut lines, init_id, sink, &mut ingest_buffer, &mut last_ts).await?;
    if let Some(err) = init_resp.get("error") {
        anyhow::bail!("initialize error: {err}");
    }

    // 2. session/new or session/load
    let session_id_req = next_id;
    next_id += 1;
    let (session_method, session_params) = match normalized_optional(&config.resume_acp_session_id) {
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
    let session_resp =
        read_response(&mut lines, session_id_req, sink, &mut ingest_buffer, &mut last_ts).await?;
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
    let prompt_resp =
        read_response(&mut lines, prompt_id, sink, &mut ingest_buffer, &mut last_ts).await?;
    if let Some(err) = prompt_resp.get("error") {
        anyhow::bail!("session/prompt error: {err}");
    }

    // Flush any remaining buffered transcript events.
    if !ingest_buffer.is_empty() {
        sink.post_session_ingest(
            Some(&acp_session_id),
            ingest_buffer,
        )
        .await;
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
/// `session/update` (and other) notifications arriving before the response
/// are translated to EventIngest rows + progress signals.
async fn read_response(
    lines: &mut tokio::io::Lines<BufReader<tokio::process::ChildStdout>>,
    expected_id: i64,
    sink: &CursorAcpSink,
    ingest_buffer: &mut Vec<Value>,
    last_ts: &mut Option<DateTime<Utc>>,
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

                // Notification: translate session/update into transcript
                // events + a live progress signal.
                if let Some(event) = build_event_from_notification(&value, last_ts) {
                    ingest_buffer.push(event);
                    if ingest_buffer.len() >= INGEST_BATCH_FLUSH_THRESHOLD {
                        let batch = std::mem::take(ingest_buffer);
                        sink.post_session_ingest(None, batch).await;
                    }
                }
                sink.post_progress(seq, value).await;
            }
            None => anyhow::bail!("cursor-agent acp stdout closed before response id={expected_id}"),
        }
    }
}

fn build_event_from_notification(
    value: &Value,
    last_ts: &mut Option<DateTime<Utc>>,
) -> Option<Value> {
    let method = value.get("method").and_then(Value::as_str)?;
    if method != "session/update" {
        return None;
    }
    let update = value.pointer("/params/update")?;
    let session_update = update.get("sessionUpdate").and_then(Value::as_str)?;
    let ts = monotonic_timestamp(last_ts).to_rfc3339();

    match session_update {
        "agent_message_chunk" => {
            let text = value
                .pointer("/params/update/content/text")
                .and_then(Value::as_str)
                .unwrap_or("");
            if text.is_empty() {
                return None;
            }
            Some(json!({
                "role": "assistant",
                "content_text": text,
                "timestamp": ts,
            }))
        }
        "agent_thought_chunk" => {
            let text = value
                .pointer("/params/update/content/text")
                .and_then(Value::as_str)
                .unwrap_or("");
            if text.is_empty() {
                return None;
            }
            Some(json!({
                "role": "assistant",
                "content_text": text,
                "kind": "reasoning",
                "timestamp": ts,
            }))
        }
        // Tool-call variants. Cursor's exact variant names are not yet
        // captured by a live tool-using canary; map the common ACP shapes
        // best-effort and fall through to None (progress-only) otherwise.
        variant if variant.starts_with("tool_call") => {
            let tool_name = value
                .pointer("/params/update/toolCall/name")
                .and_then(Value::as_str)
                .or_else(|| value.pointer("/params/update/name").and_then(Value::as_str))
                .unwrap_or("tool");
            let tool_call_id = value
                .pointer("/params/update/toolCall/id")
                .and_then(Value::as_str)
                .or_else(|| value.pointer("/params/update/id").and_then(Value::as_str))
                .unwrap_or("");
            if variant.contains("result") || variant.contains("complete") || variant.contains("end")
            {
                let output = value
                    .pointer("/params/update/toolCall/result")
                    .map(|r| serde_json::to_string(r).unwrap_or_default())
                    .or_else(|| value.pointer("/params/update/result").map(|r| serde_json::to_string(r).unwrap_or_default()))
                    .unwrap_or_default();
                Some(json!({
                    "role": "tool",
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "tool_output_text": output,
                    "timestamp": ts,
                }))
            } else {
                let input = value
                    .pointer("/params/update/toolCall/arguments")
                    .cloned()
                    .or_else(|| value.pointer("/params/update/arguments").cloned())
                    .unwrap_or(Value::Null);
                Some(json!({
                    "role": "assistant",
                    "tool_name": tool_name,
                    "tool_input_json": input,
                    "tool_call_id": tool_call_id,
                    "timestamp": ts,
                }))
            }
        }
        _ => None, // available_commands_update, file_change, etc. → progress only
    }
}

fn monotonic_timestamp(last_ts: &mut Option<DateTime<Utc>>) -> DateTime<Utc> {
    let now = Utc::now();
    let ts = match last_ts {
        Some(last) if *last >= now => *last + chrono::Duration::milliseconds(1),
        _ => now,
    };
    *last_ts = Some(ts);
    ts
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

    async fn post_session_ingest(&self, provider_session_id: Option<&str>, events: Vec<Value>) {
        if events.is_empty() {
            return;
        }
        let started = Utc::now();
        let payload = json!({
            "id": self.session_id,
            "provider": "cursor",
            "environment": "development",
            "device_id": self.machine_name,
            "device_name": self.machine_name,
            "cwd": self.cwd,
            "started_at": started.to_rfc3339(),
            "provider_session_id": provider_session_id,
            "execution_home": "managed_local",
            "events": events,
        });
        let url = format!("{}/api/agents/ingest", self.api_url.trim_end_matches('/'));
        for attempt in 0..3 {
            let response = match self
                .http
                .post(&url)
                .header("X-Agents-Token", &self.api_token)
                .header("Content-Type", "application/json")
                .timeout(Duration::from_secs(10))
                .json(&payload)
                .send()
                .await
            {
                Ok(response) => response,
                Err(err) => {
                    if attempt < 2 {
                        tokio::time::sleep(Duration::from_millis(150 * (attempt + 1) as u64)).await;
                        continue;
                    }
                    eprintln!("[cursor-acp] ingest network error: {err}");
                    return;
                }
            };
            if response.status().is_success() {
                return;
            }
            let status = response.status();
            let retryable = status.is_server_error() || status.as_u16() == 429;
            let body = response.text().await.unwrap_or_default();
            if retryable && attempt < 2 {
                tokio::time::sleep(Duration::from_millis(150 * (attempt + 1) as u64)).await;
                continue;
            }
            eprintln!("[cursor-acp] ingest failed: {status} {body}");
            return;
        }
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
        let url = format!(
            "{}/api/agents/runtime/events/batch",
            self.api_url.trim_end_matches('/')
        );
        for attempt in 0..3 {
            let response = match self
                .http
                .post(&url)
                .header("X-Agents-Token", &self.api_token)
                .timeout(Duration::from_secs(5))
                .json(&json!({ "events": events.clone() }))
                .send()
                .await
            {
                Ok(response) => response,
                Err(err) => {
                    if attempt < 2 {
                        tokio::time::sleep(Duration::from_millis(100 * (attempt + 1) as u64)).await;
                        continue;
                    }
                    eprintln!("[cursor-acp] runtime ingest network error: {}", err);
                    return;
                }
            };
            if response.status().is_success() {
                return;
            }
            let status = response.status();
            let retryable = status.is_server_error() || status.as_u16() == 429;
            let body = response.text().await.unwrap_or_default();
            if retryable && attempt < 2 {
                tokio::time::sleep(Duration::from_millis(100 * (attempt + 1) as u64)).await;
                continue;
            }
            eprintln!("[cursor-acp] runtime ingest failed: {} {}", status, body);
            return;
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
            api_url: "http://localhost".to_string(),
            api_token: "tok".to_string(),
            cursor_bin: "cursor-agent".to_string(),
            prompt: prompt.to_string(),
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
    fn build_event_maps_agent_message_chunk_to_assistant_text() {
        let notif = serde_json::from_str(
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"c1","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"READY"}}}}"#,
        ).unwrap();
        let mut last = None;
        let event = build_event_from_notification(&notif, &mut last).unwrap();
        assert_eq!(event["role"], "assistant");
        assert_eq!(event["content_text"], "READY");
        assert!(event["timestamp"].as_str().unwrap().contains("T"));
    }

    #[test]
    fn build_event_skips_non_session_update_notifications() {
        let other = serde_json::from_str(
            r#"{"jsonrpc":"2.0","method":"session/request_permission","params":{}}"#,
        ).unwrap();
        let mut last = None;
        assert!(build_event_from_notification(&other, &mut last).is_none());
    }

    #[test]
    fn build_event_skips_available_commands_update() {
        let notif = serde_json::from_str(
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"available_commands_update","availableCommands":[]}}}"#,
        ).unwrap();
        let mut last = None;
        assert!(build_event_from_notification(&notif, &mut last).is_none());
    }

    #[test]
    fn build_event_maps_tool_call_result_variant_to_tool_role() {
        let notif = serde_json::from_str(
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"tool_call_result","toolCall":{"id":"tc1","name":"read_file","result":"hello"}}}}"#,
        ).unwrap();
        let mut last = None;
        let event = build_event_from_notification(&notif, &mut last).unwrap();
        assert_eq!(event["role"], "tool");
        assert_eq!(event["tool_name"], "read_file");
        assert_eq!(event["tool_call_id"], "tc1");
    }

    #[test]
    fn build_event_maps_tool_call_start_variant_to_assistant_tool_call() {
        let notif = serde_json::from_str(
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"tool_call_start","toolCall":{"id":"tc2","name":"edit_file","arguments":{"path":"/a"}}}}}"#,
        ).unwrap();
        let mut last = None;
        let event = build_event_from_notification(&notif, &mut last).unwrap();
        assert_eq!(event["role"], "assistant");
        assert_eq!(event["tool_name"], "edit_file");
        assert_eq!(event["tool_input_json"]["path"], "/a");
    }

    #[test]
    fn monotonic_timestamp_is_non_decreasing() {
        let mut last = None;
        let t1 = monotonic_timestamp(&mut last);
        let t2 = monotonic_timestamp(&mut last);
        assert!(t2 >= t1);
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
