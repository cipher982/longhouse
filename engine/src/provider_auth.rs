use std::collections::HashMap;

use serde_json::Value;

pub const AUTH_REQUIRED: &str = "provider_auth_required";
pub const AUTH_MISSING: &str = "provider_auth_missing";
pub const AUTH_INVALID: &str = "provider_auth_invalid";
pub const AUTH_HELPER_FAILED: &str = "provider_auth_helper_failed";

const AUTH_FAILURE_REASONS: &[&str] = &[
    AUTH_REQUIRED,
    AUTH_MISSING,
    AUTH_INVALID,
    AUTH_HELPER_FAILED,
];

pub fn auth_failure_reason(reason: &str) -> Option<&'static str> {
    match reason {
        AUTH_REQUIRED => Some(AUTH_REQUIRED),
        AUTH_MISSING => Some(AUTH_MISSING),
        AUTH_INVALID => Some(AUTH_INVALID),
        AUTH_HELPER_FAILED => Some(AUTH_HELPER_FAILED),
        _ => None,
    }
}

pub fn is_auth_failure_reason(reason: &str) -> bool {
    AUTH_FAILURE_REASONS.contains(&reason)
}

/// OMP reports key resolution failures in the structured `prompt_result.error.message` field.
pub fn omp_prompt_result_reason(message: &str) -> Option<&'static str> {
    let message = message.trim();
    if message.starts_with("No API key found for ") {
        Some(AUTH_MISSING)
    } else if message.starts_with("omp-openai-key:") {
        Some(AUTH_HELPER_FAILED)
    } else if message.starts_with("401 ")
        && (message.contains("Incorrect API key provided") || message.contains("invalid_api_key"))
    {
        Some(AUTH_INVALID)
    } else {
        None
    }
}

/// Codex's app-server turn error is structured at `params.turn.error`.
pub fn codex_turn_error_reason(completed: &Value) -> Option<&'static str> {
    let error = completed.pointer("/params/turn/error")?;
    let code = error.get("code").and_then(Value::as_str);
    let kind = error.get("type").and_then(Value::as_str);
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or_default();
    (code == Some("invalid_api_key")
        || kind == Some("authentication_error")
        || (message.starts_with("unexpected status 401 Unauthorized: Incorrect API key provided")
            || (message.starts_with("401 ") && message.contains("invalid_api_key"))))
    .then_some(AUTH_INVALID)
}

/// Latest typed auth refusal per provider from the durable Console turn claims.
/// A later completed turn is evidence that the provider accepted authentication;
/// unrelated failures do not erase the refusal.
pub fn latest_auth_failures() -> HashMap<String, &'static str> {
    let Ok(registry) = crate::turn_claims::default_registry() else {
        return HashMap::new();
    };
    let Ok(claims) = registry.list_all_shared() else {
        return HashMap::new();
    };
    let mut history: HashMap<String, AuthHistory> = HashMap::new();
    for claim in claims.iter() {
        let Some(event) = claim.terminal_event.as_ref() else {
            continue;
        };
        let Some(payload) = event.get("payload") else {
            continue;
        };
        let provider = claim.provider.as_str();
        if provider.is_empty() {
            continue;
        }
        let occurred_at = event
            .get("occurred_at")
            .and_then(Value::as_str)
            .unwrap_or(claim.updated_at.as_str());
        let item = history.entry(provider.to_string()).or_default();
        match payload.get("terminal_state").and_then(Value::as_str) {
            Some("run_completed") => {
                if item
                    .success_at
                    .as_deref()
                    .is_none_or(|previous| occurred_at > previous)
                {
                    item.success_at = Some(occurred_at.to_string());
                }
            }
            Some("run_failed") => {
                let Some(reason) = payload.get("terminal_reason").and_then(Value::as_str) else {
                    continue;
                };
                let Some(reason) = auth_failure_reason(reason) else {
                    continue;
                };
                if item
                    .failure_at
                    .as_deref()
                    .is_some_and(|previous| occurred_at <= previous)
                {
                    continue;
                }
                item.failure_at = Some(occurred_at.to_string());
                item.failure_reason = Some(reason);
            }
            _ => {}
        }
    }
    history
        .into_iter()
        .filter_map(|(provider, item)| {
            let failure_at = item.failure_at?;
            if item
                .success_at
                .as_deref()
                .is_some_and(|success_at| success_at >= failure_at.as_str())
            {
                return None;
            }
            Some((provider, item.failure_reason?))
        })
        .collect()
}

#[derive(Default)]
struct AuthHistory {
    failure_at: Option<String>,
    failure_reason: Option<&'static str>,
    success_at: Option<String>,
}

