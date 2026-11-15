//! Canonical item facts a parser asserts about one transcript row.
//!
//! A row's role and tool fields already say whether it is user input,
//! assistant text, a tool call or a tool result. These facts carry what the
//! role cannot: which phase of the turn an assistant text belongs to and how
//! that is known, how much of a model's reasoning the provider exposed, the
//! agent's stated purpose for a tool call, and why a turn ended. Unknown is a
//! value, never a guess: a field is set only on evidence in the raw source.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ItemKind {
    /// Model reasoning; `visibility` says how much of it is present.
    Reasoning,
    /// The end of a turn; `end_reason` says why.
    TurnEnd,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    /// Narration inside a turn that continues with tool calls.
    Commentary,
    /// The turn's answer.
    Final,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PhaseSource {
    /// The provider labels the phase itself (Codex `phase`).
    Native,
    /// Read from the provider's own stop reason or call structure.
    Derived,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Visibility {
    Full,
    Summary,
    /// The provider recorded that reasoning happened but kept its text
    /// (a signature or encrypted content only).
    Withheld,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EndReason {
    Completed,
    Interrupted,
    Aborted,
    Error,
    /// The turn hit the model's output limit.
    Length,
    /// A stop hook made the agent continue instead of ending.
    HookContinued,
    Unknown,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ItemFacts {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<ItemKind>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<Phase>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase_source: Option<PhaseSource>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub visibility: Option<Visibility>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub intent: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub end_reason: Option<EndReason>,
}

impl ItemFacts {
    pub fn is_empty(&self) -> bool {
        *self == Self::default()
    }

    pub fn reasoning(visibility: Visibility) -> Self {
        Self {
            kind: Some(ItemKind::Reasoning),
            visibility: Some(visibility),
            ..Self::default()
        }
    }

    pub fn phase(phase: Phase, source: PhaseSource) -> Self {
        Self {
            phase: Some(phase),
            phase_source: Some(source),
            ..Self::default()
        }
    }

    pub fn turn_end(reason: EndReason) -> Self {
        Self {
            kind: Some(ItemKind::TurnEnd),
            end_reason: Some(reason),
            ..Self::default()
        }
    }

    /// A tool call's stated purpose, trimmed and bounded; blank is absent.
    pub fn intent(text: Option<&str>) -> Self {
        Self {
            intent: text.and_then(bounded_intent),
            ..Self::default()
        }
    }
}

const MAX_INTENT_CHARS: usize = 500;

pub fn bounded_intent(text: &str) -> Option<String> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(trimmed.chars().take(MAX_INTENT_CHARS).collect())
}

/// Phase from a provider stop reason on the message that carries the text:
/// a message that stopped to call tools is commentary, one that stopped on its
/// own is the answer. Anything else (length, error, absent) says nothing.
pub fn phase_from_stop_reason(stop_reason: Option<&str>) -> Option<ItemFacts> {
    let phase = match stop_reason? {
        "tool_use" | "toolUse" | "tool-calls" | "tool_calls" => Phase::Commentary,
        "end_turn" | "stop" | "stop_sequence" => Phase::Final,
        _ => return None,
    };
    Some(ItemFacts::phase(phase, PhaseSource::Derived))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_facts_serialize_to_an_empty_object() {
        assert_eq!(serde_json::to_string(&ItemFacts::default()).unwrap(), "{}");
        assert!(ItemFacts::default().is_empty());
    }

    #[test]
    fn facts_serialize_in_snake_case() {
        let facts = ItemFacts::phase(Phase::Commentary, PhaseSource::Derived);
        assert_eq!(
            serde_json::to_value(&facts).unwrap(),
            serde_json::json!({"phase": "commentary", "phase_source": "derived"})
        );
        assert_eq!(
            serde_json::to_value(ItemFacts::turn_end(EndReason::HookContinued)).unwrap(),
            serde_json::json!({"kind": "turn_end", "end_reason": "hook_continued"})
        );
    }

    #[test]
    fn only_a_known_stop_reason_names_a_phase() {
        assert_eq!(
            phase_from_stop_reason(Some("tool_use")).unwrap().phase,
            Some(Phase::Commentary)
        );
        assert_eq!(
            phase_from_stop_reason(Some("end_turn")).unwrap().phase,
            Some(Phase::Final)
        );
        assert!(phase_from_stop_reason(Some("max_tokens")).is_none());
        assert!(phase_from_stop_reason(None).is_none());
    }

    #[test]
    fn a_blank_intent_is_absent_and_a_long_one_is_bounded() {
        assert_eq!(ItemFacts::intent(Some("  ")).intent, None);
        let long = "x".repeat(2000);
        assert_eq!(
            ItemFacts::intent(Some(&long))
                .intent
                .unwrap()
                .chars()
                .count(),
            MAX_INTENT_CHARS
        );
    }
}
