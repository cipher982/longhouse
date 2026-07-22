//! Provider-receipt evidence for managed Cursor turns.
//!
//! Cursor's store is a lossless artifact archive, not a presentation log.  The
//! managed hook stream supplies the stronger turn boundary and committed
//! response receipt used by the renderer.

use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use chrono::{DateTime, Duration, Utc};
use serde_json::Value;

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
pub(crate) struct CursorHookTurn {
    pub generation_id: String,
    pub prompt: String,
    pub response_text: Option<String>,
    pub stop_status: Option<String>,
    pub stop_observed_at: Option<DateTime<Utc>>,
}

impl CursorHookTurn {
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
    pub turns: Vec<CursorHookTurn>,
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

fn hook_events_path(session_id: &str) -> Result<PathBuf> {
    Ok(crate::config::get_longhouse_home()?
        .join("managed-local/cursor-helm/hook-events")
        .join(format!("{session_id}.ndjson")))
}

pub(crate) fn load_cursor_visibility_evidence(
    session_id: &str,
    conversation_id: &str,
) -> Result<Option<CursorVisibilityEvidence>> {
    let path = hook_events_path(session_id)?;
    let contents = match fs::read_to_string(&path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("reading Cursor hook evidence {}", path.display()));
        }
    };
    let mut evidence = parse_cursor_visibility_evidence(&contents, conversation_id)?;
    if !evidence.turns.is_empty() {
        evidence.session_ended |= session_lifecycle_ended(
            path.parent()
                .and_then(|events| events.parent())
                .context("Cursor hook evidence path has no lifecycle root")?,
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
    let mut turns = Vec::<CursorHookTurn>::new();
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
            if let Some(index) = indices.get(generation_id).copied() {
                ambiguous |= turns.get(index).is_some_and(|turn| turn.prompt != prompt);
                continue;
            }
            indices.insert(generation_id.to_string(), turns.len());
            turns.push(CursorHookTurn {
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
                if let (Some(existing), Some(next)) = (&turn.stop_status, &stop_status) {
                    ambiguous |= existing != next;
                } else if turn.stop_status.is_none() {
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
