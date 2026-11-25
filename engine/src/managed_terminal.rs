//! Shared terminal facts for managed provider cleanup.
//!
//! Provider drivers own how their process or bridge stops. This module owns
//! the runtime event shape and durable handoff that tells the Runtime Host the
//! corresponding managed run ended.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::process::Child;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use uuid::Uuid;

pub struct ManagedTerminalEvent<'a> {
    pub runtime_key: &'a str,
    pub session_id: &'a str,
    pub run_id: &'a str,
    pub provider: &'a str,
    pub managed_transport: &'a str,
    pub provider_session_id: Option<&'a str>,
    pub device_id: Option<&'a str>,
    pub source: &'a str,
    pub dedupe_prefix: &'a str,
    pub terminal_state: &'a str,
    pub terminal_reason: &'a str,
    pub exit_code: Option<i32>,
}

pub fn terminal_state_for_exit(exit_code: i32) -> &'static str {
    if exit_code == 0 {
        "session_ended"
    } else {
        "process_gone"
    }
}

/// Classify a managed Helm process exit into `(terminal_state, terminal_reason)`.
///
/// `terminal_reason = "user_closed"` paired with `terminal_state = "session_ended"`
/// records that the attached Helm process exited cleanly (exit 0) without a remote
/// terminate. It describes how this run ended, never durable session disposition
/// (which only closes on `terminal_state = "user_closed"` or an explicit user close).
pub fn helm_terminal_classification(
    exit_code: Option<i32>,
    remote_terminate: bool,
) -> (&'static str, &'static str) {
    if remote_terminate {
        return ("session_ended", "remote_terminate");
    }
    match exit_code {
        Some(0) => ("session_ended", "user_closed"),
        Some(code) if code >= 128 => ("process_gone", "provider_signal"),
        Some(_) => ("process_gone", "provider_exit"),
        None => ("process_gone", "process_gone"),
    }
}

/// Format a run duration compactly for the terminal exit receipt.
pub fn format_duration_compact(elapsed: Duration) -> String {
    let total_secs = elapsed.as_secs();
    if total_secs == 0 {
        return "<1s".to_string();
    }
    if total_secs < 60 {
        return format!("{total_secs}s");
    }
    let hours = total_secs / 3600;
    let minutes = (total_secs % 3600) / 60;
    let secs = total_secs % 60;
    if hours > 0 {
        format!("{hours}h {minutes}m")
    } else if secs == 0 {
        format!("{minutes}m")
    } else {
        format!("{minutes}m {secs:02}s")
    }
}

fn posix_shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value.bytes().all(|b| {
            b.is_ascii_alphanumeric()
                || matches!(
                    b,
                    b'@' | b'%' | b'+' | b'=' | b':' | b',' | b'.' | b'/' | b'-' | b'_'
                )
        })
    {
        return value.to_string();
    }
    let mut quoted = String::with_capacity(value.len() + 2);
    quoted.push('\'');
    for ch in value.chars() {
        if ch == '\'' {
            quoted.push_str("'\"'\"'");
        } else {
            quoted.push(ch);
        }
    }
    quoted.push('\'');
    quoted
}

/// Build the canonical Longhouse Helm resume command matching
/// `server/zerg/services/session_resume.py::_RESUME_FLAG`.
pub fn helm_resume_command(provider_cli: &str, cwd: &Path, session_id: &str) -> Option<String> {
    let flag = match provider_cli {
        "claude" => "--resume",
        "codex" | "cursor" | "opencode" | "omp" | "pi" => "--resume-session",
        _ => return None,
    };
    let cwd_str = cwd.to_string_lossy();
    Some(format!(
        "longhouse {} --cwd {} {} {}",
        provider_cli,
        posix_shell_quote(&cwd_str),
        flag,
        posix_shell_quote(session_id)
    ))
}

