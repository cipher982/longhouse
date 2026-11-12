//! Live reply text for Claude Helm.
//!
//! Claude writes an assistant line to its transcript only when the whole message
//! has ended, and registering its `MessageDisplay` hook changes how its own
//! terminal paints. The source is the `turn.step` stream that Longhouse's Claude
//! Code mod (`engine/assets/claude-mod`) already has loaded: the mod forwards
//! each batch of reply text, about ten times a second, to this relay over a Unix
//! socket (`POST /live`, a JSON array of entries). The relay accumulates it with
//! the shared helper and publishes the cumulative text as the session's status
//! preview, the way the Pi and OMP Helm launchers do; the Machine Agent ships it
//! and the Runtime Host publishes it (`claude_helm_channel`).
//!
//! `longhouse claude` starts the relay beside Claude (`claude-live-relay`) and
//! ends it by closing its standard input. Without the relay the mod forwards
//! nothing and Helm behaves as it did.
//!
//! The mod forwards reply text, `thinking` text and the start of each `tool`
//! call (its name and id; a `tool` chunk is the model beginning the call, so it
//! is "tool started", not a measure of when the tool runs). Each is published as
//! a preview that says what it is (`StatusPreview::kind`). Claude has no stream
//! of a running tool's output and none of a subagent's text, so none is posted.
//! Its argument JSON (`input` chunks) is shown once it parses. The mod drops
//! `stop` chunks.

use serde::Deserialize;

/// The status slot's transport, and the Runtime Host's allowlisted source.
pub const TRANSPORT: &str = "claude_helm_channel";
/// `progress_kind` of the preview events.
pub const PROGRESS_KIND: &str = "claude_helm_stream";
/// The mod reads the relay's socket path from this variable.
pub const SOCKET_ENV: &str = "LONGHOUSE_CLAUDE_LIVE_SOCKET";

/// One forwarded entry: reply text, thinking, or the start of a tool call, of
/// step `step` of turn `turn`, or `done` when the turn ended. An entry with no
/// `kind` is reply text, which is all an older mod sends.
#[derive(Debug, Clone, Default, Deserialize, PartialEq)]
pub struct Entry {
    pub turn: String,
    #[serde(default)]
    pub step: Option<u64>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub done: bool,
    /// `thinking` or `tool`; absent for reply text.
    #[serde(default)]
    pub kind: Option<String>,
    /// The tool call's `tool_use_id` (kind `tool`).
    #[serde(default)]
    pub id: Option<String>,
    /// The tool's name (kind `tool`).
    #[serde(default)]
    pub name: Option<String>,
    /// The content block a `tool` or `input` entry belongs to.
    #[serde(default)]
    pub index: Option<u64>,
    /// The next piece of a tool call's argument JSON (kind `input`).
    #[serde(default)]
    pub json: Option<String>,
}

/// What to publish after applying an entry.
#[derive(Debug, Clone, PartialEq)]
pub struct Preview {
    pub turn_id: String,
    pub seq: u64,
    pub text: String,
    pub completed: bool,
    /// `thinking`, `tool` or `subagent`; `None` is reply text.
    pub kind: Option<&'static str>,
    pub tool_name: Option<String>,
    pub tool_call_id: Option<String>,
    pub tool_input: Option<serde_json::Value>,
}

/// One tool call of the current step: its block, id, name and the argument JSON
/// received so far.
#[derive(Debug)]
struct ToolCall {
    index: Option<u64>,
    id: String,
    name: String,
    json: String,
    seq: u64,
}

/// What a tool call's arguments say in one line, for the preview text: the
/// command, a subagent's description, a path, pattern or query, else the JSON.
fn arguments_summary(name: &str, input: &serde_json::Value) -> String {
    let keys: &[&str] = if crate::status_slot::tool_preview_kind(name) == "subagent" {
        &["description", "prompt", "subagent_type"]
    } else {
        &[
            "command",
            "description",
            "file_path",
            "path",
            "pattern",
            "url",
            "query",
            "prompt",
        ]
    };
    keys.iter()
        .find_map(|key| input.get(*key).and_then(|value| value.as_str()))
        .filter(|text| !text.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| input.to_string())
}

/// The live content of the message Claude is writing: reply text and thinking,
/// each cumulative, reset when a new step (a new assistant message) or a new
/// turn begins.
#[derive(Debug, Default)]
pub struct LiveText {
    text: String,
    thinking: String,
    seq: u64,
    thinking_seq: u64,
    tools: Vec<ToolCall>,
    turn_seq: u64,
    message_seq: u64,
    turn: Option<String>,
    step: Option<u64>,
}

