//! Provider-receipt evidence for managed Cursor turns.
//!
//! Cursor's store is a lossless artifact archive, not a presentation log.  The
//! managed hook stream supplies the stronger turn boundary and committed
//! response receipt used by the renderer.

use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CursorHookTurn {
    pub generation_id: String,
    pub prompt: String,
    pub response_text: Option<String>,
    pub stop_status: Option<String>,
}

impl CursorHookTurn {
    pub(crate) fn is_settled(&self) -> bool {
        match self.stop_status.as_deref() {
            Some("completed") => self.response_text.is_some(),
            Some(_) => true,
            None => false,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct CursorVisibilityEvidence {
    pub turns: Vec<CursorHookTurn>,
}

impl CursorVisibilityEvidence {
    pub(crate) fn has_unsettled_turn(&self) -> bool {
        self.turns.last().is_some_and(|turn| !turn.is_settled())
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
    parse_cursor_visibility_evidence(&contents, conversation_id).map(Some)
}

pub(crate) fn parse_cursor_visibility_evidence(
    contents: &str,
    conversation_id: &str,
) -> Result<CursorVisibilityEvidence> {
    let mut turns = Vec::<CursorHookTurn>::new();
    let mut indices = HashMap::<String, usize>::new();
    for (line_index, line) in contents.lines().enumerate() {
        let row: Value = match serde_json::from_str(line) {
            Ok(row) => row,
            Err(_) => continue,
        };
        if row.get("conversation_id").and_then(Value::as_str) != Some(conversation_id) {
            continue;
        }
        let event = row.get("event").and_then(Value::as_str).unwrap_or_default();
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
            if prompt.is_empty() || indices.contains_key(generation_id) {
                continue;
            }
            indices.insert(generation_id.to_string(), turns.len());
            turns.push(CursorHookTurn {
                generation_id: generation_id.to_string(),
                prompt: prompt.to_string(),
                response_text: None,
                stop_status: None,
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
                if turn.response_text.is_none() {
                    turn.response_text = payload
                        .and_then(|payload| payload.get("text"))
                        .and_then(Value::as_str)
                        .map(str::to_owned);
                }
            }
            "stop" => {
                if turn.stop_status.is_none() {
                    turn.stop_status = payload
                        .and_then(|payload| payload.get("status"))
                        .and_then(Value::as_str)
                        .map(str::to_owned);
                }
            }
            _ => {}
        }
    }
    Ok(CursorVisibilityEvidence { turns })
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
        assert!(evidence.has_unsettled_turn());

        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"stop","conversation_id":"conversation","payload":{"generation_id":"g1","status":"completed"}}
{"event":"afterAgentResponse","conversation_id":"conversation","payload":{"generation_id":"g1","text":"world"}}"#,
            "conversation",
        )
        .unwrap();
        assert!(!evidence.has_unsettled_turn());
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
        assert!(!evidence.has_unsettled_turn());
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
        assert!(!evidence.has_unsettled_turn());
    }
}
