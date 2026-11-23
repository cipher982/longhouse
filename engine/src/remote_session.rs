//! Persistent remote agent sessions over SSH + `dtach` with Warp status replay.
//!
//! Launches or reattaches interactive `longhouse <provider>` sessions on a
//! remote host without coupling hostnames or private aliases into Longhouse.

use std::fs::{File, OpenOptions};
use std::io::{IsTerminal, Write};
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::Context;
use clap::{Args, Subcommand};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::managed_identity_contract::ManagedProvider;
use crate::warp_cli_agent;

const MAX_SESSION_NAME_LEN: usize = 64;
const MAX_SOCKET_PATH_BYTES: usize = 100;

#[derive(Args, Debug, Clone, Default)]
pub struct RemoteTargetArgs {
    /// Run or reattach this session on a remote SSH target instead of locally.
    #[arg(long, value_name = "SSH_TARGET")]
    pub on: Option<String>,
    /// Attach to a specific named remote session instead of auto-selecting one.
    #[arg(long, requires = "on", value_name = "NAME")]
    pub remote_attach: Option<String>,
    /// Override the base name used for the remote session socket.
    #[arg(long, requires = "on", value_name = "NAME")]
    pub remote_name: Option<String>,
    /// Fast-forward the remote folder's clean git checkout, or each clean repo directly inside a folder that is not one, before launching a new session.
    #[arg(long, requires = "on")]
    pub pull: bool,
}

#[derive(Subcommand, Debug, Clone)]
pub enum RemoteCommand {
    /// List active persistent sessions on a remote host.
    Ls {
        #[arg(long, value_name = "SSH_TARGET")]
        on: String,
        #[arg(long)]
        json: bool,
    },
    /// Attach to a named persistent session on a remote host.
    Attach {
        name: String,
        #[arg(long, value_name = "SSH_TARGET")]
        on: String,
    },
}

#[derive(Args, Debug, Clone)]
pub struct RemoteSessionArgs {
    #[arg(long)]
    pub cwd: String,
    #[arg(long)]
    pub provider: String,
    #[arg(long)]
    pub attach: Option<String>,
    #[arg(long)]
    pub remote_name: Option<String>,
    #[arg(long)]
    pub pull: bool,
    #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
    pub provider_args: Vec<String>,
}

#[derive(Args, Debug, Clone)]
pub struct RemoteHostArgs {
    #[arg(long)]
    pub state_file: PathBuf,
    #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
    pub args: Vec<String>,
}

#[derive(Args, Debug, Clone)]
pub struct RemoteListArgs {
    #[arg(long)]
    pub json: bool,
}

pub struct RemoteLaunchSpec {
    pub provider: ManagedProvider,
    pub cwd: PathBuf,
    pub remote: RemoteTargetArgs,
    pub provider_args: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RemoteSessionRecord {
    name: String,
    provider: String,
    cwd: String,
    created_at: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
struct RemoteListEntry {
    name: String,
    provider: String,
    state: String,
    viewers: usize,
    cwd: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    session_id: Option<String>,
}

fn interactive_stdio() -> bool {
    std::io::stdin().is_terminal() && std::io::stdout().is_terminal()
}

pub fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

pub fn sanitize_session_base(raw: &str) -> String {
    let mut cleaned = String::with_capacity(raw.len());
    for ch in raw.chars() {
        if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-') {
            cleaned.push(ch);
        } else {
            cleaned.push('_');
        }
    }
    let trimmed = cleaned.trim_matches(|ch: char| ch == '.' || ch == '-');
    let result = if trimmed.is_empty() {
        "session".to_string()
    } else {
        trimmed.to_string()
    };
    result.chars().take(48).collect()
}

pub fn validate_session_name(name: &str) -> anyhow::Result<()> {
    if name.is_empty() || name.len() > MAX_SESSION_NAME_LEN {
        anyhow::bail!("session name must be 1..={MAX_SESSION_NAME_LEN} characters: {name:?}");
    }
    if name == "." || name == ".." || name.contains("..") {
        anyhow::bail!("invalid session name {name:?}");
    }
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        anyhow::bail!("empty session name");
    };
    if !first.is_ascii_alphanumeric() {
        anyhow::bail!("session name must start with an ASCII letter or digit: {name:?}");
    }
    if !chars.all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-')) {
        anyhow::bail!("session name contains unsupported characters: {name:?}");
    }
    Ok(())
}

pub fn map_cwd_for_remote(cwd: &Path, local_home: Option<&Path>) -> anyhow::Result<String> {
    let resolved = std::fs::canonicalize(cwd)
        .with_context(|| format!("resolve working directory {}", cwd.display()))?;
    if let Some(home) = local_home.and_then(|dir| std::fs::canonicalize(dir).ok()) {
        if resolved == home {
            return Ok("~".to_string());
        }
        if let Ok(rel) = resolved.strip_prefix(&home) {
            return Ok(format!("~/{}", rel.to_string_lossy()));
        }
    }
    Ok(resolved.to_string_lossy().into_owned())
}

