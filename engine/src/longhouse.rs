//! Native human-facing Longhouse device facade.
//!
//! This intentionally starts small: it proves the public executable, paired
//! engine resolution, and build-identity boundary before provider launch is
//! moved here. It never falls back to Python or uv.

#[path = "build_identity.rs"]
mod build_identity;

use anyhow::Context;
use clap::{Args, Parser, Subcommand};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::io::{IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use std::time::Duration;
use uuid::Uuid;

#[derive(Parser)]
#[command(name = "longhouse", about = "Native Longhouse device CLI")]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// Print this facade's build identity and its paired engine path.
    BuildIdentity {
        #[arg(long)]
        json: bool,
    },
    /// Verify the paired engine is present and built from the same commit.
    VerifyPair,
    /// Store or clear the device credentials used by native Longhouse commands.
    Auth(AuthArgs),
    /// Print the native fast local-health snapshot used by Longhouse.app.
    LocalHealth(LocalHealthArgs),
    /// Repair native Machine Agent service state without invoking Python.
    Machine {
        #[command(subcommand)]
        command: MachineCommand,
    },
    /// Configure native Longhouse hooks for Claude.
    #[command(args_conflicts_with_subcommands = true)]
    Claude {
        #[command(subcommand)]
        command: Option<ClaudeCommand>,
        #[command(flatten)]
        launch: ClaudeLaunchArgs,
    },
    /// Launch or manage a native Longhouse Codex Helm session.
    #[command(args_conflicts_with_subcommands = true)]
    Codex {
        #[command(subcommand)]
        command: Option<CodexCommand>,
        #[command(flatten)]
        launch: CodexLaunchArgs,
    },
    /// Launch or manage a native Longhouse OpenCode Helm session.
    #[command(args_conflicts_with_subcommands = true)]
    Opencode {
        #[command(subcommand)]
        command: Option<OpencodeCommand>,
        #[command(flatten)]
        launch: OpencodeLaunchArgs,
    },
}

#[derive(Subcommand)]
enum CodexCommand {
    /// Create a managed Codex session and attach its stock TUI.
    Launch(CodexLaunchArgs),
    /// Attach the stock Codex TUI to a running managed session.
    Attach(CodexAttachArgs),
    /// Stop a managed Codex bridge and its provider execution.
    Stop(CodexStopArgs),
}

#[derive(Subcommand)]
enum OpencodeCommand {
    Attach(OpencodeAttachArgs),
    Stop(OpencodeStopArgs),
}

#[derive(Subcommand)]
enum ClaudeCommand {
    /// Replace the Python Claude permission hook with the paired native engine.
    Configure {
        #[arg(long)]
        claude_dir: Option<PathBuf>,
    },
}

#[derive(Args)]
struct ClaudeLaunchArgs {
    #[arg(long, default_value = ".")]
    cwd: PathBuf,
    #[arg(long)]
    project: Option<String>,
    #[arg(long)]
    name: Option<String>,
    #[arg(long, default_value = "assist")]
    loop_mode: String,
    #[arg(long)]
    url: Option<String>,
    #[arg(long)]
    token: Option<String>,
    #[arg(long)]
    remote_approve: bool,
    /// Resume an existing Longhouse Claude Helm session.
    #[arg(long)]
    resume: Option<String>,
    #[arg(long)]
    claude_bin: Option<String>,
    #[arg(long, alias = "config-dir")]
    claude_dir: Option<PathBuf>,
}

#[derive(Args)]
struct LocalHealthArgs {
    /// Kept for Desktop and existing CLI compatibility; the native snapshot is always fast.
    #[arg(long)]
    fast: bool,
    /// Emit the snapshot as JSON.
    #[arg(long)]
    json: bool,
    /// Longhouse state root override for diagnostics and tests.
    #[arg(long)]
    state_root: Option<PathBuf>,
}

#[derive(Args)]
struct AuthArgs {
    /// Runtime Host URL. Required when no URL is already configured.
    #[arg(long)]
    url: Option<String>,
    /// Environment variable containing the device token (default: LONGHOUSE_DEVICE_TOKEN).
    #[arg(long, default_value = "LONGHOUSE_DEVICE_TOKEN")]
    token_env: String,
    /// Remove stored native device credentials.
    #[arg(long)]
    clear: bool,
    /// Override the stored machine name.
    #[arg(long)]
    device: Option<String>,
}

#[derive(Subcommand)]
enum MachineCommand {
    Repair(MachineRepairArgs),
}

#[derive(Args)]
struct MachineRepairArgs {
    #[arg(long)]
    dry_run: bool,
    #[arg(long)]
    repair_service: bool,
    #[arg(long)]
    json: bool,
    #[arg(long)]
    state_root: Option<PathBuf>,
}

#[derive(Args)]
struct CodexLaunchArgs {
    #[arg(long, default_value = ".")]
    cwd: PathBuf,
    #[arg(long)]
    project: Option<String>,
    #[arg(long)]
    name: Option<String>,
    #[arg(long, default_value = "assist")]
    loop_mode: String,
    #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
    attach: bool,
    #[arg(long)]
    no_attach: bool,
    #[arg(long)]
    url: Option<String>,
    #[arg(long)]
    token: Option<String>,
    #[arg(long)]
    codex_bin: Option<String>,
    #[arg(long)]
    model: Option<String>,
    #[arg(long)]
    model_reasoning_effort: Option<String>,
    #[arg(long)]
    dangerously_bypass_approvals_and_sandbox: bool,
}

#[derive(Args)]
struct OpencodeLaunchArgs {
    #[arg(long, default_value = ".")]
    cwd: PathBuf,
    #[arg(long)]
    project: Option<String>,
    #[arg(long)]
    name: Option<String>,
    #[arg(long, default_value = "assist")]
    loop_mode: String,
    #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
    attach: bool,
    #[arg(long)]
    no_attach: bool,
    #[arg(long)]
    url: Option<String>,
    #[arg(long)]
    token: Option<String>,
    #[arg(long)]
    opencode_bin: Option<String>,
    #[arg(long, alias = "config-dir")]
    claude_dir: Option<PathBuf>,
}

#[derive(Args)]
struct OpencodeAttachArgs {
    #[arg(long)]
    session_id: String,
    #[arg(long)]
    opencode_bin: Option<String>,
    #[arg(long, alias = "config-dir")]
    claude_dir: Option<PathBuf>,
}
#[derive(Args)]
struct OpencodeStopArgs {
    #[arg(long)]
    session_id: String,
}

#[derive(Args)]
struct CodexAttachArgs {
    #[arg(long)]
    session_id: String,
    #[arg(long)]
    codex_bin: Option<String>,
    #[arg(long)]
    model: Option<String>,
    #[arg(long)]
    model_reasoning_effort: Option<String>,
    #[arg(long)]
    dangerously_bypass_approvals_and_sandbox: bool,
}

#[derive(Args)]
struct CodexStopArgs {
    #[arg(long)]
    session_id: String,
}

#[derive(Serialize)]
struct PairIdentity {
    facade: build_identity::BuildIdentity,
    engine_path: String,
    engine: serde_json::Value,
}

#[derive(Deserialize)]
struct MachineState {
    runtime_url: Option<String>,
    machine_name: Option<String>,
}

#[derive(Deserialize)]
struct ManagedLaunchResponse {
    session_id: String,
    run_id: String,
    provider_session_id: Option<String>,
    permission_mode: Option<String>,
    hook_token: Option<String>,
    managed_transport: Option<String>,
}

#[derive(Deserialize)]
struct BridgeStartResponse {
    ws_url: String,
    thread_id: Option<String>,
}

#[derive(Deserialize)]
struct BridgeState {
    cwd: String,
    codex_bin: String,
    ws_url: Option<String>,
    status: Option<String>,
    thread_id: Option<String>,
}

