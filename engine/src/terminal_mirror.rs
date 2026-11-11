//! SPIKE (terminal-truth-baseline, track C): mirror a relayed terminal to a
//! Runtime Host so a browser can draw it. Not wired into any launcher.
//!
//! The relay hands copies of its output to an `OutputTap` (bounded, never
//! blocks the relay). A sender thread feeds each frame to a `vt100` emulator,
//! stamps it with a running byte offset and sends it over one WebSocket to the
//! Runtime Host. The Runtime Host asks for a snapshot whenever a viewer joins;
//! the emulator's current screen is the late-join mechanism, so the host keeps
//! no byte history. Keystrokes sent by a viewer come back over the same socket
//! and are written through the relay's own `PtyWriter` lock.
//!
//! Wire format, one binary WebSocket message per frame:
//! `kind u8 | ts_us u64 | offset u64 | cols u16 | rows u16 | payload` (little
//! endian). Kinds: 1 output (payload = the bytes, offset = stream offset after
//! them), 2 size, 3 snapshot (payload = escape codes that redraw the screen,
//! offset = stream offset it is current at), 4 gap (offset = frames dropped),
//! 16 input (viewer to producer, payload = keystrokes), 17 resize (viewer
//! to producer, payload = cols u16, rows u16: the producer sets the PTY size).

use std::ffi::CString;
use std::num::NonZeroU16;
use std::os::fd::RawFd;
use std::os::unix::ffi::OsStrExt;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;

use anyhow::Context;
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::tungstenite::Message;

use crate::pty_relay::{self, OutputTap, PtyCommand, PtyWriter, TapFrame};

pub const KIND_OUTPUT: u8 = 1;
pub const KIND_SIZE: u8 = 2;
pub const KIND_SNAPSHOT: u8 = 3;
pub const KIND_GAP: u8 = 4;
pub const KIND_INPUT: u8 = 16;
pub const KIND_RESIZE: u8 = 17;

/// Frames the relay may have queued for the sender before it starts dropping.
const TAP_CAPACITY: usize = 4096;

fn encode(kind: u8, ts_us: u64, offset: u64, cols: u16, rows: u16, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(21 + payload.len());
    out.push(kind);
    out.extend_from_slice(&ts_us.to_le_bytes());
    out.extend_from_slice(&offset.to_le_bytes());
    out.extend_from_slice(&cols.to_le_bytes());
    out.extend_from_slice(&rows.to_le_bytes());
    out.extend_from_slice(payload);
    out
}

fn now_us() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_micros() as u64)
        .unwrap_or(0)
}

fn nz(value: u16) -> NonZeroU16 {
    NonZeroU16::new(value).unwrap_or(NonZeroU16::MIN)
}

/// Escape codes that redraw the emulator's current screen in a fresh terminal.
/// Only the active grid is carried: behind an alternate screen the main
/// screen is not reproduced.
pub fn snapshot_bytes(parser: &vt100::Parser) -> Vec<u8> {
    let screen = parser.screen();
    let mut out = String::new();
    if screen.alternate_screen() {
        out.push_str("\x1b[?1049h");
    }
    out.push_str("\x1b[0m\x1b[H\x1b[2J");
    out.push_str(&screen.state_formatted());
    out.into_bytes()
}