pub fn expand_remote_cwd(raw: &str, remote_home: &Path) -> PathBuf {
    if raw == "~" {
        return remote_home.to_path_buf();
    }
    if let Some(rest) = raw.strip_prefix("~/") {
        return remote_home.join(rest);
    }
    PathBuf::from(raw)
}

fn local_home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

fn remote_sessions_root() -> anyhow::Result<PathBuf> {
    let root = crate::longhouse_home()?.join("remote-sessions");
    ensure_private_dir(&root)?;
    Ok(root)
}

fn ensure_private_dir(path: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(path)?;
    #[cfg(unix)]
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    Ok(())
}

fn write_private_file(path: &Path, contents: &[u8]) -> std::io::Result<()> {
    let tmp = path.with_extension(format!("tmp.{}", Uuid::new_v4().simple()));
    std::fs::write(&tmp, contents)?;
    #[cfg(unix)]
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
    if let Err(err) = std::fs::rename(&tmp, path) {
        let _ = std::fs::remove_file(&tmp);
        return Err(err);
    }
    Ok(())
}

fn collect_forwarded_env(provider: Option<ManagedProvider>) -> Vec<(String, String)> {
    let mut env = Vec::new();
    let colorterm = std::env::var("COLORTERM")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "truecolor".to_string());
    env.push(("COLORTERM".to_string(), colorterm));
    for key in [
        "TERM_PROGRAM",
        "WARP_CLI_AGENT_PROTOCOL_VERSION",
        "WARP_CLIENT_VERSION",
    ] {
        if let Ok(val) = std::env::var(key) {
            if !val.trim().is_empty() {
                env.push((key.to_string(), val));
            }
        }
    }
    if provider == Some(ManagedProvider::Claude) {
        if let Ok(model) = std::env::var("ANTHROPIC_MODEL") {
            if !model.trim().is_empty() {
                env.push(("ANTHROPIC_MODEL".to_string(), model));
            }
        }
    }
    env
}

pub fn build_remote_shell_command(
    env_vars: &[(String, String)],
    longhouse_args: &[String],
) -> String {
    let mut parts =
        vec!["export PATH=\"$HOME/.local/bin:$HOME/.nix-profile/bin:$HOME/bin:$PATH\"".to_string()];
    for (key, value) in env_vars {
        parts.push(format!("export {key}={}", shell_quote(value)));
    }
    let quoted_args: Vec<String> = longhouse_args.iter().map(|arg| shell_quote(arg)).collect();
    parts.push(format!("exec longhouse {}", quoted_args.join(" ")));
    parts.join("; ")
}