fn paired_engine_path() -> anyhow::Result<PathBuf> {
    if let Some(override_path) = std::env::var_os("LONGHOUSE_ENGINE_BIN") {
        return Ok(PathBuf::from(override_path));
    }
    let exe = std::fs::canonicalize(
        std::env::current_exe().context("resolve native longhouse executable")?,
    )
    .context("resolve native longhouse executable path")?;
    let dir = exe
        .parent()
        .context("native longhouse executable has no parent")?;
    Ok(dir.join(if cfg!(windows) {
        "longhouse-engine.exe"
    } else {
        "longhouse-engine"
    }))
}

fn pair_identity() -> anyhow::Result<PairIdentity> {
    let engine_path = paired_engine_path()?;
    if !engine_path.is_file() {
        anyhow::bail!(
            "paired longhouse-engine not found at {}",
            engine_path.display()
        );
    }
    let output = Command::new(&engine_path)
        .args(["build-identity", "--json"])
        .output()
        .with_context(|| format!("run paired engine {}", engine_path.display()))?;
    if !output.status.success() {
        anyhow::bail!("paired engine build-identity failed with {}", output.status);
    }
    let engine: serde_json::Value = serde_json::from_slice(&output.stdout)
        .context("paired engine returned invalid build identity JSON")?;
    let facade = build_identity::BuildIdentity::current();
    let engine_commit = engine
        .get("commit")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    if engine_commit != facade.commit {
        anyhow::bail!(
            "native longhouse/engine build mismatch: facade {} engine {}",
            facade.commit_short,
            engine_commit
        );
    }
    Ok(PairIdentity {
        facade,
        engine_path: engine_path.display().to_string(),
        engine,
    })
}

fn longhouse_home() -> anyhow::Result<PathBuf> {
    if let Some(home) = std::env::var_os("LONGHOUSE_HOME") {
        return Ok(PathBuf::from(home));
    }
    if let Some(provider_home) = std::env::var_os("CLAUDE_CONFIG_DIR") {
        let provider_home = PathBuf::from(provider_home);
        if provider_home.file_name().and_then(|name| name.to_str()) == Some(".longhouse") {
            return Ok(provider_home);
        }
        if let Some(parent) = provider_home.parent() {
            return Ok(parent.join(".longhouse"));
        }
    }
    Ok(PathBuf::from(std::env::var("HOME").context("HOME not set")?).join(".longhouse"))
}

fn configure_claude_hooks(claude_dir: Option<PathBuf>) -> anyhow::Result<()> {
    let claude_dir = claude_dir.unwrap_or_else(|| {
        std::env::var_os("CLAUDE_CONFIG_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into())).join(".claude")
            })
    });
    let settings_path = claude_dir.join("settings.json");
    let user_config_path = claude_dir.parent().unwrap_or(Path::new(".")).join(format!(
        "{}.json",
        claude_dir
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or(".claude")
    ));
    let mut settings: serde_json::Map<String, serde_json::Value> =
        match std::fs::read(&settings_path) {
            Ok(raw) => serde_json::from_slice(&raw)
                .with_context(|| format!("parse {}", settings_path.display()))?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => serde_json::Map::new(),
            Err(error) => {
                return Err(error).with_context(|| format!("read {}", settings_path.display()))
            }
        };
    let hooks = settings
        .entry("hooks")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .context("Claude settings hooks must be an object")?;
    let pre_tool = hooks
        .entry("PreToolUse")
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .context("Claude PreToolUse hooks must be an array")?;
    // Replace both the old Python hook and any prior native hook.  This is
    // deliberately idempotent: Claude runs every configured PreToolUse hook.
    pre_tool.retain(|entry| {
        let entry = entry.to_string();
        !entry.contains("longhouse-permission-gate.py") && !entry.contains("claude-permission-gate")
    });
    let engine = shell_quote_path(&paired_engine_path()?);
    pre_tool.push(json!({"hooks": [{"type": "command", "command": format!("{engine} claude-permission-gate"), "async": false, "timeout": 30}]}));
    let lifecycle_command = format!("{engine} claude-lifecycle-hook");
    for event in [
        "SessionStart",
        "Stop",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionRequest",
        "Notification",
    ] {
        let entries = hooks
            .entry(event)
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .with_context(|| format!("Claude {event} hooks must be an array"))?;
        entries.retain(|entry| {
            let entry = entry.to_string();
            !entry.contains("longhouse-hook.sh") && !entry.contains("claude-lifecycle-hook")
        });
        entries.push(json!({"hooks": [{"type": "command", "command": lifecycle_command, "async": false, "timeout": 5}]}));
    }
    let mut user_config: serde_json::Map<String, serde_json::Value> =
        match std::fs::read(&user_config_path) {
            Ok(raw) => serde_json::from_slice(&raw)
                .with_context(|| format!("parse {}", user_config_path.display()))?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => serde_json::Map::new(),
            Err(error) => {
                return Err(error).with_context(|| format!("read {}", user_config_path.display()))
            }
        };
    let mcp = user_config
        .entry("mcpServers")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .context("Claude settings mcpServers must be an object")?;
    mcp.insert("longhouse-channel".into(), json!({"type":"stdio", "command": paired_engine_path()?, "args":["claude-channel","serve"], "env":{}}));
    std::fs::create_dir_all(&claude_dir)?;
    if let Some(projects) = user_config
        .get_mut("projects")
        .and_then(serde_json::Value::as_object_mut)
    {
        for project in projects
            .values_mut()
            .filter_map(serde_json::Value::as_object_mut)
        {
            if let Some(servers) = project
                .get_mut("mcpServers")
                .and_then(serde_json::Value::as_object_mut)
            {
                servers.remove("longhouse-channel");
            }
        }
    }
    std::fs::write(
        &settings_path,
        format!("{}\n", serde_json::to_string_pretty(&settings)?),
    )?;
    std::fs::write(
        &user_config_path,
        format!("{}\n", serde_json::to_string_pretty(&user_config)?),
    )?;
    let legacy_gate = claude_dir.join("hooks/longhouse-permission-gate.py");
    if legacy_gate.exists() {
        std::fs::remove_file(&legacy_gate)?;
    }
    let legacy_lifecycle = claude_dir.join("hooks/longhouse-hook.sh");
    if legacy_lifecycle.exists() {
        std::fs::remove_file(&legacy_lifecycle)?;
    }
    println!(
        "Configured native Claude hooks in {}",
        settings_path.display()
    );
    Ok(())
}

/// Quote a path for Claude's shell-invoked command hook without changing the
/// command's argument boundary when the install location contains whitespace.
fn shell_quote_path(path: &Path) -> String {
    format!(
        "'{}'",
        path.display().to_string().replace('\'', "'\\\"'\\\"'")
    )
}

fn native_local_health(args: LocalHealthArgs) -> anyhow::Result<()> {
    let _ = args.fast;
    let mut command = Command::new(paired_engine_path()?);
    command.args(["device", "local-health"]);
    if args.json {
        command.arg("--json");
    }
    if let Some(state_root) = args.state_root {
        command.arg("--state-root").arg(state_root);
    }
    let status = command.status().context("run paired native local-health")?;
    if !status.success() {
        std::process::exit(status.code().unwrap_or(1));
    }
    Ok(())
}