/// Start the sender thread. It ends when the tap's sending side is dropped.
pub fn start(
    url: String,
    mut frames: tokio::sync::mpsc::Receiver<TapFrame>,
    writer: Option<PtyWriter>,
    master: RawFd,
    dump_screen: Option<PathBuf>,
) -> JoinHandle<()> {
    std::thread::spawn(move || {
        let runtime = match tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
        {
            Ok(runtime) => runtime,
            Err(_) => return,
        };
        runtime.block_on(async move {
            let mut parser = vt100::Parser::new(nz(24), nz(80), 0);
            let (mut cols, mut rows) = (80u16, 24u16);
            let mut offset = 0u64;
            let mut socket = None;
            for _ in 0..50 {
                if let Ok((stream, _)) = tokio_tungstenite::connect_async(url.as_str()).await {
                    socket = Some(stream);
                    break;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            let (mut sink, mut stream) = match socket {
                Some(socket) => socket.split(),
                None => {
                    // No host: keep draining so the tap never reports drops
                    // for a mirror nobody is reading.
                    while frames.recv().await.is_some() {}
                    return;
                }
            };
            loop {
                tokio::select! {
                    frame = frames.recv() => {
                        let Some(frame) = frame else { break };
                        let message = match frame {
                            TapFrame::Output { read_at_us, bytes } => {
                                parser.process(&bytes);
                                offset += bytes.len() as u64;
                                encode(KIND_OUTPUT, read_at_us, offset, cols, rows, &bytes)
                            }
                            TapFrame::Size { cols: c, rows: r } => {
                                cols = c;
                                rows = r;
                                parser.set_size(nz(r), nz(c));
                                encode(KIND_SIZE, now_us(), offset, cols, rows, &[])
                            }
                            TapFrame::Gap { dropped } => {
                                encode(KIND_GAP, now_us(), dropped, cols, rows, &[])
                            }
                        };
                        if sink.send(Message::Binary(message.into())).await.is_err() {
                            break;
                        }
                    }
                    incoming = stream.next() => {
                        match incoming {
                            Some(Ok(Message::Text(_))) => {
                                // The only text message is a snapshot request.
                                let snapshot = encode(KIND_SNAPSHOT, now_us(), offset, cols, rows, &snapshot_bytes(&parser));
                                if sink.send(Message::Binary(snapshot.into())).await.is_err() {
                                    break;
                                }
                            }
                            Some(Ok(Message::Binary(data))) => {
                                if data.first() == Some(&KIND_RESIZE) && data.len() >= 5 {
                                    // Another viewer's size becomes the PTY's size, for everyone.
                                    let c = u16::from_le_bytes([data[1], data[2]]);
                                    let r = u16::from_le_bytes([data[3], data[4]]);
                                    if c > 0 && r > 0 {
                                        let mut size: libc::winsize = unsafe { std::mem::zeroed() };
                                        unsafe { libc::ioctl(master, libc::TIOCGWINSZ, &mut size) };
                                        size.ws_col = c;
                                        size.ws_row = r;
                                        unsafe { libc::ioctl(master, libc::TIOCSWINSZ, &size) };
                                        cols = c;
                                        rows = r;
                                        parser.set_size(nz(r), nz(c));
                                        let size_frame = encode(KIND_SIZE, now_us(), offset, cols, rows, &[]);
                                        if sink.send(Message::Binary(size_frame.into())).await.is_err() {
                                            break;
                                        }
                                    }
                                }
                                if data.first() == Some(&KIND_INPUT) {
                                    if let Some(writer) = &writer {
                                        let _ = writer.lock().write(&data[1..]);
                                    }
                                }
                            }
                            Some(Ok(_)) => {}
                            _ => break,
                        }
                    }
                }
            }
            if let Some(path) = dump_screen {
                let _ = std::fs::write(path, parser.screen().contents());
            }
            let _ = sink.close().await;
        });
    })
}

fn resolve(program: &str) -> Option<PathBuf> {
    if program.contains('/') {
        return Some(PathBuf::from(program));
    }
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths)
            .map(|dir| dir.join(program))
            .find(|candidate| candidate.is_file())
    })
}

/// Run `command` through the same PTY relay the Helm launchers use, mirroring
/// its output to `mirror_url` when given. Exits with the command's code.
pub fn run_spike(
    mirror_url: Option<String>,
    dump_screen: Option<PathBuf>,
    command: Vec<String>,
) -> anyhow::Result<i32> {
    let program = command.first().context("a command is required")?;
    let program = resolve(program).context("command not found on PATH")?;
    let mut argv = vec![CString::new(program.as_os_str().as_bytes())?];
    for arg in &command[1..] {
        argv.push(CString::new(arg.as_bytes())?);
    }
    let mut size = pty_relay::terminal_winsize(1);
    let mut env: Vec<(std::ffi::OsString, std::ffi::OsString)> = std::env::vars_os()
        .filter(|(key, _)| key != "LINES" && key != "COLUMNS" && key != "TERM")
        .collect();
    env.push(("LINES".into(), size.ws_row.to_string().into()));
    env.push(("COLUMNS".into(), size.ws_col.to_string().into()));
    env.push(("TERM".into(), "xterm-256color".into()));
    let env = env
        .into_iter()
        .map(|(key, value)| {
            let mut pair = key.as_bytes().to_vec();
            pair.push(b'=');
            pair.extend_from_slice(value.as_bytes());
            CString::new(pair)
        })
        .collect::<Result<Vec<_>, _>>()?;
    let cwd = CString::new(std::env::current_dir()?.as_os_str().as_bytes())?;
    let child = pty_relay::spawn(&PtyCommand { argv, env, cwd }, &mut size)?;
    let stop = Arc::new(AtomicBool::new(false));
    let resized = Arc::new(AtomicBool::new(true));
    let terminal = match (|| -> anyhow::Result<pty_relay::RawTerminal> {
        let terminal = pty_relay::raw_mode(0)?;
        signal_hook::flag::register(libc::SIGWINCH, resized.clone())?;
        signal_hook::flag::register(libc::SIGTERM, stop.clone())?;
        Ok(terminal)
    })() {
        Ok(terminal) => terminal,
        Err(error) => {
            child.abort();
            return Err(error);
        }
    };
    pty_relay::sync_winsize(child.master);
    let writer = PtyWriter::new(child.master);
    let (tap, sender) = match mirror_url {
        Some(url) => {
            let (tap, frames) = OutputTap::new(TAP_CAPACITY);
            let handle = start(url, frames, Some(writer.clone()), child.master, dump_screen);
            (Some(tap), Some(handle))
        }
        None => (None, None),
    };
    let reaped = pty_relay::relay_tapped(&child, &writer, 0, 1, &stop, &resized, tap.as_ref());
    drop(terminal);
    drop(tap);
    if let Some(sender) = sender {
        let _ = sender.join();
    }
    child.release_slave_hold();
    let code = pty_relay::reap(&child, reaped, stop.load(Ordering::Relaxed));
    unsafe {
        libc::close(child.master);
    }
    Ok(code)
}
