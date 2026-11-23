//! A command the user can run before each managed session starts.
//!
//! When `<Longhouse home>/prelaunch` (or the file `LONGHOUSE_PRELAUNCH` names)
//! exists and is executable, `longhouse <provider>` runs it in the session's
//! working directory, on the host where the provider runs, before the provider
//! starts. The hook never blocks a launch: a failure, a timeout or a missing
//! file only changes what is printed. Its standard output is printed to the
//! terminal and, for Claude, added to the session's SessionStart context
//! (`claude_lifecycle_hook.rs` reads the file named by `CONTEXT_ENV`).

use std::ffi::OsString;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

/// Overrides the hook path; set to an empty string to turn the hook off.
pub const HOOK_ENV: &str = "LONGHOUSE_PRELAUNCH";
pub const TIMEOUT_ENV: &str = "LONGHOUSE_PRELAUNCH_TIMEOUT_SECS";
/// Set on a Claude provider process: the file holding this launch's hook output.
pub const CONTEXT_ENV: &str = "LONGHOUSE_PRELAUNCH_CONTEXT";
pub const PROVIDER_ENV: &str = "LONGHOUSE_PROVIDER";
pub const SESSION_CWD_ENV: &str = "LONGHOUSE_SESSION_CWD";
const HOOK_FILE_NAME: &str = "prelaunch";
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(30);
pub const STDOUT_CAP_BYTES: usize = 16 * 1024;
/// Readers of the context file stop here: the capped output, the truncation
/// marker and one status line all fit inside it.
#[allow(dead_code)]
pub const CONTEXT_READ_LIMIT_BYTES: u64 = STDOUT_CAP_BYTES as u64 + 1024;
pub const TRUNCATION_MARKER: &str = "[prelaunch hook output truncated at 16 KiB]";
/// How long to wait for the output pipe to close once the hook itself has
/// exited. A background process the hook started can hold it open for good.
const PIPE_DRAIN_GRACE: Duration = Duration::from_millis(500);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HookLocation {
    Disabled,
    Path { path: PathBuf, explicit: bool },
}

/// Where the hook lives: `LONGHOUSE_PRELAUNCH` when set (empty turns it off),
/// otherwise `prelaunch` in the Longhouse home.
pub fn locate(override_value: Option<OsString>, longhouse_home: Option<&Path>) -> HookLocation {
    match override_value {
        Some(value) if value.is_empty() => HookLocation::Disabled,
        Some(value) => {
            let path = PathBuf::from(value);
            let path = if path.is_relative() {
                std::env::current_dir()
                    .map(|dir| dir.join(&path))
                    .unwrap_or(path)
            } else {
                path
            };
            HookLocation::Path {
                path,
                explicit: true,
            }
        }
        None => match longhouse_home {
            Some(home) => HookLocation::Path {
                path: home.join(HOOK_FILE_NAME),
                explicit: false,
            },
            None => HookLocation::Disabled,
        },
    }
}

/// Whole seconds from `LONGHOUSE_PRELAUNCH_TIMEOUT_SECS`; anything else is the default.
pub fn timeout_from(value: Option<String>) -> Duration {
    value
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|secs| *secs > 0)
        .map(Duration::from_secs)
        .unwrap_or(DEFAULT_TIMEOUT)
}