/// Quiet 1-2 line terminal receipt printed after an attached Helm provider exits
/// and the terminal has been restored. Zero network round-trips.
pub struct HelmExitReceipt<'a> {
    pub provider_label: &'a str,
    pub provider_cli: &'a str,
    pub session_id: &'a str,
    pub url: Option<&'a str>,
    pub cwd: &'a Path,
    pub elapsed: Duration,
    pub terminal_reason: &'a str,
    pub exit_code: Option<i32>,
    pub resumable: bool,
}

impl HelmExitReceipt<'_> {
    pub fn format(&self) -> String {
        let duration = format_duration_compact(self.elapsed);
        let code_prefix = match self.exit_code {
            Some(code) if code != 0 => format!("code {code}, "),
            _ => String::new(),
        };
        let status = match self.terminal_reason {
            "remote_terminate" => format!("{} stopped remotely ({duration})", self.provider_label),
            "user_closed" | "bridge_stop" => {
                format!("{} run ended ({duration})", self.provider_label)
            }
            "provider_signal" => format!(
                "{} stopped by signal ({code_prefix}{duration})",
                self.provider_label
            ),
            _ => format!(
                "{} exited unexpectedly ({code_prefix}{duration})",
                self.provider_label
            ),
        };
        let short_id = self
            .session_id
            .split('-')
            .next()
            .filter(|prefix| !prefix.is_empty())
            .unwrap_or(self.session_id);
        let first_line = match self.url.map(str::trim).filter(|base| !base.is_empty()) {
            Some(base) => format!(
                "Longhouse: {status} - {}/s/{short_id}",
                base.trim_end_matches('/')
            ),
            None => format!("Longhouse: {status}"),
        };
        if self.resumable {
            if let Some(command) = helm_resume_command(self.provider_cli, self.cwd, self.session_id)
            {
                return format!("{first_line}\nResume:    {command}");
            }
        }
        first_line
    }

    pub fn print(&self) {
        eprintln!("\n{}", self.format());
    }
}

/// Terminal ownership shared by interactive managed launchers. The provider
/// receives the foreground group and Drop restores the caller's attributes.
#[cfg(unix)]
pub struct ForegroundTerminal {
    fd: libc::c_int,
    parent_pgrp: libc::pid_t,
    attributes: libc::termios,
    old_sigttou: libc::sighandler_t,
}