impl LiveText {
    pub fn apply(&mut self, run_id: &str, entry: &Entry) -> Option<Preview> {
        if entry.done {
            // Close the preview of the turn that ended, if it said anything.
            if self.turn.as_deref() != Some(entry.turn.as_str()) || self.text.is_empty() {
                return None;
            }
            self.seq = self.seq.saturating_add(1);
            return Some(self.preview(run_id, true));
        }
        match entry.kind.as_deref() {
            None | Some("text") => {
                let delta = entry.text.as_deref().filter(|text| !text.is_empty())?;
                self.enter(entry)?;
                crate::status_slot::append_live_text(&mut self.text, delta);
                self.seq = self.seq.saturating_add(1);
                Some(self.preview(run_id, false))
            }
            Some("thinking") => {
                let delta = entry.text.as_deref().filter(|text| !text.is_empty())?;
                self.enter(entry)?;
                crate::status_slot::append_live_text(&mut self.thinking, delta);
                self.thinking_seq = self.thinking_seq.saturating_add(1);
                Some(Preview {
                    turn_id: format!("{}:thinking", self.message_id(run_id)),
                    seq: self.thinking_seq,
                    text: self.thinking.clone(),
                    completed: false,
                    kind: Some("thinking"),
                    tool_name: None,
                    tool_call_id: None,
                    tool_input: None,
                })
            }
            Some("tool") => {
                let name = entry.name.as_deref().filter(|name| !name.is_empty())?;
                let id = entry.id.as_deref().filter(|id| !id.is_empty())?;
                self.enter(entry)?;
                self.tools.push(ToolCall {
                    index: entry.index,
                    id: id.to_string(),
                    name: name.to_string(),
                    json: String::new(),
                    seq: 1,
                });
                Some(self.tool_preview(run_id, self.tools.len() - 1, name.to_string(), None))
            }
            Some("input") => {
                let piece = entry.json.as_deref().filter(|json| !json.is_empty())?;
                self.enter(entry)?;
                let position = self
                    .tools
                    .iter()
                    .rposition(|call| entry.index.is_some() && call.index == entry.index)?;
                let call = &mut self.tools[position];
                crate::status_slot::append_live_text(&mut call.json, piece);
                // Show the arguments once they parse; a partial object says
                // nothing a reader can use.
                let input: serde_json::Value = serde_json::from_str(&call.json).ok()?;
                if !input.is_object() {
                    return None;
                }
                call.seq = call.seq.saturating_add(1);
                let text = arguments_summary(&call.name, &input);
                Some(self.tool_preview(run_id, position, text, Some(input)))
            }
            Some(_) => None,
        }
    }

    /// Move to the entry's turn and step, starting fresh content for a new one.
    fn enter(&mut self, entry: &Entry) -> Option<()> {
        let step = entry.step?;
        if self.turn.as_deref() != Some(entry.turn.as_str()) {
            self.turn = Some(entry.turn.clone());
            self.turn_seq = self.turn_seq.saturating_add(1);
            self.message_seq = 0;
            self.step = None;
            self.text.clear();
            self.thinking.clear();
            self.tools.clear();
            self.seq = 0;
            self.thinking_seq = 0;
        }
        if self.step != Some(step) {
            self.step = Some(step);
            self.message_seq = self.message_seq.saturating_add(1);
            self.text.clear();
            self.thinking.clear();
            self.tools.clear();
            self.seq = 0;
            self.thinking_seq = 0;
        }
        Some(())
    }

    fn tool_preview(
        &self,
        run_id: &str,
        position: usize,
        text: String,
        input: Option<serde_json::Value>,
    ) -> Preview {
        let call = &self.tools[position];
        Preview {
            turn_id: format!("{}:tool:{}", self.message_id(run_id), call.id),
            seq: call.seq,
            text,
            completed: false,
            kind: Some(crate::status_slot::tool_preview_kind(&call.name)),
            tool_name: Some(call.name.clone()),
            tool_call_id: Some(call.id.clone()),
            tool_input: input,
        }
    }

    fn message_id(&self, run_id: &str) -> String {
        format!("{run_id}:{}:{}", self.turn_seq, self.message_seq)
    }

    fn preview(&self, run_id: &str, completed: bool) -> Preview {
        Preview {
            turn_id: self.message_id(run_id),
            seq: self.seq,
            text: self.text.clone(),
            completed,
            kind: None,
            tool_name: None,
            tool_call_id: None,
            tool_input: None,
        }
    }
}

