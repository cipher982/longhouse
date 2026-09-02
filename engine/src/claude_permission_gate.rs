//! Native Claude PreToolUse permission gate.
//!
//! The hook is deliberately fail-closed once enabled: a lost control plane or
//! malformed response produces a Claude `deny`, never an implicit allow.

use std::io::Read;
use std::time::Duration;

use serde_json::{json, Value};

const DEFAULT_TIMEOUT: Duration = Duration::from_secs(20);

pub fn run() -> anyhow::Result<()> {
    if !enabled() {
        return Ok(());
    }
    let base_url = std::env::var("LONGHOUSE_HOOK_URL").unwrap_or_default();
    if base_url.trim().is_empty() {
        return Ok(());
    }
    let mut raw = String::new();
    // Match the legacy hook's not-engaged behavior when Claude gives us no
    // readable JSON. A broken stdin is not enough context to deny a tool.
    if std::io::stdin().read_to_string(&mut raw).is_err() {
        return Ok(());
    }
    let input: Value = match serde_json::from_str(&raw) {
        Ok(value) => value,
        Err(_) => return Ok(()),
    };
    let session_id = crate::managed_identity::managed_session_id_for(
        crate::managed_identity_contract::ManagedProvider::Claude,
    )
        .or_else(|| input.get("session_id").and_then(trimmed_value_string));
    let tool_use_id = input.get("tool_use_id").and_then(trimmed_value_string);
    let tool_name = input
        .get("tool_name")
        .and_then(trimmed_value_string)
        .unwrap_or_default();
    let tool_input = input
        .get("tool_input")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let (Some(session_id), Some(tool_use_id)) = (session_id, tool_use_id) else {
        return Ok(());
    };
    let decision = crate::permission_gate::remote_decision(
        base_url.trim_end_matches('/'),
        &std::env::var("LONGHOUSE_HOOK_TOKEN").unwrap_or_default(),
        &session_id,
        &tool_use_id,
        &tool_name,
        tool_input,
        None,
        timeout_from_env(),
    );
    emit(
        decision
            .map(|decision| (decision.decision, decision.reason.unwrap_or_default()))
            .unwrap_or_else(|| {
                (
                    "deny".into(),
                    "Longhouse permission gate could not reach a decision".into(),
                )
            }),
    );
    Ok(())
}

fn trimmed_value_string(value: &Value) -> Option<String> {
    let raw = match value {
        Value::Null => String::new(),
        Value::String(value) => value.clone(),
        other => other.to_string(),
    };
    let trimmed = raw.trim();
    (!trimmed.is_empty()).then(|| trimmed.to_owned())
}

fn enabled() -> bool {
    !matches!(
        std::env::var("LONGHOUSE_PERMISSION_HOOK_ENABLED")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "" | "0" | "false" | "no" | "off"
    )
}

fn timeout_from_env() -> Duration {
    let Some(seconds) = std::env::var("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S")
        .ok()
        .and_then(|value| value.parse::<f64>().ok())
        .filter(|value| value.is_finite())
    else {
        return DEFAULT_TIMEOUT;
    };
    if seconds <= 0.0 {
        Duration::ZERO
    } else {
        Duration::from_secs_f64(seconds).min(DEFAULT_TIMEOUT)
    }
}

fn emit((decision, reason): (String, String)) {
    println!(
        "{}",
        json!({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}})
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timeout_is_clamped_to_twenty_seconds() {
        temp_env::with_var("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S", Some("999"), || {
            assert_eq!(timeout_from_env(), DEFAULT_TIMEOUT);
        });
    }

    #[test]
    fn negative_timeout_matches_legacy_immediate_expiry() {
        temp_env::with_var("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S", Some("-1"), || {
            assert_eq!(timeout_from_env(), Duration::ZERO);
        });
    }

    #[test]
    fn ids_follow_legacy_string_coercion_and_trimming() {
        assert_eq!(trimmed_value_string(&json!(42)).as_deref(), Some("42"));
        assert_eq!(
            trimmed_value_string(&json!("  id  ")).as_deref(),
            Some("id")
        );
        assert_eq!(trimmed_value_string(&json!("   ")), None);
    }
}
