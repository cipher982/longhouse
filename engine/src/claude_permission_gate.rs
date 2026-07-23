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
    std::io::stdin().read_to_string(&mut raw)?;
    let input: Value = match serde_json::from_str(&raw) {
        Ok(value) => value,
        Err(_) => return Ok(()),
    };
    let session_id = std::env::var("LONGHOUSE_MANAGED_SESSION_ID")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| {
            input
                .get("session_id")
                .and_then(Value::as_str)
                .map(str::to_owned)
        });
    let tool_use_id = input.get("tool_use_id").and_then(Value::as_str);
    let tool_name = input.get("tool_name").and_then(Value::as_str).unwrap_or("");
    let tool_input = input
        .get("tool_input")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let (Some(session_id), Some(tool_use_id)) = (session_id, tool_use_id) else {
        return Ok(());
    };
    let decision = permission_decision(
        base_url.trim_end_matches('/'),
        &std::env::var("LONGHOUSE_HOOK_TOKEN").unwrap_or_default(),
        &session_id,
        tool_use_id,
        tool_name,
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
    std::env::var("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S")
        .ok()
        .and_then(|value| value.parse::<f64>().ok())
        .filter(|value| value.is_finite() && *value >= 0.0)
        .map(Duration::from_secs_f64)
        .unwrap_or(DEFAULT_TIMEOUT)
        .min(DEFAULT_TIMEOUT)
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
        let ack: Value = client
            .post(format!("{base_url}/api/agents/permission-requests"))
            .header("X-Agents-Token", token)
            .json(&json!({"session_id": session_id, "tool_use_id": tool_use_id, "tool_name": tool_name, "tool_input": tool_input}))
            .send().await.ok()?.error_for_status().ok()?.json().await.ok()?;
        let pause_request_id = ack.get("pause_request_id")?.as_str()?.trim();
        if pause_request_id.is_empty() { return None; }
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            let result: Value = client.get(format!("{base_url}/api/agents/permission-decision"))
                .header("X-Agents-Token", token)
                .query(&[("session_id", session_id), ("tool_use_id", tool_use_id), ("pause_request_id", pause_request_id)])
                .send().await.ok()?.error_for_status().ok()?.json().await.ok()?;
            if result.get("resolved").and_then(Value::as_bool) == Some(true) {
                let decision = result.get("decision").and_then(Value::as_str)?.to_ascii_lowercase();
                if !matches!(decision.as_str(), "allow" | "deny" | "ask") { return None; }
                let reason = result.get("reason").and_then(Value::as_str).unwrap_or("Longhouse decision").to_owned();
                return Some((decision, reason));
            }
            tokio::time::sleep(POLL_INTERVAL).await;
        }
        None
    })
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
}