/// The git checkouts a session in `dir` works on, each with the prefix its
/// messages carry: `dir`'s own repo (no prefix), or, when `dir` is not inside
/// a repo, each direct child folder that is one, skipping names that start
/// with `.` or `_`.
fn git_checkouts(dir: &Path) -> Vec<(PathBuf, String)> {
    let is_repo = |path: &Path| {
        Command::new("git")
            .current_dir(path)
            .args(["rev-parse", "--git-dir"])
            .output()
            .is_ok_and(|out| out.status.success())
    };
    if is_repo(dir) {
        return vec![(dir.to_path_buf(), String::new())];
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut repos: Vec<(PathBuf, String)> = entries
        .flatten()
        .filter_map(|entry| {
            let name = entry.file_name().into_string().ok()?;
            let path = entry.path();
            (!name.starts_with(['.', '_']) && path.is_dir() && is_repo(&path))
                .then(|| (path, format!("{name}: ")))
        })
        .collect();
    repos.sort();
    repos
}

fn warn_local_git_divergence(cwd: &Path, target: &str, out: &mut impl Write) {
    for (repo, scope) in git_checkouts(cwd) {
        if let Ok(status) = Command::new("git")
            .current_dir(&repo)
            .args(["status", "--porcelain"])
            .output()
        {
            if status.status.success() {
                let dirty = String::from_utf8_lossy(&status.stdout)
                    .lines()
                    .filter(|line| !line.trim().is_empty())
                    .count();
                if dirty > 0 {
                    let _ = writeln!(
                        out,
                        "longhouse: {scope}local checkout has {dirty} uncommitted change(s); {target} will not see them."
                    );
                }
            }
        }
        if let Ok(rev) = Command::new("git")
            .current_dir(&repo)
            .args(["rev-list", "--count", "@{u}..HEAD"])
            .output()
        {
            if rev.status.success() {
                let ahead: usize = String::from_utf8_lossy(&rev.stdout)
                    .trim()
                    .parse()
                    .unwrap_or(0);
                if ahead > 0 {
                    let _ = writeln!(
                        out,
                        "longhouse: {scope}local checkout has {ahead} unpushed commit(s); push them for {target} to see them."
                    );
                }
            }
        }
    }
}

pub fn run_remote_launch(spec: RemoteLaunchSpec) -> anyhow::Result<i32> {
    if !interactive_stdio() {
        anyhow::bail!("remote longhouse sessions require an interactive terminal");
    }
    let target = spec
        .remote
        .on
        .as_deref()
        .context("missing --on target for remote launch")?;
    if let Some(attach) = spec.remote.remote_attach.as_deref() {
        validate_session_name(attach)?;
    }
    if let Some(remote_name) = spec.remote.remote_name.as_deref() {
        validate_session_name(remote_name)?;
    }
    let resolved_cwd = std::fs::canonicalize(&spec.cwd)
        .with_context(|| format!("resolve working directory {}", spec.cwd.display()))?;
    if spec.remote.remote_attach.is_none() {
        warn_local_git_divergence(&resolved_cwd, target, &mut std::io::stderr());
    }
    let mapped_cwd = map_cwd_for_remote(&resolved_cwd, local_home_dir().as_deref())?;
    let mut remote_args = vec![
        "__remote-session".to_string(),
        "--cwd".to_string(),
        mapped_cwd,
        "--provider".to_string(),
        spec.provider.as_str().to_string(),
    ];
    if let Some(attach) = spec.remote.remote_attach {
        remote_args.push("--attach".to_string());
        remote_args.push(attach);
    }
    if let Some(remote_name) = spec.remote.remote_name {
        remote_args.push("--remote-name".to_string());
        remote_args.push(remote_name);
    }
    if spec.remote.pull {
        remote_args.push("--pull".to_string());
    }
    if !spec.provider_args.is_empty() {
        remote_args.push("--".to_string());
        remote_args.extend(spec.provider_args);
    }
    let remote_cmd =
        build_remote_shell_command(&collect_forwarded_env(Some(spec.provider)), &remote_args);
    run_ssh(target, true, &remote_cmd)
}

pub fn run_remote_command(command: RemoteCommand) -> anyhow::Result<i32> {
    match command {
        RemoteCommand::Ls { on, json } => {
            let mut remote_args = vec!["__remote-list".to_string()];
            if json {
                remote_args.push("--json".to_string());
            }
            let cmd = build_remote_shell_command(&[], &remote_args);
            run_ssh(&on, false, &cmd)
        }
        RemoteCommand::Attach { name, on } => {
            if !interactive_stdio() {
                anyhow::bail!("longhouse remote attach requires an interactive terminal");
            }
            validate_session_name(&name)?;
            let remote_args = vec![
                "__remote-session".to_string(),
                "--cwd".to_string(),
                ".".to_string(),
                "--provider".to_string(),
                ManagedProvider::Claude.as_str().to_string(),
                "--attach".to_string(),
                name,
            ];
            let cmd = build_remote_shell_command(&collect_forwarded_env(None), &remote_args);
            run_ssh(&on, true, &cmd)
        }
    }
}

fn run_ssh(target: &str, allocate_tty: bool, remote_cmd: &str) -> anyhow::Result<i32> {
    let mut cmd = Command::new("ssh");
    if allocate_tty {
        cmd.arg("-t");
    }
    cmd.arg(target).arg("--").arg(remote_cmd);
    let status = cmd
        .status()
        .with_context(|| format!("run ssh to {target}"))?;
    Ok(status.code().unwrap_or(1))
}

fn state_file_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.json"))
}

fn warp_state_file_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.warp.json"))
}

fn socket_file_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.sock"))
}

fn alive_lock_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!("{name}.alive.lock"))
}

fn viewers_dir_path(root: &Path, name: &str) -> PathBuf {
    root.join("viewers").join(name)
}

struct FileLockGuard {
    file: File,
    unlink_path: Option<PathBuf>,
}

impl FileLockGuard {
    #[cfg(unix)]
    fn acquire_blocking(path: &Path, unlink_on_drop: bool) -> anyhow::Result<Self> {
        if let Some(parent) = path.parent() {
            ensure_private_dir(parent)?;
        }
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(path)
            .with_context(|| format!("open lock file {}", path.display()))?;
        #[cfg(unix)]
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
        let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) };
        if rc != 0 {
            return Err(std::io::Error::last_os_error())
                .with_context(|| format!("lock {}", path.display()));
        }
        Ok(Self {
            file,
            unlink_path: unlink_on_drop.then(|| path.to_path_buf()),
        })
    }

    #[cfg(not(unix))]
    fn acquire_blocking(path: &Path, unlink_on_drop: bool) -> anyhow::Result<Self> {
        let _ = (path, unlink_on_drop);
        anyhow::bail!("remote session locking requires unix")
    }
}

