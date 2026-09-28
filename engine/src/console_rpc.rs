//! Console turns driven over a provider's RPC stdin (Pi and OMP `--mode rpc`).
//!
//! The provider opens a FIFO in the run directory read-write as its stdin, so it
//! never sees EOF: the turn survives a Machine Agent restart, and Longhouse can
//! write a command (prompt, steer) at any time by opening the FIFO for writing.
//! stdout still goes to the run's file, which the live and recovered monitors
//! tail, so a command's `response` frame is read back from there.

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};
use serde_json::{json, Value};
use uuid::Uuid;

pub const RPC_STDIN: &str = "stdin.fifo";
/// `get_state`'s request id: its `sessionId` confirms the native session,
/// because RPC mode prints no session header.
pub const RPC_IDENTITY_ID: &str = "longhouse-identity";
pub const RPC_PROMPT_ID: &str = "longhouse-prompt";

pub fn create_fifo(path: &Path) -> Result<()> {
    let _ = std::fs::remove_file(path);
    let c_path = std::ffi::CString::new(path.as_os_str().as_bytes())?;
    if unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) } != 0 {
        return Err(std::io::Error::last_os_error()).context("creating the Console RPC stdin FIFO");
    }
    Ok(())
}

/// The child side: open the FIFO read-write as fd 0. Call from `pre_exec`.
///
/// # Safety
/// Async-signal-safe calls only (`open`, `dup2`, `close`).
pub unsafe fn adopt_fifo_as_stdin(path: &std::ffi::CStr) -> std::io::Result<()> {
    let fd = libc::open(path.as_ptr(), libc::O_RDWR);
    if fd < 0 || libc::dup2(fd, 0) < 0 {
        return Err(std::io::Error::last_os_error());
    }
    if fd != 0 {
        libc::close(fd);
    }
    Ok(())
}

/// Write one RPC command. The open fails at once when no provider process
/// holds the FIFO (it has exited); the write then blocks only while the
/// provider drains a large prompt, off the async runtime.
pub async fn write_command(fifo: &Path, command: &Value) -> Result<()> {
    let fifo = fifo.to_path_buf();
    let mut line = serde_json::to_vec(command)?;
    line.push(b'\n');
    tokio::task::spawn_blocking(move || -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&fifo)
            .context("no provider process is reading its RPC stdin")?;
        use std::os::unix::io::AsRawFd;
        let fd = file.as_raw_fd();
        unsafe {
            let flags = libc::fcntl(fd, libc::F_GETFL);
            libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
        }
        file.write_all(&line)?;
        Ok(())
    })
    .await?
}

pub fn prompt_command(prompt: &str, image_paths: &[PathBuf]) -> Result<Value> {
    use base64::Engine as _;
    let mut images = Vec::new();
    for path in image_paths {
        let bytes = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
        let mime = match path
            .extension()
            .and_then(|ext| ext.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref()
        {
            Some("png") => "image/png",
            Some("jpg" | "jpeg") => "image/jpeg",
            Some("gif") => "image/gif",
            Some("webp") => "image/webp",
            _ => "application/octet-stream",
        };
        images.push(json!({
            "type": "image",
            "data": base64::engine::general_purpose::STANDARD.encode(bytes),
            "mimeType": mime,
        }));
    }
    let mut command = json!({"id": RPC_PROMPT_ID, "type": "prompt", "message": prompt});
    if !images.is_empty() {
        command["images"] = Value::Array(images);
    }
    Ok(command)
}

/// Send an RPC `steer` to the provider holding `fifo` and wait for its own
/// `response` in `stdout_path`. `Err("turn_not_steerable")` when the provider
/// is gone or refuses; `Err("steer_outcome_unknown")` when no response came.
pub async fn steer(fifo: &Path, stdout_path: &Path, text: &str) -> std::result::Result<(), String> {
    let not_steerable = || "turn_not_steerable".to_string();
    if !fifo.exists() {
        return Err(not_steerable());
    }
    let id = format!("longhouse-steer-{}", Uuid::new_v4());
    let start = std::fs::metadata(stdout_path)
        .map(|meta| meta.len())
        .unwrap_or(0);
    write_command(fifo, &json!({"id": id, "type": "steer", "message": text}))
        .await
        .map_err(|_| not_steerable())?;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    while tokio::time::Instant::now() < deadline {
        if let Some(response) = find_response(stdout_path, start, &id) {
            return if response.get("success").and_then(Value::as_bool) == Some(true) {
                Ok(())
            } else {
                Err(not_steerable())
            };
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Err("steer_outcome_unknown".to_string())
}

pub fn find_response(stdout_path: &Path, start: u64, id: &str) -> Option<Value> {
    use std::io::{Seek, SeekFrom};
    let mut file = File::open(stdout_path).ok()?;
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut tail = String::new();
    file.read_to_string(&mut tail).ok()?;
    tail.lines()
        .filter(|line| line.contains(id))
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find(|event| {
            event.get("type").and_then(Value::as_str) == Some("response")
                && event.get("id").and_then(Value::as_str) == Some(id)
        })
}
