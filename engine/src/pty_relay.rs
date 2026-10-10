//! A provider TUI behind a PTY that Longhouse also writes into.
//!
//! A Helm launcher that owns its provider's terminal runs the provider under
//! `forkpty`, relays the user's keystrokes and the provider's output, and lets
//! Longhouse type into the same terminal (owner input, steer, interrupt). This
//! module is the provider-neutral part of that: spawning, raw mode, window
//! size, the relay loop, one write lock shared by the user's keystrokes and
//! remote writes, bracketed-paste writes, draft tracking, and reaping. What a
//! provider's TUI expects to receive (which keys submit, settle delays, phase
//! checks) stays in its launcher.
//!
//! Cursor Helm (`cursor_helm_launcher`) is the first user; Claude Helm is next
//! (control-plane `docs/specs/claude-owner-input-delivery.md`).

use std::ffi::CString;
use std::os::fd::RawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use anyhow::Context;

/// Bracketed-paste delimiters (xterm `?2004`). A TUI that enabled paste mode
/// takes everything between them as pasted text, so a newline inside does
/// not submit.
pub const PASTE_START: &[u8] = b"\x1b[200~";
pub const PASTE_END: &[u8] = b"\x1b[201~";

/// Put `fd` in raw mode; the returned guard restores the saved state on drop.
pub fn raw_mode(fd: RawFd) -> anyhow::Result<RawTerminal> {
    unsafe {
        let mut saved = std::mem::zeroed();
        if libc::tcgetattr(fd, &mut saved) != 0 {
            anyhow::bail!("read terminal state failed")
        };
        let mut next = saved;
        libc::cfmakeraw(&mut next);
        if libc::tcsetattr(fd, libc::TCSADRAIN, &next) != 0 {
            anyhow::bail!("set terminal raw mode failed")
        };
        Ok(RawTerminal(fd, saved))
    }
}

pub struct RawTerminal(RawFd, libc::termios);

impl Drop for RawTerminal {
    fn drop(&mut self) {
        unsafe {
            libc::tcsetattr(self.0, libc::TCSADRAIN, &self.1);
        }
    }
}

/// Give the PTY the size of the user's terminal (stdout).
pub fn sync_winsize(master: RawFd) {
    copy_winsize(1, master);
}

pub fn copy_winsize(source: RawFd, target: RawFd) -> bool {
    unsafe {
        let mut size: libc::winsize = std::mem::zeroed();
        if libc::ioctl(source, libc::TIOCGWINSZ, &mut size) == 0 {
            return libc::ioctl(target, libc::TIOCSWINSZ, &size) == 0;
        }
    }
    false
}

/// The size of `fd`'s terminal, or 80x24 when it has none.
pub fn terminal_winsize(fd: RawFd) -> libc::winsize {
    unsafe {
        let mut size: libc::winsize = std::mem::zeroed();
        if libc::ioctl(fd, libc::TIOCGWINSZ, &mut size) != 0 || size.ws_row == 0 || size.ws_col == 0
        {
            size.ws_row = 24;
            size.ws_col = 80;
        }
        size
    }
}