impl Drop for FileLockGuard {
    fn drop(&mut self) {
        if let Some(path) = self.unlink_path.take() {
            let _ = std::fs::remove_file(path);
        }
        #[cfg(unix)]
        unsafe {
            libc::flock(self.file.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

#[cfg(unix)]
fn is_lock_held(path: &Path) -> bool {
    let Ok(file) = OpenOptions::new().read(true).open(path) else {
        return false;
    };
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        unsafe {
            libc::flock(file.as_raw_fd(), libc::LOCK_UN);
        }
        return false;
    }
    let err = std::io::Error::last_os_error().raw_os_error();
    err == Some(libc::EWOULDBLOCK) || err == Some(libc::EAGAIN)
}

#[cfg(not(unix))]
fn is_lock_held(_path: &Path) -> bool {
    false
}

fn cleanup_dead_session(root: &Path, name: &str) {
    let _ = std::fs::remove_file(state_file_path(root, name));
    let _ = std::fs::remove_file(warp_state_file_path(root, name));
    let _ = std::fs::remove_file(socket_file_path(root, name));
    let _ = std::fs::remove_file(alive_lock_path(root, name));
    let viewers = viewers_dir_path(root, name);
    if let Ok(entries) = std::fs::read_dir(&viewers) {
        for entry in entries.flatten() {
            let path = entry.path();
            if !is_lock_held(&path) {
                let _ = std::fs::remove_file(path);
            }
        }
    }
    let _ = std::fs::remove_dir(&viewers);
}

fn is_session_alive(root: &Path, name: &str) -> bool {
    let lock = alive_lock_path(root, name);
    let sock = socket_file_path(root, name);
    if is_lock_held(&lock) && sock.exists() {
        return true;
    }
    if state_file_path(root, name).exists()
        || warp_state_file_path(root, name).exists()
        || sock.exists()
        || lock.exists()
    {
        cleanup_dead_session(root, name);
    }
    false
}

fn count_live_viewers(root: &Path, name: &str) -> usize {
    let dir = viewers_dir_path(root, name);
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return 0;
    };
    let mut live = 0usize;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) != Some("lock") {
            continue;
        }
        if is_lock_held(&path) {
            live += 1;
        } else {
            let _ = std::fs::remove_file(path);
        }
    }
    live
}

fn read_session_record(root: &Path, name: &str) -> Option<RemoteSessionRecord> {
    let bytes = std::fs::read(state_file_path(root, name)).ok()?;
    serde_json::from_slice(&bytes).ok()
}

fn acquire_viewer_lock(root: &Path, name: &str, is_warp: bool) -> anyhow::Result<FileLockGuard> {
    let dir = viewers_dir_path(root, name);
    ensure_private_dir(&dir)?;
    let filename = format!("{}-{}.lock", std::process::id(), Uuid::new_v4().simple());
    let path = dir.join(filename);
    let guard = FileLockGuard::acquire_blocking(&path, true)?;
    let body = if is_warp { "warp=1\n" } else { "warp=0\n" };
    std::fs::write(&path, body)?;
    Ok(guard)
}

#[derive(Debug, PartialEq, Eq)]
enum SessionSelection {
    New { name: String },
    Reattach { name: String, provider: String },
}

fn list_live_names(root: &Path) -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    let mut names = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(file_name) = path.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if !file_name.ends_with(".json")
            || file_name.ends_with(".warp.json")
            || file_name.contains(".tmp.")
        {
            continue;
        }
        let name = file_name.trim_end_matches(".json");
        if is_session_alive(root, name) {
            names.push(name.to_string());
        }
    }
    names.sort();
    names
}

fn select_or_create_session(
    root: &Path,
    provider: &str,
    base_name: &str,
    explicit_attach: Option<&str>,
) -> anyhow::Result<SessionSelection> {
    if let Some(target) = explicit_attach {
        validate_session_name(target)?;
        if is_session_alive(root, target) {
            let actual_provider = read_session_record(root, target)
                .map(|record| record.provider)
                .unwrap_or_else(|| provider.to_string());
            return Ok(SessionSelection::Reattach {
                name: target.to_string(),
                provider: actual_provider,
            });
        }
        let live = list_live_names(root);
        let hint = if live.is_empty() {
            "none running".to_string()
        } else {
            format!("active: {}", live.join(", "))
        };
        anyhow::bail!("no active remote session named {target:?} ({hint})");
    }

    let mut suffix = 1usize;
    loop {
        let candidate = if suffix == 1 {
            base_name.to_string()
        } else {
            format!("{base_name}-{suffix}")
        };
        if !is_session_alive(root, &candidate) {
            let sock = socket_file_path(root, &candidate);
            if sock.to_string_lossy().len() > MAX_SOCKET_PATH_BYTES {
                anyhow::bail!(
                    "socket path too long ({} > {MAX_SOCKET_PATH_BYTES} bytes): {}",
                    sock.to_string_lossy().len(),
                    sock.display()
                );
            }
            return Ok(SessionSelection::New { name: candidate });
        }
        let existing_provider = read_session_record(root, &candidate)
            .map(|record| record.provider)
            .unwrap_or_default();
        if existing_provider == provider && count_live_viewers(root, &candidate) == 0 {
            return Ok(SessionSelection::Reattach {
                name: candidate,
                provider: existing_provider,
            });
        }
        suffix += 1;
    }
}

fn ensure_dtach_on_path() -> anyhow::Result<()> {
    let ok = std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .map(|dir| dir.join("dtach"))
        .any(|path| path.is_file());
    if ok {
        Ok(())
    } else {
        anyhow::bail!("dtach is required on the remote host PATH (install dtach first)")
    }
}

