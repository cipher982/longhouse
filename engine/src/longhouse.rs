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
use std::io::IsTerminal;
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
    /// Launch or manage a native Longhouse Codex Helm session.
    #[command(args_conflicts_with_subcommands = true)]
    Codex {
        #[command(subcommand)]
        command: Option<CodexCommand>,
        #[command(flatten)]
        launch: CodexLaunchArgs,
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
}

#[derive(Deserialize)]
struct BridgeStartResponse {
    ws_url: String,
}

#[derive(Deserialize)]
struct BridgeState {
    cwd: String,
    codex_bin: String,
    ws_url: Option<String>,
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
    let candidate = explicit
        .or_else(|| std::env::var("LONGHOUSE_CODEX_BIN").ok())
        .unwrap_or_else(|| "codex".into());
    let path = PathBuf::from(&candidate);
    if path.components().count() > 1 {
        return path
            .is_file()
            .then(|| path.display().to_string())
            .context("--codex-bin is not an executable file");
    }
    for dir in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        let found = dir.join(&candidate);
        if found.is_file() {
            return Ok(found.display().to_string());
        }
    }
    anyhow::bail!("Codex executable not found. Install stock `codex` or set LONGHOUSE_CODEX_BIN / --codex-bin.")
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
    print_helm_closed(&machine_name);
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
    std::fs::read(path)
        .ok()
        .and_then(|raw| serde_json::from_slice::<BridgeState>(&raw).ok())
        .and_then(|state| state.ws_url)
        .is_some_and(|url| !url.trim().is_empty())
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
    let status = Command::new(paired_engine_path()?)
        .args([
            "codex-bridge",
            "stop",
            "--session-id",
            session_id,
            "--reason",
            reason,
            "--force",
        ])
        .status()?;
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
    print_helm_closed("this machine");
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
}
