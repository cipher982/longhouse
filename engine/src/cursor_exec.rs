//! Cursor one-shot (Console mode) exec runtime.
//!
//! Parallels `codex_exec.rs`: the engine spawns `cursor-agent --print
//! --output-format stream-json` headless on behalf of a Console-mode launch
//! (POST /api/sessions/launch with execution_lifetime=one_shot), returns the
//! pid/argv upstream immediately so the Runtime Host can flip the connection
//! to `attached`, then streams stdout in the background.
//!
//! Unlike codex (whose durable transcript ships from codex's own session
//! file), cursor-agent's `--print stream-json` stdout IS the complete
//! transcript with real per-event timestamps on tool calls. So this module
//! parses the stream into `EventIngest` rows and posts a `SessionIngest` to
//! `/api/agents/ingest` with the pre-allocated managed session id, plus
//! runtime phase/progress/terminal signals to `/api/agents/runtime/events/batch`
//! for the live overlay.
//!
//! Timestamp fidelity: `tool_call` events carry a real `timestamp_ms` /
//! `startedAtMs` / `completedAtMs`; `assistant` / `user` / `system` / `result`
//! events do not, so those use a monotonic receipt clock anchored to the last
//! real tool timestamp. Do not fabricate per-event timestamps beyond this.

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
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

const CURSOR_EXEC_RUNTIME_SOURCE: &str = "cursor_exec";
const STDERR_TAIL_LINES: usize = 40;
const INGEST_BATCH_FLUSH_THRESHOLD: usize = 5;