fn native_auth(args: AuthArgs) -> anyhow::Result<()> {
    let machine_dir = longhouse_home()?.join("machine");
    let state_path = machine_dir.join("state.json");
    if args.clear {
        let _ = std::fs::remove_file(machine_dir.join("device-token"));
        if let Ok(raw) = std::fs::read(&state_path) {
            let mut state: serde_json::Value =
                serde_json::from_slice(&raw).unwrap_or_else(|_| json!({}));
            state["runtime_url"] = serde_json::Value::Null;
            write_private_json(&state_path, &state)?;
        }
        println!("Cleared stored Longhouse device credentials");
        return Ok(());
    }
    let existing: serde_json::Value = std::fs::read(&state_path)
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_else(|| json!({}));
    let url = args
        .url
        .or_else(|| {
            existing
                .get("runtime_url")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
        })
        .filter(|value| value.starts_with("http://") || value.starts_with("https://"))
        .context("No Longhouse URL configured. Pass --url.")?;
    let token = std::env::var(&args.token_env).with_context(|| {
        format!(
            "{} is not set; tokens are accepted only through an environment variable",
            args.token_env
        )
    })?;
    if token.trim().is_empty() {
        anyhow::bail!("{} is empty", args.token_env);
    }
    let runtime = tokio::runtime::Runtime::new()?;
    let valid = runtime.block_on(async {
        let response = reqwest::Client::new()
            .get(format!(
                "{}/api/agents/sessions?limit=1",
                url.trim_end_matches('/')
            ))
            .header("X-Agents-Token", &token)
            .send()
            .await?;
        Ok::<_, anyhow::Error>(
            response.status().as_u16() == 200 || response.status().as_u16() == 501,
        )
    })?;
    if !valid {
        anyhow::bail!("device token was rejected by the Runtime Host");
    }
    let mut state = existing;
    state["schema_version"] = json!(1);
    state["runtime_url"] = json!(url.trim_end_matches('/'));
    state["machine_name"] = json!(args.device.unwrap_or_else(native_machine_name));
    state["written_by"] = json!("native-auth");
    state["written_at"] = json!(chrono::Utc::now().to_rfc3339());
    std::fs::create_dir_all(&machine_dir)?;
    write_private_json(&state_path, &state)?;
    write_private_text(&machine_dir.join("device-token"), token.trim())?;
    println!(
        "Stored native Longhouse credentials for {}",
        state["machine_name"].as_str().unwrap_or("this machine")
    );
    Ok(())
}

fn native_machine_repair(args: MachineRepairArgs) -> anyhow::Result<()> {
    let mut command = Command::new(paired_engine_path()?);
    command.args(["device", "repair"]);
    if args.dry_run {
        command.arg("--dry-run");
    }
    if args.repair_service {
        command.arg("--repair-service");
    }
    if args.json {
        command.arg("--json");
    }
    if let Some(root) = args.state_root {
        command.arg("--state-root").arg(root);
    }
    let status = command
        .status()
        .context("run paired native machine repair")?;
    if !status.success() {
        std::process::exit(status.code().unwrap_or(1));
    }
    Ok(())
}

fn native_machine_name() -> String {
    Command::new("hostname")
        .output()
        .ok()
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "unknown".into())
}

fn write_private_json(path: &Path, value: &serde_json::Value) -> anyhow::Result<()> {
    write_private_text(path, &format!("{}\n", serde_json::to_string_pretty(value)?))
}

fn write_private_text(path: &Path, value: &str) -> anyhow::Result<()> {
    let parent = path.parent().context("credential path has no parent")?;
    std::fs::create_dir_all(parent)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
    }
    let temporary = path.with_extension(format!("tmp.{}", std::process::id()));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(value.as_bytes())?;
    file.sync_all()?;
    drop(file);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))?;
    }
    std::fs::rename(temporary, path)?;
    Ok(())
}

fn launch_managed_claude(args: ClaudeLaunchArgs) -> anyhow::Result<()> {
    let mut cwd = std::fs::canonicalize(&args.cwd)?;
    configure_claude_hooks(args.claude_dir.clone())?;
    let (url, token, machine_name) = resolve_codex_config(args.url, args.token)?;
    let binary = resolve_provider_binary(
        args.claude_bin
            .or_else(|| std::env::var("LONGHOUSE_CLAUDE_BIN").ok()),
        "claude",
        "Claude",
        "--claude-bin",
    )?;
    ensure_claude_channel_prerequisite(&binary)?;
    let git = |args: &[&str]| {
        Command::new("git")
            .arg("-C")
            .arg(&cwd)
            .args(args)
            .output()
            .ok()
            .filter(|output| output.status.success())
            .and_then(|output| String::from_utf8(output.stdout).ok())
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    };
    let (launch_actor, launch_surface) = interactive_human_shell_provenance();
    let runtime = tokio::runtime::Runtime::new()?;
    let resuming = args.resume.is_some();
    let response: ManagedLaunchResponse = if let Some(session_id) = &args.resume {
        let (response, session_cwd) =
            resolve_managed_claude_resume(&runtime, &url, &token, session_id)?;
        cwd = session_cwd;
        response
    } else {
        let mut payload = json!({"cwd":cwd,"provider":"claude","project":args.project,"git_repo":git(&["rev-parse", "--show-toplevel"]),"git_branch":git(&["rev-parse", "--abbrev-ref", "HEAD"]),"display_name":args.name,"loop_mode":args.loop_mode,"machine_name":machine_name,"permission_mode":if args.remote_approve {"remote_approve"} else {"bypass"},"native_claude_channels_available":true});
        if let Some(actor) = launch_actor {
            payload["launch_actor"] = json!(actor);
        }
        if let Some(surface) = launch_surface {
            payload["launch_surface"] = json!(surface);
        }
        let endpoint = format!(
            "{}/api/sessions/managed-local/this-device",
            url.trim_end_matches('/')
        );
        runtime.block_on(async {
            let r = reqwest::Client::new()
                .post(endpoint)
                .header("X-Agents-Token", &token)
                .json(&payload)
                .send()
                .await?;
            if !r.status().is_success() {
                anyhow::bail!("managed Claude launch failed ({})", r.status());
            }
            Ok::<_, anyhow::Error>(r.json().await?)
        })?
    };
    let provider_session_id = response
        .provider_session_id
        .context("Longhouse did not return a Claude provider session")?;
    if response.managed_transport.as_deref() != Some("claude_channel_bridge") {
        anyhow::bail!("Longhouse returned an unsupported managed-local transport for Claude");
    }
    let mut command = Command::new(&binary);
    if response.permission_mode.as_deref() != Some("remote_approve") {
        command.arg("--dangerously-skip-permissions");
    }
    if resuming {
        command.args(["--resume", &provider_session_id]);
    } else {
        command.args(["--session-id", &provider_session_id]);
    }
    command
        .args([
            "--dangerously-load-development-channels",
            "server:longhouse-channel",
        ])
        .current_dir(&cwd)
        .env("LONGHOUSE_MANAGED_SESSION_ID", &response.session_id)
        .env("LONGHOUSE_RUN_ID", &response.run_id)
        .env("LONGHOUSE_CHANNEL_SESSION_ID", &response.session_id)
        .env("LONGHOUSE_PROVIDER_SESSION_ID", &provider_session_id)
        .env("LONGHOUSE_CHANNEL_CWD", &cwd)
        .env("LONGHOUSE_HOOK_URL", &url)
        .env("LONGHOUSE_HOOK_TOKEN", response.hook_token.unwrap_or(token))
        .env(
            "LONGHOUSE_PERMISSION_HOOK_ENABLED",
            if response.permission_mode.as_deref() == Some("remote_approve") {
                "1"
            } else {
                "0"
            },
        );
    if let Err(error) = record_claude_contract(&response.session_id, &cwd, &binary) {
        eprintln!("Longhouse warning: could not record managed-session contract: {error}");
    }
    let run_result = run_foreground_command(&mut command);
    if let Err(error) = remove_claude_contract(&response.session_id) {
        eprintln!("Longhouse warning: could not remove managed-session contract: {error}");
    }
    let exit = run_result?;
    if let Err(error) = record_claude_terminal_event(
        &response.session_id,
        &provider_session_id,
        &machine_name,
        exit,
    ) {
        eprintln!("Longhouse warning: could not queue Claude terminal lifecycle event: {error}");
    }
    if exit != 0 {
        std::process::exit(exit);
    }
    Ok(())
}