/// Write every byte, retrying on EINTR.
pub fn write_all(fd: RawFd, mut bytes: &[u8]) -> std::io::Result<()> {
    while !bytes.is_empty() {
        let written = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if written > 0 {
            bytes = &bytes[written as usize..];
        } else if written < 0
            && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted
        {
            continue;
        } else {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}

/// The exec data for a PTY child, built before `forkpty`: the parent may be
/// multi-threaded, so the child may only call async-signal-safe functions and
/// must not allocate.
pub struct PtyCommand {
    pub argv: Vec<CString>,
    pub env: Vec<CString>,
    pub cwd: CString,
}

/// A provider running under a PTY.
pub struct PtyChild {
    pub pid: libc::pid_t,
    pub master: RawFd,
    /// The macOS slave hold (see `spawn`), or -1.
    pub slave_hold: RawFd,
}

/// `forkpty` and exec `command` at `size`.
///
/// On macOS the parent keeps the slave open: XNU can discard queued PTY output
/// or hold a session-leader child in the exiting state until the master reads
/// it, and an open slave keeps POLLIN observable while the relay drains it.
/// Call `release` once the relay has finished.
pub fn spawn(command: &PtyCommand, size: &mut libc::winsize) -> anyhow::Result<PtyChild> {
    // no managed identity: this only execs the command it is handed. The
    // provider launcher builds `command.env` with the managed identity overlay
    // before calling (cursor_helm_launcher: managed_identity_env), because the
    // child may not allocate after forkpty.
    let mut argv_ptrs: Vec<*const libc::c_char> =
        command.argv.iter().map(|value| value.as_ptr()).collect();
    argv_ptrs.push(std::ptr::null());
    let mut env_ptrs: Vec<*const libc::c_char> =
        command.env.iter().map(|value| value.as_ptr()).collect();
    env_ptrs.push(std::ptr::null());
    let mut master = -1;
    let mut slave_name = [0 as libc::c_char; 1024];
    let slave_name_ptr = if cfg!(target_os = "macos") {
        slave_name.as_mut_ptr()
    } else {
        std::ptr::null_mut()
    };
    let pid = unsafe { libc::forkpty(&mut master, slave_name_ptr, std::ptr::null_mut(), size) };
    if pid < 0 {
        anyhow::bail!("forkpty failed: {}", std::io::Error::last_os_error());
    }
    if pid == 0 {
        unsafe {
            libc::chdir(command.cwd.as_ptr());
            libc::execve(
                command.argv[0].as_ptr(),
                argv_ptrs.as_ptr(),
                env_ptrs.as_ptr(),
            );
            libc::_exit(127)
        }
    }
    #[cfg(target_os = "macos")]
    let slave_hold = unsafe { libc::open(slave_name.as_ptr(), libc::O_RDWR | libc::O_NOCTTY) };
    #[cfg(not(target_os = "macos"))]
    let slave_hold = -1;
    if cfg!(target_os = "macos") && slave_hold < 0 {
        let error = std::io::Error::last_os_error();
        unsafe {
            libc::kill(pid, libc::SIGKILL);
            libc::close(master);
            libc::waitpid(pid, std::ptr::null_mut(), 0);
        }
        return Err(error).context("open PTY slave hold failed");
    }
    Ok(PtyChild {
        pid,
        master,
        slave_hold,
    })
}

impl PtyChild {
    /// Kill and reap a child whose relay never started.
    pub fn abort(&self) {
        unsafe {
            libc::kill(self.pid, libc::SIGKILL);
            if self.slave_hold >= 0 {
                libc::close(self.slave_hold);
            }
            libc::close(self.master);
            libc::waitpid(self.pid, std::ptr::null_mut(), 0);
        }
    }

    /// Close the macOS slave hold once the relay has drained the PTY.
    pub fn release_slave_hold(&self) {
        if self.slave_hold >= 0 {
            unsafe {
                libc::close(self.slave_hold);
            }
        }
    }
}

/// The one writer into a provider's terminal.
///
/// The user's keystrokes and Longhouse's writes go through the same lock, so
/// a remote write never interleaves with a keystroke batch. The writer also
/// tracks the user's local draft: bytes typed since the last submit (Enter),
/// interrupt (^C) or line kill (^U). A remote write can wait for the draft to
/// clear instead of landing in the middle of what the user is typing.
#[derive(Clone)]
pub struct PtyWriter {
    inner: Arc<PtyWriterInner>,
}

struct PtyWriterInner {
    master: RawFd,
    lock: Mutex<()>,
    draft: Mutex<DraftState>,
}

#[derive(Default)]
struct DraftState {
    pending: bool,
}

impl PtyWriter {
    pub fn new(master: RawFd) -> Self {
        Self {
            inner: Arc::new(PtyWriterInner {
                master,
                lock: Mutex::new(()),
                draft: Mutex::new(DraftState::default()),
            }),
        }
    }

    /// Hold the terminal for a sequence of writes.
    pub fn lock(&self) -> PtyWriteGuard<'_> {
        PtyWriteGuard {
            master: self.inner.master,
            _hold: self
                .inner
                .lock
                .lock()
                .unwrap_or_else(|poison| poison.into_inner()),
        }
    }

    /// Relay the user's own keystrokes, recording whether they leave a draft.
    pub fn relay_local_input(&self, bytes: &[u8]) -> std::io::Result<()> {
        let guard = self.lock();
        guard.write(bytes)?;
        self.note_local_input(bytes);
        Ok(())
    }

    fn note_local_input(&self, bytes: &[u8]) {
        let mut draft = self
            .inner
            .draft
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        for byte in bytes {
            match byte {
                // Enter submits; ^C interrupts; ^U kills the line.
                b'\r' | b'\n' | 0x03 | 0x15 => draft.pending = false,
                _ => draft.pending = true,
            }
        }
    }

    /// Whether the user has typed something not yet submitted.
    pub fn local_draft_pending(&self) -> bool {
        self.inner
            .draft
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .pending
    }

    // Claude Helm's owner input is the first caller (step 2 of the
    // claude-owner-input-delivery spec); Cursor's TUI writes do not wait.
    #[allow(dead_code)]
    /// Wait up to `timeout` for the user's draft to clear. True when it did
    /// (or none was pending); false means the user is still typing and a
    /// remote write would land inside their draft.
    pub fn wait_for_clear_draft(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        loop {
            if !self.local_draft_pending() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

pub struct PtyWriteGuard<'a> {
    master: RawFd,
    _hold: MutexGuard<'a, ()>,
}

impl PtyWriteGuard<'_> {
    pub fn write(&self, bytes: &[u8]) -> std::io::Result<()> {
        write_all(self.master, bytes)
    }

    // Claude Helm's multiline owner input is the first caller (step 2 of the
    // claude-owner-input-delivery spec); Cursor sends its text unbracketed.
    #[allow(dead_code)]
    /// Write `text` as one bracketed paste, so newlines in it do not submit.
    /// Only for a TUI that enabled paste mode; the caller sends the submit key.
    pub fn write_paste(&self, text: &[u8]) -> std::io::Result<()> {
        let mut bytes = Vec::with_capacity(text.len() + PASTE_START.len() + PASTE_END.len());
        bytes.extend_from_slice(PASTE_START);
        bytes.extend_from_slice(text);
        bytes.extend_from_slice(PASTE_END);
        write_all(self.master, &bytes)
    }
}

/// Relay between the user's terminal and the provider until the provider
/// exits, `stop` is set, or a side fails. Returns the child's wait status when
/// the loop itself reaped it (macOS), or `None`.
pub fn relay(
    child: &PtyChild,
    writer: &PtyWriter,
    input: RawFd,
    output: RawFd,
    stop: &AtomicBool,
    resized: &AtomicBool,
) -> Option<libc::c_int> {
    let master = child.master;
    let mut input_buffer = [0u8; 8192];
    let mut output_buffer = [0u8; 65536];
    loop {
        if stop.load(Ordering::Relaxed) {
            return None;
        }
        if resized.swap(false, Ordering::Relaxed) {
            sync_winsize(master);
        }
        let mut fds = [
            libc::pollfd {
                fd: input,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: master,
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        if unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as _, 250) } < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return None;
        }
        if fds[0].revents & libc::POLLIN != 0 {
            let count =
                unsafe { libc::read(input, input_buffer.as_mut_ptr().cast(), input_buffer.len()) };
            if count > 0
                && writer
                    .relay_local_input(&input_buffer[..count as usize])
                    .is_err()
            {
                return None;
            }
        }
        if fds[1].revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0 {
            let count = unsafe {
                libc::read(
                    master,
                    output_buffer.as_mut_ptr().cast(),
                    output_buffer.len(),
                )
            };
            if count > 0 {
                if write_all(output, &output_buffer[..count as usize]).is_err() {
                    return None;
                }
            } else {
                return None;
            }
        }
        let mut status = 0;
        if cfg!(target_os = "macos")
            && unsafe { libc::waitpid(child.pid, &mut status, libc::WNOHANG) } == child.pid
        {
            return Some(status);
        }
    }
}

/// Reap the child after the relay, with a deadline, and return its exit code.
///
/// The relay can end with the child still alive (a poll error, or a write to
/// stdout failing when an SSH connection drops). Nothing drains the PTY after
/// that, so the child blocks on a full buffer while a plain `waitpid` would
/// block forever. A normal exit is unaffected: the relay ends on PTY EOF, the
/// child is already gone and the first probe reaps it. The grace is longer
/// when the exit was not requested.
pub fn reap(child: &PtyChild, reaped_status: Option<libc::c_int>, requested_stop: bool) -> i32 {
    unsafe {
        let mut status = reaped_status.unwrap_or(0);
        if reaped_status.is_none() {
            let grace_ticks = if requested_stop { 25 } else { 200 };
            let mut observed = libc::waitpid(child.pid, &mut status, libc::WNOHANG);
            for _ in 0..grace_ticks {
                if observed != 0 {
                    break;
                }
                std::thread::sleep(Duration::from_millis(10));
                observed = libc::waitpid(child.pid, &mut status, libc::WNOHANG);
            }
            if observed == 0 {
                libc::kill(child.pid, libc::SIGKILL);
                libc::waitpid(child.pid, &mut status, 0);
            }
        }
        if libc::WIFEXITED(status) {
            libc::WEXITSTATUS(status)
        } else {
            128 + libc::WTERMSIG(status)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    fn pipe() -> (RawFd, RawFd) {
        let mut fds = [0; 2];
        assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
        (fds[0], fds[1])
    }

    fn read_until(fd: RawFd, needle: &[u8], timeout: Duration) -> Vec<u8> {
        let deadline = Instant::now() + timeout;
        let mut seen = Vec::new();
        let mut buffer = [0u8; 4096];
        while Instant::now() < deadline {
            let mut pfd = libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            };
            if unsafe { libc::poll(&mut pfd, 1, 50) } > 0 {
                let count = unsafe { libc::read(fd, buffer.as_mut_ptr().cast(), buffer.len()) };
                if count > 0 {
                    seen.extend_from_slice(&buffer[..count as usize]);
                    if seen.windows(needle.len()).any(|window| window == needle) {
                        break;
                    }
                }
            }
        }
        seen
    }

    /// A `cat` under a PTY. `forkpty` children inherit every descriptor without
    /// close-on-exec, and parallel tests hold plain `libc::pipe` ends; a `cat`
    /// keeping another test's write end open would leave that test waiting
    /// for EOF forever. The shell closes them before it becomes `cat`.
    fn cat_child() -> PtyChild {
        let command = PtyCommand {
            argv: vec![
                CString::new("/bin/sh").unwrap(),
                CString::new("-c").unwrap(),
                CString::new("fd=3; while [ $fd -lt 1024 ]; do eval \"exec $fd>&-\"; fd=$((fd+1)); done; exec /bin/cat").unwrap(),
            ],
            env: vec![CString::new("PATH=/usr/bin:/bin").unwrap()],
            cwd: CString::new("/").unwrap(),
        };
        let mut size = terminal_winsize(-1);
        spawn(&command, &mut size).unwrap()
    }

    /// The user's keystrokes reach the child and its output reaches the user,
    /// and a locked remote write lands in the same terminal.
    #[test]
    fn relay_passes_input_and_output_and_carries_a_locked_write() {
        let child = cat_child();
        let writer = PtyWriter::new(child.master);
        let (input_read, input_write) = pipe();
        let (output_read, output_write) = pipe();
        let stop = Arc::new(AtomicBool::new(false));
        let resized = AtomicBool::new(false);
        let relay_writer = writer.clone();
        let relay_stop = stop.clone();
        let relay_pid = child.pid;
        let relay_master = child.master;
        let handle = std::thread::spawn(move || {
            let child = PtyChild {
                pid: relay_pid,
                master: relay_master,
                slave_hold: -1,
            };
            relay(
                &child,
                &relay_writer,
                input_read,
                output_write,
                &relay_stop,
                &resized,
            )
        });

        write_all(input_write, b"typed-by-user\r").unwrap();
        let seen = read_until(output_read, b"typed-by-user", Duration::from_secs(5));
        assert!(seen.windows(13).any(|w| w == b"typed-by-user"), "{seen:?}");

        writer.lock().write(b"remote-write\r").unwrap();
        let seen = read_until(output_read, b"remote-write", Duration::from_secs(5));
        assert!(seen.windows(12).any(|w| w == b"remote-write"), "{seen:?}");

        stop.store(true, Ordering::Relaxed);
        handle.join().unwrap();
        child.release_slave_hold();
        unsafe {
            libc::kill(child.pid, libc::SIGKILL);
        }
        let _ = reap(&child, None, true);
        unsafe {
            for fd in [
                input_read,
                input_write,
                output_read,
                output_write,
                child.master,
            ] {
                libc::close(fd);
            }
        }
    }

    /// A paste write wraps the text in the bracketed-paste delimiters,
    /// newlines included, as one write.
    #[test]
    fn paste_write_wraps_multiline_text_in_bracketed_paste() {
        let (read, write) = pipe();
        let writer = PtyWriter::new(write);
        writer
            .lock()
            .write_paste(b"first line\nsecond line")
            .unwrap();
        let seen = read_until(read, PASTE_END, Duration::from_secs(2));
        assert_eq!(seen, b"\x1b[200~first line\nsecond line\x1b[201~".to_vec());
        unsafe {
            libc::close(read);
            libc::close(write);
        }
    }

    /// Typing leaves a draft; a remote write waiting for it is held until the
    /// user submits, interrupts or kills the line.
    #[test]
    fn a_pending_local_draft_holds_a_remote_write_until_it_clears() {
        let (read, write) = pipe();
        let writer = PtyWriter::new(write);
        assert!(writer.wait_for_clear_draft(Duration::from_millis(10)));

        writer.relay_local_input(b"half a thou").unwrap();
        assert!(writer.local_draft_pending());
        assert!(!writer.wait_for_clear_draft(Duration::from_millis(60)));

        let submitter = writer.clone();
        let submit = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(80));
            submitter.relay_local_input(b"ght\r").unwrap();
        });
        assert!(writer.wait_for_clear_draft(Duration::from_secs(2)));
        submit.join().unwrap();

        for clear in [b"x\x03".as_slice(), b"y\x15".as_slice()] {
            writer.relay_local_input(clear).unwrap();
            assert!(
                !writer.local_draft_pending(),
                "{clear:?} should clear the draft"
            );
        }
        unsafe {
            libc::close(read);
            libc::close(write);
        }
    }
}