#[cfg(unix)]
impl ForegroundTerminal {
    pub fn capture() -> anyhow::Result<Self> {
        use std::os::unix::io::AsRawFd;
        let fd = std::io::stdin().as_raw_fd();
        let mut attributes = std::mem::MaybeUninit::<libc::termios>::uninit();
        if unsafe { libc::tcgetattr(fd, attributes.as_mut_ptr()) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(Self {
            fd,
            parent_pgrp: unsafe { libc::tcgetpgrp(fd) },
            attributes: unsafe { attributes.assume_init() },
            old_sigttou: unsafe { libc::signal(libc::SIGTTOU, libc::SIG_IGN) },
        })
    }

    pub fn give_to(&self, pgid: libc::pid_t) -> anyhow::Result<()> {
        if self.parent_pgrp >= 0 && unsafe { libc::tcsetpgrp(self.fd, pgid) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        unsafe { libc::kill(-pgid, libc::SIGCONT) };
        Ok(())
    }
}

#[cfg(unix)]
impl Drop for ForegroundTerminal {
    fn drop(&mut self) {
        unsafe {
            if self.parent_pgrp >= 0 {
                libc::tcsetpgrp(self.fd, self.parent_pgrp);
            }
            libc::tcsetattr(self.fd, libc::TCSANOW, &self.attributes);
            libc::signal(libc::SIGTTOU, self.old_sigttou);
        }
    }
}

/// Stop a process group that this launcher created, escalating only after a
/// bounded grace period and reaping the direct child to avoid zombies.
#[cfg(unix)]
pub fn terminate_owned_group(child: &mut Child, pgid: libc::pid_t) {
    unsafe {
        libc::kill(-pgid, libc::SIGCONT);
        libc::kill(-pgid, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_millis(500);
    while Instant::now() < deadline {
        if child.try_wait().ok().flatten().is_some() {
            return;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    unsafe { libc::kill(-pgid, libc::SIGKILL) };
    let _ = child.wait();
}

impl ManagedTerminalEvent<'_> {
    pub fn to_json(&self) -> Value {
        json!({
            "runtime_key": self.runtime_key,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "provider": self.provider,
            "device_id": self.device_id,
            "source": self.source,
            "kind": "terminal_signal",
            "phase": "finished",
            "occurred_at": chrono::Utc::now().to_rfc3339(),
            "dedupe_key": format!("{}:{}:{}", self.dedupe_prefix, self.session_id, self.run_id),
            "payload": {
                "managed_transport": self.managed_transport,
                "provider_session_id": self.provider_session_id,
                "terminal_state": self.terminal_state,
                "terminal_reason": self.terminal_reason,
                "terminal_source": self.source,
                "exit_code": self.exit_code,
            },
        })
    }
}

/// Atomically enqueue one terminal event for the Machine Agent's retry loop.
pub fn enqueue(dir: &Path, event: &Value) -> anyhow::Result<()> {
    std::fs::create_dir_all(dir)?;
    let nonce = Uuid::new_v4();
    let temporary = dir.join(format!(".{nonce}.tmp"));
    let ready = dir.join(format!("{nonce}.json"));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(&serde_json::to_vec(event)?)?;
    file.sync_all()?;
    drop(file);
    std::fs::rename(temporary, ready)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_event_carries_explicit_run_and_transport() {
        let event = ManagedTerminalEvent {
            runtime_key: "opencode:session",
            session_id: "session",
            run_id: "run",
            provider: "opencode",
            managed_transport: "opencode_server_bridge",
            provider_session_id: Some("provider-session"),
            device_id: Some("cinder"),
            source: "opencode_server_bridge",
            dedupe_prefix: "opencode-terminal",
            terminal_state: "session_ended",
            terminal_reason: "bridge_stop",
            exit_code: None,
        }
        .to_json();

        assert_eq!(event["run_id"], "run");
        assert_eq!(
            event["payload"]["managed_transport"],
            "opencode_server_bridge"
        );
        assert_eq!(event["dedupe_key"], "opencode-terminal:session:run");
    }

    #[test]
    fn enqueue_exposes_only_complete_json_files() {
        let temp = tempfile::tempdir().unwrap();
        enqueue(temp.path(), &json!({"kind": "terminal_signal"})).unwrap();
        let entries = std::fs::read_dir(temp.path())
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect::<Vec<_>>();
        assert_eq!(entries.len(), 1);
        assert_eq!(
            entries[0].extension().and_then(|value| value.to_str()),
            Some("json")
        );
        let event: Value = serde_json::from_slice(&std::fs::read(&entries[0]).unwrap()).unwrap();
        assert_eq!(event["kind"], "terminal_signal");
    }

    #[test]
    fn nonzero_provider_exit_is_not_reported_as_a_clean_end() {
        assert_eq!(terminal_state_for_exit(0), "session_ended");
        assert_eq!(terminal_state_for_exit(7), "process_gone");
    }

    #[test]
    fn helm_terminal_classification_separates_clean_remote_signal_and_crash() {
        assert_eq!(
            helm_terminal_classification(Some(0), false),
            ("session_ended", "user_closed")
        );
        assert_eq!(
            helm_terminal_classification(Some(143), true),
            ("session_ended", "remote_terminate")
        );
        assert_eq!(
            helm_terminal_classification(Some(0), true),
            ("session_ended", "remote_terminate")
        );
        assert_eq!(
            helm_terminal_classification(Some(130), false),
            ("process_gone", "provider_signal")
        );
        assert_eq!(
            helm_terminal_classification(Some(143), false),
            ("process_gone", "provider_signal")
        );
        assert_eq!(
            helm_terminal_classification(Some(1), false),
            ("process_gone", "provider_exit")
        );
        assert_eq!(
            helm_terminal_classification(None, false),
            ("process_gone", "process_gone")
        );
    }

    #[test]
    fn compact_duration_formatting_covers_seconds_minutes_and_hours() {
        assert_eq!(format_duration_compact(Duration::from_millis(400)), "<1s");
        assert_eq!(format_duration_compact(Duration::from_secs(42)), "42s");
        assert_eq!(format_duration_compact(Duration::from_secs(120)), "2m");
        assert_eq!(format_duration_compact(Duration::from_secs(862)), "14m 22s");
        assert_eq!(
            format_duration_compact(Duration::from_secs(21 * 3600 + 23 * 60 + 6)),
            "21h 23m"
        );
    }

    #[test]
    fn helm_resume_command_matches_per_provider_flags_and_quotes_cwd() {
        let sid = "2b78c4f2-2667-4158-8f28-1b41f452d8bc";
        assert_eq!(
            helm_resume_command("claude", Path::new("/Users/david/repo"), sid).as_deref(),
            Some("longhouse claude --cwd /Users/david/repo --resume 2b78c4f2-2667-4158-8f28-1b41f452d8bc")
        );
        for provider in ["codex", "cursor", "opencode", "omp", "pi"] {
            assert_eq!(
                helm_resume_command(provider, Path::new("/tmp/my repo"), sid).as_deref(),
                Some(
                    format!("longhouse {provider} --cwd '/tmp/my repo' --resume-session {sid}")
                        .as_str()
                )
            );
        }
        assert_eq!(
            helm_resume_command("antigravity", Path::new("/tmp/repo"), sid),
            None
        );
    }

    #[test]
    fn exit_receipt_formats_clean_remote_unexpected_and_non_resumable_runs() {
        let sid = "2b78c4f2-2667-4158-8f28-1b41f452d8bc";
        let agy = HelmExitReceipt {
            provider_label: "Antigravity",
            provider_cli: "antigravity",
            session_id: sid,
            url: Some("https://david010.longhouse.ai/"),
            cwd: Path::new("/Users/david"),
            elapsed: Duration::from_secs(21 * 3600 + 23 * 60),
            terminal_reason: "user_closed",
            exit_code: Some(0),
            resumable: false,
        };
        assert_eq!(
            agy.format(),
            "Longhouse: Antigravity run ended (21h 23m) - https://david010.longhouse.ai/s/2b78c4f2"
        );

        let claude = HelmExitReceipt {
            provider_label: "Claude",
            provider_cli: "claude",
            session_id: sid,
            url: Some("https://david010.longhouse.ai"),
            cwd: Path::new("/Users/david/repo"),
            elapsed: Duration::from_secs(862),
            terminal_reason: "user_closed",
            exit_code: Some(0),
            resumable: true,
        };
        assert_eq!(
            claude.format(),
            "Longhouse: Claude run ended (14m 22s) - https://david010.longhouse.ai/s/2b78c4f2\nResume:    longhouse claude --cwd /Users/david/repo --resume 2b78c4f2-2667-4158-8f28-1b41f452d8bc"
        );

        let remote = HelmExitReceipt {
            provider_label: "Cursor",
            provider_cli: "cursor",
            session_id: sid,
            url: Some("https://david010.longhouse.ai"),
            cwd: Path::new("/Users/david/repo"),
            elapsed: Duration::from_secs(485),
            terminal_reason: "remote_terminate",
            exit_code: Some(143),
            resumable: true,
        };
        assert_eq!(
            remote.format(),
            "Longhouse: Cursor stopped remotely (8m 05s) - https://david010.longhouse.ai/s/2b78c4f2\nResume:    longhouse cursor --cwd /Users/david/repo --resume-session 2b78c4f2-2667-4158-8f28-1b41f452d8bc"
        );

        let crash = HelmExitReceipt {
            provider_label: "Codex",
            provider_cli: "codex",
            session_id: sid,
            url: None,
            cwd: Path::new("/Users/david/repo"),
            elapsed: Duration::from_secs(12),
            terminal_reason: "provider_exit",
            exit_code: Some(1),
            resumable: false,
        };
        assert_eq!(
            crash.format(),
            "Longhouse: Codex exited unexpectedly (code 1, 12s)"
        );
    }
}