fn maybe_pull_remote_checkout(cwd: &Path, out: &mut impl Write) {
    for (repo, scope) in git_checkouts(cwd) {
        let dirty = Command::new("git")
            .current_dir(&repo)
            .args(["status", "--porcelain"])
            .output()
            .map(|status| !String::from_utf8_lossy(&status.stdout).trim().is_empty())
            .unwrap_or(false);
        if dirty {
            let _ = writeln!(
                out,
                "longhouse: {scope}remote checkout has uncommitted changes; skipping git pull"
            );
        } else if let Ok(pull) = Command::new("git")
            .current_dir(&repo)
            .args(["pull", "--ff-only", "-q"])
            .output()
        {
            if !pull.status.success() {
                let msg = String::from_utf8_lossy(&pull.stderr).trim().to_string();
                if msg.is_empty() {
                    let _ = writeln!(
                        out,
                        "longhouse: {scope}git pull --ff-only exited with {}",
                        pull.status
                    );
                } else {
                    let _ = writeln!(out, "longhouse: {scope}git pull --ff-only warning: {msg}");
                }
            }
        }
        if let Ok(head) = Command::new("git")
            .current_dir(&repo)
            .args(["log", "-1", "--format=%h %cr"])
            .output()
        {
            if head.status.success() {
                let text = String::from_utf8_lossy(&head.stdout).trim().to_string();
                if !text.is_empty() {
                    let _ = writeln!(out, "longhouse: {scope}remote at {text}");
                }
            }
        }
    }
}

fn run_dtach_command(
    args: &[&str],
    program: Option<&Path>,
    child_args: &[String],
) -> anyhow::Result<i32> {
    let mut cmd = Command::new("dtach");
    cmd.args(args);
    if let Some(prog) = program {
        cmd.arg(prog);
        cmd.args(child_args);
    }
    #[cfg(unix)]
    unsafe {
        cmd.pre_exec(|| {
            libc::signal(libc::SIGINT, libc::SIG_DFL);
            libc::signal(libc::SIGQUIT, libc::SIG_DFL);
            Ok(())
        });
    }
    let status = cmd.status().context("run dtach")?;
    Ok(status.code().unwrap_or(1))
}

pub fn run_remote_session(args: RemoteSessionArgs) -> anyhow::Result<i32> {
    ensure_dtach_on_path()?;
    let root = remote_sessions_root()?;
    let remote_home = local_home_dir().unwrap_or_else(|| PathBuf::from("."));
    let target_cwd = if args.attach.is_some() && args.cwd == "." {
        None
    } else {
        let expanded = expand_remote_cwd(&args.cwd, &remote_home);
        let canonical = std::fs::canonicalize(&expanded).with_context(|| {
            format!(
                "directory {} does not exist on remote host",
                expanded.display()
            )
        })?;
        std::env::set_current_dir(&canonical)
            .with_context(|| format!("enter directory {}", canonical.display()))?;
        Some(canonical)
    };

    let folder_name = target_cwd
        .as_deref()
        .and_then(|path| path.file_name())
        .and_then(|name| name.to_str())
        .unwrap_or("session");
    let default_base = format!(
        "{}-{}",
        sanitize_session_base(&args.provider),
        sanitize_session_base(folder_name)
    );
    let base_name = match args.remote_name.as_deref() {
        Some(custom) => {
            validate_session_name(custom)?;
            custom.to_string()
        }
        None => default_base,
    };

    let create_lock = FileLockGuard::acquire_blocking(&root.join(".create.lock"), false)?;
    let selection =
        select_or_create_session(&root, &args.provider, &base_name, args.attach.as_deref())?;
    let name = match &selection {
        SessionSelection::New { name } | SessionSelection::Reattach { name, .. } => name.clone(),
    };
    let viewer_guard = acquire_viewer_lock(&root, &name, warp_cli_agent::is_warp_env_current())?;
    let sock_path = socket_file_path(&root, &name);
    let state_path = state_file_path(&root, &name);
    let warp_path = warp_state_file_path(&root, &name);

    let reattach_cwd = if matches!(selection, SessionSelection::New { .. }) {
        let cwd_str = target_cwd
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|| ".".to_string());
        let record = RemoteSessionRecord {
            name: name.clone(),
            provider: args.provider.clone(),
            cwd: cwd_str,
            created_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
        };
        let bytes = serde_json::to_vec_pretty(&record)?;
        write_private_file(&state_path, &bytes)?;
        None
    } else {
        read_session_record(&root, &name).map(|record| PathBuf::from(record.cwd))
    };
    drop(create_lock);

    if let Some(dir) = reattach_cwd {
        if dir.is_dir() {
            let _ = std::env::set_current_dir(&dir);
        }
    }

    let sock_str = sock_path.to_string_lossy().into_owned();
    let code = match selection {
        SessionSelection::Reattach { .. } => {
            eprintln!("longhouse: reattaching to {name}");
            warp_cli_agent::emit_replay_markers(&warp_path);
            run_dtach_command(&["-a", &sock_str, "-r", "winch", "-z"], None, &[])?
        }
        SessionSelection::New { .. } => {
            if args.pull {
                if let Some(dir) = target_cwd.as_deref() {
                    maybe_pull_remote_checkout(dir, &mut std::io::stderr());
                }
            }
            eprintln!("longhouse: new {} session {name}", args.provider);
            let self_exe = std::env::current_exe().context("resolve current longhouse binary")?;
            let mut host_args = vec![
                "__remote-host".to_string(),
                "--state-file".to_string(),
                state_path.to_string_lossy().into_owned(),
                "--".to_string(),
                args.provider,
            ];
            host_args.extend(args.provider_args);
            run_dtach_command(
                &["-c", &sock_str, "-r", "winch", "-z"],
                Some(&self_exe),
                &host_args,
            )?
        }
    };
    drop(viewer_guard);
    Ok(code)
}