fn is_executable_file(path: &Path) -> bool {
    let Ok(metadata) = std::fs::metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Status {
    Succeeded,
    Exited(i32),
    Signaled(i32),
    TimedOut(Duration),
    SpawnFailed(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    /// The hook's standard output, trailing whitespace removed, capped at
    /// `STDOUT_CAP_BYTES` with `TRUNCATION_MARKER` on its own line when cut.
    pub stdout: String,
    pub status: Status,
}

impl Outcome {
    /// One short line describing a failure, or `None` when the hook succeeded.
    pub fn failure_line(&self) -> Option<String> {
        match &self.status {
            Status::Succeeded => None,
            Status::Exited(code) => Some(format!("prelaunch hook exited {code}")),
            Status::Signaled(signal) => {
                Some(format!("prelaunch hook was killed by signal {signal}"))
            }
            Status::TimedOut(timeout) => Some(format!(
                "prelaunch hook timed out after {}s",
                timeout.as_secs()
            )),
            Status::SpawnFailed(error) => Some(format!("prelaunch hook could not start: {error}")),
        }
    }

    /// What the session is told: the output, then the failure line. `None`
    /// when both are empty, so a silent successful hook adds nothing.
    pub fn context(&self) -> Option<String> {
        let mut parts = Vec::new();
        if !self.stdout.is_empty() {
            parts.push(self.stdout.clone());
        }
        parts.extend(self.failure_line());
        (!parts.is_empty()).then(|| parts.join("\n"))
    }
}

#[derive(Default)]
struct Captured {
    bytes: Vec<u8>,
    truncated: bool,
}

fn spawn_with_retry(command: &mut Command) -> std::io::Result<std::process::Child> {
    // A process that forks while another thread still holds a freshly written
    // script open for writing leaves that fd in the child until its exec;
    // exec of the script fails with ETXTBSY for that brief window.
    let mut attempts = 0;
    loop {
        match command.spawn() {
            Err(error)
                if error.kind() == std::io::ErrorKind::ExecutableFileBusy && attempts < 50 =>
            {
                attempts += 1;
                std::thread::sleep(Duration::from_millis(10));
            }
            result => return result,
        }
    }
}

fn status_of(status: ExitStatus) -> Status {
    if status.success() {
        return Status::Succeeded;
    }
    if let Some(code) = status.code() {
        return Status::Exited(code);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(signal) = status.signal() {
            return Status::Signaled(signal);
        }
    }
    Status::Exited(1)
}

/// Run the hook at `hook` for `provider` in `cwd` and collect its output.
///
/// The hook gets no standard input; its standard error goes to the terminal.
/// It runs with no inherited managed-session identity, and with the hook
/// turned off for anything it launches, so a hook that starts Longhouse cannot
/// recurse. A hook still running at `timeout` is killed.
pub fn execute(hook: &Path, provider: &str, cwd: &Path, timeout: Duration) -> Outcome {
    let mut command = Command::new(hook);
    crate::managed_identity::ManagedIdentity::scrub(&mut command);
    command
        .current_dir(cwd)
        .env(PROVIDER_ENV, provider)
        .env(SESSION_CWD_ENV, cwd)
        .env(HOOK_ENV, "")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    let mut child = match spawn_with_retry(&mut command) {
        Ok(child) => child,
        Err(error) => {
            return Outcome {
                stdout: String::new(),
                status: Status::SpawnFailed(error.to_string()),
            }
        }
    };
    let captured = Arc::new(Mutex::new(Captured::default()));
    let (done_sender, done) = mpsc::channel();
    if let Some(mut stdout) = child.stdout.take() {
        let captured = Arc::clone(&captured);
        std::thread::spawn(move || {
            let mut buffer = [0u8; 8192];
            loop {
                match stdout.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(read) => {
                        let mut captured = captured.lock().unwrap_or_else(|err| err.into_inner());
                        let room = STDOUT_CAP_BYTES.saturating_sub(captured.bytes.len());
                        if read > room {
                            captured.truncated = true;
                        }
                        // Past the cap the pipe is still drained, so a chatty
                        // hook never blocks on a full pipe.
                        captured.bytes.extend_from_slice(&buffer[..read.min(room)]);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
                    Err(_) => break,
                }
            }
            let _ = done_sender.send(());
        });
    }
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status_of(status),
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                break Status::TimedOut(timeout);
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                break Status::SpawnFailed(error.to_string());
            }
        }
    };
    let _ = done.recv_timeout(PIPE_DRAIN_GRACE);
    let captured = captured.lock().unwrap_or_else(|err| err.into_inner());
    Outcome {
        stdout: render_stdout(&captured.bytes, captured.truncated),
        status,
    }
}

fn render_stdout(bytes: &[u8], truncated: bool) -> String {
    let mut bytes = bytes;
    if truncated {
        // Do not end the capped output halfway through a character.
        if let Err(error) = std::str::from_utf8(bytes) {
            if error.error_len().is_none() {
                bytes = &bytes[..error.valid_up_to()];
            }
        }
    }
    let mut text = String::from_utf8_lossy(bytes).trim_end().to_string();
    if truncated {
        if !text.is_empty() {
            text.push('\n');
        }
        text.push_str(TRUNCATION_MARKER);
    }
    text
}

