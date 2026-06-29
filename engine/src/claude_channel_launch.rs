use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::json;
use serde_json::Map;
use serde_json::Value;
use thiserror::Error;
use tokio::time::sleep;
use uuid::Uuid;

const CLAUDE_CHANNEL_SERVER_NAME: &str = "longhouse-channel";
const CLAUDE_CHANNEL_DEVELOPMENT_FLAG: &str = "--dangerously-load-development-channels";
const MANAGED_SESSION_ENV: &str = "LONGHOUSE_MANAGED_SESSION_ID";
const CLAUDE_REMOTE_LAUNCH_LOG_DIR: &str = "claude-channel-launch";
const CLAUDE_LIFECYCLE_HOOK_SCRIPT: &str = "longhouse-hook.sh";
const CLAUDE_PERMISSION_GATE_SCRIPT: &str = "longhouse-permission-gate.py";
const DEFAULT_POLL_INTERVAL: Duration = Duration::from_millis(100);

const CLAUDE_LIFECYCLE_HOOK_EVENTS: &[&str] = &[
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Notification",
];

const STANDARD_PATH_PREFIXES: &[&str] = &[
    "$HOME/.local/bin",
    "$HOME/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/home/linuxbrew/.linuxbrew/sbin",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ClaudePermissionMode {
    Bypass,
    RemoteApprove,
}

impl ClaudePermissionMode {
    fn permission_hook_enabled(self) -> &'static str {
        match self {
            Self::Bypass => "0",
            Self::RemoteApprove => "1",
        }
    }

    fn uses_skip_permissions(self) -> bool {
        matches!(self, Self::Bypass)
    }
}

