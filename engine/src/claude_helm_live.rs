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
//! Only reply text arrives. The mod drops `thinking`, `tool` and `input` chunks,
//! because the preview has one cumulative text and no field to label a kind.

use serde::Deserialize;

/// The status slot's transport, and the Runtime Host's allowlisted source.
pub const TRANSPORT: &str = "claude_helm_channel";
/// `progress_kind` of the preview events.
pub const PROGRESS_KIND: &str = "claude_helm_stream";
/// The mod reads the relay's socket path from this variable.
pub const SOCKET_ENV: &str = "LONGHOUSE_CLAUDE_LIVE_SOCKET";

/// One forwarded entry: reply text of step `step` of turn `turn`, or `done`
/// when the turn ended.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Entry {
    pub turn: String,
    #[serde(default)]
    pub step: Option<u64>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub done: bool,
}

/// What to publish after applying an entry.
#[derive(Debug, Clone, PartialEq)]
pub struct Preview {
    pub turn_id: String,
    pub seq: u64,
    pub text: String,
    pub completed: bool,
}

/// The reply text of the message Claude is writing: cumulative, and reset when
/// a new step (a new assistant message) or a new turn begins.
#[derive(Debug, Default)]
pub struct LiveText {
    text: String,
    seq: u64,
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
        let delta = entry.text.as_deref().filter(|text| !text.is_empty())?;
        let step = entry.step?;
        if self.turn.as_deref() != Some(entry.turn.as_str()) {
            self.turn = Some(entry.turn.clone());
            self.turn_seq = self.turn_seq.saturating_add(1);
            self.message_seq = 0;
            self.step = None;
            self.text.clear();
            self.seq = 0;
        }
        if self.step != Some(step) {
            self.step = Some(step);
            self.message_seq = self.message_seq.saturating_add(1);
            self.text.clear();
            self.seq = 0;
        }
        crate::status_slot::append_live_text(&mut self.text, delta);
        self.seq = self.seq.saturating_add(1);
        Some(self.preview(run_id, false))
    }

    fn preview(&self, run_id: &str, completed: bool) -> Preview {
        Preview {
            turn_id: format!("{run_id}:{}:{}", self.turn_seq, self.message_seq),
            seq: self.seq,
            text: self.text.clone(),
            completed,
        }
    }
}

/// The preview as a status slot states it. The slot carries no phase (the empty
/// phase is not a wire phase and is not recorded locally): Claude's own hooks
/// own the session's phase, and this only adds text.
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
    use std::path::Path;
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
        status: crate::status_slot::StatusPublisher,
    }

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

        fn publish(&self, preview: &Preview) {
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
            done: false,
        }
    }

    fn done(turn: &str) -> Entry {
        Entry {
            turn: turn.into(),
            step: None,
            text: None,
            done: true,
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
            step: None,
            text: Some("x".into()),
            done: false,
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
}