/// Annotate probe results with the last auth refusal without confusing that
/// evidence with a fresh CLI credential probe.
pub fn apply_auth_failure_overrides(readiness: &mut Value) {
    let Some(entries) = readiness.as_object_mut() else {
        return;
    };
    for (provider, reason) in latest_auth_failures() {
        let Some(entry) = entries.get_mut(&provider).and_then(Value::as_object_mut) else {
            continue;
        };
        let probe_state = match entry.get("state").and_then(Value::as_str) {
            Some("ready") => Some("ready"),
            Some("unknown") | None => Some("unknown"),
            _ => None,
        };
        entry.insert("auth_failure_reason".into(), Value::String(reason.into()));
        if let Some(probe_state) = probe_state {
            entry.insert("probe_state".into(), Value::String(probe_state.into()));
            entry.insert("state".into(), Value::String("auth_failed".into()));
            entry.insert(
                "detail".into(),
                Value::String(
                    "The last Console turn was rejected for provider authentication".into(),
                ),
            );
            entry.insert(
                "remediation".into(),
                Value::String(if reason == AUTH_HELPER_FAILED {
                    format!("Check the {provider} credential helper on this machine")
                } else {
                    format!("Sign in to {provider} on this machine")
                }),
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn omp_only_classifies_its_prompt_result_error_shapes() {
        assert_eq!(
            omp_prompt_result_reason("No API key found for openai"),
            Some(AUTH_MISSING)
        );
        assert_eq!(
            omp_prompt_result_reason(
                "omp-openai-key: target dev unavailable: credential helper timed out"
            ),
            Some(AUTH_HELPER_FAILED)
        );
        assert_eq!(
            omp_prompt_result_reason(
                "401 Incorrect API key provided: masked (param=invalid_api_key)"
            ),
            Some(AUTH_INVALID)
        );
        assert_eq!(omp_prompt_result_reason("request timed out"), None);
    }

    #[test]
    fn codex_only_classifies_its_structured_turn_auth_error() {
        let failed = serde_json::json!({
            "params": {"turn": {"status": "failed", "error": {
                "message": "unexpected status 401 Unauthorized: Incorrect API key provided"
            }}}
        });
        let timeout = serde_json::json!({
            "params": {"turn": {"status": "failed", "error": {"message": "request timed out"}}}
        });
        assert_eq!(codex_turn_error_reason(&failed), Some(AUTH_INVALID));
        assert_eq!(codex_turn_error_reason(&timeout), None);
        assert_eq!(
            codex_turn_error_reason(&serde_json::json!({"message":"401 Unauthorized"})),
            None
        );
    }

    #[test]
    fn last_auth_refusal_overrides_a_ready_probe_until_a_later_completed_turn() {
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            let registry = crate::turn_claims::default_registry().unwrap();
            let session_id = uuid::Uuid::new_v4().to_string();
            let thread_id = uuid::Uuid::new_v4().to_string();
            let terminal_event = |run_id: &str, state: &str, reason: &str, occurred_at: &str| {
                serde_json::json!({
                    "runtime_key": format!("codex:{session_id}"),
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "provider": "codex",
                    "device_id": "cinder",
                    "source": "codex_exec",
                    "kind": "terminal_signal",
                    "occurred_at": occurred_at,
                    "dedupe_key": format!("codex:{run_id}:terminal"),
                    "payload": {"terminal_state": state, "terminal_reason": reason}
                })
            };
            let failed_run = uuid::Uuid::new_v4().to_string();
            let _ = registry
                .claim(&failed_run, &session_id, &thread_id, None, None, "codex")
                .unwrap();
            registry
                .mark_terminal_with_event(
                    &failed_run,
                    "run_failed",
                    Some("rejected".to_string()),
                    terminal_event(
                        &failed_run,
                        "run_failed",
                        AUTH_INVALID,
                        "2026-10-12T00:00:00Z",
                    ),
                )
                .unwrap();

            let mut readiness =
                serde_json::json!({"codex": {"state": "ready", "detail": "max plan"}});
            apply_auth_failure_overrides(&mut readiness);
            assert_eq!(readiness["codex"]["state"], "auth_failed");
            assert_eq!(readiness["codex"]["probe_state"], "ready");
            assert_eq!(readiness["codex"]["auth_failure_reason"], AUTH_INVALID);

            let success_run = uuid::Uuid::new_v4().to_string();
            let _ = registry
                .claim(&success_run, &session_id, &thread_id, None, None, "codex")
                .unwrap();
            registry
                .mark_terminal_with_event(
                    &success_run,
                    "run_completed",
                    None,
                    terminal_event(
                        &success_run,
                        "run_completed",
                        "run_completed",
                        "2026-10-12T00:01:00Z",
                    ),
                )
                .unwrap();
            let mut readiness =
                serde_json::json!({"codex": {"state": "ready", "detail": "max plan"}});
            apply_auth_failure_overrides(&mut readiness);
            assert_eq!(readiness["codex"]["state"], "ready");
            assert!(readiness["codex"].get("auth_failure_reason").is_none());
        });
    }
}