#[derive(Clone, Debug)]
pub struct ClaudeChannelLaunchConfig {
    pub session_id: String,
    pub provider_session_id: String,
    pub cwd: PathBuf,
    pub api_url: String,
    pub api_token: String,
    pub hook_token: Option<String>,
    pub resume: bool,
    pub wait_ready: Duration,
    pub claude_bin: String,
    pub permission_mode: ClaudePermissionMode,
    pub state_root: Option<PathBuf>,
    pub claude_dir: Option<PathBuf>,
    pub log_dir: Option<PathBuf>,
    pub script_bin: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaudeChannelLaunchResult {
    pub session_id: String,
    pub provider_session_id: String,
    pub pid: u32,
    pub log_path: PathBuf,
    pub channel_state: Value,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaudeLaunchCommandPlan {
    pub program: String,
    pub args: Vec<String>,
    pub shell_script: String,
    pub hook_token_env: String,
}

#[derive(Debug, Error)]
pub enum ClaudeChannelLaunchError {
    #[error("Claude launch config is invalid: {0}")]
    InvalidConfig(String),
    #[error("failed to prepare Claude channel config: {0}")]
    ConfigFailed(String),
    #[error("failed to start Claude launch process: {0}")]
    SpawnFailed(String),
    #[error("Claude channel state did not become ready: {0}")]
    StateNotReady(String),
}

pub async fn launch_detached(
    config: ClaudeChannelLaunchConfig,
) -> Result<ClaudeChannelLaunchResult, ClaudeChannelLaunchError> {
    validate_launch_config(&config)?;
    ensure_claude_launch_prereqs(&config)?;
    let plan = build_launch_command_plan(&config)?;
    let log_path = launch_log_path(&config)?;
    if let Some(parent) = log_path.parent() {
        std::fs::create_dir_all(parent).map_err(|err| {
            ClaudeChannelLaunchError::SpawnFailed(format!(
                "creating Claude launch log directory {}: {err}",
                parent.display()
            ))
        })?;
    }

    let mut command = build_launch_command(&config, &plan);
    let mut child = command
        .spawn()
        .map_err(|err| ClaudeChannelLaunchError::SpawnFailed(err.to_string()))?;
    match wait_for_channel_state(
        &config.session_id,
        config.state_root.as_deref(),
        config.wait_ready,
    )
    .await
    {
        Ok(channel_state) => {
            let pid = child.id();
            reap_child_on_exit(child);
            Ok(ClaudeChannelLaunchResult {
                session_id: config.session_id,
                provider_session_id: config.provider_session_id,
                pid,
                log_path,
                channel_state,
            })
        }
        Err(err) => {
            terminate_child_group(&mut child);
            Err(err)
        }
    }
}

pub fn build_launch_command_plan(
    config: &ClaudeChannelLaunchConfig,
) -> Result<ClaudeLaunchCommandPlan, ClaudeChannelLaunchError> {
    validate_launch_config(config)?;
    let log_path = launch_log_path(config)?;
    let shell_script = build_claude_shell_script(config);
    let args = build_script_args(&shell_script, &log_path);
    let hook_token_env = config
        .hook_token
        .as_deref()
        .unwrap_or(&config.api_token)
        .to_string();
    Ok(ClaudeLaunchCommandPlan {
        program: config.script_bin.clone(),
        args,
        shell_script,
        hook_token_env,
    })
}

fn build_launch_command(
    config: &ClaudeChannelLaunchConfig,
    plan: &ClaudeLaunchCommandPlan,
) -> Command {
    let mut command = Command::new(&plan.program);
    command
        .args(&plan.args)
        .current_dir(&config.cwd)
        .env("LONGHOUSE_HOOK_TOKEN", &plan.hook_token_env)
        .env_remove("CLAUDE_CONFIG_DIR")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(unix)]
    unsafe {
        use std::os::unix::process::CommandExt;
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    command
}

fn validate_launch_config(
    config: &ClaudeChannelLaunchConfig,
) -> Result<(), ClaudeChannelLaunchError> {
    if config.session_id.trim().is_empty() {
        return Err(ClaudeChannelLaunchError::InvalidConfig(
            "session_id must not be empty".to_string(),
        ));
    }
    if Uuid::parse_str(config.session_id.trim()).is_err() {
        return Err(ClaudeChannelLaunchError::InvalidConfig(
            "session_id must be a UUID".to_string(),
        ));
    }
    if config.provider_session_id.trim().is_empty() {
        return Err(ClaudeChannelLaunchError::InvalidConfig(
            "provider_session_id must not be empty".to_string(),
        ));
    }
    if !config.cwd.is_absolute() {
        return Err(ClaudeChannelLaunchError::InvalidConfig(
            "cwd must be absolute".to_string(),
        ));
    }
    if config.api_url.trim().is_empty() {
        return Err(ClaudeChannelLaunchError::InvalidConfig(
            "api_url must not be empty".to_string(),
        ));
    }
    if config.api_token.trim().is_empty() {
        return Err(ClaudeChannelLaunchError::InvalidConfig(
            "api_token must not be empty".to_string(),
        ));
    }
    if config.claude_bin.trim().is_empty() {
        return Err(ClaudeChannelLaunchError::InvalidConfig(
            "claude_bin must not be empty".to_string(),
        ));
    }
    if config.script_bin.trim().is_empty() {
        return Err(ClaudeChannelLaunchError::InvalidConfig(
            "script_bin must not be empty".to_string(),
        ));
    }
    if config.resume && config.provider_session_id.trim().is_empty() {
        return Err(ClaudeChannelLaunchError::InvalidConfig(
            "provider_session_id is required for resume".to_string(),
        ));
    }
    Ok(())
}

fn build_claude_shell_script(config: &ClaudeChannelLaunchConfig) -> String {
    let mut commands = vec![
        format!("export PATH=\"{}:$PATH\"", STANDARD_PATH_PREFIXES.join(":")),
        format!(
            "if ! command -v {} >/dev/null 2>&1; then source ~/.zshrc >/dev/null 2>&1 || true; fi",
            shell_quote(&config.claude_bin)
        ),
        format!("cd {}", shell_quote_path(&config.cwd)),
        format!(
            "export {MANAGED_SESSION_ENV}={}",
            shell_quote(&config.session_id)
        ),
        format!(
            "export LONGHOUSE_CHANNEL_SESSION_ID={}",
            shell_quote(&config.session_id)
        ),
        format!(
            "export LONGHOUSE_PROVIDER_SESSION_ID={}",
            shell_quote(&config.provider_session_id)
        ),
        format!(
            "export LONGHOUSE_CHANNEL_CWD={}",
            shell_quote_path(&config.cwd)
        ),
        format!("export LONGHOUSE_HOOK_URL={}", shell_quote(&config.api_url)),
        format!(
            "export LONGHOUSE_PERMISSION_HOOK_ENABLED={}",
            config.permission_mode.permission_hook_enabled()
        ),
    ];

    let target_flag = if config.resume {
        "--resume"
    } else {
        "--session-id"
    };
    let mut claude_bits = vec![config.claude_bin.clone()];
    if config.permission_mode.uses_skip_permissions() {
        claude_bits.push("--dangerously-skip-permissions".to_string());
    }
    claude_bits.extend([
        target_flag.to_string(),
        config.provider_session_id.clone(),
        CLAUDE_CHANNEL_DEVELOPMENT_FLAG.to_string(),
        format!("server:{CLAUDE_CHANNEL_SERVER_NAME}"),
    ]);
    commands.push(format!(
        "exec {}",
        claude_bits
            .iter()
            .map(|part| shell_quote(part))
            .collect::<Vec<_>>()
            .join(" ")
    ));
    commands.join("; ")
}

fn build_script_args(shell_script: &str, log_path: &Path) -> Vec<String> {
    #[cfg(target_os = "macos")]
    {
        vec![
            "-q".to_string(),
            log_path.display().to_string(),
            "zsh".to_string(),
            "-lc".to_string(),
            shell_script.to_string(),
        ]
    }
    #[cfg(not(target_os = "macos"))]
    {
        vec![
            "-q".to_string(),
            "-c".to_string(),
            format!("zsh -lc {}", shell_quote(shell_script)),
            log_path.display().to_string(),
        ]
    }
}

fn launch_log_path(
    config: &ClaudeChannelLaunchConfig,
) -> Result<PathBuf, ClaudeChannelLaunchError> {
    if let Some(log_dir) = config.log_dir.as_ref() {
        return Ok(log_dir.join(format!("{}.log", config.session_id)));
    }
    let claude_dir = config.claude_dir.clone().unwrap_or_else(default_claude_dir);
    Ok(claude_dir
        .join("logs")
        .join(CLAUDE_REMOTE_LAUNCH_LOG_DIR)
        .join(format!("{}.log", config.session_id)))
}

fn ensure_claude_launch_prereqs(
    config: &ClaudeChannelLaunchConfig,
) -> Result<(), ClaudeChannelLaunchError> {
    ensure_claude_hook_settings(config.claude_dir.as_deref(), config.permission_mode)?;
    ensure_claude_channel_mcp_server(config.claude_dir.as_deref())
}

fn ensure_claude_hook_settings(
    claude_dir: Option<&Path>,
    permission_mode: ClaudePermissionMode,
) -> Result<(), ClaudeChannelLaunchError> {
    let resolved_claude_dir = claude_dir
        .map(Path::to_path_buf)
        .unwrap_or_else(default_claude_dir);
    let hooks_dir = resolved_claude_dir.join("hooks");
    let lifecycle_hook = hooks_dir.join(CLAUDE_LIFECYCLE_HOOK_SCRIPT);
    let permission_gate_hook = hooks_dir.join(CLAUDE_PERMISSION_GATE_SCRIPT);

    require_existing_hook(&lifecycle_hook, "Claude Longhouse lifecycle hook")?;
    chmod_executable_if_possible(&lifecycle_hook);
    let permission_gate_exists = permission_gate_hook.is_file();
    if permission_mode == ClaudePermissionMode::RemoteApprove {
        require_existing_hook(
            &permission_gate_hook,
            "Claude Longhouse permission gate hook",
        )?;
    }
    if permission_gate_exists {
        chmod_executable_if_possible(&permission_gate_hook);
    }

    let settings_path = resolved_claude_dir.join("settings.json");
    let mut settings = read_json_object_or_empty(&settings_path)?;
    let root = settings.as_object_mut().ok_or_else(|| {
        ClaudeChannelLaunchError::ConfigFailed("Claude settings root is not an object".to_string())
    })?;
    let hooks = root
        .entry("hooks")
        .or_insert_with(|| Value::Object(Map::new()));
    if !hooks.is_object() {
        *hooks = Value::Object(Map::new());
    }
    let hooks = hooks.as_object_mut().expect("hooks normalized to object");

    let lifecycle_entry = command_hook_entry(&lifecycle_hook, 5);
    upsert_hook_entry(
        hooks,
        "Stop",
        lifecycle_entry.clone(),
        is_longhouse_lifecycle_entry,
    );
    for event in CLAUDE_LIFECYCLE_HOOK_EVENTS {
        upsert_hook_entry(
            hooks,
            event,
            lifecycle_entry.clone(),
            is_longhouse_lifecycle_entry,
        );
    }

    if permission_gate_exists {
        let permission_gate_entry = command_hook_entry(&permission_gate_hook, 30);
        upsert_hook_entry(hooks, "PreToolUse", permission_gate_entry, |entry| {
            entry_has_command_substr(entry, CLAUDE_PERMISSION_GATE_SCRIPT)
        });
    }

    write_json_object(&settings_path, &settings)
}

fn require_existing_hook(path: &Path, label: &str) -> Result<(), ClaudeChannelLaunchError> {
    if path.is_file() {
        return Ok(());
    }
    Err(ClaudeChannelLaunchError::ConfigFailed(format!(
        "{label} is missing at {}; run `longhouse machine repair` or `longhouse connect --install` before remote launch",
        path.display()
    )))
}

fn command_hook_entry(command: &Path, timeout: u64) -> Value {
    json!({
        "hooks": [{
            "type": "command",
            "command": command.display().to_string(),
            "async": false,
            "timeout": timeout,
        }]
    })
}

fn upsert_hook_entry<F>(hooks: &mut Map<String, Value>, event: &str, new_entry: Value, matches: F)
where
    F: Fn(&Value) -> bool,
{
    let existing = hooks
        .remove(event)
        .and_then(|value| value.as_array().cloned())
        .unwrap_or_default();
    let mut updated = false;
    let mut merged = Vec::with_capacity(existing.len() + 1);
    for entry in existing {
        if matches(&entry) {
            if !updated {
                merged.push(new_entry.clone());
                updated = true;
            }
        } else {
            merged.push(entry);
        }
    }
    if !updated {
        merged.push(new_entry);
    }
    hooks.insert(event.to_string(), Value::Array(merged));
}

fn is_longhouse_lifecycle_entry(entry: &Value) -> bool {
    hook_commands(entry).any(|command| {
        command.contains("longhouse-") && !command.contains(CLAUDE_PERMISSION_GATE_SCRIPT)
    })
}

fn entry_has_command_substr(entry: &Value, needle: &str) -> bool {
    hook_commands(entry).any(|command| command.contains(needle))
}

fn hook_commands(entry: &Value) -> impl Iterator<Item = &str> {
    entry
        .get("hooks")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|hook| hook.get("command").and_then(Value::as_str))
}

fn chmod_executable_if_possible(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(metadata) = std::fs::metadata(path) {
            let mut permissions = metadata.permissions();
            let mode = permissions.mode();
            let executable_mode = mode | 0o755;
            if executable_mode != mode {
                permissions.set_mode(executable_mode);
                let _ = std::fs::set_permissions(path, permissions);
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
}

fn ensure_claude_channel_mcp_server(
    claude_dir: Option<&Path>,
) -> Result<(), ClaudeChannelLaunchError> {
    let config_path = claude_user_config_path(claude_dir);
    let mut settings = read_json_object_or_empty(&config_path)?;
    let desired = json!({
        "type": "stdio",
        "command": "longhouse",
        "args": ["claude-channel", "serve"],
        "env": {},
    });

    let root = settings.as_object_mut().ok_or_else(|| {
        ClaudeChannelLaunchError::ConfigFailed("Claude config root is not an object".to_string())
    })?;
    let mcp_servers = root
        .entry("mcpServers")
        .or_insert_with(|| Value::Object(Map::new()));
    if !mcp_servers.is_object() {
        *mcp_servers = Value::Object(Map::new());
    }
    mcp_servers
        .as_object_mut()
        .expect("mcpServers normalized to object")
        .insert(CLAUDE_CHANNEL_SERVER_NAME.to_string(), desired);

    if let Some(projects) = root.get_mut("projects").and_then(Value::as_object_mut) {
        for project_settings in projects.values_mut() {
            if let Some(project_mcp_servers) = project_settings
                .get_mut("mcpServers")
                .and_then(Value::as_object_mut)
            {
                project_mcp_servers.remove(CLAUDE_CHANNEL_SERVER_NAME);
            }
        }
    }

    write_json_object(&config_path, &settings)
}

fn claude_user_config_path(claude_dir: Option<&Path>) -> PathBuf {
    let resolved = claude_dir
        .map(Path::to_path_buf)
        .unwrap_or_else(default_claude_dir);
    let parent = resolved.parent().unwrap_or_else(|| Path::new("."));
    let name = resolved
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or(".claude");
    parent.join(format!("{name}.json"))
}

fn read_json_object_or_empty(path: &Path) -> Result<Value, ClaudeChannelLaunchError> {
    if !path.exists() {
        return Ok(Value::Object(Map::new()));
    }
    let raw = std::fs::read_to_string(path).map_err(|err| {
        ClaudeChannelLaunchError::ConfigFailed(format!("reading {}: {err}", path.display()))
    })?;
    let value: Value = serde_json::from_str(&raw).map_err(|err| {
        ClaudeChannelLaunchError::ConfigFailed(format!("parsing {}: {err}", path.display()))
    })?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(ClaudeChannelLaunchError::ConfigFailed(format!(
            "{} is not a JSON object",
            path.display()
        )))
    }
}

fn write_json_object(path: &Path, value: &Value) -> Result<(), ClaudeChannelLaunchError> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|err| {
            ClaudeChannelLaunchError::ConfigFailed(format!("creating {}: {err}", parent.display()))
        })?;
    }
    let raw = serde_json::to_vec_pretty(value).map_err(|err| {
        ClaudeChannelLaunchError::ConfigFailed(format!("serializing {}: {err}", path.display()))
    })?;
    let mut with_newline = raw;
    with_newline.push(b'\n');
    std::fs::write(path, with_newline).map_err(|err| {
        ClaudeChannelLaunchError::ConfigFailed(format!("writing {}: {err}", path.display()))
    })
}

