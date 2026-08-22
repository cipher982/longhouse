//! Provider-receipt evidence for managed Cursor turns.
//!
//! Cursor's store is a lossless artifact archive, not a presentation log.  The
//! managed Helm hooks and Console stream both supply stronger turn boundaries
//! and committed response receipts used by the renderer.

use std::collections::HashMap;
use std::fs;
use std::io::Write;
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::path::PathBuf;
use std::time::Duration as StdDuration;

use anyhow::{Context, Result};
use chrono::{DateTime, Duration, Utc};
use serde_json::{json, Value};

const COMPLETED_RECEIPT_GRACE: Duration = Duration::seconds(30);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CursorEvidenceWait {
    InFlight,
    CompletedReceiptGrace,
}

impl CursorEvidenceWait {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::InFlight => "in_flight",
            Self::CompletedReceiptGrace => "completed_receipt_grace",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CursorProviderTurn {
    pub generation_id: String,
    pub prompt: String,
    pub response_text: Option<String>,
    pub stop_status: Option<String>,
    pub stop_observed_at: Option<DateTime<Utc>>,
}

impl CursorProviderTurn {
    fn unsettled_reason(
        &self,
        session_ended: bool,
        now: DateTime<Utc>,
    ) -> Option<CursorEvidenceWait> {
        // afterAgentResponse is itself the provider's semantic commit receipt;
        // a separately dropped stop hook must not wedge raw archival.
        if self.response_text.is_some() {
            return None;
        }
        match self.stop_status.as_deref() {
            Some("completed") if session_ended => None,
            Some("completed")
                if self.stop_observed_at.is_some_and(|observed| {
                    now.signed_duration_since(observed) >= COMPLETED_RECEIPT_GRACE
                }) =>
            {
                None
            }
            Some("completed") => Some(CursorEvidenceWait::CompletedReceiptGrace),
            Some(_) => None,
            None if session_ended => None,
            None => Some(CursorEvidenceWait::InFlight),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct CursorVisibilityEvidence {
    pub turns: Vec<CursorProviderTurn>,
    pub session_ended: bool,
    pub ambiguous: bool,
}

impl CursorVisibilityEvidence {
    pub(crate) fn unsettled_reason(&self) -> Option<CursorEvidenceWait> {
        self.unsettled_reason_at(Utc::now())
    }

    fn unsettled_reason_at(&self, now: DateTime<Utc>) -> Option<CursorEvidenceWait> {
        self.turns
            .last()
            .and_then(|turn| turn.unsettled_reason(self.session_ended, now))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum CursorProviderReceipt<'a> {
    Prompt(&'a str),
    Response(&'a str),
    Stop(&'a str),
}

fn receipt_events_path(root: &Path, session_id: &str) -> PathBuf {
    root.join("hook-events")
        .join(format!("{session_id}.ndjson"))
}

pub(crate) fn append_cursor_provider_receipt(
    root: &Path,
    session_id: &str,
    conversation_id: &str,
    generation_id: &str,
    source: &str,
    receipt: CursorProviderReceipt<'_>,
) -> Result<()> {
    let (event, payload) = match receipt {
        CursorProviderReceipt::Prompt(prompt) => (
            "beforeSubmitPrompt",
            json!({"generation_id": generation_id, "prompt": prompt}),
        ),
        CursorProviderReceipt::Response(text) => (
            "afterAgentResponse",
            json!({"generation_id": generation_id, "text": text}),
        ),
        CursorProviderReceipt::Stop(status) => (
            "stop",
            json!({"generation_id": generation_id, "status": status}),
        ),
    };
    let path = receipt_events_path(root, session_id);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut line = serde_json::to_vec(&json!({
        "event": event,
        "observed_at": Utc::now().to_rfc3339(),
        "session_id": session_id,
        "conversation_id": conversation_id,
        "source": source,
        "payload": payload,
    }))?;
    line.push(b'\n');
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?
        .write_all(&line)?;
    Ok(())
}

pub(crate) fn has_cursor_prompt_receipt(
    root: &Path,
    session_id: &str,
    conversation_id: &str,
    generation_id: &str,
) -> bool {
    let Ok(contents) = fs::read_to_string(receipt_events_path(root, session_id)) else {
        return false;
    };
    contents.lines().any(|line| {
        serde_json::from_str::<Value>(line).ok().is_some_and(|row| {
            row.get("event").and_then(Value::as_str) == Some("beforeSubmitPrompt")
                && row.get("conversation_id").and_then(Value::as_str) == Some(conversation_id)
                && row
                    .get("payload")
                    .and_then(|payload| payload.get("generation_id"))
                    .and_then(Value::as_str)
                    == Some(generation_id)
        })
    })
}

fn configured_cursor_store(conversation_id: &str) -> Option<PathBuf> {
    let home = PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into()));
    let cursor_home = std::env::var_os("CURSOR_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".cursor"));
    let xdg_config_home = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"));
    [
        xdg_config_home.join("cursor/chats"),
        cursor_home.join("chats"),
    ]
    .into_iter()
    .find_map(|root| {
        let stores = fs::read_dir(root)
            .ok()?
            .flatten()
            .map(|entry| entry.path().join(conversation_id).join("store.db"))
            .filter(|path| path.is_file())
            .collect::<Vec<_>>();
        let [store] = stores.as_slice() else {
            return None;
        };
        Some(store.clone())
    })
}

#[cfg(unix)]
pub(crate) fn wake_cursor_transcript(
    session_id: &str,
    conversation_id: &str,
    generation_id: &str,
    run_id: Option<&str>,
    transcript_path: Option<&Path>,
) {
    let transcript = transcript_path
        .filter(|path| path.is_file())
        .map(Path::to_path_buf)
        .or_else(|| configured_cursor_store(conversation_id));
    let Some(transcript) = transcript else {
        return;
    };
    let Ok(socket) = crate::config::get_agent_transcript_wake_socket_path() else {
        return;
    };
    let Ok(mut stream) = UnixStream::connect(socket) else {
        return;
    };
    let _ = stream.set_write_timeout(Some(StdDuration::from_millis(75)));
    let payload = json!({
        "provider": "cursor",
        "path": transcript,
        "phase": "idle",
        "session_id": session_id,
        "run_id": run_id,
        "turn_id": generation_id,
        "provider_turn_id": conversation_id,
        "wake_reason": "turn_completed",
        "observed_at_ms": Utc::now().timestamp_millis(),
        "file_len_hint": transcript.metadata().ok().map(|metadata| metadata.len()),
    });
    let _ = stream.write_all(payload.to_string().as_bytes());
}

pub(crate) fn load_cursor_visibility_evidence(
    session_id: &str,
    conversation_id: &str,
) -> Result<Option<CursorVisibilityEvidence>> {
    let path = receipt_events_path(
        &crate::config::get_longhouse_home()?.join("managed-local/cursor-helm"),
        session_id,
    );
    let contents = match fs::read_to_string(&path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("reading Cursor provider receipts {}", path.display()));
        }
    };
    let mut evidence = parse_cursor_visibility_evidence(&contents, conversation_id)?;
    if !evidence.turns.is_empty() {
        evidence.session_ended |= session_lifecycle_ended(
            path.parent()
                .and_then(|events| events.parent())
                .context("Cursor receipt path has no lifecycle root")?,
            session_id,
        );
    }
    Ok(Some(evidence))
}

fn session_lifecycle_ended(root: &std::path::Path, session_id: &str) -> bool {
    let phase_ended = fs::read(root.join(format!("{session_id}.phase.json")))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .and_then(|value| {
            value
                .get("phase")
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .as_deref()
        == Some("ended");
    if phase_ended {
        return true;
    }
    // The launcher writes ready=false/cursor_pid=0 in its independent cleanup
    // path, so a provider crash can settle raw-only even if Cursor never emits
    // sessionEnd. A hook turn cannot exist during the launcher's pre-ready state.
    fs::read(root.join(format!("{session_id}.json")))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|state| {
            state.get("ready").and_then(Value::as_bool) == Some(false)
                && state.get("cursor_pid").and_then(Value::as_u64) == Some(0)
        })
}

pub(crate) fn parse_cursor_visibility_evidence(
    contents: &str,
    conversation_id: &str,
) -> Result<CursorVisibilityEvidence> {
    let mut turns = Vec::<CursorProviderTurn>::new();
    let mut indices = HashMap::<String, usize>::new();
    let mut session_ended = false;
    let mut ambiguous = false;
    for (line_index, line) in contents.lines().enumerate() {
        let row: Value = match serde_json::from_str(line) {
            Ok(row) => row,
            Err(_) => continue,
        };
        if row.get("conversation_id").and_then(Value::as_str) != Some(conversation_id) {
            continue;
        }
        let event = row.get("event").and_then(Value::as_str).unwrap_or_default();
        if event == "sessionEnd" {
            session_ended = true;
            continue;
        }
        let payload = row.get("payload").and_then(Value::as_object);
        let generation_id = payload
            .and_then(|payload| payload.get("generation_id"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim();
        if generation_id.is_empty() {
            continue;
        }
        if event == "beforeSubmitPrompt" {
            let prompt = payload
                .and_then(|payload| payload.get("prompt"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .trim();
            if prompt.is_empty() {
                continue;
            }
            // Cursor emits lifecycle receipts for local TUI commands too.
            // They change provider state but are not model turns and have no
            // corresponding message in the Cursor store. Keeping them out of
            // the turn sequence prevents teardown/reset commands from making
            // an otherwise unique transcript alignment look ambiguous.
            if matches!(
                prompt,
                "/exit" | "/clear" | "/new" | "/new-chat" | "/newchat"
            ) {
                continue;
            }
            if let Some(index) = indices.get(generation_id).copied() {
                ambiguous |= turns.get(index).is_some_and(|turn| turn.prompt != prompt);
                continue;
            }
            indices.insert(generation_id.to_string(), turns.len());
            turns.push(CursorProviderTurn {
                generation_id: generation_id.to_string(),
                prompt: prompt.to_string(),
                response_text: None,
                stop_status: None,
                stop_observed_at: None,
            });
            continue;
        }
        let Some(index) = indices.get(generation_id).copied() else {
            continue;
        };
        let turn = turns.get_mut(index).with_context(|| {
            format!(
                "Cursor hook turn index was invalid at evidence line {}",
                line_index + 1
            )
        })?;
        match event {
            "afterAgentResponse" => {
                let response_text = payload
                    .and_then(|payload| payload.get("text"))
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                if let (Some(existing), Some(next)) = (&turn.response_text, &response_text) {
                    ambiguous |= existing != next;
                } else if turn.response_text.is_none() {
                    turn.response_text = response_text;
                }
            }
            "stop" => {
                let stop_status = payload
                    .and_then(|payload| payload.get("status"))
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                if stop_status.is_some() {
                    turn.stop_status = stop_status;
                    turn.stop_observed_at = row
                        .get("observed_at")
                        .and_then(Value::as_str)
                        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
                        .map(|value| value.with_timezone(&Utc));
                }
            }
            _ => {}
        }
    }
    Ok(CursorVisibilityEvidence {
        turns,
        session_ended,
        ambiguous,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn completed_turn_waits_for_response_when_stop_arrives_first() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"g1","status":"completed"}}"#,
            "conversation",
        )
        .unwrap();
        assert_eq!(
            evidence.unsettled_reason(),
            Some(CursorEvidenceWait::CompletedReceiptGrace)
        );

        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"g1","status":"completed"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g1","text":"world"}}"#,
            "conversation",
        )
        .unwrap();
        assert_eq!(evidence.unsettled_reason(), None);
        assert_eq!(evidence.turns[0].response_text.as_deref(), Some("world"));
    }

    #[test]
    fn failed_turn_without_response_is_settled_raw_only_evidence() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"g1","status":"error"}}"#,
            "conversation",
        )
        .unwrap();
        assert_eq!(evidence.unsettled_reason(), None);
        assert_eq!(evidence.turns[0].response_text, None);
    }

    #[test]
    fn duplicate_hooks_and_other_conversations_do_not_duplicate_turns() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"other","payload":{"generation_id":"g0","prompt":"ignore"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g1","text":"world"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g1","text":"world"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"g1","status":"completed"}}"#,
            "conversation",
        )
        .unwrap();
        assert_eq!(evidence.turns.len(), 1);
        assert_eq!(evidence.turns[0].response_text.as_deref(), Some("world"));
    }