pub fn run_remote_host(args: RemoteHostArgs) -> anyhow::Result<i32> {
    let root = args
        .state_file
        .parent()
        .context("remote state file missing parent directory")?
        .to_path_buf();
    let file_stem = args
        .state_file
        .file_stem()
        .and_then(|s| s.to_str())
        .context("remote state file missing valid stem")?
        .to_string();
    validate_session_name(&file_stem)?;

    let alive_guard = FileLockGuard::acquire_blocking(&alive_lock_path(&root, &file_stem), true)?;
    let warp_path = warp_state_file_path(&root, &file_stem);
    let viewers_dir = viewers_dir_path(&root, &file_stem);
    ensure_private_dir(&viewers_dir)?;

    let self_exe = std::env::current_exe().context("resolve current longhouse binary")?;
    let mut cmd = Command::new(&self_exe);
    cmd.args(&args.args)
        .env(warp_cli_agent::WARP_STATE_FILE_ENV, &warp_path)
        .env(warp_cli_agent::REMOTE_VIEWERS_DIR_ENV, &viewers_dir);
    let status = cmd.status().context("run managed session inside dtach")?;

    drop(alive_guard);
    cleanup_dead_session(&root, &file_stem);
    Ok(status.code().unwrap_or(1))
}

fn state_label(warp_state: Option<&warp_cli_agent::WarpSessionState>) -> &'static str {
    match warp_state.map(|state| state.last_event.as_str()) {
        Some("prompt_submit") => "working",
        Some("input_required") => "attention",
        Some("stop" | "session_start") => "idle",
        Some("session_end") => "ended",
        Some(_) => "active",
        None => "starting",
    }
}

fn collect_list_entries(root: &Path) -> Vec<RemoteListEntry> {
    let mut out = Vec::new();
    for name in list_live_names(root) {
        let Some(record) = read_session_record(root, &name) else {
            continue;
        };
        let warp_state = warp_cli_agent::read_warp_state(&warp_state_file_path(root, &name));
        let state = state_label(warp_state.as_ref()).to_string();
        let viewers = count_live_viewers(root, &name);
        out.push(RemoteListEntry {
            name: record.name,
            provider: record.provider,
            state,
            viewers,
            cwd: record.cwd,
            session_id: warp_state.map(|s| s.session_id),
        });
    }
    out
}

fn format_list_table(entries: &[RemoteListEntry]) -> String {
    if entries.is_empty() {
        return "No active remote sessions.\n".to_string();
    }
    let mut out = format!(
        "{:<24} {:<12} {:<10} {:<8} {}\n",
        "NAME", "PROVIDER", "STATE", "VIEWERS", "CWD"
    );
    for entry in entries {
        out.push_str(&format!(
            "{:<24} {:<12} {:<10} {:<8} {}\n",
            entry.name, entry.provider, entry.state, entry.viewers, entry.cwd
        ));
    }
    out
}

