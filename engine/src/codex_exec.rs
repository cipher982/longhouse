use std::collections::VecDeque;
use std::ffi::OsString;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::Utc;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

const CODEX_DISABLE_UPDATE_CHECK_CONFIG: &str = "check_for_update_on_startup=false";
const CODEX_EXEC_RUNTIME_SOURCE: &str = "codex_exec";
const STDERR_TAIL_LINES: usize = 40;

#[derive(Clone, Debug)]
pub struct CodexExecRunConfig {
    pub session_id: String,
    pub run_id: String,
    pub cwd: PathBuf,
    pub api_url: String,
    pub api_token: String,
    pub codex_bin: String,
    pub approval_policy: Option<String>,
    pub sandbox: Option<String>,
    pub prompt: String,
    pub launch_actor: Option<String>,
    pub launch_surface: Option<String>,
    pub resume_thread_id: Option<String>,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct CodexExecRunSummary {
    pub session_id: String,
    pub run_id: String,
    pub pid: Option<u32>,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct CodexExecRuntimeSink {
    session_id: String,
    run_id: String,
    api_url: String,
    api_token: String,
    machine_name: String,
    cwd: String,
    local_db_path: Option<PathBuf>,
    http: reqwest::Client,
}

pub fn codex_exec_args(config: &CodexExecRunConfig) -> Vec<OsString> {
    let mut args = vec![
        OsString::from("exec"),
        OsString::from("--json"),
        OsString::from("-c"),
        OsString::from(CODEX_DISABLE_UPDATE_CHECK_CONFIG),
    ];
    if let Some(approval_policy) = normalized_optional(&config.approval_policy) {
        args.push(OsString::from("-c"));
        args.push(OsString::from(format!(
            "approval_policy={}",
            toml_quote_string(&approval_policy)
        )));
    }
    if let Some(sandbox) = normalized_optional(&config.sandbox) {
        args.push(OsString::from("-s"));
        args.push(OsString::from(sandbox));
    }
    args.push(OsString::from("-C"));
    args.push(config.cwd.as_os_str().to_os_string());
    args.push(OsString::from("--skip-git-repo-check"));
    if let Some(thread_id) = normalized_optional(&config.resume_thread_id) {
        args.push(OsString::from("resume"));
        args.push(OsString::from(thread_id));
    }
    args.push(OsString::from(config.prompt.clone()));
    args
}

fn toml_quote_string(value: &str) -> String {
    let escaped = value.replace('\\', "\\\\").replace('"', "\\\"");
    format!("\"{escaped}\"")
}

pub async fn start_codex_exec_once(config: CodexExecRunConfig) -> Result<CodexExecRunSummary> {
    let args = codex_exec_args(&config);
    let argv = std::iter::once(OsString::from(config.codex_bin.clone()))
        .chain(args.iter().cloned())
        .map(|item| item.to_string_lossy().to_string())
        .collect::<Vec<_>>();

    let mut command = Command::new(&config.codex_bin);
    command
        .args(&args)
        .env("LONGHOUSE_MANAGED_SESSION_ID", &config.session_id)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
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
        .with_context(|| format!("spawning `{}` exec", config.codex_bin))?;
    let pid = child.id();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let sink = CodexExecRuntimeSink {
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
                read_stdout_jsonl(stream, sink).await;
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

    Ok(CodexExecRunSummary {
        session_id: config.session_id,
        run_id: config.run_id,
        pid,
        argv,
    })
}

async fn read_stdout_jsonl(stream: tokio::process::ChildStdout, sink: CodexExecRuntimeSink) {
    let mut lines = BufReader::new(stream).lines();
    let mut seq = 0u64;
    while let Ok(Some(line)) = lines.next_line().await {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        seq += 1;
        let payload = serde_json::from_str::<Value>(trimmed)
            .map(|value| json!({"progress_kind": "codex_exec_jsonl", "seq": seq, "event": value}))
            .unwrap_or_else(
                |_| json!({"progress_kind": "codex_exec_stdout", "seq": seq, "line": trimmed}),
            );
        sink.post_progress(seq, payload).await;
    }
}

async fn read_stderr_tail(stream: tokio::process::ChildStderr, tail: Arc<Mutex<VecDeque<String>>>) {
    let mut lines = BufReader::new(stream).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let mut guard = tail.lock().expect("codex exec stderr tail lock poisoned");
        if guard.len() >= STDERR_TAIL_LINES {
            guard.pop_front();
        }
        guard.push_back(trimmed.to_string());
    }
}

fn stderr_tail_snapshot(tail: &Arc<Mutex<VecDeque<String>>>) -> Option<String> {
    let guard = tail.lock().expect("codex exec stderr tail lock poisoned");
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

impl CodexExecRuntimeSink {
    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        let observed_at = Utc::now();
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("codex-exec:{}:{}:phase:{}", self.session_id, self.run_id, phase),
            "payload": {
                "managed_transport": CODEX_EXEC_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
            }
        })])
        .await;
    }

    async fn post_progress(&self, seq: u64, mut payload: Value) {
        if let Some(obj) = payload.as_object_mut() {
            obj.insert(
                "managed_transport".to_string(),
                Value::String(CODEX_EXEC_RUNTIME_SOURCE.to_string()),
            );
            obj.insert(
                "execution_lifetime".to_string(),
                Value::String("one_shot".to_string()),
            );
        }
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "progress_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("codex-exec:{}:{}:stdout:{seq}", self.session_id, self.run_id),
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
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "terminal_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("codex-exec:{}:{}:terminal", self.session_id, self.run_id),
            "payload": {
                "managed_transport": CODEX_EXEC_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": CODEX_EXEC_RUNTIME_SOURCE,
                "exit_code": exit_code,
                "stderr_tail": stderr_tail,
            }
        })])
        .await;
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
                eprintln!("[codex-exec] open local phase DB failed: {err}");
                return;
            }
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "codex".to_string(),
            phase: phase.to_string(),
            tool_name,
            source: CODEX_EXEC_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal)
        {
            eprintln!(
                "[codex-exec] persist local phase failed for {}: {err}",
                self.session_id
            );
        }
        let managed_signal = crate::state::managed_session_state::ManagedSessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "codex".to_string(),
            workspace_path: Some(self.cwd.clone()),
            phase_kind: phase.to_string(),
            tool_name: signal.tool_name.clone(),
            phase_source: CODEX_EXEC_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::managed_session_state::ManagedSessionStateStore::new(&conn)
            .record_phase(&managed_signal)
        {
            eprintln!(
                "[codex-exec] persist managed session state failed for {}: {err}",
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
                    eprintln!("[codex-exec] runtime ingest network error: {err}");
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
            eprintln!("[codex-exec] runtime ingest failed: {status} {body}");
            return;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> CodexExecRunConfig {
        CodexExecRunConfig {
            session_id: "11111111-1111-4111-8111-111111111111".to_string(),
            run_id: "22222222-2222-4222-8222-222222222222".to_string(),
            cwd: PathBuf::from("/tmp/project"),
            api_url: "http://localhost:8080".to_string(),
            api_token: "token".to_string(),
            codex_bin: "codex".to_string(),
            approval_policy: Some("never".to_string()),
            sandbox: Some("workspace-write".to_string()),
            prompt: "Do one bounded turn".to_string(),
            launch_actor: None,
            launch_surface: None,
            resume_thread_id: None,
            machine_name: "cinder".to_string(),
            local_db_path: None,
        }
    }

    #[test]
    fn codex_exec_args_are_noninteractive_and_bounded() {
        let args = codex_exec_args(&config())
            .into_iter()
            .map(|value| value.to_string_lossy().to_string())
            .collect::<Vec<_>>();

        assert_eq!(
            args,
            vec![
                "exec",
                "--json",
                "-c",
                "check_for_update_on_startup=false",
                "-c",
                "approval_policy=\"never\"",
                "-s",
                "workspace-write",
                "-C",
                "/tmp/project",
                "--skip-git-repo-check",
                "Do one bounded turn",
            ]
        );
    }

    #[test]
    fn codex_exec_args_resume_previous_thread_and_still_exit() {
        let mut config = config();
        config.resume_thread_id = Some("33333333-3333-4333-8333-333333333333".to_string());
        config.prompt = "Continue with one bounded follow-up".to_string();

        let args = codex_exec_args(&config)
            .into_iter()
            .map(|value| value.to_string_lossy().to_string())
            .collect::<Vec<_>>();

        assert_eq!(
            args,
            vec![
                "exec",
                "--json",
                "-c",
                "check_for_update_on_startup=false",
                "-c",
                "approval_policy=\"never\"",
                "-s",
                "workspace-write",
                "-C",
                "/tmp/project",
                "--skip-git-repo-check",
                "resume",
                "33333333-3333-4333-8333-333333333333",
                "Continue with one bounded follow-up",
            ]
        );
    }

    #[test]
    fn terminal_payload_uses_run_terminal_state() {
        let session_id = "session-1".to_string();
        let run_id = "run-1".to_string();
        let machine_name = "cinder".to_string();
        let event = json!({
            "runtime_key": format!("codex:{}", session_id),
            "session_id": session_id,
            "run_id": run_id,
            "provider": "codex",
            "device_id": machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "terminal_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": "test",
            "payload": {
                "managed_transport": CODEX_EXEC_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
                "terminal_state": "run_completed",
                "terminal_reason": "run_completed",
                "terminal_source": CODEX_EXEC_RUNTIME_SOURCE,
                "exit_code": 0,
            }
        });

        assert_eq!(event["payload"]["terminal_state"], "run_completed");
        assert_eq!(event["run_id"], "run-1");
    }

    #[test]
    fn progress_payload_marks_codex_exec_transport() {
        let payload = json!({"progress_kind": "codex_exec_jsonl"});
        let mut obj = payload.as_object().unwrap().clone();
        obj.insert(
            "managed_transport".to_string(),
            Value::String(CODEX_EXEC_RUNTIME_SOURCE.to_string()),
        );
        obj.insert(
            "execution_lifetime".to_string(),
            Value::String("one_shot".to_string()),
        );
        assert_eq!(obj["managed_transport"], "codex_exec");
        assert_eq!(obj["execution_lifetime"], "one_shot");
    }
}