    #[test]
    fn local_cursor_commands_do_not_shift_provider_turn_alignment() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g1","text":"world"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"g1","status":"completed"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g2","prompt":"/exit"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"g2","status":"completed"}}"#,
            "conversation",
        )
        .unwrap();
        assert!(!evidence.ambiguous);
        assert_eq!(evidence.turns.len(), 1);
        assert_eq!(evidence.turns[0].prompt, "hello");
        assert_eq!(evidence.turns[0].response_text.as_deref(), Some("world"));
    }

    #[test]
    fn an_old_incomplete_turn_does_not_block_a_later_settled_turn() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"crashed"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g2","prompt":"recovered"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g2","text":"done"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"g2","status":"completed"}}"#,
            "conversation",
        )
        .unwrap();
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn interrupt_stop_transition_does_not_hide_a_later_recovery() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"sleep"}}
{"event":"stop","observed_at":"2026-07-21T12:00:00Z","conversation_id":"conversation","payload":{"generation_id":"g1","status":"aborted"}}
{"event":"stop","observed_at":"2026-07-21T12:00:01Z","conversation_id":"conversation","payload":{"generation_id":"g1","status":"error"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g2","prompt":"recover"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g2","text":"done"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"g2","status":"completed"}}"#,
            "conversation",
        )
        .unwrap();
        assert!(!evidence.ambiguous);
        assert_eq!(evidence.turns[0].stop_status.as_deref(), Some("error"));
        assert_eq!(evidence.turns[1].response_text.as_deref(), Some("done"));
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn response_receipt_settles_when_stop_hook_is_missing() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g1","text":"world"}}"#,
            "conversation",
        )
        .unwrap();
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn completed_turn_without_receipt_degrades_to_raw_only_after_grace() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"stop","observed_at":"2026-07-21T12:00:00Z","conversation_id":"conversation","payload":{"generation_id":"g1","status":"completed"}}"#,
            "conversation",
        )
        .unwrap();
        assert_eq!(
            evidence.unsettled_reason_at(
                DateTime::parse_from_rfc3339("2026-07-21T12:00:31Z")
                    .unwrap()
                    .with_timezone(&Utc)
            ),
            None
        );
    }

    #[test]
    fn session_end_settles_incomplete_turn_raw_only() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"sessionEnd","conversation_id":"conversation","payload":{}}"#,
            "conversation",
        )
        .unwrap();
        assert!(evidence.session_ended);
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn conflicting_duplicate_receipts_are_ambiguous() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g1","text":"first"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g1","text":"second"}}"#,
            "conversation",
        )
        .unwrap();
        assert!(evidence.ambiguous);
    }

    #[test]
    fn console_provider_receipts_form_one_settled_idempotent_turn() {
        let root = tempfile::tempdir().unwrap();
        for _ in 0..2 {
            append_cursor_provider_receipt(
                root.path(),
                "session",
                "conversation",
                "run-1",
                "cursor_print",
                CursorProviderReceipt::Prompt("hello"),
            )
            .unwrap();
            append_cursor_provider_receipt(
                root.path(),
                "session",
                "conversation",
                "run-1",
                "cursor_print",
                CursorProviderReceipt::Response("world"),
            )
            .unwrap();
            append_cursor_provider_receipt(
                root.path(),
                "session",
                "conversation",
                "run-1",
                "cursor_print",
                CursorProviderReceipt::Stop("completed"),
            )
            .unwrap();
        }
        let contents = fs::read_to_string(receipt_events_path(root.path(), "session")).unwrap();
        let evidence = parse_cursor_visibility_evidence(&contents, "conversation").unwrap();
        assert_eq!(evidence.turns.len(), 1);
        assert_eq!(evidence.turns[0].prompt, "hello");
        assert_eq!(evidence.turns[0].response_text.as_deref(), Some("world"));
        assert_eq!(evidence.turns[0].stop_status.as_deref(), Some("completed"));
        assert!(!evidence.ambiguous);
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn launcher_cleanup_settles_crashed_session_without_session_end_hook() {
        let root = tempfile::tempdir().unwrap();
        fs::write(
            root.path().join("session.json"),
            r#"{"session_id":"session","ready":false,"cursor_pid":0}"#,
        )
        .unwrap();
        assert!(session_lifecycle_ended(root.path(), "session"));
    }
}