/// The preview as a status slot states it. The slot carries no phase (the empty
/// phase is not a wire phase and is not recorded locally): Claude's own hooks
/// own the session's phase, and this only adds content.
pub fn status_preview(
    preview: &Preview,
    provider_session_id: &str,
) -> crate::status_slot::StatusPreview {
    crate::status_slot::StatusPreview {
        turn_id: preview.turn_id.clone(),
        seq: preview.seq,
        live_text: preview.text.clone(),
        turn_completed: preview.completed,
        progress_kind: PROGRESS_KIND.into(),
        provider_session_id: (!provider_session_id.is_empty())
            .then(|| provider_session_id.to_string()),
        kind: preview.kind.map(str::to_string),
        tool_name: preview.tool_name.clone(),
        tool_call_id: preview.tool_call_id.clone(),
        tool_input: preview.tool_input.clone(),
        tool_state: preview.tool_name.as_ref().map(|_| "running".to_string()),
        ..Default::default()
    }
}

#[cfg(unix)]
pub use relay::run;

#[cfg(unix)]
mod relay {
    use super::*;
    use anyhow::{Context, Result};
    use std::io::{Read, Write};
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    const MAX_HEADER_BYTES: usize = 16 * 1024;
    const MAX_BODY_BYTES: usize = 1024 * 1024;
    const READ_TIMEOUT: Duration = Duration::from_secs(10);

    struct Relay {
        session_id: String,
        run_id: String,
        provider_session_id: String,
        live: Mutex<LiveText>,
        /// Where tool and subagent previews go (see `publish`), and whether one
        /// may still be waiting there.
        outbox_dir: PathBuf,
        call_in_outbox: Mutex<bool>,
        status: crate::status_slot::StatusPublisher,
    }

    /// How long text that follows a tool call waits for the call's event to
    /// leave the outbox (the Machine Agent posts it and removes the file).
    const OUTBOX_DRAIN_WAIT: Duration = Duration::from_millis(1500);

    impl Relay {
        fn apply(&self, entries: &[Entry]) {
            for entry in entries {
                let preview = self
                    .live
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .apply(&self.run_id, entry);
                if let Some(preview) = preview {
                    self.publish(&preview);
                }
            }
        }

        /// A tool or subagent call is a discrete event, and an async subagent
        /// returns within milliseconds, so the next step's text follows its
        /// arguments at once. The status slot holds one preview and the Machine
        /// Agent reads it on its own beat, so the call would be overwritten
        /// before it was ever posted. Calls therefore go through the durable
        /// runtime-event outbox, which keeps every event and posts it in turn.
        /// Text or thinking that follows a call waits (bounded) for that event
        /// to be posted, so the host sees the call first and the text after it.
        /// Reply text and thinking stay on the slot, which coalesces a stream.
        fn publish(&self, preview: &Preview) {
            let mut pending = self
                .call_in_outbox
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if preview.tool_name.is_some() {
                let slot = self.slot_for(preview);
                let events = crate::status_slot::runtime_events_since(&slot, None);
                if let Some(event) = events
                    .iter()
                    .find(|event| event["kind"] == "progress_signal")
                {
                    if crate::outbox::enqueue_runtime_event(&self.outbox_dir, event).is_ok() {
                        *pending = true;
                    }
                }
                return;
            }
            if std::mem::take(&mut *pending) {
                let deadline = std::time::Instant::now() + OUTBOX_DRAIN_WAIT;
                while std::time::Instant::now() < deadline && outbox_has_events(&self.outbox_dir) {
                    std::thread::sleep(Duration::from_millis(20));
                }
            }
            self.status.publish(
                crate::status_slot::StatusUpdate::phase(
                    &self.session_id,
                    &self.run_id,
                    &chrono::Utc::now().to_rfc3339(),
                    "",
                )
                .with_preview(Some(status_preview(preview, &self.provider_session_id))),
            );
        }

        /// The status slot a preview would be stated from, for its event.
        fn slot_for(&self, preview: &Preview) -> crate::status_slot::StatusSlot {
            crate::status_slot::StatusSlot {
                schema: crate::status_slot::STATUS_SLOT_SCHEMA,
                session_id: self.session_id.clone(),
                provider: "claude".into(),
                runtime_key: format!("claude:{}", self.session_id),
                run_id: self.run_id.clone(),
                source: TRANSPORT.into(),
                phase: String::new(),
                tool_name: None,
                observed_at: chrono::Utc::now().to_rfc3339(),
                payload: serde_json::json!({}),
                preview: Some(status_preview(preview, &self.provider_session_id)),
                producer_epoch: String::new(),
                seq: 0,
            }
        }
    }

