use std::collections::VecDeque;
use std::ffi::OsString;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result};
use chrono::Utc;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

const STDERR_TAIL_LINES: usize = 40;

pub struct CliTurnConfig {
    pub session_id: String,
    pub run_id: String,
    pub provider: String,
    pub transport: String,
    pub cwd: PathBuf,
    pub binary: String,
    pub args: Vec<OsString>,
    pub api_url: String,
    pub api_token: String,
    pub machine_name: String,
    pub launch_actor: Option<String>,
    pub launch_surface: Option<String>,
}

pub struct CliTurnSummary {
    pub session_id: String,
    pub run_id: String,
    pub pid: Option<u32>,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct RuntimeSink {
    session_id: String,
    run_id: String,
    provider: String,
    transport: String,
    machine_name: String,
    api_url: String,
    api_token: String,
    http: reqwest::Client,
}

pub async fn start_cli_turn(config: CliTurnConfig) -> Result<CliTurnSummary> {
    let argv = std::iter::once(OsString::from(config.binary.clone()))
        .chain(config.args.iter().cloned())
        .map(|item| item.to_string_lossy().to_string())
        .collect::<Vec<_>>();
    let mut command = Command::new(&config.binary);
    command
        .args(&config.args)
        .env("LONGHOUSE_MANAGED_SESSION_ID", &config.session_id)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(value) = normalized_optional(&config.launch_actor) {
        command.env("LONGHOUSE_LAUNCH_ACTOR", value);
    }
    if let Some(value) = normalized_optional(&config.launch_surface) {
        command.env("LONGHOUSE_LAUNCH_SURFACE", value);
    }
    #[cfg(unix)]
    unsafe {
        command.pre_exec(|| {
            if libc::setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command
        .spawn()
        .with_context(|| format!("spawning Console {} adapter", config.provider))?;
    let pid = child.id();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let sink = RuntimeSink {
        session_id: config.session_id.clone(),
        run_id: config.run_id.clone(),
        provider: config.provider,
        transport: config.transport,
        machine_name: config.machine_name,
        api_url: config.api_url,
        api_token: config.api_token,
        http: reqwest::Client::new(),
    };
    let stderr_tail = Arc::new(Mutex::new(VecDeque::with_capacity(STDERR_TAIL_LINES)));
    tokio::spawn(async move {
        sink.post_phase("thinking").await;
        let stdout_task = stdout.map(|stream| {
            let sink = sink.clone();
            tokio::spawn(async move { read_json_lines(stream, sink).await })
        });
        let stderr_task = stderr.map(|stream| {
            let tail = stderr_tail.clone();
            tokio::spawn(async move { read_stderr(stream, tail).await })
        });
        let status = child.wait().await;
        if let Some(task) = stdout_task {
            let _ = task.await;
        }
        if let Some(task) = stderr_task {
            let _ = task.await;
        }
        let (terminal, exit_code, error) = match status {
            Ok(status) if status.success() => ("run_completed", status.code(), None),
            Ok(status) => ("run_failed", status.code(), stderr_snapshot(&stderr_tail)),
            Err(error) => ("run_failed", None, Some(error.to_string())),
        };
        sink.post_terminal(terminal, exit_code, error).await;
    });
    Ok(CliTurnSummary {
        session_id: config.session_id,
        run_id: config.run_id,
        pid,
        argv,
    })
}

async fn read_json_lines(stream: tokio::process::ChildStdout, sink: RuntimeSink) {
    let mut lines = BufReader::new(stream).lines();
    let mut seq = 0u64;
    let mut binding_sent = false;
    while let Ok(Some(line)) = lines.next_line().await {
        if line.trim().is_empty() {
            continue;
        }
        seq += 1;
        let payload =
            serde_json::from_str::<Value>(&line).unwrap_or_else(|_| json!({"text": line}));
        if !binding_sent {
            if let Some(provider_session_id) = find_provider_session_id(&payload) {
                sink.post_binding(provider_session_id).await;
                binding_sent = true;
            }
        }
        sink.post_progress(seq, payload).await;
    }
}

fn find_provider_session_id(value: &Value) -> Option<&str> {
    let object = value.as_object()?;
    for key in ["session_id", "sessionId", "sessionID"] {
        if let Some(found) = object.get(key).and_then(Value::as_str) {
            return Some(found);
        }
    }
    object.values().find_map(find_provider_session_id)
}

async fn read_stderr(stream: tokio::process::ChildStderr, tail: Arc<Mutex<VecDeque<String>>>) {
    let mut lines = BufReader::new(stream).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let mut guard = tail.lock().expect("cli turn stderr lock poisoned");
        if guard.len() == STDERR_TAIL_LINES {
            guard.pop_front();
        }
        guard.push_back(line);
    }
}

fn stderr_snapshot(tail: &Arc<Mutex<VecDeque<String>>>) -> Option<String> {
    let guard = tail.lock().expect("cli turn stderr lock poisoned");
    (!guard.is_empty()).then(|| guard.iter().cloned().collect::<Vec<_>>().join("\n"))
}

fn normalized_optional(value: &Option<String>) -> Option<String> {
    value
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

impl RuntimeSink {
    async fn post_phase(&self, phase: &str) {
        self.post(vec![json!({
            "runtime_key": format!("{}:{}", self.provider, self.session_id),
            "session_id": self.session_id, "run_id": self.run_id, "provider": self.provider,
            "device_id": self.machine_name, "source": self.transport, "kind": "phase_signal",
            "phase": phase, "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("{}:{}:{}:phase:{}", self.transport, self.session_id, self.run_id, phase),
            "payload": {"managed_transport": self.transport},
        })]).await;
    }

    async fn post_binding(&self, provider_session_id: &str) {
        self.post(vec![json!({
            "runtime_key": format!("{}:{}", self.provider, self.session_id),
            "session_id": self.session_id, "run_id": self.run_id, "provider": self.provider,
            "device_id": self.machine_name, "source": self.transport, "kind": "binding_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("{}:{}:{}:binding", self.transport, self.session_id, self.run_id),
            "payload": {"provider_session_id": provider_session_id},
        })])
        .await;
    }

    async fn post_progress(&self, seq: u64, payload: Value) {
        self.post(vec![json!({
            "runtime_key": format!("{}:{}", self.provider, self.session_id),
            "session_id": self.session_id, "run_id": self.run_id, "provider": self.provider,
            "device_id": self.machine_name, "source": self.transport, "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("{}:{}:{}:stdout:{}", self.transport, self.session_id, self.run_id, seq),
            "payload": payload,
        })]).await;
    }

    async fn post_terminal(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        error: Option<String>,
    ) {
        crate::turn_claims::mark_terminal(&self.run_id, terminal_state, error.clone());
        self.post(vec![json!({
            "runtime_key": format!("{}:{}", self.provider, self.session_id),
            "session_id": self.session_id, "run_id": self.run_id, "provider": self.provider,
            "device_id": self.machine_name, "source": self.transport, "kind": "terminal_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("{}:{}:{}:terminal", self.transport, self.session_id, self.run_id),
            "payload": {"terminal_state": terminal_state, "exit_code": exit_code, "stderr_tail": error},
        })]).await;
    }

    async fn post(&self, events: Vec<Value>) {
        let url = format!(
            "{}/api/agents/runtime/events/batch",
            self.api_url.trim_end_matches('/')
        );
        for attempt in 0..3 {
            if self
                .http
                .post(&url)
                .header("X-Agents-Token", &self.api_token)
                .json(&json!({"events": events}))
                .send()
                .await
                .map(|response| response.status().is_success())
                .unwrap_or(false)
            {
                return;
            }
            tokio::time::sleep(std::time::Duration::from_millis(200 * (attempt + 1))).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_nested_provider_session_identity() {
        assert_eq!(
            find_provider_session_id(&json!({"part": {"sessionID": "ses-1"}})),
            Some("ses-1")
        );
    }
}