fn launch_managed_opencode(args: OpencodeLaunchArgs) -> anyhow::Result<()> {
    let cwd = std::fs::canonicalize(&args.cwd)?;
    let (url, token, machine_name) = resolve_codex_config(args.url, args.token)?;
    let opencode_bin = resolve_provider_binary(
        args.opencode_bin
            .or_else(|| std::env::var("LONGHOUSE_OPENCODE_BIN").ok()),
        "opencode",
        "OpenCode",
        "--opencode-bin",
    )?;
    let (launch_actor, launch_surface) = interactive_human_shell_provenance();
    let mut payload = json!({"cwd": cwd, "provider":"opencode", "project":args.project, "display_name":args.name, "loop_mode":args.loop_mode, "machine_name":machine_name});
    if let Some(actor) = launch_actor {
        payload["launch_actor"] = json!(actor);
    }
    if let Some(surface) = launch_surface {
        payload["launch_surface"] = json!(surface);
    }
    let endpoint = format!(
        "{}/api/sessions/managed-local/this-device",
        url.trim_end_matches('/')
    );
    let runtime = tokio::runtime::Runtime::new()?;
    let response: ManagedLaunchResponse = runtime.block_on(async {
        let response = reqwest::Client::new()
            .post(endpoint)
            .header("X-Agents-Token", &token)
            .json(&payload)
            .send()
            .await?;
        if !response.status().is_success() {
            anyhow::bail!("managed OpenCode launch failed ({})", response.status());
        }
        Ok::<_, anyhow::Error>(response.json().await?)
    })?;
    if response.managed_transport.as_deref() != Some("opencode_server_bridge") {
        anyhow::bail!("Longhouse returned an unsupported managed-local transport for OpenCode");
    }
    let bridge = paired_engine_path()?;
    let mut start = Command::new(&bridge);
    start
        .args([
            "opencode-bridge",
            "start",
            "--session-id",
            &response.session_id,
            "--run-id",
            &response.run_id,
            "--cwd",
        ])
        .arg(&cwd)
        .args([
            "--opencode-bin",
            &opencode_bin,
            "--launch-mode",
            if args.attach
                && !args.no_attach
                && std::io::stdin().is_terminal()
                && std::io::stdout().is_terminal()
            {
                "attached_tui"
            } else {
                "detached"
            },
        ]);
    if let Some(name) = &args.name {
        start.args(["--display-name", name]);
    }
    if let Some(dir) = &args.claude_dir {
        start.arg("--claude-dir").arg(dir);
    }
    let output = start.output().context("start native OpenCode bridge")?;
    if !output.status.success() {
        anyhow::bail!(
            "OpenCode bridge failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    println!(
        "Managed OpenCode ready\n→ {}/s/{}",
        url.trim_end_matches('/'),
        response
            .session_id
            .split('-')
            .next()
            .unwrap_or(&response.session_id)
    );
    let attached = args.attach
        && !args.no_attach
        && std::io::stdin().is_terminal()
        && std::io::stdout().is_terminal();
    if !attached {
        println!(
            "Attach: longhouse opencode attach --session-id {}",
            response.session_id
        );
        return Ok(());
    }
    let mut attach = Command::new(&bridge);
    attach.args([
        "opencode-bridge",
        "attach",
        "--session-id",
        &response.session_id,
        "--opencode-bin",
        &opencode_bin,
    ]);
    let run_result = run_foreground_command(&mut attach);
    let stop_result = stop_opencode_bridge(&response.session_id, args.claude_dir.clone());
    let exit = run_result?;
    stop_result?;
    if exit != 0 {
        std::process::exit(exit);
    }
    print_helm_closed(&machine_name);
    Ok(())
}

fn attach_managed_opencode(args: OpencodeAttachArgs) -> anyhow::Result<()> {
    validate_session_id(&args.session_id)?;
    let mut command = Command::new(paired_engine_path()?);
    command.args([
        "opencode-bridge",
        "attach",
        "--session-id",
        &args.session_id,
    ]);
    if let Some(bin) = args.opencode_bin {
        command.args(["--opencode-bin", &bin]);
    }
    if let Some(dir) = args.claude_dir {
        command.arg("--claude-dir").arg(dir);
    }
    let run_result = run_foreground_command(&mut command);
    let stop_result = stop_opencode_bridge(&args.session_id, None);
    let exit = run_result?;
    stop_result?;
    if exit != 0 {
        std::process::exit(exit);
    }
    Ok(())
}

fn stop_opencode_bridge(session_id: &str, claude_dir: Option<PathBuf>) -> anyhow::Result<()> {
    validate_session_id(session_id)?;
    let mut command = Command::new(paired_engine_path()?);
    command.args(["opencode-bridge", "stop", "--session-id", session_id]);
    if let Some(dir) = claude_dir {
        command.arg("--claude-dir").arg(dir);
    }
    let output = command.output()?;
    if !output.status.success() {
        anyhow::bail!(
            "failed to stop native OpenCode bridge: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(())
}

fn record_claude_terminal_event(
    session_id: &str,
    provider_session_id: &str,
    machine_name: &str,
    exit_code: i32,
) -> anyhow::Result<()> {
    let occurred_at = chrono::Utc::now().to_rfc3339();
    let terminal_state = if exit_code == 0 {
        "session_ended"
    } else {
        "process_gone"
    };
    let event = json!({
        "runtime_key": format!("claude:{provider_session_id}"),
        "session_id": session_id,
        "provider": "claude",
        "device_id": machine_name,
        "source": "claude_channel_wrapper",
        "kind": "terminal_signal",
        "occurred_at": occurred_at,
        "dedupe_key": format!("claude-terminal:{provider_session_id}:{exit_code}:{occurred_at}"),
        "payload": {
            "terminal_state": terminal_state,
            "terminal_reason": "provider_exit",
            "terminal_source": "claude_channel_wrapper",
            "provider_session_id": provider_session_id,
            "exit_code": exit_code,
        },
    });
    enqueue_runtime_event(
        &longhouse_home()?.join("agent/runtime-events-outbox"),
        &event,
    )
}

fn resolve_managed_claude_resume(
    runtime: &tokio::runtime::Runtime,
    url: &str,
    token: &str,
    session_id: &str,
) -> anyhow::Result<(ManagedLaunchResponse, PathBuf)> {
    validate_session_id(session_id).context("--resume must be a Longhouse session UUID")?;
    let endpoint = format!(
        "{}/api/agents/sessions/{session_id}",
        url.trim_end_matches('/')
    );
    runtime.block_on(async {
        let response = reqwest::Client::new()
            .get(endpoint)
            .header("X-Agents-Token", token)
            .send()
            .await?;
        match response.status().as_u16() {
            200 => {}
            401 => anyhow::bail!("Authentication failed. Run 'longhouse auth' to re-authenticate."),
            404 => anyhow::bail!("Session not found: {session_id}"),
            status => anyhow::bail!("Could not load Claude session {session_id}: HTTP {status}"),
        }
        let provider_session_id = response
            .headers()
            .get("X-Provider-Session-ID")
            .and_then(|value| value.to_str().ok())
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .context("Claude session has no provider resume identity yet")?
            .to_owned();
        Uuid::parse_str(&provider_session_id)
            .context("Claude session has an invalid provider resume identity")?;
        let payload: serde_json::Value = response.json().await?;
        if payload.get("provider").and_then(serde_json::Value::as_str) != Some("claude") {
            anyhow::bail!("--resume requires an existing Claude session");
        }
        let cwd = payload
            .get("cwd")
            .and_then(serde_json::Value::as_str)
            .map(PathBuf::from)
            .filter(|path| path.is_absolute() && path.is_dir())
            .context("Claude session workspace is unavailable")?;
        Ok((
            ManagedLaunchResponse {
                session_id: session_id.to_owned(),
                run_id: payload
                    .get("run_id")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
                provider_session_id: Some(provider_session_id),
                permission_mode: payload
                    .get("permission_mode")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_owned),
                hook_token: None,
                managed_transport: Some("claude_channel_bridge".into()),
            },
            cwd,
        ))
    })
}

fn claude_contract_path(session_id: &str) -> anyhow::Result<PathBuf> {
    validate_session_id(session_id)?;
    Ok(longhouse_home()?
        .join("managed-local/contracts/claude")
        .join(format!("{session_id}.json")))
}

fn record_claude_contract(session_id: &str, cwd: &Path, claude_bin: &str) -> anyhow::Result<()> {
    let path = claude_contract_path(session_id)?;
    let payload = json!({
        "schema_version": 1,
        "session_id": session_id,
        "provider": "claude",
        "launch_mode": "tui",
        "created_at": chrono::Utc::now().to_rfc3339(),
        "longhouse_build": build_identity::BuildIdentity::current().qualified(),
        "provider_binary": {"path": claude_bin, "source": "path", "version": serde_json::Value::Null},
        "workspace": {"cwd": cwd, "canonical_cwd": std::fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf()), "file_identity": serde_json::Value::Null},
        "control": {"kind": "claude_channel_bridge"},
    });
    let parent = path
        .parent()
        .context("managed Claude contract has no parent")?;
    std::fs::create_dir_all(parent)?;
    let temporary = path.with_extension(format!("json.tmp.{}", std::process::id()));
    std::fs::write(
        &temporary,
        format!("{}\n", serde_json::to_string_pretty(&payload)?),
    )?;
    std::fs::rename(temporary, path)?;
    Ok(())
}

fn remove_claude_contract(session_id: &str) -> anyhow::Result<()> {
    let path = claude_contract_path(session_id)?;
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => {
            Err(error).with_context(|| format!("remove managed Claude contract {}", path.display()))
        }
    }
}

