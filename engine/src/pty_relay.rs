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

/// Maximum time an unsubmitted local keystroke may hold off a remote write.
///
/// Tracking a modern TUI's exact line buffer from raw stdin bytes is unsound in
/// general (history recall, readline kills, vim mode, slash menus, IME). Even
/// if a sequence leaves `chars > 0`, the hold always expires after this window
/// so a remote send can never wedge permanently.
pub const LOCAL_DRAFT_MAX_IDLE: Duration = Duration::from_secs(3);

/// An estimate of whether the user is actively typing an unsubmitted line.
///
/// Printable input increments `chars` and stamps `last_typing_at`; Enter, ^C,
/// ^U (including their Kitty `CSI u` forms) or backspacing to zero clears it
/// immediately; and any non-cleared state expires after `LOCAL_DRAFT_MAX_IDLE`.
/// ECMA-48 control strings (`OSC`, `DCS`, `APC`, `SOS`, `PM`), `CSI` cursor/
/// focus/device-attribute/mouse reports, and `SS3` sequences are skipped whole
/// so terminal probe replies never look like user typing.
#[derive(Default)]
struct DraftState {
    chars: usize,
    last_typing_at: Option<Instant>,
    escape: EscapeState,
}

const CSI_BUF_CAP: usize = 24;
const CONTROL_STRING_CAP: u16 = 4096;

#[derive(Default, Clone, Copy, PartialEq)]
enum EscapeState {
    #[default]
    None,
    /// After ESC: the next byte decides the sequence kind.
    Start,
    /// Just after `ESC [`: `M` starts an X10 mouse report.
    CsiFirst,
    /// Inside CSI (`ESC [` ...), ended by a byte in 0x40..=0x7e.
    Csi { buf: [u8; CSI_BUF_CAP], len: u8 },
    /// An X10 mouse report: three raw bytes follow `ESC [ M`.
    Mouse(u8),
    /// Inside SS3 (`ESC O` x): one more byte.
    Ss3,
    /// Inside an ECMA-48 control string (`OSC` `]`, `DCS` `P`, `APC` `_`,
    /// `SOS` `X`, `PM` `^`), ended by BEL (`0x07`) or ST (`ESC \` / `0x9c`).
    ControlString { prev_esc: bool, seen: u16 },
}

impl DraftState {
    fn clear(&mut self) {
        self.chars = 0;
        self.last_typing_at = None;
    }

    fn record_printable(&mut self, now: Instant) {
        self.chars += 1;
        self.last_typing_at = Some(now);
    }

    fn record_backspace(&mut self) {
        self.chars = self.chars.saturating_sub(1);
        if self.chars == 0 {
            self.last_typing_at = None;
        }
    }

    fn handle_kitty_csi_u(&mut self, params: &[u8], now: Instant) {
        // Capability replies (`CSI ? flags u`) and mode changes (`CSI > flags u`,
        // `CSI < u`, `CSI = ... u`) are terminal protocol traffic, not keys.
        if matches!(params.first(), Some(b'?' | b'>' | b'<' | b'=')) {
            return;
        }
        let Ok(text) = std::str::from_utf8(params) else {
            return;
        };
        let mut fields = text.split(';');
        let Some(key_field) = fields.next() else {
            return;
        };
        let key_code: u32 = match key_field.split(':').next().unwrap_or("").parse() {
            Ok(code) => code,
            Err(_) => return,
        };
        let (mods, event_type) = match fields.next() {
            Some(mod_field) => {
                let mut sub = mod_field.split(':');
                let mods = sub.next().unwrap_or("1").parse::<u32>().unwrap_or(1);
                let event = sub.next().unwrap_or("1").parse::<u32>().unwrap_or(1);
                (mods, event)
            }
            None => (1, 1),
        };
        // Event type 3 is key release in Kitty's progressive enhancement mode.
        if event_type == 3 {
            return;
        }
        let mod_bits = mods.saturating_sub(1);
        let ctrl = (mod_bits & 4) != 0;
        let alt_or_super = (mod_bits & (2 | 8)) != 0;
        if (key_code == 13 || key_code == 57414) && mod_bits == 0 {
            // Plain Enter / Keypad Enter submits. (Shift+Enter inserts a newline.)
            self.clear();
        } else if ctrl && matches!(key_code, 99 | 67 | 117 | 85) {
            // Ctrl+C or Ctrl+U clears the active line.
            self.clear();
        } else if key_code == 127 || key_code == 8 {
            self.record_backspace();
        } else if key_code >= 0x20
            && key_code != 127
            && !(57344..=63743).contains(&key_code)
            && !ctrl
            && !alt_or_super
        {
            self.record_printable(now);
        }
    }

