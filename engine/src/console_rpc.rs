//! Console turns driven over a provider's RPC stdin (Pi and OMP `--mode rpc`).
//!
//! The provider opens a FIFO in the run directory read-write as its stdin, so it
//! never sees EOF: the turn survives a Machine Agent restart, and Longhouse can
//! write a command (prompt, steer) at any time by opening the FIFO for writing.
//! stdout still goes to the run's file, which the live and recovered monitors
//! tail, so a command's `response` frame is read back from there.

use crate::console_lifecycle::{ConsoleInput, InputFuture};
use anyhow::{Context, Result};
use serde_json::{json, Value};
use std::fs::{File, OpenOptions};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use uuid::Uuid;

pub const RPC_STDIN: &str = "stdin.fifo";
/// `get_state`'s request id: its `sessionId` confirms the native session,
/// because RPC mode prints no session header.
pub const RPC_IDENTITY_ID: &str = "longhouse-identity";
pub const RPC_PROMPT_ID: &str = "longhouse-prompt";

#[derive(Clone)]
pub struct ConsoleRpcInput {
    fifo: PathBuf,
}

impl ConsoleRpcInput {
    pub fn new(fifo: PathBuf) -> Self {
        Self { fifo }
    }
}

impl ConsoleInput for ConsoleRpcInput {
    fn send_input<'a>(&'a self, text: &'a str, images: &'a [PathBuf]) -> InputFuture<'a> {
        Box::pin(async move {
            let command = prompt_command(text, images)?;
            write_command(&self.fifo, &command).await
        })
    }

    fn close_input(&self) -> InputFuture<'_> {
        Box::pin(async move {
            match std::fs::remove_file(&self.fifo) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error).context("closing the Console RPC stdin FIFO"),
            }
        })
    }
}

/// Abort the provider's active response. Closing the FIFO is deliberately
/// separate: it prevents future prompts but must not cancel a running turn.
pub async fn abort(fifo: &Path) -> Result<()> {
    match write_command(fifo, &json!({"type": "abort"})).await {
        Ok(()) => Ok(()),
        Err(error) if fifo_provider_is_gone(&error) => Ok(()),
        Err(error) => Err(error),
    }
}

fn fifo_provider_is_gone(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause.downcast_ref::<std::io::Error>().is_some_and(|error| {
            matches!(
                error.raw_os_error(),
                Some(libc::ENOENT) | Some(libc::ENXIO) | Some(libc::EPIPE)
            )
        })
    })
}

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

/// How long the provider may go without taking a single byte of a command
/// before the write is abandoned.
///
/// A healthy provider drains a command at memory speed (a 617 KB image prompt
/// is accepted in milliseconds), so this is a stall detector, not a transfer
/// budget: any accepted byte restarts it. 10 s is two orders of magnitude over
/// the healthy case, and matches the 10 s `steer` already waits for its
/// response. It exists because a write with no bound is how a provider that
/// stopped reading its stdin held a Console launch for 28 minutes (2026-10-01).
pub const COMMAND_WRITE_STALL: Duration = Duration::from_secs(10);

/// How long a provider gets to print its `ready` frame after spawn. Measured on
/// an idle Mac: 2.6-3.4 s, one cold start at 6.4 s; 20 s is over three times the
/// worst of those and leaves headroom for a loaded laptop.
pub const RPC_READY_DEADLINE: Duration = Duration::from_secs(20);

/// How long a provider gets to acknowledge a `prompt` once the command is
/// written. The acknowledgement is immediate ("`prompt` is acknowledged after
/// the command is accepted, not after a model turn finishes"): it follows the
/// `get_state` response in the same second in every healthy run on this
/// machine. 30 s is more than three times the 8 s the launch already allows for
/// that earlier response.
pub const PROMPT_ACK_DEADLINE: Duration = if cfg!(test) {
    Duration::from_secs(2)
} else {
    Duration::from_secs(30)
};

/// Write one RPC command. The open fails at once when no provider process
/// holds the FIFO (it has exited); the write then waits for the provider to
/// drain a large prompt, off the async runtime, and fails when the provider
/// stops taking bytes for [`COMMAND_WRITE_STALL`].
pub async fn write_command(fifo: &Path, command: &Value) -> Result<()> {
    write_command_within(fifo, command, COMMAND_WRITE_STALL).await
}