pub fn run_remote_list_local(args: RemoteListArgs) -> anyhow::Result<i32> {
    let root = remote_sessions_root()?;
    let entries = collect_list_entries(&root);
    if args.json {
        println!("{}", serde_json::to_string_pretty(&entries)?);
    } else {
        print!("{}", format_list_table(&entries));
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cwd_mapping_and_expansion_handle_home_and_outside_paths() {
        let dir = tempfile::tempdir().unwrap();
        let home = std::fs::canonicalize(dir.path()).unwrap();
        let sub = home.join("git/project");
        std::fs::create_dir_all(&sub).unwrap();

        assert_eq!(map_cwd_for_remote(&home, Some(&home)).unwrap(), "~");
        assert_eq!(
            map_cwd_for_remote(&sub, Some(&home)).unwrap(),
            "~/git/project"
        );
        let outside = tempfile::tempdir().unwrap();
        let outside_canon = std::fs::canonicalize(outside.path()).unwrap();
        assert_eq!(
            map_cwd_for_remote(&outside_canon, Some(&home)).unwrap(),
            outside_canon.to_string_lossy()
        );

        let remote_home = Path::new("/remote/user");
        assert_eq!(expand_remote_cwd("~", remote_home), remote_home);
        assert_eq!(
            expand_remote_cwd("~/git/project", remote_home),
            PathBuf::from("/remote/user/git/project")
        );
        assert_eq!(
            expand_remote_cwd("/opt/work", remote_home),
            PathBuf::from("/opt/work")
        );
    }

    #[test]
    fn session_name_validation_and_sanitization_reject_traversal() {
        assert_eq!(sanitize_session_base("my repo/1!"), "my_repo_1_");
        assert_eq!(sanitize_session_base("..--"), "session");
        assert!(validate_session_name("claude-my_repo.1").is_ok());
        assert!(validate_session_name("").is_err());
        assert!(validate_session_name("../escape").is_err());
        assert!(validate_session_name("a..b").is_err());
        assert!(validate_session_name("a/b").is_err());
        assert!(validate_session_name("-leading-dash").is_err());
    }

    #[test]
    fn build_remote_shell_command_quotes_safely_and_prepends_path() {
        let cmd = build_remote_shell_command(
            &[
                ("TERM_PROGRAM".into(), "WarpTerminal".into()),
                ("ANTHROPIC_MODEL".into(), "o'pus".into()),
            ],
            &[
                "__remote-session".into(),
                "--cwd".into(),
                "~/git/foo bar".into(),
                "--".into(),
                "--flag".into(),
            ],
        );
        assert!(cmd.contains("PATH=\"$HOME/.local/bin:$HOME/.nix-profile/bin:$HOME/bin:$PATH\""));
        assert!(cmd.contains("export TERM_PROGRAM='WarpTerminal'"));
        assert!(cmd.contains("export ANTHROPIC_MODEL='o'\\''pus'"));
        assert!(
            cmd.contains("exec longhouse '__remote-session' '--cwd' '~/git/foo bar' '--' '--flag'")
        );
    }

    fn git(dir: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .current_dir(dir)
            .args(["-c", "user.name=Longhouse Test"])
            .args(["-c", "user.email=test@example.invalid"])
            .args([
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
            ])
            .args(args)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    }

    fn commit(repo: &Path, text: &str) {
        std::fs::write(repo.join("file.txt"), text).unwrap();
        git(repo, &["add", "file.txt"]);
        git(repo, &["commit", "-q", "-m", text]);
    }

    /// Clone a new repo with one commit, from a bare upstream under `remotes`,
    /// to `checkout`. Returns a second clone that pushes new upstream commits.
    fn cloned_repo(remotes: &Path, name: &str, checkout: &Path) -> PathBuf {
        let pusher = remotes.join(format!("{name}-pusher"));
        std::fs::create_dir_all(&pusher).unwrap();
        git(&pusher, &["init", "-q", "-b", "main"]);
        commit(&pusher, "first");
        let upstream = remotes.join(format!("{name}.git"));
        let upstream = upstream.to_str().unwrap();
        git(
            remotes,
            &["clone", "-q", "--bare", pusher.to_str().unwrap(), upstream],
        );
        git(&pusher, &["remote", "add", "origin", upstream]);
        git(
            remotes,
            &["clone", "-q", upstream, checkout.to_str().unwrap()],
        );
        pusher
    }

    fn push_new_commit(pusher: &Path, text: &str) {
        commit(pusher, text);
        git(pusher, &["push", "-q", "origin", "main"]);
    }

    fn head(repo: &Path) -> String {
        git(repo, &["rev-parse", "HEAD"])
    }

    #[test]
    fn pull_fast_forwards_each_clean_repo_in_a_folder_of_repos() {
        let remotes = tempfile::tempdir().unwrap();
        let folder = tempfile::tempdir().unwrap();
        let repo = |name: &str| folder.path().join(name);
        let names = ["clean", "dirty", ".hidden", "_parked"];
        let pushers: Vec<PathBuf> = names
            .iter()
            .map(|name| cloned_repo(remotes.path(), name, &repo(name)))
            .collect();
        std::fs::create_dir(repo("notes")).unwrap();
        let before: Vec<String> = names.iter().map(|name| head(&repo(name))).collect();
        pushers
            .iter()
            .for_each(|pusher| push_new_commit(pusher, "second"));
        std::fs::write(repo("dirty").join("file.txt"), "local edit").unwrap();

        let mut out = Vec::new();
        maybe_pull_remote_checkout(folder.path(), &mut out);
        let out = String::from_utf8(out).unwrap();

        assert_eq!(head(&repo("clean")), head(&pushers[0]));
        for (name, before) in names.iter().zip(&before).skip(1) {
            assert_eq!(&head(&repo(name)), before, "{name} moved");
        }
        let lines: Vec<&str> = out.lines().collect();
        assert_eq!(lines.len(), 3, "{out}");
        assert!(
            lines[0].starts_with("longhouse: clean: remote at "),
            "{out}"
        );
        assert_eq!(
            lines[1],
            "longhouse: dirty: remote checkout has uncommitted changes; skipping git pull"
        );
        assert!(
            lines[2].starts_with("longhouse: dirty: remote at "),
            "{out}"
        );
    }

    #[test]
    fn pull_in_a_repo_works_on_that_repo_alone_as_before() {
        let remotes = tempfile::tempdir().unwrap();
        let parent = tempfile::tempdir().unwrap();
        let repo = parent.path().join("repo");
        let pusher = cloned_repo(remotes.path(), "repo", &repo);
        // A repo nested in the launch repo is not a child to pull.
        let nested = repo.join("nested");
        let nested_pusher = cloned_repo(remotes.path(), "nested", &nested);
        std::fs::write(repo.join(".git/info/exclude"), "nested/\n").unwrap();
        let nested_before = head(&nested);
        push_new_commit(&pusher, "second");
        push_new_commit(&nested_pusher, "second");
        std::fs::create_dir(repo.join("sub")).unwrap();

        // From a subfolder of the repo, as before.
        let mut out = Vec::new();
        maybe_pull_remote_checkout(&repo.join("sub"), &mut out);
        let out = String::from_utf8(out).unwrap();
        assert_eq!(head(&repo), head(&pusher));
        assert_eq!(head(&nested), nested_before);
        assert_eq!(out.lines().count(), 1, "{out}");
        assert!(out.starts_with("longhouse: remote at "), "{out}");

        push_new_commit(&pusher, "third");
        std::fs::write(repo.join("file.txt"), "local edit").unwrap();
        let before = head(&repo);
        let mut out = Vec::new();
        maybe_pull_remote_checkout(&repo, &mut out);
        let out = String::from_utf8(out).unwrap();
        assert_eq!(head(&repo), before);
        assert_eq!(
            out.lines().next(),
            Some("longhouse: remote checkout has uncommitted changes; skipping git pull")
        );
    }

    #[test]
    fn launch_warning_names_local_work_in_each_child_repo() {
        let remotes = tempfile::tempdir().unwrap();
        let folder = tempfile::tempdir().unwrap();
        let repo = |name: &str| folder.path().join(name);
        for name in ["ahead", "edited", "synced", "_parked"] {
            cloned_repo(remotes.path(), name, &repo(name));
        }
        commit(&repo("ahead"), "local commit");
        commit(&repo("_parked"), "local commit");
        std::fs::write(repo("edited").join("file.txt"), "local edit").unwrap();
        std::fs::write(repo("edited").join("new.txt"), "new file").unwrap();

        let mut out = Vec::new();
        warn_local_git_divergence(folder.path(), "devbox", &mut out);
        assert_eq!(
            String::from_utf8(out).unwrap(),
            "longhouse: ahead: local checkout has 1 unpushed commit(s); push them for devbox to see them.\n\
             longhouse: edited: local checkout has 2 uncommitted change(s); devbox will not see them.\n"
        );

        // A folder that is a repo warns as before, with no name.
        let mut out = Vec::new();
        warn_local_git_divergence(&repo("ahead"), "devbox", &mut out);
        assert_eq!(
            String::from_utf8(out).unwrap(),
            "longhouse: local checkout has 1 unpushed commit(s); push them for devbox to see them.\n"
        );
    }

    #[cfg(unix)]
    #[test]
    fn session_selection_isolates_providers_and_reattaches_idle_viewers() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();

        let claude = ManagedProvider::Claude.as_str();
        let omp = ManagedProvider::Omp.as_str();
        let first_name = format!("{claude}-demo");

        // Fresh state creates claude-demo.
        assert_eq!(
            select_or_create_session(root, claude, &first_name, None).unwrap(),
            SessionSelection::New {
                name: first_name.clone()
            }
        );

        // Create live session with 0 viewers -> same provider reattaches cleanly.
        std::fs::write(socket_file_path(root, &first_name), b"").unwrap();
        write_private_file(
            &state_file_path(root, &first_name),
            serde_json::to_vec(&RemoteSessionRecord {
                name: first_name.clone(),
                provider: claude.to_string(),
                cwd: "/tmp/demo".to_string(),
                created_at: "2026-11-22T00:00:00Z".to_string(),
            })
            .unwrap()
            .as_slice(),
        )
        .unwrap();
        let _alive =
            FileLockGuard::acquire_blocking(&alive_lock_path(root, &first_name), true).unwrap();

        assert_eq!(
            select_or_create_session(root, claude, &first_name, None).unwrap(),
            SessionSelection::Reattach {
                name: first_name.clone(),
                provider: claude.to_string(),
            }
        );

        // Different provider in same directory uses its own name without colliding.
        let omp_name = format!("{omp}-demo");
        assert_eq!(
            select_or_create_session(root, omp, &omp_name, None).unwrap(),
            SessionSelection::New { name: omp_name }
        );

        // Holding a live viewer lock causes a second tab to create `-2` while `--attach` hits the original.
        let _viewer = acquire_viewer_lock(root, &first_name, true).unwrap();
        assert_eq!(count_live_viewers(root, &first_name), 1);
        assert_eq!(
            select_or_create_session(root, claude, &first_name, None).unwrap(),
            SessionSelection::New {
                name: format!("{first_name}-2")
            }
        );
        assert_eq!(
            select_or_create_session(root, claude, &first_name, Some(&first_name)).unwrap(),
            SessionSelection::Reattach {
                name: first_name.clone(),
                provider: claude.to_string(),
            }
        );

        // List shows the live session cleanly and drops dead ones.
        let entries = collect_list_entries(root);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, first_name);
        assert_eq!(entries[0].viewers, 1);
        assert_eq!(entries[0].state, "starting");
        assert!(format_list_table(&entries).contains(&first_name));

        // Explicit attach to missing name errors clearly.
        assert!(select_or_create_session(root, claude, &first_name, Some("missing")).is_err());
    }
}
