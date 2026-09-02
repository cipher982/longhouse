//! Shared remote permission protocol for provider control hooks.
//!
//! Claude and Cursor expose different hook payloads, but Longhouse's remote
//! approval protocol is the same. Keeping the request/poll deadline here makes
//! the safety property one implementation: an engaged hook never waits longer
//! than its configured decision budget and never turns a transport failure into
//! an allow.

use std::time::{Duration, Instant};

use serde_json::{json, Value};

const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
const POLL_INTERVAL: Duration = Duration::from_millis(500);

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PermissionDecision {
    pub decision: String,
    pub reason: Option<String>,
}

pub(crate) fn remote_decision(
    base_url: &str,
    token: &str,
    session_id: &str,
    tool_use_id: &str,
    tool_name: &str,
    tool_input: Value,
    provider: Option<&str>,
    timeout: Duration,
) -> Option<PermissionDecision> {
    let runtime = tokio::runtime::Runtime::new().ok()?;
    runtime.block_on(async move {
        let client = reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .build()
            .ok()?;
        let deadline = Instant::now() + timeout;
        let request_timeout = remaining_request_timeout(deadline)?;
        let mut request = json!({
            "session_id": session_id,
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        });
        if let Some(provider) = provider {
            request["provider"] = json!(provider);
            request["wait_timeout_seconds"] = json!(timeout.as_secs_f64());
        }
        let mut register = client
            .post(format!("{base_url}/api/agents/permission-requests"))
            .json(&request)
            .timeout(request_timeout);
        if !token.trim().is_empty() {
            register = register.header("X-Agents-Token", token.trim());
        }
        if provider.is_some() {
            register = register.header("User-Agent", "Longhouse-Permission-Hook/1");
        }
        let ack: Value = register
            .send()
            .await
            .ok()?
            .error_for_status()
            .ok()?
            .json()
            .await
            .ok()?;
        let pause_request_id = ack.get("pause_request_id")?.as_str()?.trim();
        if pause_request_id.is_empty() {
            return None;
        }

        let mut decision = None;
        while let Some(request_timeout) = remaining_request_timeout(deadline) {
            let mut poll = client
                .get(format!("{base_url}/api/agents/permission-decision"))
                .query(&[
                    ("session_id", session_id),
                    ("tool_use_id", tool_use_id),
                    ("pause_request_id", pause_request_id),
                ])
                .timeout(request_timeout);
            if !token.trim().is_empty() {
                poll = poll.header("X-Agents-Token", token.trim());
            }
            if let Some(provider) = provider {
                poll = poll.query(&[("provider", provider)]);
            }
            let result: Value = match poll.send().await {
                Ok(response) => match response.error_for_status() {
                    Ok(response) => match response.json().await {
                        Ok(result) => result,
                        Err(_) => break,
                    },
                    Err(_) => break,
                },
                Err(_) => break,
            };
            if result.get("resolved").is_some_and(json_truthy) {
                let Some(value) = result.get("decision").and_then(Value::as_str) else {
                    break;
                };
                let value = value.to_ascii_lowercase();
                if !matches!(value.as_str(), "allow" | "deny" | "ask") {
                    break;
                }
                decision = Some(PermissionDecision {
                    decision: value,
                    reason: result
                        .get("reason")
                        .and_then(Value::as_str)
                        .filter(|value| !value.is_empty())
                        .map(str::to_owned),
                });
                break;
            }
            let Some(sleep_for) =
                remaining_request_timeout(deadline).map(|value| value.min(POLL_INTERVAL))
            else {
                break;
            };
            tokio::time::sleep(sleep_for).await;
        }

        if decision.is_some() {
            return decision;
        }

        // A provider hook owns the local wait, so it also owns cleanup after
        // any post-registration failure (timeout, malformed response, or
        // transport loss). Keep that cleanup bounded and provider-specific:
        // Claude's native gate has a server-side pause lifecycle, while
        // Cursor's hook server uses request completion as its shutdown signal.
        if provider.is_some() {
            let mut expire = client
                .post(format!(
                    "{base_url}/api/agents/permission-requests/{pause_request_id}/expire"
                ))
                .json(&json!({
                    "session_id": session_id,
                    "reason": "Approval deadline expired before a human decision",
                }))
                .timeout(Duration::from_secs(2));
            if !token.trim().is_empty() {
                expire = expire.header("X-Agents-Token", token.trim());
            }
            let _ = expire.send().await;
        }
        None
    })
}

fn remaining_request_timeout(deadline: Instant) -> Option<Duration> {
    let remaining = deadline.saturating_duration_since(Instant::now());
    (!remaining.is_zero()).then_some(remaining.min(REQUEST_TIMEOUT))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_timeout_is_never_longer_than_decision_deadline() {
        let deadline = Instant::now() + Duration::from_millis(50);
        assert!(remaining_request_timeout(deadline).unwrap() <= Duration::from_millis(50));
        assert!(remaining_request_timeout(Instant::now()).is_none());
    }
}