    fn outbox_has_events(dir: &Path) -> bool {
        std::fs::read_dir(dir).is_ok_and(|entries| {
            entries
                .flatten()
                .any(|entry| entry.file_name().to_string_lossy().ends_with(".json"))
        })
    }

    /// Serve the mod's posts on `socket` until standard input closes, which is
    /// how `longhouse claude` ends the relay. Prints `ready` once listening.
    pub fn run(
        socket: &Path,
        session_id: &str,
        run_id: &str,
        provider_session_id: &str,
    ) -> Result<()> {
        let directory = socket.parent().context("socket path has no directory")?;
        std::fs::create_dir(directory)
            .with_context(|| format!("create private socket directory {}", directory.display()))?;
        std::fs::set_permissions(directory, std::fs::Permissions::from_mode(0o700))?;
        let listener =
            UnixListener::bind(socket).with_context(|| format!("bind {}", socket.display()))?;
        let relay = Arc::new(Relay {
            session_id: session_id.to_string(),
            run_id: run_id.to_string(),
            provider_session_id: provider_session_id.to_string(),
            live: Mutex::new(LiveText::default()),
            outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()
                .context("runtime-event outbox directory")?,
            call_in_outbox: Mutex::new(false),
            status: crate::status_slot::StatusPublisher::for_provider("claude", TRANSPORT),
        });
        {
            let relay = Arc::clone(&relay);
            std::thread::spawn(move || {
                for stream in listener.incoming().flatten() {
                    let relay = Arc::clone(&relay);
                    std::thread::spawn(move || {
                        let _ = serve(&relay, stream);
                    });
                }
            });
        }
        println!("ready");
        std::io::stdout().flush().ok();
        let mut sink = [0u8; 256];
        let mut stdin = std::io::stdin();
        while matches!(stdin.read(&mut sink), Ok(read) if read > 0) {}
        relay.status.retire_run(session_id, run_id);
        let _ = std::fs::remove_file(socket);
        let _ = std::fs::remove_dir(directory);
        Ok(())
    }

    fn serve(relay: &Relay, mut stream: UnixStream) -> std::io::Result<()> {
        stream.set_read_timeout(Some(READ_TIMEOUT))?;
        let mut buffered: Vec<u8> = Vec::new();
        loop {
            let Some(body) = read_request(&mut stream, &mut buffered)? else {
                return Ok(());
            };
            if let Ok(entries) = serde_json::from_slice::<Vec<Entry>>(&body) {
                relay.apply(&entries);
            }
            stream.write_all(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
            )?;
        }
    }

    /// One HTTP/1.1 request's body, or `None` when the peer closed.
    fn read_request(
        stream: &mut UnixStream,
        buffered: &mut Vec<u8>,
    ) -> std::io::Result<Option<Vec<u8>>> {
        let mut chunk = [0u8; 8192];
        let header_end = loop {
            if let Some(position) = find(buffered, b"\r\n\r\n") {
                break position;
            }
            if buffered.len() > MAX_HEADER_BYTES {
                return Err(std::io::ErrorKind::InvalidData.into());
            }
            let read = stream.read(&mut chunk)?;
            if read == 0 {
                return Ok(None);
            }
            buffered.extend_from_slice(&chunk[..read]);
        };
        let headers = String::from_utf8_lossy(&buffered[..header_end]).to_ascii_lowercase();
        let length = headers
            .lines()
            .find_map(|line| line.strip_prefix("content-length:"))
            .and_then(|value| value.trim().parse::<usize>().ok())
            .unwrap_or(0);
        if length > MAX_BODY_BYTES {
            return Err(std::io::ErrorKind::InvalidData.into());
        }
        let body_start = header_end + 4;
        while buffered.len() < body_start + length {
            let read = stream.read(&mut chunk)?;
            if read == 0 {
                return Ok(None);
            }
            buffered.extend_from_slice(&chunk[..read]);
        }
        let body = buffered[body_start..body_start + length].to_vec();
        buffered.drain(..body_start + length);
        Ok(Some(body))
    }

    fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
        haystack
            .windows(needle.len())
            .position(|window| window == needle)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn relay_in(dir: &Path) -> Relay {
            Relay {
                session_id: "session-1".into(),
                run_id: "run-1".into(),
                provider_session_id: "native-1".into(),
                live: Mutex::new(LiveText::default()),
                outbox_dir: dir.join("outbox"),
                call_in_outbox: Mutex::new(false),
                status: crate::status_slot::StatusPublisher::new(
                    dir.to_path_buf(),
                    "claude",
                    TRANSPORT,
                ),
            }
        }

