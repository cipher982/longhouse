//! Deliberate, explicit fault injection for acceptance testing.
//!
//! The Console truth-plane epic needs one thing it could not otherwise get: a
//! terminal signal that is genuinely lost in transit on the real path, so the
//! detection can be proven rather than assumed. Simulating the loss inside a
//! test proves the predicate; only dropping it here proves the pipeline.
//!
//! Three properties make this safe to carry in production code.
//!
//! It is off unless a control file exists, so an unset deployment behaves
//! exactly as before. It is scoped to a single session id, so it can never
//! affect other work on a shared machine -- which matters, because this laptop
//! runs several agents at once. And every drop is logged at warn with the
//! session and kind, so a machine behaving oddly explains itself rather than
//! going quietly wrong.
//!
//! Control file: `<agent-dir>/fault-drop-runtime-events`, one line,
//! `<session_id>:<kind>` (for example `abc-123:terminal_signal`). Delete the
//! file to disarm. The file is read per batch rather than cached, so arming and
//! disarming take effect without restarting the engine.

use serde_json::Value;
use std::path::PathBuf;

const CONTROL_FILE: &str = "fault-drop-runtime-events";

fn control_path() -> Option<PathBuf> {
    crate::config::get_agent_dir()
        .ok()
        .map(|dir| dir.join(CONTROL_FILE))
}

/// Parse `<session_id>:<kind>`, rejecting anything under-specified.
///
/// A blank or malformed control file must disarm rather than match broadly. An
/// injection that guessed at what to drop would be a production hazard, not a
/// test tool.
fn parse_target(contents: &str) -> Option<(String, String)> {
    let (session_id, kind) = contents.trim().split_once(':')?;
    let session_id = session_id.trim();
    let kind = kind.trim();
    if session_id.is_empty() || kind.is_empty() {
        return None;
    }
    Some((session_id.to_string(), kind.to_string()))
}

fn event_matches(event: &Value, target: &(String, String)) -> bool {
    let session_id = event
        .get("session_id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let kind = event
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or_default();
    !session_id.is_empty() && session_id == target.0 && kind == target.1
}

/// `(session_id, kind)` to drop, or None when disarmed.
fn armed_target() -> Option<(String, String)> {
    parse_target(&std::fs::read_to_string(control_path()?).ok()?)
}

/// Should this runtime event be dropped on the floor instead of shipped?
pub fn should_drop_runtime_event(event: &Value) -> bool {
    let Some(target) = armed_target() else {
        return false;
    };
    if !event_matches(event, &target) {
        return false;
    }
    tracing::warn!(
        session_id = %target.0,
        kind = %target.1,
        "fault injection armed: dropping runtime event in transit"
    );
    eprintln!(
        "[fault-injection] dropping {} for session={} (control file armed)",
        target.1, target.0
    );
    true
}

/// Drop any armed events from a batch, returning what survives.
pub fn filter_runtime_events(events: Vec<Value>) -> Vec<Value> {
    if armed_target().is_none() {
        return events;
    }
    events
        .into_iter()
        .filter(|event| !should_drop_runtime_event(event))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_disarmed_engine_drops_nothing() {
        // No control file in a normal test environment.
        let event = json!({"session_id": "s1", "kind": "terminal_signal"});
        assert!(!should_drop_runtime_event(&event));
        assert_eq!(filter_runtime_events(vec![event]).len(), 1);
    }

    #[test]
    fn an_armed_target_drops_only_the_named_session_and_kind() {
        let target = parse_target("sess-a:terminal_signal").expect("valid target");
        assert!(event_matches(
            &json!({"session_id": "sess-a", "kind": "terminal_signal"}),
            &target
        ));
        // A different session on the same machine must be untouched. This laptop
        // runs several agents at once; a broad match would corrupt their turns.
        assert!(!event_matches(
            &json!({"session_id": "sess-b", "kind": "terminal_signal"}),
            &target
        ));
        // Other kinds for the same session still ship, so the turn behaves
        // normally right up to the terminal.
        assert!(!event_matches(
            &json!({"session_id": "sess-a", "kind": "phase_signal"}),
            &target
        ));
    }

    #[test]
    fn a_malformed_control_file_disarms_rather_than_matching_broadly() {
        for contents in ["", "   ", "sess-a", ":terminal_signal", "sess-a:"] {
            assert!(
                parse_target(contents).is_none(),
                "under-specified control file must disarm: {contents:?}"
            );
        }
    }

    #[test]
    fn an_event_without_a_session_never_matches() {
        let target = parse_target("sess-a:terminal_signal").expect("valid target");
        assert!(!event_matches(&json!({"kind": "terminal_signal"}), &target));
    }
}
