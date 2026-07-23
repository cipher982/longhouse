//! Native owner for a managed OpenCode localhost server bridge.
//!
//! This intentionally owns only the stock `opencode serve` lifecycle and its
//! private state. Runtime plugins and answerable permission pauses stay out of
//! this first native slice until their reply path is native too.

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use reqwest::Url;
use serde::Serialize;
use serde_json::json;
use uuid::Uuid;

const USERNAME: &str = "opencode";
const READY_TIMEOUT: Duration = Duration::from_secs(20);

pub struct StartConfig {
    pub session_id: String,
    pub run_id: String,
    pub cwd: PathBuf,
    pub display_name: Option<String>,
    pub opencode_bin: Option<String>,
    pub claude_dir: Option<PathBuf>,
    pub launch_mode: String,
}

#[derive(Serialize)]
pub struct StartResult {
    pub session_id: String,
    pub provider_session_id: String,
    pub server_url: String,
}

pub fn start(config: StartConfig) -> Result<StartResult> {
    let session_id = normalize_uuid(&config.session_id, "session_id")?;
    let run_id = normalize_uuid(&config.run_id, "run_id")?;
    let cwd = fs::canonicalize(&config.cwd).with_context(|| {
        format!(
            "OpenCode workspace is unavailable: {}",
            config.cwd.display()
        )
    })?;
    if !cwd.is_dir() {
        bail!("OpenCode workspace is not a directory: {}", cwd.display());
    }
    if !matches!(
        config.launch_mode.as_str(),
        "attached_tui" | "detached" | "keep_server"
    ) {
        bail!("unsupported OpenCode launch mode");
    }
    let state_dir = state_dir(config.claude_dir.as_deref())?;
    let state_path = state_dir.join(format!("{session_id}.json"));
    if state_path.exists() {
        let stale_pid = fs::read(&state_path)
            .ok()
            .and_then(|raw| serde_json::from_slice::<serde_json::Value>(&raw).ok())
            .and_then(|value| value.get("pid").and_then(serde_json::Value::as_u64))
            .and_then(|pid| u32::try_from(pid).ok());
        if stale_pid.is_none_or(|pid| !pid_alive(pid)) {
            fs::remove_file(&state_path)?;
        } else {
            bail!("managed OpenCode bridge already has a live state for {session_id}; stop it before starting again");
        }
    }
    fs::create_dir_all(state_dir.join("logs"))?;
    let binary = resolve_binary(config.opencode_bin)?;
    let password = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let log_path = state_dir.join("logs").join(format!("{session_id}.log"));
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)?;
    let mut command = Command::new(&binary);
    command
        .args([
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
            "--print-logs",
        ])
        .current_dir(&cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log))
        .env("LONGHOUSE_MANAGED_SESSION_ID", &session_id)
        .env("OPENCODE_SERVER_USERNAME", USERNAME)
        .env("OPENCODE_SERVER_PASSWORD", &password);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn().context("start stock OpenCode server")?;
    let pid = child.id();
    let started = Instant::now();
    let server_url = loop {
        if let Some(url) = read_listen_url(&log_path)? {
            break url;
        }
        if let Some(status) = child.try_wait()? {
            bail!(
                "OpenCode server exited before readiness ({status}): {}",
                tail(&log_path)?
            );
        }
        if started.elapsed() > READY_TIMEOUT {
            let _ = stop_pid(pid);
            bail!(
                "timed out waiting for OpenCode server readiness: {}",
                tail(&log_path)?
            );
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    let result = (|| -> Result<StartResult> {
        let runtime = tokio::runtime::Runtime::new()?;
        let provider_session_id = runtime.block_on(create_session(
            &server_url,
            &password,
            &cwd,
            config.display_name.as_deref(),
        ))?;
        let (process_start_time, process_command) = process_identity(pid).unwrap_or_default();
        let now = chrono::Utc::now().to_rfc3339();
        let payload = json!({
            "schema_version": 1, "session_id": session_id, "run_id": run_id,
            "connection_id": Uuid::new_v4().to_string(), "lease_generation": Uuid::new_v4().to_string(),
            "provider_session_id": provider_session_id, "server_url": server_url,
            "pid": pid, "cwd": cwd, "username": USERNAME, "password": password,
            "log_path": log_path, "started_at": now, "updated_at": now,
            "process_start_time": process_start_time, "process_command": process_command,
            "launch_mode": config.launch_mode, "owner_wrapper_pid": 0, "owner_wrapper_start_time": ""
        });
        write_private_json(&state_path, &payload)?;
        Ok(StartResult {
            session_id,
            provider_session_id,
            server_url,
        })
    })();
    if result.is_err() {
        let _ = stop_pid(pid);
    }
    result
}

pub fn stop(
    session_id: &str,
    claude_dir: Option<PathBuf>,
) -> Result<crate::opencode_control::OpenCodeStopResult> {
    let state_dir = claude_dir.map(|path| path.join("managed-local/opencode-server"));
    crate::opencode_control::stop_server_bridge_at(session_id, state_dir.as_deref())
}

pub fn attach(
    session_id: &str,
    opencode_bin: Option<String>,
    claude_dir: Option<PathBuf>,
) -> Result<i32> {
    let state_dir = claude_dir.map(|path| path.join("managed-local/opencode-server"));
    let state = crate::opencode_control::read_for_bridge(session_id, state_dir.as_deref())?;
    let binary = resolve_binary(opencode_bin)?;
    let runtime = tokio::runtime::Runtime::new()?;
    runtime.block_on(assert_health(&state.server_url, &state.password))?;
    let status = Command::new(binary)
        .args([
            "attach",
            &state.server_url,
            "--session",
            &state.provider_session_id,
        ])
        .current_dir(&state.cwd)
        .env("OPENCODE_SERVER_USERNAME", &state.username)
        .env("OPENCODE_SERVER_PASSWORD", &state.password)
        .status()
        .context("attach stock OpenCode TUI")?;
    Ok(status.code().unwrap_or(1))
}

fn state_dir(claude_dir: Option<&Path>) -> Result<PathBuf> {
    match claude_dir {
        Some(path) => Ok(path.join("managed-local/opencode-server")),
        None => crate::managed_opencode_scan::default_opencode_server_state_dir()
            .context("could not resolve OpenCode provider state directory"),
    }
}

fn normalize_uuid(value: &str, name: &str) -> Result<String> {
    Uuid::parse_str(value.trim())
        .with_context(|| format!("{name} must be a UUID"))
        .map(|id| id.to_string())
}

fn resolve_binary(explicit: Option<String>) -> Result<String> {
    let candidate = explicit
        .or_else(|| std::env::var("LONGHOUSE_OPENCODE_BIN").ok())
        .unwrap_or_else(|| "opencode".into());
    if candidate.contains('/') {
        let path = PathBuf::from(&candidate);
        if path.is_file() {
            return Ok(path.to_string_lossy().into_owned());
        }
        bail!("OpenCode executable is not a file: {candidate}");
    }
    let path = std::env::var_os("PATH")
        .into_iter()
        .flat_map(|value| std::env::split_paths(&value).collect::<Vec<_>>())
        .map(|dir| dir.join(&candidate))
        .find(|path| path.is_file())
        .context("OpenCode executable not found; install `opencode` or pass --opencode-bin")?;
    Ok(path.to_string_lossy().into_owned())
}

fn read_listen_url(path: &Path) -> Result<Option<String>> {
    let text = fs::read_to_string(path).unwrap_or_default();
    Ok(text
        .lines()
        .rev()
        .find_map(|line| line.split("opencode server listening on ").nth(1))
        .map(str::trim)
        .filter(|url| url.starts_with("http://127.0.0.1:"))
        .map(str::to_owned))
}

fn tail(path: &Path) -> Result<String> {
    let mut text = String::new();
    OpenOptions::new()
        .read(true)
        .open(path)?
        .read_to_string(&mut text)?;
    Ok(text
        .chars()
        .rev()
        .take(2000)
        .collect::<String>()
        .chars()
        .rev()
        .collect())
}

async fn create_session(
    server_url: &str,
    password: &str,
    cwd: &Path,
    title: Option<&str>,
) -> Result<String> {
    let base = Url::parse(server_url)?;
    if base.host_str() != Some("127.0.0.1") {
        bail!("OpenCode server must listen on localhost");
    }
    assert_health(server_url, password).await?;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?;
    let mut session_url = Url::parse(&format!("{server_url}/session"))?;
    session_url
        .query_pairs_mut()
        .append_pair("directory", &cwd.to_string_lossy());
    let response = client.post(session_url)
        .basic_auth(USERNAME, Some(password)).json(&json!({"title": title.unwrap_or_else(|| cwd.file_name().and_then(|v| v.to_str()).unwrap_or("Longhouse"))})).send().await?;
    if !response.status().is_success() {
        bail!("OpenCode session creation failed ({})", response.status());
    }
    response
        .json::<serde_json::Value>()
        .await?
        .get("id")
        .and_then(serde_json::Value::as_str)
        .filter(|id| !id.trim().is_empty())
        .map(str::to_owned)
        .context("OpenCode session creation returned no id")
}

async fn assert_health(server_url: &str, password: &str) -> Result<()> {
    let health = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?
        .get(format!("{server_url}/global/health"))
        .basic_auth(USERNAME, Some(password))
        .send()
        .await?;
    if !health.status().is_success() {
        bail!("OpenCode server health check failed ({})", health.status());
    }
    if health
        .json::<serde_json::Value>()
        .await?
        .get("healthy")
        .and_then(serde_json::Value::as_bool)
        != Some(true)
    {
        bail!("OpenCode server health check did not report healthy");
    }
    Ok(())
}

fn write_private_json(path: &Path, payload: &serde_json::Value) -> Result<()> {
    let parent = path.parent().context("OpenCode state has no parent")?;
    fs::create_dir_all(parent)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
    }
    let temporary = path.with_extension(format!("json.tmp.{}", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(format!("{}\n", serde_json::to_string_pretty(payload)?).as_bytes())?;
    file.sync_all()?;
    drop(file);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))?;
    }
    fs::rename(temporary, path)?;
    Ok(())
}

fn process_identity(pid: u32) -> Option<(String, String)> {
    let output = Command::new("ps")
        .args(["-o", "lstart=,command=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    let line = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    (line.len() > 24).then(|| (line[..24].trim().to_owned(), line[24..].trim().to_owned()))
}

fn stop_pid(pid: u32) -> Result<()> {
    #[cfg(unix)]
    {
        if unsafe { libc::killpg(pid as i32, libc::SIGTERM) } == 0 {
            return Ok(());
        }
    }
    if unsafe { libc::kill(pid as i32, libc::SIGTERM) } == 0 {
        Ok(())
    } else {
        Ok(())
    }
}

fn pid_alive(pid: u32) -> bool {
    pid > 0 && unsafe { libc::kill(pid as i32, 0) == 0 }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn bridge_state_is_private() {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("private/state.json");
        write_private_json(&path, &json!({"password":"secret"})).unwrap();
        assert_eq!(
            fs::metadata(path.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[test]
    fn rejects_non_local_listen_urls() {
        assert!(read_listen_url(Path::new("/definitely/not/there"))
            .unwrap()
            .is_none());
    }
}