fn enqueue_runtime_event(dir: &Path, event: &serde_json::Value) -> anyhow::Result<()> {
    std::fs::create_dir_all(dir)?;
    let temporary = dir.join(format!(".{}.tmp", Uuid::new_v4()));
    let ready = dir.join(format!("{}.json", Uuid::new_v4()));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(&serde_json::to_vec(event)?)?;
    file.sync_all()?;
    drop(file);
    std::fs::rename(temporary, ready)?;
    Ok(())
}

fn resolve_codex_config(
    url: Option<String>,
    token: Option<String>,
) -> anyhow::Result<(String, String, String)> {
    let machine_dir = longhouse_home()?.join("machine");
    let state: MachineState = std::fs::read(machine_dir.join("state.json"))
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or(MachineState {
            runtime_url: None,
            machine_name: None,
        });
    let url = url
        .or(state.runtime_url)
        .filter(|value| !value.trim().is_empty())
        .context("No Longhouse URL configured. Run `longhouse auth` first.")?;
    let token = token
        .or_else(|| std::fs::read_to_string(machine_dir.join("device-token")).ok())
        .filter(|value| !value.trim().is_empty())
        .context("No device token found. Run `longhouse auth` first.")?;
    let machine_name = state
        .machine_name
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| {
            Command::new("hostname")
                .output()
                .ok()
                .and_then(|output| String::from_utf8(output.stdout).ok())
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| "unknown".into())
        });
    Ok((url, token.trim().to_string(), machine_name))
}

fn resolve_codex_binary(explicit: Option<String>) -> anyhow::Result<String> {
    resolve_provider_binary(
        explicit.or_else(|| std::env::var("LONGHOUSE_CODEX_BIN").ok()),
        "codex",
        "Codex",
        "--codex-bin",
    )
}

fn resolve_provider_binary(
    explicit: Option<String>,
    default: &str,
    label: &str,
    flag: &str,
) -> anyhow::Result<String> {
    let candidate = explicit.unwrap_or_else(|| default.into());
    let path = PathBuf::from(&candidate);
    if path.components().count() > 1 {
        return path
            .is_file()
            .then(|| path.display().to_string())
            .with_context(|| format!("{flag} is not an executable file"));
    }
    for dir in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        let found = dir.join(&candidate);
        if found.is_file() {
            return Ok(found.display().to_string());
        }
    }
    anyhow::bail!(
        "{label} executable not found. Install stock `{default}` or set an explicit binary path."
    )
}

fn ensure_claude_channel_prerequisite(binary: &str) -> anyhow::Result<()> {
    let output = Command::new(binary)
        .args(["auth", "status", "--json"])
        .output()
        .with_context(|| format!("run {binary} auth status"))?;
    if !output.status.success() {
        anyhow::bail!(
            "Claude native channels unavailable: `claude auth status` exited {}",
            output.status
        );
    }
    let status: serde_json::Value = serde_json::from_slice(&output.stdout)
        .context("Claude auth status returned invalid JSON")?;
    if status.get("loggedIn").and_then(serde_json::Value::as_bool) != Some(true) {
        anyhow::bail!("Claude native channels unavailable: Claude is not logged in");
    }
    Ok(())
}

fn interactive_human_shell_provenance() -> (Option<&'static str>, Option<&'static str>) {
    let hidden = std::env::var("LONGHOUSE_ORIGIN_KIND")
        .ok()
        .is_some_and(|value| !value.trim().is_empty());
    let sidechain = matches!(
        std::env::var("LONGHOUSE_IS_SIDECHAIN")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    );
    if std::io::stdin().is_terminal() && std::io::stdout().is_terminal() && !hidden && !sidechain {
        (Some("human_shell"), Some("terminal"))
    } else {
        (None, None)
    }
}