pub async fn write_command_within(fifo: &Path, command: &Value, stall: Duration) -> Result<()> {
    let fifo = fifo.to_path_buf();
    let mut line = serde_json::to_vec(command)?;
    line.push(b'\n');
    tokio::task::spawn_blocking(move || write_line_within(&fifo, &line, stall)).await?
}

fn write_line_within(fifo: &Path, line: &[u8], stall: Duration) -> Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(fifo)
        .context("no provider process is reading its RPC stdin")?;
    let fd = file.as_raw_fd();
    let mut written = 0_usize;
    let mut last_progress = Instant::now();
    while written < line.len() {
        match file.write(&line[written..]) {
            Ok(0) => anyhow::bail!("the provider's RPC stdin accepted no bytes"),
            Ok(accepted) => {
                written += accepted;
                last_progress = Instant::now();
            }
            Err(error) if error.kind() == ErrorKind::WouldBlock => {
                let idle = last_progress.elapsed();
                if idle >= stall {
                    anyhow::bail!(
                        "the provider stopped reading its RPC stdin: {written} of {} bytes written, none accepted for {}s",
                        line.len(),
                        idle.as_secs()
                    );
                }
                let mut pollfd = libc::pollfd {
                    fd,
                    events: libc::POLLOUT,
                    revents: 0,
                };
                let wait = (stall - idle).min(Duration::from_millis(250));
                unsafe {
                    libc::poll(&mut pollfd, 1, wait.as_millis() as libc::c_int);
                }
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => {}
            Err(error) => return Err(error).context("writing the RPC command"),
        }
    }
    Ok(())
}

/// Whether the provider has printed its `ready` frame.
///
/// RPC mode writes `ready` "before processing commands", and that is the only
/// moment commands may be written. OMP's stdin reader wedges for good when any
/// byte is already pending in the FIFO while the process is still starting:
/// reproduced on macOS with stock OMP 18.4.5, where a `get_state` written at
/// spawn followed by a 450 KB `prompt` blocks the writer forever with the
/// provider alive and idle (the 2026-10-01 incident), while the identical
/// commands written after `ready` were acknowledged 8 times out of 8.
pub fn stdout_has_ready(stdout_path: &Path) -> bool {
    let Ok(file) = File::open(stdout_path) else {
        return false;
    };
    let mut head = Vec::new();
    // `ready` is the first frame a provider prints; a megabyte is far past any
    // startup burst measured (advisor and command-list frames total ~35 KB).
    if file.take(1024 * 1024).read_to_end(&mut head).is_err() {
        return false;
    }
    head.split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .any(|line| {
            serde_json::from_slice::<Value>(line)
                .ok()
                .is_some_and(|frame| frame.get("type").and_then(Value::as_str) == Some("ready"))
        })
}

/// Whether a prompt that was written and never acknowledged has outlived the
/// time a provider gets to acknowledge it. `age` is how long ago the prompt
/// was handed over, and an `acknowledged` or `rejected` prompt is settled.
pub fn prompt_ack_overdue(acknowledged: bool, rejected: bool, age: Duration) -> bool {
    !acknowledged && !rejected && age >= PROMPT_ACK_DEADLINE
}

pub fn prompt_ack_overdue_reason(age: Duration) -> String {
    format!(
        "the provider did not acknowledge the prompt within {}s; the message was not delivered",
        age.as_secs().max(PROMPT_ACK_DEADLINE.as_secs())
    )
}