    fn note(&mut self, byte: u8, now: Instant) {
        match self.escape {
            EscapeState::Start => match byte {
                b'[' => {
                    self.escape = EscapeState::CsiFirst;
                    return;
                }
                b'O' => {
                    self.escape = EscapeState::Ss3;
                    return;
                }
                // OSC (`]`), DCS (`P`), APC (`_`, Kitty graphics), SOS (`X`), PM (`^`).
                b']' | b'P' | b'_' | b'X' | b'^' => {
                    self.escape = EscapeState::ControlString {
                        prev_esc: false,
                        seen: 0,
                    };
                    return;
                }
                // A bare Escape key followed by a control byte or another
                // ESC: the Escape was its own key, so the next byte is read
                // normally (an Enter right after Escape still submits).
                0x00..=0x1f | 0x7f => self.escape = EscapeState::None,
                // ESC then a printable byte is Alt+key (or `ESC \`): consumed.
                _ => {
                    self.escape = EscapeState::None;
                    return;
                }
            },
            EscapeState::CsiFirst => {
                if byte == b'M' {
                    self.escape = EscapeState::Mouse(3);
                } else if (0x40..=0x7e).contains(&byte) {
                    self.escape = EscapeState::None;
                } else {
                    let mut buf = [0u8; CSI_BUF_CAP];
                    buf[0] = byte;
                    self.escape = EscapeState::Csi { buf, len: 1 };
                }
                return;
            }
            EscapeState::Mouse(left) => {
                self.escape = if left > 1 {
                    EscapeState::Mouse(left - 1)
                } else {
                    EscapeState::None
                };
                return;
            }
            EscapeState::Csi { mut buf, mut len } => {
                if (0x40..=0x7e).contains(&byte) {
                    let slice_len = usize::from(len).min(CSI_BUF_CAP);
                    self.escape = EscapeState::None;
                    if byte == b'u' {
                        self.handle_kitty_csi_u(&buf[..slice_len], now);
                    }
                } else if usize::from(len) < CSI_BUF_CAP {
                    buf[usize::from(len)] = byte;
                    len += 1;
                    self.escape = EscapeState::Csi { buf, len };
                } else {
                    self.escape = EscapeState::Csi { buf, len };
                }
                return;
            }
            EscapeState::Ss3 => {
                self.escape = EscapeState::None;
                return;
            }
            EscapeState::ControlString { prev_esc, seen } => {
                if prev_esc && byte == b'\\' {
                    self.escape = EscapeState::None;
                } else if matches!(byte, 0x07 | 0x9c | 0x18 | 0x1a | b'\r' | b'\n')
                    || seen >= CONTROL_STRING_CAP
                {
                    self.escape = EscapeState::None;
                } else {
                    self.escape = EscapeState::ControlString {
                        prev_esc: byte == 0x1b,
                        seen: seen.saturating_add(1),
                    };
                }
                return;
            }
            EscapeState::None => {}
        }
        match byte {
            0x1b => self.escape = EscapeState::Start,
            // Enter submits; ^C interrupts; ^U kills the line.
            b'\r' | b'\n' | 0x03 | 0x15 => self.clear(),
            // Backspace and DEL remove a character.
            0x08 | 0x7f => self.record_backspace(),
            // Other control bytes (tab completion, ^A, ^E ...) do not change
            // how much text is on the line.
            0x00..=0x1f => {}
            // UTF-8 continuation bytes belong to the character already counted.
            0x80..=0xbf => {}
            _ => self.record_printable(now),
        }
    }