async fn wait_for_channel_state(
    session_id: &str,
    state_root: Option<&Path>,
    timeout: Duration,
) -> Result<Value, ClaudeChannelLaunchError> {
    let path = state_file_path(session_id, state_root)?;
    let deadline = Instant::now() + timeout;
    let mut last_not_ready = false;
    loop {
        match read_state_value(&path) {
            Ok(value) => {
                if value.get("ready").and_then(Value::as_bool).unwrap_or(false) {
                    return Ok(value);
                }
                last_not_ready = true;
            }
            Err(StateReadError::Missing) => {}
            Err(StateReadError::Invalid(message)) => {
                return Err(ClaudeChannelLaunchError::StateNotReady(message));
            }
        }
        if Instant::now() >= deadline {
            let message = if last_not_ready {
                format!("state at {} did not become ready", path.display())
            } else {
                format!("state did not appear at {}", path.display())
            };
            return Err(ClaudeChannelLaunchError::StateNotReady(message));
        }
        sleep(DEFAULT_POLL_INTERVAL).await;
    }
}

fn state_file_path(
    session_id: &str,
    state_root: Option<&Path>,
) -> Result<PathBuf, ClaudeChannelLaunchError> {
    let normalized = Uuid::parse_str(session_id).map_err(|_| {
        ClaudeChannelLaunchError::StateNotReady("session id is not a UUID".to_string())
    })?;
    let root = state_root
        .map(Path::to_path_buf)
        .unwrap_or_else(|| default_claude_dir().join("channels/longhouse"));
    Ok(root.join("sessions").join(format!("{normalized}.json")))
}