#[derive(Clone, Debug)]
pub struct CursorExecRunConfig {
    pub session_id: String,
    pub run_id: String,
    pub cwd: PathBuf,
    pub api_url: String,
    pub api_token: String,
    pub cursor_bin: String,
    pub prompt: String,
    pub resume_chat_id: Option<String>,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct CursorExecRunSummary {
    pub session_id: String,
    pub run_id: String,
    pub pid: Option<u32>,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct CursorExecSink {
    session_id: String,
    run_id: String,
    api_url: String,
    api_token: String,
    machine_name: String,
    cwd: String,
    local_db_path: Option<PathBuf>,
    http: reqwest::Client,
}

pub fn cursor_exec_args(config: &CursorExecRunConfig) -> Vec<OsString> {
    let mut args = vec![
        OsString::from("--print"),
        OsString::from("--output-format"),
        OsString::from("stream-json"),
        OsString::from("--yolo"),
        OsString::from("--trust"),
        OsString::from("--workspace"),
        config.cwd.as_os_str().to_os_string(),
    ];
    if let Some(chat_id) = normalized_optional(&config.resume_chat_id) {
        args.push(OsString::from("--resume"));
        args.push(OsString::from(chat_id));
    }
    args.push(OsString::from(config.prompt.clone()));
    args
}

pub async fn start_cursor_exec_once(config: CursorExecRunConfig) -> Result<CursorExecRunSummary> {
    let args = cursor_exec_args(&config);
    let argv = std::iter::once(OsString::from(config.cursor_bin.clone()))
        .chain(args.iter().cloned())
        .map(|item| item.to_string_lossy().to_string())
        .collect::<Vec<_>>();

    let mut command = Command::new(&config.cursor_bin);
    command
        .args(&args)
        .env("LONGHOUSE_MANAGED_SESSION_ID", &config.session_id)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
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
        .with_context(|| format!("spawning `{}` exec", config.cursor_bin))?;
    let pid = child.id();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let sink = CursorExecSink {
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

    tokio::spawn(async move {
        monitor_sink.post_phase("thinking", None).await;

        let stdout_task = stdout.map(|stream| {
            let sink = monitor_sink.clone();
            tokio::spawn(async move {
                read_stdout_stream_json(stream, sink).await;
            })
        });
        let stderr_task = stderr.map(|stream| {
            tokio::spawn(async move {
                read_stderr_tail(stream, monitor_tail).await;
            })
        });

        let status = child.wait().await;
        if let Some(task) = stdout_task {
            let _ = task.await;
        }
        if let Some(task) = stderr_task {
            let _ = task.await;
        }

        match status {
            Ok(status) => {
                let exit_code = status.code();
                let terminal_state = if exit_code == Some(0) {
                    "run_completed"
                } else {
                    "run_failed"
                };
                monitor_sink
                    .post_terminal(
                        terminal_state,
                        exit_code,
                        stderr_tail_snapshot(&stderr_tail),
                    )
                    .await;
            }
            Err(err) => {
                monitor_sink
                    .post_terminal("run_failed", None, Some(format!("wait failed: {err}")))
                    .await;
            }
        }
    });

    Ok(CursorExecRunSummary {
        session_id: config.session_id,
        run_id: config.run_id,
        pid,
        argv,
    })
}

async fn read_stdout_stream_json(
    stream: tokio::process::ChildStdout,
    sink: CursorExecSink,
) {
    let mut lines = BufReader::new(stream).lines();
    let mut provider_session_id: Option<String> = None;
    let mut started_at: Option<DateTime<Utc>> = None;
    let mut ended_at: Option<DateTime<Utc>> = None;
    let mut ingest_buffer: Vec<Value> = Vec::new();
    let mut last_real_ts: Option<DateTime<Utc>> = None;
    let mut last_seq = 0u64;

    while let Ok(Some(line)) = lines.next_line().await {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        last_seq += 1;
        let seq = last_seq;
        let parsed = serde_json::from_str::<Value>(trimmed);

        if let Ok(ref value) = parsed {
            observe_stream_anchors(
                value,
                &mut provider_session_id,
                &mut started_at,
                &mut ended_at,
                &mut last_real_ts,
            );
        }

        if let Ok(ref value) = parsed {
            if let Some(event) = build_event_ingest(value, &mut last_real_ts) {
                ingest_buffer.push(event);
                if ingest_buffer.len() >= INGEST_BATCH_FLUSH_THRESHOLD {
                    let batch = std::mem::take(&mut ingest_buffer);
                    sink.post_session_ingest(
                        provider_session_id.as_deref(),
                        started_at,
                        ended_at,
                        batch,
                    )
                    .await;
                }
            }
        }

        let progress_payload = parsed
            .as_ref()
            .map(|value| {
                json!({"progress_kind": "cursor_stream_json", "seq": seq, "event": value})
            })
            .unwrap_or_else(|_| {
                json!({"progress_kind": "cursor_stream_stdout", "seq": seq, "line": trimmed})
            });
        sink.post_progress(seq, progress_payload).await;
    }

    // Flush any remaining buffered transcript events at end of stream.
    if !ingest_buffer.is_empty() {
        sink.post_session_ingest(
            provider_session_id.as_deref(),
            started_at,
            ended_at,
            ingest_buffer,
        )
        .await;
    }
}

fn observe_stream_anchors(
    value: &Value,
    provider_session_id: &mut Option<String>,
    started_at: &mut Option<DateTime<Utc>>,
    ended_at: &mut Option<DateTime<Utc>>,
    last_real_ts: &mut Option<DateTime<Utc>>,
) {
    let event_type = value.get("type").and_then(Value::as_str).unwrap_or("");
    if event_type == "system" {
        if provider_session_id.is_none() {
            if let Some(sid) = value.get("session_id").and_then(Value::as_str) {
                let sid = sid.trim();
                if !sid.is_empty() {
                    *provider_session_id = Some(sid.to_string());
                }
            }
        }
    }
    if let Some(ts) = real_timestamp_ms(value) {
        if started_at.is_none() {
            *started_at = Some(ts);
        }
        *ended_at = Some(ts);
        *last_real_ts = Some(ts);
    } else if let Some(now) = monotonic_now(last_real_ts) {
        if started_at.is_none() {
            *started_at = Some(now);
        }
    }
    if event_type == "result" {
        if let Some(dur) = value.get("duration_ms").and_then(Value::as_u64) {
            if let Some(start) = started_at {
                let end = *start + chrono::Duration::milliseconds(dur as i64);
                *ended_at = Some(end);
            }
        }
    }
}

fn build_event_ingest(value: &Value, last_real_ts: &mut Option<DateTime<Utc>>) -> Option<Value> {
    let event_type = value.get("type").and_then(Value::as_str)?;
    match event_type {
        "user" => {
            let text = message_text(value);
            let ts = event_timestamp(value, last_real_ts);
            Some(json!({
                "role": "user",
                "content_text": text,
                "timestamp": ts.to_rfc3339(),
            }))
        }
        "assistant" => {
            let text = message_text(value);
            let ts = event_timestamp(value, last_real_ts);
            Some(json!({
                "role": "assistant",
                "content_text": text,
                "timestamp": ts.to_rfc3339(),
            }))
        }
        "tool_call" => {
            let subtype = value.get("subtype").and_then(Value::as_str).unwrap_or("");
            let tool_call_obj = value.get("tool_call").and_then(Value::as_object)?;
            // The tool name is the single key under `tool_call` (readToolCall, etc.).
            let tool_name = tool_call_obj.keys().next()?;
            let inner = tool_call_obj.get(tool_name)?;
            let tool_call_id = value
                .get("toolCallId")
                .and_then(Value::as_str)
                .unwrap_or("");
            let ts = event_timestamp(value, last_real_ts);
            if subtype == "completed" {
                let result = inner.get("result");
                let tool_output_text = result
                    .map(|r| stringify_tool_result(r));
                Some(json!({
                    "role": "tool",
                    "tool_name": tool_name,
                    "tool_output_text": tool_output_text,
                    "tool_call_id": tool_call_id,
                    "timestamp": ts.to_rfc3339(),
                }))
            } else {
                // `started` (or any non-completed): assistant-initiated tool call with input.
                let args = inner.get("args").cloned().unwrap_or(Value::Null);
                Some(json!({
                    "role": "assistant",
                    "tool_name": tool_name,
                    "tool_input_json": args,
                    "tool_call_id": tool_call_id,
                    "timestamp": ts.to_rfc3339(),
                }))
            }
        }
        _ => None,
    }
}

fn message_text(value: &Value) -> Option<String> {
    let content = value
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(Value::as_array)?;
    let mut parts: Vec<String> = Vec::new();
    for item in content {
        if item.get("type").and_then(Value::as_str) == Some("text") {
            if let Some(text) = item.get("text").and_then(Value::as_str) {
                parts.push(text.to_string());
            }
        }
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("\n"))
    }
}

fn real_timestamp_ms(value: &Value) -> Option<DateTime<Utc>> {
    if let Some(ms) = value.get("timestamp_ms").and_then(Value::as_u64) {
        return ms_to_datetime(ms);
    }
    if let Some(ms_str) = value.get("startedAtMs").and_then(Value::as_str) {
        if let Ok(ms) = ms_str.parse::<u64>() {
            return ms_to_datetime(ms);
        }
    }
    None
}

fn ms_to_datetime(ms: u64) -> Option<DateTime<Utc>> {
    DateTime::from_timestamp((ms / 1000) as i64, ((ms % 1000) * 1_000_000) as u32)
}

fn monotonic_now(last_real_ts: &Option<DateTime<Utc>>) -> Option<DateTime<Utc>> {
    let now = Utc::now();
    match last_real_ts {
        Some(last) if *last > now => Some(*last + chrono::Duration::milliseconds(1)),
        _ => Some(now),
    }
}

fn event_timestamp(value: &Value, last_real_ts: &mut Option<DateTime<Utc>>) -> DateTime<Utc> {
    if let Some(ts) = real_timestamp_ms(value) {
        *last_real_ts = Some(ts);
        ts
    } else {
        let ts = monotonic_now(last_real_ts).unwrap_or_else(Utc::now);
        ts
    }
}

fn stringify_tool_result(result: &Value) -> String {
    // Prefer the human-readable content when present (readToolCall.result.success.content),
    // else fall back to a compact JSON serialization.
    if let Some(content) = result
        .get("success")
        .and_then(|s| s.get("content"))
        .and_then(Value::as_str)
    {
        return content.to_string();
    }
    serde_json::to_string(result).unwrap_or_else(|_| "{}".to_string())
}

async fn read_stderr_tail(stream: tokio::process::ChildStderr, tail: Arc<Mutex<VecDeque<String>>>) {
    let mut lines = BufReader::new(stream).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let mut guard = tail.lock().expect("cursor exec stderr tail lock poisoned");
        if guard.len() >= STDERR_TAIL_LINES {
            guard.pop_front();
        }
        guard.push_back(trimmed.to_string());
    }
}

fn stderr_tail_snapshot(tail: &Arc<Mutex<VecDeque<String>>>) -> Option<String> {
    let guard = tail.lock().expect("cursor exec stderr tail lock poisoned");
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

impl CursorExecSink {
    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        let observed_at = Utc::now();
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_EXEC_RUNTIME_SOURCE,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("cursor-exec:{}:{}:phase:{}", self.session_id, self.run_id, phase),
            "payload": {
                "managed_transport": CURSOR_EXEC_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
            }
        })])
        .await;
    }

    async fn post_progress(&self, seq: u64, mut payload: Value) {
        if let Some(obj) = payload.as_object_mut() {
            obj.insert(
                "managed_transport".to_string(),
                Value::String(CURSOR_EXEC_RUNTIME_SOURCE.to_string()),
            );
            obj.insert(
                "execution_lifetime".to_string(),
                Value::String("one_shot".to_string()),
            );
        }
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_EXEC_RUNTIME_SOURCE,
            "kind": "progress_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("cursor-exec:{}:{}:stdout:{seq}", self.session_id, self.run_id),
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
            "source": CURSOR_EXEC_RUNTIME_SOURCE,
            "kind": "terminal_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("cursor-exec:{}:{}:terminal", self.session_id, self.run_id),
            "payload": {
                "managed_transport": CURSOR_EXEC_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": CURSOR_EXEC_RUNTIME_SOURCE,
                "exit_code": exit_code,
                "stderr_tail": stderr_tail,
            }
        })])
        .await;
    }

    async fn post_session_ingest(
        &self,
        provider_session_id: Option<&str>,
        started_at: Option<DateTime<Utc>>,
        ended_at: Option<DateTime<Utc>>,
        events: Vec<Value>,
    ) {
        if events.is_empty() {
            return;
        }
        let started = started_at.unwrap_or_else(Utc::now);
        let payload = json!({
            "id": self.session_id,
            "provider": "cursor",
            "environment": "development",
            "device_id": self.machine_name,
            "device_name": self.machine_name,
            "cwd": self.cwd,
            "started_at": started.to_rfc3339(),
            "ended_at": ended_at,
            "provider_session_id": provider_session_id,
            "execution_home": "managed_local",
            "events": events,
        });
        let url = format!(
            "{}/api/agents/ingest",
            self.api_url.trim_end_matches('/')
        );
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
                    eprintln!("[cursor-exec] ingest network error: {err}");
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
            eprintln!("[cursor-exec] ingest failed: {status} {body}");
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
                eprintln!("[cursor-exec] open local phase DB failed: {err}");
                return;
            }
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "cursor".to_string(),
            phase: phase.to_string(),
            tool_name,
            source: CURSOR_EXEC_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal)
        {
            eprintln!(
                "[cursor-exec] persist local phase failed for {}: {err}",
                self.session_id
            );
        }
        let managed_signal = crate::state::managed_session_state::ManagedSessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "cursor".to_string(),
            workspace_path: Some(self.cwd.clone()),
            phase_kind: phase.to_string(),
            tool_name: signal.tool_name.clone(),
            phase_source: CURSOR_EXEC_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::managed_session_state::ManagedSessionStateStore::new(&conn)
            .record_phase(&managed_signal)
        {
            eprintln!(
                "[cursor-exec] persist managed session state failed for {}: {err}",
                self.session_id
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
                    eprintln!("[cursor-exec] runtime ingest network error: {err}");
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
            eprintln!("[cursor-exec] runtime ingest failed: {status} {body}");
            return;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(prompt: &str, resume: Option<&str>) -> CursorExecRunConfig {
        CursorExecRunConfig {
            session_id: "s-1".to_string(),
            run_id: "r-1".to_string(),
            cwd: PathBuf::from("/tmp/proj"),
            api_url: "http://localhost".to_string(),
            api_token: "tok".to_string(),
            cursor_bin: "cursor-agent".to_string(),
            prompt: prompt.to_string(),
            resume_chat_id: resume.map(str::to_string),
            machine_name: "mac".to_string(),
            local_db_path: None,
        }
    }

    #[test]
    fn cursor_exec_args_builds_headless_stream_json_argv() {
        let args = cursor_exec_args(&config("do thing", None));
        let strings: Vec<&str> = args.iter().map(|s| s.to_str().unwrap()).collect();
        assert_eq!(
            strings,
            vec![
                "--print",
                "--output-format",
                "stream-json",
                "--yolo",
                "--trust",
                "--workspace",
                "/tmp/proj",
                "do thing",
            ]
        );
    }

    #[test]
    fn cursor_exec_args_includes_resume_chat_id_before_prompt() {
        let args = cursor_exec_args(&config("next step", Some("chat-abc")));
        let strings: Vec<&str> = args.iter().map(|s| s.to_str().unwrap()).collect();
        let resume_idx = strings.iter().position(|s| *s == "--resume").unwrap();
        assert_eq!(strings[resume_idx + 1], "chat-abc");
        assert_eq!(*strings.last().unwrap(), "next step");
    }

    #[test]
    fn build_event_ingest_maps_user_and_assistant_text() {
        let user = serde_json::from_str(r#"{"type":"user","message":{"role":"user","content":[{"type":"text","text":"hi"}]},"session_id":"c1"}"#).unwrap();
        let mut last = None;
        let user_ev = build_event_ingest(&user, &mut last).unwrap();
        assert_eq!(user_ev["role"], "user");
        assert_eq!(user_ev["content_text"], "hi");

        let asst = serde_json::from_str(r#"{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hello"}]},"session_id":"c1"}"#).unwrap();
        let asst_ev = build_event_ingest(&asst, &mut last).unwrap();
        assert_eq!(asst_ev["role"], "assistant");
        assert_eq!(asst_ev["content_text"], "hello");
    }

    #[test]
    fn build_event_ingest_pairs_tool_call_started_and_completed() {
        let started = serde_json::from_str(
            r#"{"type":"tool_call","subtype":"started","call_id":"cid-1","tool_call":{"readToolCall":{"args":{"path":"/a.txt"}}},"toolCallId":"cid-1","startedAtMs":"1782847776287","timestamp_ms":1782847776312,"session_id":"c1"}"#,
        ).unwrap();
        let mut last = None;
        let started_ev = build_event_ingest(&started, &mut last).unwrap();
        assert_eq!(started_ev["role"], "assistant");
        assert_eq!(started_ev["tool_name"], "readToolCall");
        assert_eq!(started_ev["tool_call_id"], "cid-1");
        assert_eq!(started_ev["tool_input_json"]["path"], "/a.txt");
        // Real timestamp_ms is used for tool calls.
        assert!(started_ev["timestamp"].as_str().unwrap().contains("T"));

        let completed = serde_json::from_str(
            r#"{"type":"tool_call","subtype":"completed","call_id":"cid-1","tool_call":{"readToolCall":{"args":{"path":"/a.txt"},"result":{"success":{"content":"hello world\n"}}}},"toolCallId":"cid-1","startedAtMs":"1782847776287","completedAtMs":"1782847776330","timestamp_ms":1782847776360,"session_id":"c1"}"#,
        ).unwrap();
        let completed_ev = build_event_ingest(&completed, &mut last).unwrap();
        assert_eq!(completed_ev["role"], "tool");
        assert_eq!(completed_ev["tool_name"], "readToolCall");
        assert_eq!(completed_ev["tool_call_id"], "cid-1");
        assert_eq!(completed_ev["tool_output_text"], "hello world\n");
    }

    #[test]
    fn real_timestamp_ms_reads_timestamp_ms_and_started_at_ms() {
        let with_ms: Value = serde_json::from_str(r#"{"timestamp_ms":1782847776312}"#).unwrap();
        assert!(real_timestamp_ms(&with_ms).is_some());

        let with_started: Value =
            serde_json::from_str(r#"{"startedAtMs":"1782847776287"}"#).unwrap();
        assert!(real_timestamp_ms(&with_started).is_some());

        let none: Value = serde_json::from_str(r#"{"type":"assistant"}"#).unwrap();
        assert!(real_timestamp_ms(&none).is_none());
    }

    #[test]
    fn event_timestamp_falls_back_to_monotonic_receipt_clock() {
        let asst: Value =
            serde_json::from_str(r#"{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"x"}]}}"#).unwrap();
        let mut last = None;
        let ts1 = event_timestamp(&asst, &mut last);
        let ts2 = event_timestamp(&asst, &mut last);
        // No real timestamps → receipt clock, monotonic non-decreasing.
        assert!(ts2 >= ts1);
    }

    #[test]
    fn observe_stream_anchors_captures_provider_session_id_from_system_init() {
        let init: Value = serde_json::from_str(
            r#"{"type":"system","subtype":"init","session_id":"cursor-chat-123","cwd":"/tmp","model":"m"}"#,
        ).unwrap();
        let mut pid = None;
        let mut started = None;
        let mut ended = None;
        let mut last = None;
        observe_stream_anchors(&init, &mut pid, &mut started, &mut ended, &mut last);
        assert_eq!(pid.as_deref(), Some("cursor-chat-123"));
    }
}