/// Run the user's prelaunch hook, if there is one, before `provider` starts in
/// `cwd`. Prints its output and any failure line to the terminal and returns
/// the text to hand the session, `None` when there is nothing to say.
pub fn run(provider: &str, cwd: &Path, longhouse_home: Option<&Path>) -> Option<String> {
    let (path, explicit) = match locate(std::env::var_os(HOOK_ENV), longhouse_home) {
        HookLocation::Disabled => return None,
        HookLocation::Path { path, explicit } => (path, explicit),
    };
    if !path.exists() {
        if explicit {
            eprintln!(
                "longhouse: prelaunch hook {} does not exist; skipping it",
                path.display()
            );
        }
        return None;
    }
    if !is_executable_file(&path) {
        eprintln!(
            "longhouse: prelaunch hook {} is not an executable file; skipping it",
            path.display()
        );
        return None;
    }
    let outcome = execute(
        &path,
        provider,
        cwd,
        timeout_from(std::env::var(TIMEOUT_ENV).ok()),
    );
    if !outcome.stdout.is_empty() {
        eprintln!("{}", outcome.stdout);
    }
    if let Some(line) = outcome.failure_line() {
        eprintln!("longhouse: {line}");
    }
    outcome.context()
}

/// The context a Claude session is handed at SessionStart: the file named by
/// `CONTEXT_ENV`, read only for the provider session the launcher started
/// (`expected_session_id`), never for another `claude` run beneath it that
/// inherited the variable.
// The launcher writes the file; only the engine's Claude hook reads it.
#[allow(dead_code)]
pub fn read_context(
    context_path: Option<OsString>,
    expected_session_id: Option<&str>,
    session_id: Option<&str>,
) -> Option<String> {
    let path = context_path.filter(|path| !path.is_empty())?;
    let expected = expected_session_id
        .map(str::trim)
        .filter(|id| !id.is_empty())?;
    if session_id.map(str::trim) != Some(expected) {
        return None;
    }
    let mut bytes = Vec::new();
    std::fs::File::open(PathBuf::from(path))
        .ok()?
        .take(CONTEXT_READ_LIMIT_BYTES)
        .read_to_end(&mut bytes)
        .ok()?;
    let text = String::from_utf8_lossy(&bytes).trim().to_string();
    (!text.is_empty()).then_some(text)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn write_hook(dir: &Path, body: &str) -> PathBuf {
        let path = dir.join(format!("prelaunch-{}", uuid::Uuid::new_v4()));
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    #[test]
    fn location_follows_the_override_and_an_empty_override_disables_it() {
        let home = Path::new("/srv/lh-home");
        assert_eq!(
            locate(None, Some(home)),
            HookLocation::Path {
                path: home.join("prelaunch"),
                explicit: false
            }
        );
        assert_eq!(
            locate(Some(OsString::new()), Some(home)),
            HookLocation::Disabled
        );
        assert_eq!(
            locate(Some(OsString::from("/opt/hooks/before")), Some(home)),
            HookLocation::Path {
                path: PathBuf::from("/opt/hooks/before"),
                explicit: true
            }
        );
        assert_eq!(locate(None, None), HookLocation::Disabled);
    }

    #[test]
    fn timeout_defaults_to_thirty_seconds_and_accepts_whole_seconds() {
        assert_eq!(timeout_from(None), Duration::from_secs(30));
        assert_eq!(timeout_from(Some("5".into())), Duration::from_secs(5));
        assert_eq!(timeout_from(Some("0".into())), Duration::from_secs(30));
        assert_eq!(timeout_from(Some("soon".into())), Duration::from_secs(30));
    }

    #[test]
    fn a_hook_runs_in_the_session_directory_with_provider_and_cwd() {
        let dir = tempfile::tempdir().unwrap();
        let cwd = std::fs::canonicalize(dir.path()).unwrap();
        let hook = write_hook(
            &cwd,
            "echo \"provider=$LONGHOUSE_PROVIDER\"\necho \"cwd=$LONGHOUSE_SESSION_CWD\"\necho \"pwd=$(pwd)\"\necho \"nested=[${LONGHOUSE_PRELAUNCH-unset}]\"\nread line && echo \"stdin=$line\"\nexit 0",
        );
        let outcome = execute(&hook, "claude", &cwd, Duration::from_secs(10));
        assert_eq!(outcome.status, Status::Succeeded);
        let expected = format!(
            "provider=claude\ncwd={0}\npwd={0}\nnested=[]",
            cwd.display()
        );
        assert_eq!(outcome.stdout, expected);
        assert_eq!(outcome.failure_line(), None);
        assert_eq!(outcome.context().as_deref(), Some(expected.as_str()));
    }

    #[test]
    fn a_silent_successful_hook_adds_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let hook = write_hook(dir.path(), "exit 0");
        let outcome = execute(&hook, "codex", dir.path(), Duration::from_secs(10));
        assert_eq!(outcome.status, Status::Succeeded);
        assert_eq!(outcome.context(), None);
    }

    #[test]
    fn a_failing_hook_reports_one_line_after_its_output() {
        let dir = tempfile::tempdir().unwrap();
        let hook = write_hook(dir.path(), "echo skipped two checkouts\nexit 3");
        let outcome = execute(&hook, "claude", dir.path(), Duration::from_secs(10));
        assert_eq!(outcome.status, Status::Exited(3));
        assert_eq!(
            outcome.context().as_deref(),
            Some("skipped two checkouts\nprelaunch hook exited 3")
        );
    }

    #[test]
    fn a_hook_past_its_timeout_is_killed_and_the_launch_moves_on() {
        let dir = tempfile::tempdir().unwrap();
        let hook = write_hook(dir.path(), "echo started\nexec sleep 30");
        let started = Instant::now();
        let outcome = execute(&hook, "claude", dir.path(), Duration::from_secs(1));
        assert!(started.elapsed() < Duration::from_secs(10));
        assert_eq!(outcome.status, Status::TimedOut(Duration::from_secs(1)));
        assert_eq!(
            outcome.context().as_deref(),
            Some("started\nprelaunch hook timed out after 1s")
        );
    }

    #[test]
    fn a_hook_that_cannot_start_reports_why() {
        let dir = tempfile::tempdir().unwrap();
        let hook = dir.path().join("prelaunch");
        std::fs::write(&hook, "#!/nonexistent/interpreter\n").unwrap();
        std::fs::set_permissions(&hook, std::fs::Permissions::from_mode(0o755)).unwrap();
        let outcome = execute(&hook, "claude", dir.path(), Duration::from_secs(10));
        assert!(matches!(outcome.status, Status::SpawnFailed(_)));
        assert!(outcome
            .context()
            .unwrap()
            .starts_with("prelaunch hook could not start: "));
    }

    #[test]
    fn output_is_capped_at_sixteen_kib_with_a_marker() {
        let dir = tempfile::tempdir().unwrap();
        // 40 KiB of output: the cap keeps the first 16 KiB and the pipe is
        // still drained, so the hook exits normally.
        let hook = write_hook(
            dir.path(),
            "i=0; while [ $i -lt 640 ]; do printf '%063d\\n' $i; i=$((i+1)); done",
        );
        let outcome = execute(&hook, "claude", dir.path(), Duration::from_secs(10));
        assert_eq!(outcome.status, Status::Succeeded);
        let (kept, marker) = outcome.stdout.rsplit_once('\n').unwrap();
        assert_eq!(marker, TRUNCATION_MARKER);
        assert!(kept.len() <= STDOUT_CAP_BYTES);
        assert!(kept.len() > STDOUT_CAP_BYTES - 64);
        assert!(kept.starts_with(&format!("{:063}", 0)));
    }

    #[test]
    fn truncation_does_not_split_a_character() {
        let mut bytes = vec![b'a'; STDOUT_CAP_BYTES - 1];
        bytes.extend_from_slice(&"é".as_bytes()[..1]);
        let text = render_stdout(&bytes, true);
        assert!(!text.contains('\u{fffd}'));
        assert!(text.ends_with(TRUNCATION_MARKER));
    }

    #[test]
    fn context_is_read_only_for_the_session_the_launcher_started() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("context.txt");
        std::fs::write(&file, "pulled three checkouts\n").unwrap();
        let path = Some(file.clone().into_os_string());
        assert_eq!(
            read_context(path.clone(), Some("native-1"), Some("native-1")).as_deref(),
            Some("pulled three checkouts")
        );
        // A `claude` started inside that session inherits the variable but is
        // a different session.
        assert_eq!(
            read_context(path.clone(), Some("native-1"), Some("native-2")),
            None
        );
        assert_eq!(read_context(path, None, Some("native-1")), None);
        assert_eq!(read_context(None, Some("native-1"), Some("native-1")), None);
        assert_eq!(
            read_context(
                Some(dir.path().join("missing").into_os_string()),
                Some("native-1"),
                Some("native-1")
            ),
            None
        );
    }
}
