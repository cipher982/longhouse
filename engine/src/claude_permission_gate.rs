//! Native Claude PreToolUse permission gate.
//!
//! The hook is deliberately fail-closed once enabled: a lost control plane or
//! malformed response produces a Claude `deny`, never an implicit allow.

use std::io::Read;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

const DEFAULT_TIMEOUT: Duration = Duration::from_secs(20);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
const POLL_INTERVAL: Duration = Duration::from_millis(500);

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
    let session_id = std::env::var("LONGHOUSE_MANAGED_SESSION_ID")
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
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
    let decision = permission_decision(
        base_url.trim_end_matches('/'),
        &std::env::var("LONGHOUSE_HOOK_TOKEN").unwrap_or_default(),
        &session_id,
        &tool_use_id,
        &tool_name,
        tool_input,
        timeout_from_env(),
    );
    emit(decision.unwrap_or_else(|| {
        (
            "deny".into(),
            "Longhouse permission gate could not reach a decision".into(),
        )
    }));
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

fn permission_decision(
    base_url: &str,
    token: &str,
    session_id: &str,
    tool_use_id: &str,
    tool_name: &str,
    tool_input: Value,
    timeout: Duration,
) -> Option<(String, String)> {
    let runtime = tokio::runtime::Runtime::new().ok()?;
    runtime.block_on(async {
        let client = reqwest::Client::builder().timeout(REQUEST_TIMEOUT).build().ok()?;
        let mut register = client
            .post(format!("{base_url}/api/agents/permission-requests"))
            .json(&json!({"session_id": session_id, "tool_use_id": tool_use_id, "tool_name": tool_name, "tool_input": tool_input}));
        if !token.trim().is_empty() {
            register = register.header("X-Agents-Token", token.trim());
        }
        let ack: Value = register.send().await.ok()?.error_for_status().ok()?.json().await.ok()?;
        let pause_request_id = ack.get("pause_request_id")?.as_str()?.trim();
        if pause_request_id.is_empty() { return None; }
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            let mut poll = client.get(format!("{base_url}/api/agents/permission-decision"))
                .query(&[("session_id", session_id), ("tool_use_id", tool_use_id), ("pause_request_id", pause_request_id)]);
            if !token.trim().is_empty() {
                poll = poll.header("X-Agents-Token", token.trim());
            }
            let result: Value = poll.send().await.ok()?.error_for_status().ok()?.json().await.ok()?;
            if result.get("resolved").is_some_and(json_truthy) {
                let decision = result.get("decision").and_then(Value::as_str)?.to_ascii_lowercase();
                if !matches!(decision.as_str(), "allow" | "deny" | "ask") { return None; }
                let reason = result.get("reason").and_then(Value::as_str).filter(|value| !value.is_empty()).map(str::to_owned).unwrap_or_else(|| format!("Longhouse {decision}"));
                return Some((decision, reason));
            }
            tokio::time::sleep(POLL_INTERVAL).await;
        }
        None
    })
}

fn json_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
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