    fn is_pending_at(&self, now: Instant) -> bool {
        self.chars > 0
            && self
                .last_typing_at
                .is_some_and(|at| now.saturating_duration_since(at) < LOCAL_DRAFT_MAX_IDLE)
    }
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
        self.note_local_input_at(bytes, Instant::now());
    }

    pub(crate) fn note_local_input_at(&self, bytes: &[u8], now: Instant) {
        let mut draft = self
            .inner
            .draft
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        for byte in bytes {
            draft.note(*byte, now);
        }
    }

    /// Whether the user has typed something recently and not yet submitted.
    pub fn local_draft_pending(&self) -> bool {
        self.local_draft_pending_at(Instant::now())
    }

    pub(crate) fn local_draft_pending_at(&self, now: Instant) -> bool {
        self.inner
            .draft
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .is_pending_at(now)
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
    /// for EOF forever. Close inherited descriptors 3..1024 in the child,
    /// rather than using a shell loop that can close the shell's own fds.
    fn cat_child() -> PtyChild {
        let cat = CString::new("/bin/cat").unwrap();
        let path_env = CString::new("PATH=/usr/bin:/bin").unwrap();
        let argv = [cat.as_ptr(), std::ptr::null()];
        let env = [path_env.as_ptr(), std::ptr::null()];
        let mut master = -1;
        let mut slave_name = [0 as libc::c_char; 1024];
        let slave_name_ptr = if cfg!(target_os = "macos") {
            slave_name.as_mut_ptr()
        } else {
            std::ptr::null_mut()
        };
        let mut size = terminal_winsize(-1);
        let pid =
            unsafe { libc::forkpty(&mut master, slave_name_ptr, std::ptr::null_mut(), &mut size) };
        assert!(
            pid >= 0,
            "forkpty failed: {}",
            std::io::Error::last_os_error()
        );
        if pid == 0 {
            unsafe {
                for fd in 3..1024 {
                    libc::close(fd);
                }
                libc::execve(cat.as_ptr(), argv.as_ptr(), env.as_ptr());
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
            panic!("open PTY slave hold failed: {error}");
        }
        PtyChild {
            pid,
            master,
            slave_hold,
        }
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

        // Backspacing a draft away empties it; escape sequences and other
        // control bytes never start one.
        writer.relay_local_input(b"ab\x7f\x7f").unwrap();
        assert!(!writer.local_draft_pending(), "backspaced away");
        writer
            .relay_local_input(b"\x1b[A\x1b[B\x1bOP\x1b[I\x1b[O\x1b[200~\x09\x01")
            .unwrap();
        assert!(
            !writer.local_draft_pending(),
            "arrows, F1, focus, paste start, tab, ^A"
        );
        writer.relay_local_input("é".as_bytes()).unwrap();
        writer.relay_local_input(b"\x7f").unwrap();
        assert!(
            !writer.local_draft_pending(),
            "one multibyte character, one backspace"
        );
        // A bare Escape does not swallow the Enter after it, and an X10
        // mouse report's raw bytes are not typing.
        writer.relay_local_input(b"draft\x1b\r").unwrap();
        assert!(!writer.local_draft_pending(), "Escape then Enter submits");
        writer.relay_local_input(b"\x1b[M !!").unwrap();
        assert!(!writer.local_draft_pending(), "X10 mouse report");
        writer.relay_local_input(b"\x1b[<0;10;5M").unwrap();
        assert!(!writer.local_draft_pending(), "SGR mouse report");
        writer.relay_local_input(b"q\x1b[D").unwrap();
        assert!(
            writer.local_draft_pending(),
            "text stays a draft after a cursor move"
        );
        writer.relay_local_input(b"\r").unwrap();

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

    #[test]
    fn terminal_query_replies_and_kitty_keyboard_protocol_never_wedge_local_draft() {
        let (read, write) = pipe();
        let writer = PtyWriter::new(write);
        let t0 = Instant::now();

        // Real Bubbletea / terminal startup corpus: OSC 10/11 foreground &
        // background replies (ST and BEL), XTVERSION DCS, Kitty graphics APC,
        // DA1/DA2, CPR, Kitty keyboard capability reply, and focus reports.
        let startup_corpus = concat!(
            "\x1b]10;rgb:ffff/ffff/ffff\x1b\\",
            "\x1b]11;rgb:1a1a/1b1b/2626\x07",
            "\x1bP>|xterm-kitty(0.36.0)\x1b\\",
            "\x1b_Gi=31;OK\x1b\\",
            "\x1b[?62;4c",
            "\x1b[>1;388;0c",
            "\x1b[1;1R",
            "\x1b[?1u",
            "\x1b[I\x1b[O",
        );
        writer.note_local_input_at(startup_corpus.as_bytes(), t0);
        assert!(
            !writer.local_draft_pending_at(t0),
            "terminal query replies must never count as user typing"
        );

        // Kitty keyboard protocol typing (`CSI 104 u` = 'h', `CSI 105 u` = 'i')
        // followed by Kitty Enter (`CSI 13 u`) clears immediately.
        writer.note_local_input_at(b"\x1b[104u\x1b[105u", t0);
        assert!(writer.local_draft_pending_at(t0));
        // Key release (`:3`) is ignored.
        writer.note_local_input_at(b"\x1b[13;1:3u", t0);
        assert!(writer.local_draft_pending_at(t0));
        // Key press (`CSI 13 u`) submits and clears.
        writer.note_local_input_at(b"\x1b[13u", t0);
        assert!(!writer.local_draft_pending_at(t0));

        // Kitty Backspace (`CSI 127 u`), Ctrl+C (`CSI 99;5 u`), Ctrl+U (`CSI 117;5 u`).
        writer.note_local_input_at(b"a\x1b[127u", t0);
        assert!(!writer.local_draft_pending_at(t0));
        writer.note_local_input_at(b"draft\x1b[99;5u", t0);
        assert!(!writer.local_draft_pending_at(t0));
        writer.note_local_input_at(b"draft\x1b[117;5u", t0);
        assert!(!writer.local_draft_pending_at(t0));

        unsafe {
            libc::close(read);
            libc::close(write);
        }
    }

    #[test]
    fn arbitrary_byte_streams_always_expire_within_the_max_idle_window() {
        let (read, write) = pipe();
        let writer = PtyWriter::new(write);
        let t0 = Instant::now();

        // Deterministic PRNG over 512 adversarial byte sequences (truncated
        // escapes, random UTF-8, partial CSI/OSC, unsubmitted text).
        let mut seed: u64 = 0x9e37_79b9_7f4a_7c15;
        for _ in 0..512 {
            let mut stream = [0u8; 64];
            for slot in &mut stream {
                seed ^= seed << 13;
                seed ^= seed >> 7;
                seed ^= seed << 17;
                *slot = (seed & 0xff) as u8;
            }
            writer.note_local_input_at(&stream, t0);
            assert!(
                !writer.local_draft_pending_at(t0 + LOCAL_DRAFT_MAX_IDLE),
                "wake gate must always open once LOCAL_DRAFT_MAX_IDLE has elapsed"
            );
        }

        unsafe {
            libc::close(read);
            libc::close(write);
        }
    }
}