fn launch_managed_codex(args: CodexLaunchArgs) -> anyhow::Result<()> {
    let cwd = std::fs::canonicalize(&args.cwd)
        .with_context(|| format!("resolve {}", args.cwd.display()))?;
    let (url, token, machine_name) = resolve_codex_config(args.url, args.token)?;
    let codex_bin = resolve_codex_binary(args.codex_bin)?;
    let git = |args: &[&str]| {
        Command::new("git")
            .arg("-C")
            .arg(&cwd)
            .args(args)
            .output()
            .ok()
            .filter(|output| output.status.success())
            .and_then(|output| String::from_utf8(output.stdout).ok())
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    };
    let payload = json!({
        "cwd": cwd, "provider": "codex", "project": args.project, "git_repo": git(&["rev-parse", "--show-toplevel"]),
        "git_branch": git(&["rev-parse", "--abbrev-ref", "HEAD"]), "display_name": args.name,
        "loop_mode": args.loop_mode, "machine_name": machine_name, "permission_mode": "bypass"
    });
    let endpoint = format!(
        "{}/api/sessions/managed-local/this-device",
        url.trim_end_matches('/')
    );
    let runtime = tokio::runtime::Runtime::new()?;
    let response: ManagedLaunchResponse = runtime.block_on(async {
        let response = reqwest::Client::new()
            .post(&endpoint)
            .header("X-Agents-Token", &token)
            .json(&payload)
            .send()
            .await?;
        if !response.status().is_success() {
            anyhow::bail!(
                "managed Codex launch failed ({}): {}",
                response.status(),
                response.text().await.unwrap_or_default()
            );
        }
        Ok::<_, anyhow::Error>(response.json().await?)
    })?;
    if response.run_id.trim().is_empty() {
        anyhow::bail!("Longhouse server did not return the managed run identity");
    }
    let attach = args.attach && !args.no_attach && interactive_stdio();
    let launch_mode = if attach { "tui" } else { "detached_ui" };
    let engine = paired_engine_path()?;
    let mut bridge = Command::new(&engine);
    bridge
        .args([
            "codex-bridge",
            "start",
            "--session-id",
            &response.session_id,
            "--run-id",
            &response.run_id,
            "--cwd",
        ])
        .arg(&cwd)
        .args([
            "--url",
            &url,
            "--codex-bin",
            &codex_bin,
            "--launch-mode",
            launch_mode,
            "--json",
        ])
        .env("LONGHOUSE_CODEX_BRIDGE_TOKEN", &token);
    if !attach {
        bridge.arg("--create-initial-thread");
    }
    if let Some(model) = &args.model {
        bridge.args(["--model", model]);
    }
    if let Some(effort) = &args.model_reasoning_effort {
        bridge.args(["--model-reasoning-effort", effort]);
    }
    let output = bridge.output().context("start native Codex bridge")?;
    if !output.status.success() {
        anyhow::bail!(
            "Codex bridge failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let bridge: BridgeStartResponse =
        serde_json::from_slice(&output.stdout).context("parse native Codex bridge response")?;
    if !attach
        && bridge
            .thread_id
            .as_deref()
            .is_none_or(|thread| thread.trim().is_empty())
    {
        let _ = stop_codex_bridge(&response.session_id, "bridge_start_failed");
        anyhow::bail!("Native Codex bridge did not return thread_id for detached launch");
    }
    println!(
        "Managed Codex ready\n→ {}/s/{}",
        url.trim_end_matches('/'),
        response
            .session_id
            .split('-')
            .next()
            .unwrap_or(&response.session_id)
    );
    if !attach {
        if args.attach && !args.no_attach {
            eprintln!("Skipping auto-attach because stdin/stdout are not TTYs.");
        }
        println!(
            "Attach: longhouse codex attach --session-id {}",
            response.session_id
        );
        return Ok(());
    }
    if let Err(error) = record_codex_contract(&response.session_id, &cwd, &codex_bin, launch_mode) {
        eprintln!("Longhouse warning: could not record managed-session contract: {error}");
    }
    let tui_result = run_codex_tui_with_recovery(
        &codex_bin,
        &bridge.ws_url,
        &cwd,
        &response.session_id,
        args.model.as_deref(),
        args.model_reasoning_effort.as_deref(),
        args.dangerously_bypass_approvals_and_sandbox,
    );
    let stop_result = stop_codex_bridge(&response.session_id, "clean_tui_exit");
    let exit = tui_result?;
    stop_result?;
    if let Err(error) = remove_codex_contract(&response.session_id) {
        eprintln!("Longhouse warning: could not remove managed-session contract: {error}");
    }
    if exit == 0 {
        print_helm_closed(&machine_name);
    }
    if exit != 0 {
        std::process::exit(exit);
    }
    Ok(())
}

fn run_codex_tui(
    codex_bin: &str,
    ws_url: &str,
    cwd: &Path,
    session_id: &str,
    model: Option<&str>,
    effort: Option<&str>,
    bypass: bool,
) -> anyhow::Result<i32> {
    let mut command = Command::new(codex_bin);
    command.args(["-c", "check_for_update_on_startup=false"]);
    if let Some(effort) = effort {
        command.args(["-c", &format!("model_reasoning_effort={effort}")]);
    }
    if let Some(model) = model {
        command.args(["--model", model]);
    }
    if bypass {
        command.arg("--dangerously-bypass-approvals-and-sandbox");
    }
    command
        .args(["--enable", "tui_app_server", "--remote", ws_url])
        .current_dir(cwd)
        .env("LONGHOUSE_MANAGED_SESSION_ID", session_id);
    run_foreground_command(&mut command).context("run stock Codex TUI")
}

fn run_codex_tui_with_recovery(
    codex_bin: &str,
    ws_url: &str,
    cwd: &Path,
    session_id: &str,
    model: Option<&str>,
    effort: Option<&str>,
    bypass: bool,
) -> anyhow::Result<i32> {
    let exit = run_codex_tui(codex_bin, ws_url, cwd, session_id, model, effort, bypass)?;
    if exit != 1 || !codex_bridge_reattachable(session_id) {
        return Ok(exit);
    }
    eprintln!("Codex terminal exited with code 1; reattaching to the healthy managed session…");
    run_codex_tui(codex_bin, ws_url, cwd, session_id, model, effort, bypass)
}

fn codex_bridge_reattachable(session_id: &str) -> bool {
    let Ok(path) = codex_bridge_state_path(session_id) else {
        return false;
    };
    let Some(state) = std::fs::read(path)
        .ok()
        .and_then(|raw| serde_json::from_slice::<BridgeState>(&raw).ok())
    else {
        return false;
    };
    if state.status.as_deref() != Some("ready")
        || state
            .thread_id
            .as_deref()
            .is_none_or(|thread| thread.trim().is_empty())
    {
        return false;
    }
    bridge_readyz_healthy(state.ws_url.as_deref())
}

fn bridge_readyz_healthy(ws_url: Option<&str>) -> bool {
    let Some(ws_url) = ws_url.filter(|value| !value.trim().is_empty()) else {
        return false;
    };
    let Ok(mut readyz_url) = reqwest::Url::parse(ws_url) else {
        return false;
    };
    match readyz_url.scheme() {
        "ws" => {
            if readyz_url.set_scheme("http").is_err() {
                return false;
            }
        }
        "wss" => {
            if readyz_url.set_scheme("https").is_err() {
                return false;
            }
        }
        "http" | "https" => {}
        _ => return false,
    }
    let path = readyz_url.path().trim_end_matches('/');
    readyz_url.set_path(&format!("{path}/readyz"));
    readyz_url.set_query(None);
    tokio::runtime::Runtime::new()
        .ok()
        .and_then(|runtime| {
            runtime.block_on(async {
                reqwest::Client::builder()
                    .timeout(Duration::from_secs(1))
                    .build()
                    .ok()?
                    .get(readyz_url)
                    .send()
                    .await
                    .ok()
                    .map(|response| response.status().is_success())
            })
        })
        .unwrap_or(false)
}

fn interactive_stdio() -> bool {
    std::io::stdin().is_terminal() && std::io::stdout().is_terminal()
}

fn wait_for_child_or_signal(
    child: &mut std::process::Child,
    signal: &Arc<AtomicUsize>,
    process_group: Option<libc::pid_t>,
) -> anyhow::Result<i32> {
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(status.code().unwrap_or(1));
        }
        let received = signal.load(Ordering::Relaxed);
        if received != 0 {
            terminate_child(child, process_group);
            let _ = child.wait();
            return Ok(128 + received as i32);
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}

#[cfg(unix)]
fn terminate_child(child: &mut std::process::Child, process_group: Option<libc::pid_t>) {
    if let Some(group) = process_group {
        unsafe {
            libc::kill(-group, libc::SIGTERM);
        }
    } else {
        let _ = child.kill();
    }
}

#[cfg(not(unix))]
fn terminate_child(child: &mut std::process::Child, _process_group: Option<libc::pid_t>) {
    let _ = child.kill();
}

fn install_tui_signal_flag() -> anyhow::Result<Arc<AtomicUsize>> {
    let flag = Arc::new(AtomicUsize::new(0));
    for signal in [libc::SIGHUP, libc::SIGTERM, libc::SIGINT] {
        signal_hook::flag::register_usize(signal, Arc::clone(&flag), signal as usize)
            .context("install managed Codex signal cleanup")?;
    }
    Ok(flag)
}

fn print_helm_closed(machine_name: &str) {
    // This is deliberately a receipt, not a liveness claim. Helm owns the
    // provider process only while its terminal is attached; the thread remains
    // durable history after the bridge is stopped.
    println!(
        "\n╭─ ⬡ Longhouse — Session closed ──────────────────╮\n│                                                  │\n│   ░▒▓  🔥  The hearth is banked  🔥  ▓▒░       │\n│                                                  │\n│   This Helm has ended on {machine_name:<20}│\n│   The thread is safely saved in Longhouse.       │\n│                                                  │\n│   ✦  Until next time                             │\n│                                                  │\n╰──────────────────────────────────────────────────╯"
    );
}

#[cfg(unix)]
fn run_foreground_command(command: &mut Command) -> anyhow::Result<i32> {
    use std::os::unix::io::AsRawFd;
    use std::os::unix::process::CommandExt;

    unsafe {
        command.pre_exec(|| {
            // Signal-hook handlers are installed in the facade after a first
            // child launches. Reset in every provider child so stock Codex
            // keeps its normal Ctrl-C/HUP behavior on reattach.
            for signal in [libc::SIGHUP, libc::SIGTERM, libc::SIGINT] {
                libc::signal(signal, libc::SIG_DFL);
            }
            if libc::setpgid(0, 0) == 0 {
                Ok(())
            } else {
                Err(std::io::Error::last_os_error())
            }
        });
    }
    let mut child = command.spawn()?;
    let signal = install_tui_signal_flag()?;
    let child_pgrp = child.id() as libc::pid_t;
    unsafe {
        libc::setpgid(child_pgrp, child_pgrp);
    }
    if !interactive_stdio() {
        return wait_for_child_or_signal(&mut child, &signal, Some(child_pgrp));
    }
    let stdin_fd = std::io::stdin().as_raw_fd();
    let parent_pgrp = unsafe { libc::getpgrp() };
    let old_sigttou = unsafe { libc::signal(libc::SIGTTOU, libc::SIG_IGN) };
    let handed_off = unsafe { libc::tcsetpgrp(stdin_fd, child_pgrp) == 0 };
    let status = wait_for_child_or_signal(&mut child, &signal, Some(child_pgrp));
    if handed_off {
        unsafe {
            libc::tcsetpgrp(stdin_fd, parent_pgrp);
        }
    }
    unsafe {
        libc::signal(libc::SIGTTOU, old_sigttou);
    }
    status
}

#[cfg(not(unix))]
fn run_foreground_command(command: &mut Command) -> anyhow::Result<i32> {
    let mut child = command.spawn()?;
    let signal = install_tui_signal_flag()?;
    wait_for_child_or_signal(&mut child, &signal, None)
}

fn stop_codex_bridge(session_id: &str, reason: &str) -> anyhow::Result<()> {
    validate_session_id(session_id)?;
    let mut child = Command::new(paired_engine_path()?)
        .args([
            "codex-bridge",
            "stop",
            "--session-id",
            session_id,
            "--reason",
            reason,
            "--force",
        ])
        .spawn()
        .context("start managed Codex bridge cleanup")?;
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            anyhow::bail!("managed Codex bridge cleanup timed out after 2 seconds");
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    if !status.success() {
        anyhow::bail!("failed to stop managed Codex bridge");
    }
    Ok(())
}

fn validate_session_id(session_id: &str) -> anyhow::Result<()> {
    Uuid::parse_str(session_id).context("--session-id must be a Longhouse session UUID")?;
    Ok(())
}

fn codex_bridge_state_path(session_id: &str) -> anyhow::Result<PathBuf> {
    validate_session_id(session_id)?;
    Ok(longhouse_home()?
        .join("managed-local/codex-bridge")
        .join(format!("{session_id}.json")))
}

fn codex_contract_path(session_id: &str) -> anyhow::Result<PathBuf> {
    validate_session_id(session_id)?;
    Ok(longhouse_home()?
        .join("managed-local/contracts/codex")
        .join(format!("{session_id}.json")))
}

fn record_codex_contract(
    session_id: &str,
    cwd: &Path,
    codex_bin: &str,
    launch_mode: &str,
) -> anyhow::Result<()> {
    let path = codex_contract_path(session_id)?;
    let canonical_cwd = std::fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    let payload = json!({
        "schema_version": 1,
        "session_id": session_id,
        "provider": "codex",
        "launch_mode": launch_mode,
        "created_at": chrono::Utc::now().to_rfc3339(),
        "longhouse_build": build_identity::BuildIdentity::current().qualified(),
        "provider_binary": {
            "path": codex_bin,
            "source": "path",
            "version": serde_json::Value::Null,
        },
        "workspace": {
            "cwd": cwd,
            "canonical_cwd": canonical_cwd,
            "file_identity": serde_json::Value::Null,
        },
        "control": {
            "kind": "codex_bridge",
            "state_path": codex_bridge_state_path(session_id)?,
        },
    });
    let parent = path
        .parent()
        .context("managed Codex contract has no parent")?;
    std::fs::create_dir_all(parent).with_context(|| {
        format!(
            "create managed Codex contract directory {}",
            parent.display()
        )
    })?;
    let temporary = path.with_extension(format!("json.tmp.{}", std::process::id()));
    std::fs::write(
        &temporary,
        format!("{}\n", serde_json::to_string_pretty(&payload)?),
    )
    .with_context(|| format!("write managed Codex contract {}", temporary.display()))?;
    std::fs::rename(&temporary, &path)
        .with_context(|| format!("install managed Codex contract {}", path.display()))?;
    Ok(())
}

fn remove_codex_contract(session_id: &str) -> anyhow::Result<()> {
    let path = codex_contract_path(session_id)?;
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => {
            Err(error).with_context(|| format!("remove managed Codex contract {}", path.display()))
        }
    }
}

fn attach_managed_codex(args: CodexAttachArgs) -> anyhow::Result<()> {
    let state_path = codex_bridge_state_path(&args.session_id)?;
    let state: BridgeState = serde_json::from_slice(
        &std::fs::read(&state_path).with_context(|| format!("read {}", state_path.display()))?,
    )?;
    let ws_url = state
        .ws_url
        .filter(|url| !url.trim().is_empty())
        .context("managed Codex session is not reattachable")?;
    let codex_bin = resolve_codex_binary(args.codex_bin.or(Some(state.codex_bin)))?;
    if let Err(error) =
        record_codex_contract(&args.session_id, Path::new(&state.cwd), &codex_bin, "tui")
    {
        eprintln!("Longhouse warning: could not record managed-session contract: {error}");
    }
    let tui_result = run_codex_tui_with_recovery(
        &codex_bin,
        &ws_url,
        Path::new(&state.cwd),
        &args.session_id,
        args.model.as_deref(),
        args.model_reasoning_effort.as_deref(),
        args.dangerously_bypass_approvals_and_sandbox,
    );
    let stop_result = stop_codex_bridge(&args.session_id, "clean_tui_exit");
    let exit = tui_result?;
    stop_result?;
    if let Err(error) = remove_codex_contract(&args.session_id) {
        eprintln!("Longhouse warning: could not remove managed-session contract: {error}");
    }
    if exit == 0 {
        print_helm_closed("this machine");
    }
    if exit != 0 {
        std::process::exit(exit);
    }
    Ok(())
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli
        .command
        .unwrap_or(Commands::BuildIdentity { json: false })
    {
        Commands::BuildIdentity { json } => {
            let pair = pair_identity()?;
            if json {
                println!("{}", serde_json::to_string_pretty(&pair)?);
            } else {
                println!("{}", pair.facade.qualified());
            }
        }
        Commands::VerifyPair => {
            let pair = pair_identity()?;
            println!(
                "paired engine: {} ({})",
                pair.engine_path, pair.facade.commit_short
            );
        }
        Commands::Auth(args) => native_auth(args)?,
        Commands::LocalHealth(args) => native_local_health(args)?,
        Commands::Machine { command } => match command {
            MachineCommand::Repair(args) => native_machine_repair(args)?,
        },
        Commands::Claude { command, launch } => match command {
            Some(ClaudeCommand::Configure { claude_dir }) => configure_claude_hooks(claude_dir)?,
            None => launch_managed_claude(launch)?,
        },
        Commands::Codex { command, launch } => match command {
            Some(CodexCommand::Launch(args)) => launch_managed_codex(args)?,
            Some(CodexCommand::Attach(args)) => attach_managed_codex(args)?,
            Some(CodexCommand::Stop(args)) => {
                stop_codex_bridge(&args.session_id, "bridge_stop")?;
                if let Err(error) = remove_codex_contract(&args.session_id) {
                    eprintln!(
                        "Longhouse warning: could not remove managed-session contract: {error}"
                    );
                }
            }
            None => launch_managed_codex(launch)?,
        },
        Commands::Opencode { command, launch } => match command {
            Some(OpencodeCommand::Attach(args)) => attach_managed_opencode(args)?,
            Some(OpencodeCommand::Stop(args)) => stop_opencode_bridge(&args.session_id, None)?,
            None => launch_managed_opencode(launch)?,
        },
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codex_parser_keeps_python_no_attach_spelling() {
        let cli = Cli::try_parse_from(["longhouse", "codex", "--no-attach"]).unwrap();
        let Commands::Codex { command, launch } = cli.command.unwrap() else {
            panic!("expected codex command");
        };
        assert!(command.is_none());
        assert!(launch.no_attach);
        assert!(launch.attach);
    }

    #[test]
    fn local_health_parser_keeps_desktop_fast_shape() {
        let cli = Cli::try_parse_from(["longhouse", "local-health", "--fast", "--json"]).unwrap();
        let Commands::LocalHealth(args) = cli.command.unwrap() else {
            panic!("expected local-health command");
        };
        assert!(args.fast);
        assert!(args.json);
    }

    #[test]
    fn auth_parser_keeps_tokens_out_of_argv() {
        let cli =
            Cli::try_parse_from(["longhouse", "auth", "--url", "https://example.test"]).unwrap();
        let Commands::Auth(args) = cli.command.unwrap() else {
            panic!("expected auth command");
        };
        assert_eq!(args.token_env, "LONGHOUSE_DEVICE_TOKEN");
        assert_eq!(args.url.as_deref(), Some("https://example.test"));
    }

    #[test]
    fn longhouse_home_maps_claude_config_dir_to_its_sibling() {
        let temp = tempfile::tempdir().unwrap();
        let provider_home = temp.path().join(".claude");
        temp_env::with_vars(
            [
                ("LONGHOUSE_HOME", None::<String>),
                (
                    "CLAUDE_CONFIG_DIR",
                    Some(provider_home.display().to_string()),
                ),
                ("HOME", Some(temp.path().join("home").display().to_string())),
            ],
            || assert_eq!(longhouse_home().unwrap(), temp.path().join(".longhouse")),
        );
    }

    #[test]
    fn codex_contract_is_written_and_removed_at_the_native_path() {
        let temp = tempfile::tempdir().unwrap();
        let cwd = temp.path().join("workspace");
        std::fs::create_dir_all(&cwd).unwrap();
        let session_id = "11111111-1111-4111-8111-111111111111";
        temp_env::with_vars(
            [
                ("LONGHOUSE_HOME", Some(temp.path().display().to_string())),
                ("CLAUDE_CONFIG_DIR", None::<String>),
            ],
            || {
                record_codex_contract(session_id, &cwd, "/usr/bin/codex", "detached_ui").unwrap();
                let path = codex_contract_path(session_id).unwrap();
                let payload: serde_json::Value =
                    serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
                assert_eq!(payload["provider"], "codex");
                assert_eq!(payload["launch_mode"], "detached_ui");
                assert_eq!(payload["control"]["kind"], "codex_bridge");
                remove_codex_contract(session_id).unwrap();
                assert!(!path.exists());
            },
        );
    }

    #[test]
    fn claude_configure_places_hooks_and_mcp_at_the_provider_paths() {
        let temp = tempfile::tempdir().unwrap();
        let claude_dir = temp.path().join(".claude");
        let engine = temp.path().join("longhouse engine");
        std::fs::write(&engine, "").unwrap();
        std::fs::create_dir_all(claude_dir.join("hooks")).unwrap();
        std::fs::write(
            claude_dir.join("hooks/longhouse-permission-gate.py"),
            "legacy",
        )
        .unwrap();
        std::fs::write(claude_dir.join("hooks/longhouse-hook.sh"), "legacy").unwrap();
        temp_env::with_var(
            "LONGHOUSE_ENGINE_BIN",
            Some(engine.display().to_string()),
            || {
                configure_claude_hooks(Some(claude_dir.clone())).unwrap();
                configure_claude_hooks(Some(claude_dir.clone())).unwrap();
            },
        );
        let settings: serde_json::Value =
            serde_json::from_slice(&std::fs::read(claude_dir.join("settings.json")).unwrap())
                .unwrap();
        assert!(settings["hooks"]["PreToolUse"]
            .to_string()
            .contains("claude-permission-gate"));
        assert_eq!(
            settings["hooks"]["PreToolUse"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|entry| entry.to_string().contains("claude-permission-gate"))
                .count(),
            1
        );
        assert!(settings["hooks"]["PreToolUse"]
            .to_string()
            .contains("longhouse engine"));
        assert!(settings["hooks"]["SessionStart"]
            .to_string()
            .contains("claude-lifecycle-hook"));
        let user_config: serde_json::Value =
            serde_json::from_slice(&std::fs::read(temp.path().join(".claude.json")).unwrap())
                .unwrap();
        assert_eq!(
            user_config["mcpServers"]["longhouse-channel"]["args"][0],
            "claude-channel"
        );
        assert!(!claude_dir
            .join("hooks/longhouse-permission-gate.py")
            .exists());
        assert!(!claude_dir.join("hooks/longhouse-hook.sh").exists());
    }

    #[test]
    fn claude_parser_keeps_config_dir_alias() {
        let cli =
            Cli::try_parse_from(["longhouse", "claude", "--config-dir", "/tmp/claude"]).unwrap();
        let Commands::Claude { command, launch } = cli.command.unwrap() else {
            panic!("expected claude command");
        };
        assert!(command.is_none());
        assert_eq!(launch.claude_dir.unwrap(), PathBuf::from("/tmp/claude"));
    }

    #[test]
    fn claude_exit_is_queued_as_a_terminal_runtime_event() {
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var(
            "LONGHOUSE_HOME",
            Some(temp.path().display().to_string()),
            || {
                record_claude_terminal_event("session", "provider", "device", 0).unwrap();
            },
        );
        let event_path = std::fs::read_dir(temp.path().join("agent/runtime-events-outbox"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        let event: serde_json::Value =
            serde_json::from_slice(&std::fs::read(event_path).unwrap()).unwrap();
        assert_eq!(event["kind"], "terminal_signal");
        assert_eq!(event["payload"]["terminal_state"], "session_ended");
    }

    #[test]
    fn claude_contract_is_written_and_removed_at_the_native_path() {
        let temp = tempfile::tempdir().unwrap();
        let cwd = temp.path().join("workspace");
        std::fs::create_dir_all(&cwd).unwrap();
        let session_id = "11111111-1111-4111-8111-111111111111";
        temp_env::with_var(
            "LONGHOUSE_HOME",
            Some(temp.path().display().to_string()),
            || {
                record_claude_contract(session_id, &cwd, "/usr/bin/claude").unwrap();
                let path = claude_contract_path(session_id).unwrap();
                let payload: serde_json::Value =
                    serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
                assert_eq!(payload["provider"], "claude");
                assert_eq!(payload["control"]["kind"], "claude_channel_bridge");
                remove_claude_contract(session_id).unwrap();
                assert!(!path.exists());
            },
        );
    }

    #[test]
    fn shell_quotes_hook_paths() {
        assert_eq!(
            shell_quote_path(Path::new("/Applications/Longhouse Engine/bin")),
            "'/Applications/Longhouse Engine/bin'"
        );
    }
}