enum StateReadError {
    Missing,
    Invalid(String),
}

fn read_state_value(path: &Path) -> Result<Value, StateReadError> {
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Err(StateReadError::Missing)
        }
        Err(err) => return Err(StateReadError::Invalid(err.to_string())),
    };
    serde_json::from_str(&raw)
        .map_err(|err| StateReadError::Invalid(format!("state is invalid JSON: {err}")))
}

fn default_claude_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".claude")
}

fn terminate_child_group(child: &mut Child) {
    #[cfg(unix)]
    unsafe {
        let pid = child.id() as i32;
        let pgid = libc::getpgid(pid);
        if pgid > 0 {
            let _ = libc::killpg(pgid, libc::SIGTERM);
        } else {
            let _ = child.kill();
        }
    }
    #[cfg(not(unix))]
    {
        let _ = child.kill();
    }
    let _ = child.wait();
}

fn reap_child_on_exit(mut child: Child) {
    thread::spawn(move || {
        let _ = child.wait();
    });
}

fn shell_quote_path(path: &Path) -> String {
    shell_quote(&path.display().to_string())
}

fn shell_quote(value: &str) -> String {
    if value.is_empty() {
        return "''".to_string();
    }
    if value.chars().all(|ch| {
        ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.' | '/' | ':' | '=' | ',')
    }) {
        return value.to_string();
    }
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;

    use serde_json::json;

    const SESSION_ID: &str = "11111111-1111-4111-8111-111111111111";
    const PROVIDER_SESSION_ID: &str = "22222222-2222-4222-8222-222222222222";

    fn test_config(temp: &Path) -> ClaudeChannelLaunchConfig {
        ClaudeChannelLaunchConfig {
            session_id: SESSION_ID.to_string(),
            provider_session_id: PROVIDER_SESSION_ID.to_string(),
            cwd: temp.join("workspace"),
            api_url: "https://example.test".to_string(),
            api_token: "device-token-secret".to_string(),
            hook_token: Some("hook-token-secret".to_string()),
            resume: false,
            wait_ready: Duration::from_millis(100),
            claude_bin: "/opt/homebrew/bin/claude".to_string(),
            permission_mode: ClaudePermissionMode::Bypass,
            state_root: Some(temp.join("channels")),
            claude_dir: Some(temp.join(".claude")),
            log_dir: Some(temp.join("logs")),
            script_bin: "script".to_string(),
        }
    }

    fn write_existing_claude_hooks(claude_dir: &Path) {
        let hooks_dir = claude_dir.join("hooks");
        std::fs::create_dir_all(&hooks_dir).unwrap();
        std::fs::write(hooks_dir.join(CLAUDE_LIFECYCLE_HOOK_SCRIPT), "#!/bin/sh\n").unwrap();
        std::fs::write(
            hooks_dir.join(CLAUDE_PERMISSION_GATE_SCRIPT),
            "#!/usr/bin/env python3\n",
        )
        .unwrap();
    }

    #[test]
    fn ensures_claude_hook_settings_from_existing_hook_files() {
        let temp = tempfile::tempdir().unwrap();
        let claude_dir = temp.path().join(".claude");
        write_existing_claude_hooks(&claude_dir);
        let settings_path = claude_dir.join("settings.json");
        std::fs::write(
            &settings_path,
            serde_json::to_vec_pretty(&json!({
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "/usr/local/bin/custom-hook.sh"}]},
                        {"hooks": [{"type": "command", "command": "/old/longhouse-old-hook.sh"}]},
                        {"hooks": [{"type": "command", "command": "/old/longhouse-permission-gate.py"}]}
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/old/longhouse-stop.sh"}]}
                    ]
                }
            }))
            .unwrap(),
        )
        .unwrap();

        ensure_claude_hook_settings(Some(&claude_dir), ClaudePermissionMode::RemoteApprove)
            .unwrap();

        let updated: Value =
            serde_json::from_slice(&std::fs::read(&settings_path).unwrap()).unwrap();
        let pre_tool_use = updated["hooks"]["PreToolUse"].as_array().unwrap();
        let pre_commands = pre_tool_use
            .iter()
            .flat_map(|entry| hook_commands(entry))
            .collect::<Vec<_>>();
        assert!(pre_commands.contains(&"/usr/local/bin/custom-hook.sh"));
        assert!(pre_commands
            .iter()
            .any(|command| command.ends_with(CLAUDE_LIFECYCLE_HOOK_SCRIPT)));
        assert!(pre_commands
            .iter()
            .any(|command| command.ends_with(CLAUDE_PERMISSION_GATE_SCRIPT)));
        assert!(!pre_commands.iter().any(|command| command.contains("/old/")));

        for event in CLAUDE_LIFECYCLE_HOOK_EVENTS {
            let commands = updated["hooks"][*event]
                .as_array()
                .unwrap()
                .iter()
                .flat_map(|entry| hook_commands(entry))
                .collect::<Vec<_>>();
            assert!(commands
                .iter()
                .any(|command| command.ends_with(CLAUDE_LIFECYCLE_HOOK_SCRIPT)));
        }
        let stop_commands = updated["hooks"]["Stop"]
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|entry| hook_commands(entry))
            .collect::<Vec<_>>();
        assert!(stop_commands
            .iter()
            .any(|command| command.ends_with(CLAUDE_LIFECYCLE_HOOK_SCRIPT)));
    }

    #[test]
    fn remote_approve_requires_existing_permission_gate_hook() {
        let temp = tempfile::tempdir().unwrap();
        let claude_dir = temp.path().join(".claude");
        let hooks_dir = claude_dir.join("hooks");
        std::fs::create_dir_all(&hooks_dir).unwrap();
        std::fs::write(hooks_dir.join(CLAUDE_LIFECYCLE_HOOK_SCRIPT), "#!/bin/sh\n").unwrap();

        let err =
            ensure_claude_hook_settings(Some(&claude_dir), ClaudePermissionMode::RemoteApprove)
                .unwrap_err();

        assert!(err
            .to_string()
            .contains("Claude Longhouse permission gate hook is missing"));
    }

    #[test]
    fn ensures_user_mcp_config_and_removes_project_shadow() {
        let temp = tempfile::tempdir().unwrap();
        let claude_dir = temp.path().join(".claude");
        let config_path = claude_user_config_path(Some(&claude_dir));
        std::fs::write(
            &config_path,
            serde_json::to_vec_pretty(&json!({
                "projects": {
                    "/tmp/work": {
                        "mcpServers": {
                            "longhouse-channel": {
                                "type": "stdio",
                                "command": "old"
                            }
                        }
                    }
                }
            }))
            .unwrap(),
        )
        .unwrap();

        ensure_claude_channel_mcp_server(Some(&claude_dir)).unwrap();

        let updated: Value = serde_json::from_slice(&std::fs::read(&config_path).unwrap()).unwrap();
        assert_eq!(
            updated["mcpServers"]["longhouse-channel"]["command"],
            "longhouse"
        );
        assert_eq!(
            updated["mcpServers"]["longhouse-channel"]["args"],
            json!(["claude-channel", "serve"])
        );
        assert!(updated["projects"]["/tmp/work"]["mcpServers"]
            .get("longhouse-channel")
            .is_none());
    }

    #[test]
    fn launch_plan_uses_provider_id_without_leaking_tokens() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(temp.path().join("workspace")).unwrap();
        let config = test_config(temp.path());
        let plan = build_launch_command_plan(&config).unwrap();

        assert_eq!(plan.program, "script");
        assert!(plan.shell_script.contains("--session-id"));
        assert!(plan.shell_script.contains(PROVIDER_SESSION_ID));
        assert!(plan.shell_script.contains("LONGHOUSE_CHANNEL_SESSION_ID"));
        assert!(plan.shell_script.contains("LONGHOUSE_PROVIDER_SESSION_ID"));
        assert!(plan.shell_script.contains("--dangerously-skip-permissions"));
        assert!(!plan.shell_script.contains("device-token-secret"));
        assert!(!plan.args.join(" ").contains("hook-token-secret"));
        assert_eq!(plan.hook_token_env, "hook-token-secret");
    }

    #[test]
    fn launch_command_passes_hook_token_only_through_env() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(temp.path().join("workspace")).unwrap();
        let config = test_config(temp.path());
        let plan = build_launch_command_plan(&config).unwrap();

        let command = build_launch_command(&config, &plan);

        assert_eq!(command.get_current_dir(), Some(config.cwd.as_path()));
        let envs = command.get_envs().collect::<Vec<_>>();
        assert!(envs.iter().any(|(key, value)| {
            *key == OsStr::new("LONGHOUSE_HOOK_TOKEN")
                && value.as_deref() == Some(OsStr::new("hook-token-secret"))
        }));
        assert!(envs
            .iter()
            .any(|(key, value)| *key == OsStr::new("CLAUDE_CONFIG_DIR") && value.is_none()));
        assert!(!command
            .get_args()
            .any(|arg| arg.to_string_lossy().contains("hook-token-secret")));
    }

    #[test]
    fn script_args_match_platform_contract() {
        let script = "exec claude";
        let log_path = Path::new("/tmp/claude-channel.log");
        let args = build_script_args(script, log_path);

        #[cfg(target_os = "macos")]
        assert_eq!(
            args,
            vec![
                "-q".to_string(),
                "/tmp/claude-channel.log".to_string(),
                "zsh".to_string(),
                "-lc".to_string(),
                script.to_string()
            ]
        );
        #[cfg(not(target_os = "macos"))]
        assert_eq!(
            args,
            vec![
                "-q".to_string(),
                "-c".to_string(),
                "zsh -lc 'exec claude'".to_string(),
                "/tmp/claude-channel.log".to_string()
            ]
        );
    }

    #[test]
    fn remote_approve_removes_skip_permissions_and_enables_gate() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(temp.path().join("workspace")).unwrap();
        let mut config = test_config(temp.path());
        config.permission_mode = ClaudePermissionMode::RemoteApprove;
        config.hook_token = None;
        let plan = build_launch_command_plan(&config).unwrap();

        assert!(!plan.shell_script.contains("--dangerously-skip-permissions"));
        assert!(plan
            .shell_script
            .contains("LONGHOUSE_PERMISSION_HOOK_ENABLED=1"));
        assert_eq!(plan.hook_token_env, "device-token-secret");
    }

    #[test]
    fn resume_plan_uses_resume_flag() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(temp.path().join("workspace")).unwrap();
        let mut config = test_config(temp.path());
        config.resume = true;
        let plan = build_launch_command_plan(&config).unwrap();

        assert!(plan.shell_script.contains("--resume"));
        assert!(!plan.shell_script.contains("--session-id"));
    }

    #[tokio::test]
    async fn waits_for_ready_state_file() {
        let temp = tempfile::tempdir().unwrap();
        let state_root = temp.path().join("channels");
        let state_path = state_file_path(SESSION_ID, Some(&state_root)).unwrap();
        std::fs::create_dir_all(state_path.parent().unwrap()).unwrap();
        std::fs::write(
            &state_path,
            serde_json::to_vec(&json!({
                "ready": true,
                "port": 8123,
                "auth_token": "secret",
            }))
            .unwrap(),
        )
        .unwrap();

        let state =
            wait_for_channel_state(SESSION_ID, Some(&state_root), Duration::from_millis(100))
                .await
                .unwrap();
        assert_eq!(state["port"], 8123);
    }
}