        fn post(stream: &mut UnixStream, body: &str) -> String {
            let request = format!(
                "POST /live HTTP/1.1\r\nHost: longhouse.invalid\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
                body.len()
            );
            stream.write_all(request.as_bytes()).unwrap();
            let mut response = [0u8; 256];
            let read = stream.read(&mut response).unwrap();
            String::from_utf8_lossy(&response[..read]).into_owned()
        }

        fn serving(relay: &Arc<Relay>) -> UnixStream {
            let (client, server) = UnixStream::pair().unwrap();
            let served = Arc::clone(relay);
            std::thread::spawn(move || {
                let _ = serve(&served, server);
            });
            client
        }

        #[test]
        fn posts_over_one_connection_become_a_cumulative_preview_with_no_phase() {
            let temp = tempfile::tempdir().unwrap();
            let relay = Arc::new(relay_in(temp.path()));
            let mut client = serving(&relay);

            let first = post(&mut client, r#"[{"turn":"t1","step":0,"text":"Hello "}]"#);
            assert!(first.starts_with("HTTP/1.1 200 OK"), "{first}");
            // The publisher coalesces within 100 ms, as the mod's spacing does.
            std::thread::sleep(Duration::from_millis(120));
            post(&mut client, r#"[{"turn":"t1","step":0,"text":"world"}]"#);
            let slots = crate::status_slot::read_all(temp.path());
            assert_eq!(slots.len(), 1);
            let slot = &slots[0];
            assert_eq!(
                (slot.provider.as_str(), slot.source.as_str()),
                ("claude", TRANSPORT)
            );
            assert_eq!(slot.phase, "");
            let preview = slot.preview.as_ref().expect("preview");
            assert_eq!(preview.live_text, "Hello world");
            assert_eq!(preview.progress_kind, PROGRESS_KIND);
            assert_eq!(preview.provider_session_id.as_deref(), Some("native-1"));
            assert!(!preview.turn_completed);

            // The turn ends: the closing publish is not coalesced away.
            post(&mut client, r#"[{"turn":"t1","done":true}]"#);
            let closed = crate::status_slot::read_all(temp.path()).pop().unwrap();
            let preview = closed.preview.expect("preview");
            assert!(preview.turn_completed);
            assert_eq!(preview.live_text, "Hello world");
        }

        /// The progress events waiting in the runtime-event outbox, oldest first.
        fn outbox_events(dir: &Path) -> Vec<serde_json::Value> {
            let mut events: Vec<serde_json::Value> = std::fs::read_dir(dir.join("outbox"))
                .into_iter()
                .flatten()
                .flatten()
                .filter_map(|entry| std::fs::read(entry.path()).ok())
                .filter_map(|bytes| serde_json::from_slice(&bytes).ok())
                .collect();
            events.sort_by_key(|event| event["payload"]["seq"].as_u64());
            events
        }

        #[test]
        fn thinking_reaches_the_slot_labelled_and_never_as_the_reply() {
            let temp = tempfile::tempdir().unwrap();
            let relay = Arc::new(relay_in(temp.path()));
            let mut client = serving(&relay);

            post(
                &mut client,
                r#"[{"turn":"t1","step":0,"kind":"thinking","text":"pondering"}]"#,
            );
            let slot = crate::status_slot::read_all(temp.path()).pop().unwrap();
            let preview = slot.preview.clone().expect("preview");
            assert_eq!(preview.kind.as_deref(), Some("thinking"));
            assert_eq!(preview.live_text, "pondering");
            let events = crate::status_slot::runtime_events(&slot);
            let payload = &events.last().unwrap()["payload"];
            assert_eq!(payload["preview_kind"], "thinking");
            assert!(
                outbox_events(temp.path()).is_empty(),
                "a stream stays on the slot"
            );
        }

        /// A tool call is a discrete event: it goes through the durable outbox
        /// with its name, id, state and arguments, and not through the slot.
        #[test]
        fn a_tool_call_goes_through_the_outbox_with_its_name_state_and_arguments() {
            let temp = tempfile::tempdir().unwrap();
            let relay = Arc::new(relay_in(temp.path()));
            let mut client = serving(&relay);
            post(
                &mut client,
                r#"[{"turn":"t1","step":0,"kind":"tool","index":1,"id":"toolu_1","name":"Agent"},{"turn":"t1","step":0,"kind":"input","index":1,"json":"{\"description\":\"survey the repo\"}"}]"#,
            );
            let events = outbox_events(temp.path());
            assert_eq!(events.len(), 2, "the start and the arguments");
            let started = &events[0]["payload"];
            assert_eq!(started["preview_kind"], "subagent");
            assert_eq!(started["tool_name"], "Agent");
            assert_eq!(started["tool_call_id"], "toolu_1");
            assert_eq!(started["tool_state"], "running");
            assert_eq!(events[0]["source"], TRANSPORT);
            assert_eq!(events[0]["kind"], "progress_signal");
            let arguments = &events[1]["payload"];
            assert_eq!(arguments["live_text"], "survey the repo");
            assert_eq!(arguments["tool_input"]["description"], "survey the repo");
            assert_eq!(arguments["turn_id"], started["turn_id"]);
            assert!(arguments["seq"].as_u64() > started["seq"].as_u64());
            assert!(
                crate::status_slot::read_all(temp.path()).is_empty(),
                "the slot carries no tool preview"
            );
        }

        /// An async subagent returns at once, so the next step's text follows its
        /// arguments within milliseconds. The text waits until the call's event
        /// has left the outbox, so the host sees the call first.
        #[test]
        fn text_after_a_tool_call_waits_for_the_call_to_be_posted() {
            let temp = tempfile::tempdir().unwrap();
            let relay = Arc::new(relay_in(temp.path()));
            let outbox = temp.path().join("outbox");
            let drainer = std::thread::spawn({
                let outbox = outbox.clone();
                move || {
                    std::thread::sleep(Duration::from_millis(300));
                    for entry in std::fs::read_dir(&outbox).unwrap().flatten() {
                        std::fs::remove_file(entry.path()).unwrap();
                    }
                }
            });
            let started = std::time::Instant::now();
            relay.apply(
                &serde_json::from_str::<Vec<Entry>>(
                    r#"[{"turn":"t1","step":0,"kind":"tool","index":1,"id":"toolu_1","name":"Agent"},{"turn":"t1","step":1,"text":"next step"}]"#,
                )
                .unwrap(),
            );
            drainer.join().unwrap();
            assert!(
                started.elapsed() >= Duration::from_millis(300),
                "the text waited"
            );
            let preview = crate::status_slot::read_all(temp.path())
                .pop()
                .unwrap()
                .preview
                .unwrap();
            assert_eq!(preview.live_text, "next step");
            assert_eq!(preview.kind, None);
        }

        #[test]
        fn a_malformed_body_is_answered_and_changes_nothing() {
            let temp = tempfile::tempdir().unwrap();
            let relay = Arc::new(relay_in(temp.path()));
            let mut client = serving(&relay);
            let reply = post(&mut client, "not json");
            assert!(reply.starts_with("HTTP/1.1 200 OK"));
            assert!(crate::status_slot::read_all(temp.path()).is_empty());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(turn: &str, step: u64, text: &str) -> Entry {
        Entry {
            turn: turn.into(),
            step: Some(step),
            text: Some(text.into()),
            ..Default::default()
        }
    }

    fn done(turn: &str) -> Entry {
        Entry {
            turn: turn.into(),
            done: true,
            ..Default::default()
        }
    }

    #[test]
    fn deltas_accumulate_within_a_step() {
        let mut live = LiveText::default();
        let first = live.apply("run", &text("t1", 0, "Hello ")).unwrap();
        let second = live.apply("run", &text("t1", 0, "world")).unwrap();
        assert_eq!(first.text, "Hello ");
        assert_eq!(second.text, "Hello world");
        assert_eq!((first.seq, second.seq), (1, 2));
        assert_eq!(first.turn_id, second.turn_id);
        assert!(!second.completed);
    }

    #[test]
    fn a_new_step_starts_an_empty_message_with_its_own_identity() {
        let mut live = LiveText::default();
        let first = live
            .apply("run", &text("t1", 0, "before the tool"))
            .unwrap();
        let second = live.apply("run", &text("t1", 1, "after")).unwrap();
        assert_eq!(second.text, "after");
        assert_eq!(second.seq, 1);
        assert_ne!(first.turn_id, second.turn_id);
    }

    #[test]
    fn a_new_turn_resets_everything() {
        let mut live = LiveText::default();
        live.apply("run", &text("t1", 0, "one"));
        let next = live.apply("run", &text("t2", 0, "two")).unwrap();
        assert_eq!(next.text, "two");
        assert_eq!(next.turn_id, "run:2:1");
    }

    #[test]
    fn done_closes_the_last_message_with_a_new_sequence() {
        let mut live = LiveText::default();
        live.apply("run", &text("t1", 0, "answer"));
        let closing = live.apply("run", &done("t1")).unwrap();
        assert!(closing.completed);
        assert_eq!(closing.text, "answer");
        assert_eq!(closing.seq, 2);
    }

    #[test]
    fn done_for_a_turn_with_no_text_or_another_turn_publishes_nothing() {
        let mut live = LiveText::default();
        assert_eq!(live.apply("run", &done("t1")), None);
        live.apply("run", &text("t1", 0, "answer"));
        assert_eq!(live.apply("run", &done("other")), None);
    }

    #[test]
    fn empty_and_stepless_text_is_ignored() {
        let mut live = LiveText::default();
        assert_eq!(live.apply("run", &text("t1", 0, "")), None);
        let stepless = Entry {
            turn: "t1".into(),
            text: Some("x".into()),
            ..Default::default()
        };
        assert_eq!(live.apply("run", &stepless), None);
    }

    #[test]
    fn text_stops_at_the_shared_cap() {
        let mut live = LiveText::default();
        let big = "x".repeat(crate::status_slot::MAX_LIVE_TEXT_BYTES + 100);
        let preview = live.apply("run", &text("t1", 0, &big)).unwrap();
        assert_eq!(preview.text.len(), crate::status_slot::MAX_LIVE_TEXT_BYTES);
    }

    #[test]
    fn entries_parse_from_the_mods_json() {
        let parsed: Vec<Entry> =
            serde_json::from_str(r#"[{"turn":"t","step":2,"text":"hi"},{"turn":"t","done":true}]"#)
                .unwrap();
        assert_eq!(parsed[0], text("t", 2, "hi"));
        assert_eq!(parsed[1], done("t"));
    }

    fn thinking(turn: &str, step: u64, text: &str) -> Entry {
        Entry {
            kind: Some("thinking".into()),
            ..text_entry(turn, step, text)
        }
    }

    fn text_entry(turn: &str, step: u64, text: &str) -> Entry {
        self::text(turn, step, text)
    }

    fn tool(turn: &str, step: u64, id: &str, name: &str) -> Entry {
        Entry {
            turn: turn.into(),
            step: Some(step),
            kind: Some("tool".into()),
            id: Some(id.into()),
            name: Some(name.into()),
            index: Some(2),
            ..Default::default()
        }
    }

    fn input(turn: &str, step: u64, json: &str) -> Entry {
        Entry {
            turn: turn.into(),
            step: Some(step),
            kind: Some("input".into()),
            index: Some(2),
            json: Some(json.into()),
            ..Default::default()
        }
    }

    /// A tool's arguments arrive as partial JSON. Nothing is posted until they
    /// parse, then the preview text is the command and the object is the input.
    #[test]
    fn tool_arguments_show_once_they_parse() {
        let mut live = LiveText::default();
        let started = live
            .apply("run", &tool("t1", 0, "toolu_1", "Bash"))
            .unwrap();
        assert_eq!(started.text, "Bash");
        assert_eq!(
            live.apply("run", &input("t1", 0, r#"{"command":"ls "#)),
            None
        );
        let full = live
            .apply("run", &input("t1", 0, r#"-la","description":"d"}"#))
            .unwrap();
        assert_eq!(full.text, "ls -la");
        assert_eq!(full.tool_input.as_ref().unwrap()["description"], "d");
        assert_eq!(full.turn_id, started.turn_id);
        assert!(full.seq > started.seq);
        assert_eq!(full.kind, Some("tool"));
    }

    #[test]
    fn a_subagent_call_shows_its_description() {
        let mut live = LiveText::default();
        live.apply("run", &tool("t1", 0, "toolu_1", "Agent"));
        let full = live
            .apply(
                "run",
                &input(
                    "t1",
                    0,
                    r#"{"description":"survey the repo","prompt":"long prompt"}"#,
                ),
            )
            .unwrap();
        assert_eq!(full.kind, Some("subagent"));
        assert_eq!(full.text, "survey the repo");
    }

    #[test]
    fn arguments_for_an_unknown_block_or_that_are_not_an_object_are_ignored() {
        let mut live = LiveText::default();
        live.apply("run", &tool("t1", 0, "toolu_1", "Bash"));
        let mut elsewhere = input("t1", 0, r#"{"command":"x"}"#);
        elsewhere.index = Some(9);
        assert_eq!(live.apply("run", &elsewhere), None);
        assert_eq!(live.apply("run", &input("t1", 0, "[1]")), None);
    }

    /// Thinking is its own cumulative text with its own identity. It never
    /// shares the reply's turn id or text, and it is labelled on the wire.
    #[test]
    fn thinking_is_labelled_and_never_becomes_the_reply() {
        let mut live = LiveText::default();
        let first = live.apply("run", &thinking("t1", 0, "hmm ")).unwrap();
        let second = live.apply("run", &thinking("t1", 0, "ok")).unwrap();
        assert_eq!(second.text, "hmm ok");
        assert_eq!(
            (first.kind, second.kind),
            (Some("thinking"), Some("thinking"))
        );
        assert_eq!((first.seq, second.seq), (1, 2));
        let reply = live.apply("run", &text("t1", 0, "answer")).unwrap();
        assert_eq!(reply.kind, None);
        assert_eq!(reply.text, "answer", "the reply holds no thinking text");
        assert_ne!(reply.turn_id, second.turn_id);
        let wire = status_preview(&second, "native");
        assert_eq!(wire.kind.as_deref(), Some("thinking"));
        assert_eq!(wire.tool_name, None);
        assert_eq!(status_preview(&reply, "native").kind, None);
        // The turn's close restates the reply, not the thinking.
        let closing = live.apply("run", &done("t1")).unwrap();
        assert_eq!((closing.kind, closing.text.as_str()), (None, "answer"));
    }

    #[test]
    fn thinking_stops_at_the_shared_cap() {
        let mut live = LiveText::default();
        let big = "x".repeat(crate::status_slot::MAX_LIVE_TEXT_BYTES + 100);
        let preview = live.apply("run", &thinking("t1", 0, &big)).unwrap();
        assert_eq!(preview.text.len(), crate::status_slot::MAX_LIVE_TEXT_BYTES);
    }

    #[test]
    fn a_tool_call_is_a_running_tool_preview_with_its_real_name_and_id() {
        let mut live = LiveText::default();
        let started = live
            .apply("run", &tool("t1", 0, "toolu_1", "Bash"))
            .unwrap();
        assert_eq!(started.kind, Some("tool"));
        assert_eq!(started.tool_name.as_deref(), Some("Bash"));
        assert_eq!(started.tool_call_id.as_deref(), Some("toolu_1"));
        let wire = status_preview(&started, "native");
        assert_eq!(wire.tool_state.as_deref(), Some("running"));
        assert!(!wire.turn_completed);
        let other = live
            .apply("run", &tool("t1", 0, "toolu_2", "Read"))
            .unwrap();
        assert_ne!(started.turn_id, other.turn_id);
    }

    #[test]
    fn a_spawning_tool_is_a_subagent_preview() {
        let mut live = LiveText::default();
        let started = live
            .apply("run", &tool("t1", 0, "toolu_1", "Agent"))
            .unwrap();
        assert_eq!(started.kind, Some("subagent"));
        assert_eq!(started.tool_name.as_deref(), Some("Agent"));
    }

    #[test]
    fn a_tool_entry_without_a_name_or_id_or_an_unknown_kind_is_ignored() {
        let mut live = LiveText::default();
        let mut nameless = tool("t1", 0, "toolu_1", "Bash");
        nameless.name = None;
        assert_eq!(live.apply("run", &nameless), None);
        let mut idless = tool("t1", 0, "toolu_1", "Bash");
        idless.id = None;
        assert_eq!(live.apply("run", &idless), None);
        let mut other = text("t1", 0, "x");
        other.kind = Some("input".into());
        assert_eq!(live.apply("run", &other), None);
    }

    #[test]
    fn a_tool_only_turn_does_not_close_a_reply_it_never_had() {
        let mut live = LiveText::default();
        live.apply("run", &tool("t1", 0, "toolu_1", "Bash"));
        assert_eq!(live.apply("run", &done("t1")), None);
    }

    #[test]
    fn kinds_parse_from_the_mods_json() {
        let parsed: Vec<Entry> = serde_json::from_str(
            r#"[{"turn":"t","step":0,"kind":"thinking","text":"x"},{"turn":"t","step":0,"kind":"tool","id":"i","name":"Bash"}]"#,
        )
        .unwrap();
        assert_eq!(parsed[0], thinking("t", 0, "x"));
        assert_eq!(
            parsed[1],
            Entry {
                index: None,
                ..tool("t", 0, "i", "Bash")
            }
        );
    }
}