/// How long ago an RFC 3339 instant was, or `None` when it cannot be read.
pub fn age_since_rfc3339(instant: &str) -> Option<Duration> {
    let then = chrono::DateTime::parse_from_rfc3339(instant).ok()?;
    (chrono::Utc::now() - then.with_timezone(&chrono::Utc))
        .to_std()
        .ok()
        .or(Some(Duration::ZERO))
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

/// A Pi-family stream event, reduced to what the Runtime Host reads.
///
/// Pi and OMP restate the whole assistant message on every streamed token:
/// `message_update` carries it twice (`message` and
/// `assistantMessageEvent.partial`, encrypted reasoning included), and
/// `agent_end` repeats every message of the turn. Forwarded verbatim that is
/// quadratic in the length of the message: on 2026-10-05 one OMP turn queued
/// 916 MB of runtime events for a 6.6 MB transcript.
///
/// The Runtime Host takes an assistant message's text from the payload's
/// `live_text`, falling back to the message's text blocks when a stream was
/// joined mid-message; from `message` it otherwise reads only identity and
/// stop reason (server/zerg/services/session_live_previews.py). So an
/// assistant message keeps those and its text blocks, and loses the thinking
/// blocks and signatures that were 99.9% of its bytes (0.23 MB of text in
/// 394 MB of messages that day). The host never reads `assistantMessageEvent`,
/// `agent_end`'s `messages`, or `turn_end`'s `toolResults`. Everything else
/// passes through unchanged, including tool events, whose arguments and
/// results Pi's tool previews read.
pub fn runtime_stream_event(event: &Value) -> Value {
    let Some(object) = event.as_object() else {
        return event.clone();
    };
    let event_type = object.get("type").and_then(Value::as_str);
    let mut reduced = serde_json::Map::with_capacity(object.len());
    for (key, value) in object {
        match key.as_str() {
            "assistantMessageEvent" => {}
            "messages" if event_type == Some("agent_end") => {}
            "toolResults" if event_type == Some("turn_end") => {}
            "message" if value.get("role").and_then(Value::as_str) == Some("assistant") => {
                let mut kept: serde_json::Map<String, Value> =
                    ["id", "role", "stopReason", "errorMessage"]
                        .into_iter()
                        .filter_map(|field| Some((field.to_owned(), value.get(field)?.clone())))
                        .collect();
                match value.get("content") {
                    Some(Value::Array(blocks)) => {
                        let text = blocks
                            .iter()
                            .filter(|block| {
                                block.get("type").and_then(Value::as_str) == Some("text")
                            })
                            .cloned()
                            .collect();
                        kept.insert("content".to_owned(), Value::Array(text));
                    }
                    Some(content @ Value::String(_)) => {
                        kept.insert("content".to_owned(), content.clone());
                    }
                    _ => {}
                }
                reduced.insert(key.clone(), Value::Object(kept));
            }
            _ => {
                reduced.insert(key.clone(), value.clone());
            }
        }
    }
    Value::Object(reduced)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::OpenOptionsExt;

    fn fifo_with_reader(dir: &Path) -> (PathBuf, File) {
        let fifo = dir.join(RPC_STDIN);
        create_fifo(&fifo).unwrap();
        // The provider's side: opened read-write, so the pipe never sees EOF.
        let reader = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&fifo)
            .unwrap();
        (fifo, reader)
    }

    #[tokio::test]
    async fn a_provider_that_stops_reading_fails_the_write_instead_of_holding_it_forever() {
        // 2026-10-01: stock OMP stopped reading its stdin mid-prompt and the engine's
        // blocking write held the launch for 28 minutes, until the engine restarted.
        let temp = tempfile::tempdir().unwrap();
        let (fifo, _reader_that_never_reads) = fifo_with_reader(temp.path());
        let command = json!({"type": "prompt", "message": "x".repeat(2 * 1024 * 1024)});

        let started = Instant::now();
        let error = write_command_within(&fifo, &command, Duration::from_millis(300))
            .await
            .unwrap_err();

        assert!(
            error.to_string().contains("stopped reading its RPC stdin"),
            "{error:#}"
        );
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[tokio::test]
    async fn a_provider_that_drains_slowly_is_not_a_stall() {
        // The bound is on progress, not on the transfer: a frame bigger than the
        // pipe buffer is accepted so long as bytes keep being taken.
        let temp = tempfile::tempdir().unwrap();
        let (fifo, mut reader) = fifo_with_reader(temp.path());
        let command = json!({"type": "prompt", "message": "y".repeat(600 * 1024)});
        let expected = serde_json::to_vec(&command).unwrap().len() + 1;
        let drain = std::thread::spawn(move || {
            let mut seen = 0_usize;
            let mut chunk = vec![0_u8; 16 * 1024];
            let mut idle_polls = 0;
            while seen < expected && idle_polls < 400 {
                match reader.read(&mut chunk) {
                    Ok(read) if read > 0 => {
                        seen += read;
                        idle_polls = 0;
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    _ => {
                        idle_polls += 1;
                        std::thread::sleep(Duration::from_millis(5));
                    }
                }
            }
            seen
        });

        write_command_within(&fifo, &command, Duration::from_millis(500))
            .await
            .unwrap();

        assert_eq!(drain.join().unwrap(), expected);
    }

    #[tokio::test]
    async fn writing_to_a_fifo_nobody_holds_open_fails_at_once() {
        let temp = tempfile::tempdir().unwrap();
        let fifo = temp.path().join(RPC_STDIN);
        create_fifo(&fifo).unwrap();

        let error = write_command(&fifo, &json!({"type": "get_state"}))
            .await
            .unwrap_err();

        assert!(
            error.to_string().contains("no provider process"),
            "{error:#}"
        );
    }
    #[tokio::test]
    async fn close_input_unlinks_fifo_without_sending_abort() {
        let temp = tempfile::tempdir().unwrap();
        let (fifo, mut reader) = fifo_with_reader(temp.path());
        let input = ConsoleRpcInput::new(fifo.clone());

        input.close_input().await.unwrap();

        assert!(!fifo.exists());
        let mut bytes = [0_u8; 128];
        let error = reader.read(&mut bytes).unwrap_err();
        assert_eq!(error.kind(), ErrorKind::WouldBlock);
    }

    #[tokio::test]
    async fn abort_sends_an_explicit_abort_command() {
        let temp = tempfile::tempdir().unwrap();
        let (fifo, mut reader) = fifo_with_reader(temp.path());

        abort(&fifo).await.unwrap();

        let mut bytes = [0_u8; 128];
        let count = reader.read(&mut bytes).unwrap();
        let command: Value = serde_json::from_slice(&bytes[..count - 1]).unwrap();
        assert_eq!(command, json!({"type": "abort"}));
    }
    #[tokio::test]
    async fn abort_tolerates_missing_or_readerless_fifo() {
        let temp = tempfile::tempdir().unwrap();
        let missing = temp.path().join("missing.fifo");
        abort(&missing).await.unwrap();

        let readerless = temp.path().join("readerless.fifo");
        create_fifo(&readerless).unwrap();
        abort(&readerless).await.unwrap();
    }

    #[test]
    fn ready_is_the_providers_own_frame_not_anything_on_stdout() {
        let temp = tempfile::tempdir().unwrap();
        let stdout = temp.path().join("stdout.log");
        assert!(!stdout_has_ready(&stdout), "no file yet");
        std::fs::write(
            &stdout,
            "{\"type\":\"advisor_cost_changed\"}\n{\"type\":\"rea",
        )
        .unwrap();
        assert!(!stdout_has_ready(&stdout), "a partial frame is not ready");
        std::fs::write(&stdout, "{\"type\":\"ready\",\"protocolVersion\":\"1\"}\n").unwrap();
        assert!(stdout_has_ready(&stdout));
    }

    #[test]
    fn age_since_reads_an_rfc3339_instant() {
        let an_hour_ago = (chrono::Utc::now() - chrono::Duration::hours(1)).to_rfc3339();
        let age = age_since_rfc3339(&an_hour_ago).unwrap();
        assert!(age >= Duration::from_secs(3590) && age <= Duration::from_secs(3700));
        assert!(age_since_rfc3339("yesterday").is_none());
    }

    /// A streamed token must not restate the message's reasoning, which was
    /// nearly all of its bytes, while every field the Runtime Host reads
    /// survives. The visible text is still restated, as `live_text` already is.
    #[test]
    fn runtime_stream_event_keeps_what_the_runtime_host_reads_and_no_repeated_message() {
        let reasoning = "r".repeat(50_000);
        let message = json!({
            "id": "msg-1",
            "role": "assistant",
            "stopReason": null,
            "content": [
                {"type": "thinking", "thinking": "", "thinkingSignature": reasoning},
                {"type": "text", "text": "Hello"}
            ],
            "usage": {"input": 1, "output": 2}
        });
        let update = json!({
            "type": "message_update",
            "message": message,
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 1, "delta": "o", "partial": message}
        });

        let reduced = runtime_stream_event(&update);

        // Identity, stop reason and the text blocks the host falls back to
        // when it has no live_text; never the reasoning.
        assert_eq!(
            reduced,
            json!({
                "type": "message_update",
                "message": {
                    "id": "msg-1",
                    "role": "assistant",
                    "stopReason": null,
                    "content": [{"type": "text", "text": "Hello"}]
                }
            })
        );
        assert!(serde_json::to_vec(&reduced).unwrap().len() < 200);

        let agent_end = json!({"type": "agent_end", "willContinue": false, "messages": [message]});
        assert_eq!(
            runtime_stream_event(&agent_end),
            json!({"type": "agent_end", "willContinue": false})
        );

        // Not the streamed assistant message: kept whole.
        let user = json!({"type": "message_end", "message": {"role": "user", "content": "do it"}});
        assert_eq!(runtime_stream_event(&user), user);
        let tool = json!({
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "ok"}]},
            "isError": false
        });
        assert_eq!(runtime_stream_event(&tool), tool);
    }
}
