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
}
