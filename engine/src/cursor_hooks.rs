//! Native Cursor hook commands. Cursor invokes these through hooks.json; they
//! intentionally keep lifecycle evidence best-effort and permission decisions
//! fail-closed when remote authority is enabled.
use chrono::Utc;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::time::{Duration, Instant};
use uuid::Uuid;

const EVENTS: &[&str] = &[
    "sessionStart",
    "sessionEnd",
    "beforeSubmitPrompt",
    "afterAgentThought",
    "afterAgentResponse",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "stop",
];

fn home() -> PathBuf {
    std::env::var_os("LONGHOUSE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into())).join(".longhouse")
        })
}

pub fn configure(cursor_dir: Option<PathBuf>) -> anyhow::Result<PathBuf> {
    let dir = cursor_dir.unwrap_or_else(|| {
        PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into())).join(".cursor")
    });
    std::fs::create_dir_all(&dir)?;
    let path = dir.join("hooks.json");
    let mut config: Value = std::fs::read(&path)
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_else(|| json!({"version":1,"hooks":{}}));
    config["version"] = json!(1);
    let hooks = config["hooks"]
        .as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("Cursor hooks must be an object"))?;
    let engine = format!(
        "'{}'",
        std::env::current_exe()?
            .display()
            .to_string()
            .replace('\'', "'\"'\"'")
    );
    for event in EVENTS {
        let entries = hooks
            .entry((*event).to_owned())
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .ok_or_else(|| anyhow::anyhow!("Cursor hook entries must be arrays"))?;
        entries.retain(|entry| {
            let command = entry.to_string();
            !command.contains("longhouse-cursor-hook.py")
                && !command.contains("longhouse-cursor-permission-hook.py")
                && !command.contains("cursor-lifecycle-hook")
                && !command.contains("cursor-permission-hook")
        });
        entries.push(json!({"command":format!("{engine} cursor-lifecycle-hook {event}"),"timeout":5,"failClosed":false}));
        if matches!(*event, "beforeShellExecution" | "beforeMCPExecution") {
            entries.push(json!({"command":format!("{engine} cursor-permission-hook {event}"),"timeout":125,"failClosed":true}));
        }
    }
    atomic_write(
        &path,
        format!("{}\n", serde_json::to_string_pretty(&config)?).as_bytes(),
    )?;
    Ok(path)
}
fn input() -> anyhow::Result<Value> {
    let mut raw = Vec::new();
    std::io::stdin().read_to_end(&mut raw)?;
    Ok(serde_json::from_slice(&raw)?)
}
pub(crate) fn atomic_write(path: &std::path::Path, bytes: &[u8]) -> std::io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| std::io::Error::other("path has no parent"))?;
    std::fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("cursor-hook"),
        Uuid::new_v4()
    ));
    std::fs::write(&temporary, bytes)?;
    if let Err(error) = std::fs::rename(&temporary, path) {
        let _ = std::fs::remove_file(temporary);
        return Err(error);
    }
    Ok(())
}

fn write(path: PathBuf, value: &Value) -> bool {
    atomic_write(&path, value.to_string().as_bytes()).is_ok()
}

fn append_json_line(path: &std::path::Path, value: &Value) -> std::io::Result<()> {
    let mut line = serde_json::to_vec(value).map_err(std::io::Error::other)?;
    line.push(b'\n');
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    file.write_all(&line)
}

pub fn lifecycle(event: &str) {
    let Ok(payload) = input() else {
        println!("{{}}");
        return;
    };
    let session_id = std::env::var("LONGHOUSE_SESSION_ID").unwrap_or_default();
    let conversation = payload
        .get("conversation_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let launch_id = std::env::var("LONGHOUSE_CURSOR_LAUNCH_ID").unwrap_or_default();
    if session_id.is_empty() || conversation.is_empty() || launch_id.is_empty() {
        println!("{{}}");
        return;
    }
    let root = home().join("managed-local/cursor-helm");
    let claim_path = root
        .join("binding-probes")
        .join(format!("{session_id}.json"));
    let mut claim: Value = std::fs::read(&claim_path)
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_else(|| json!({}));
    if claim.get("session_id").and_then(Value::as_str) != Some(&session_id)
        || claim.get("launch_id").and_then(Value::as_str) != Some(&launch_id)
    {
        println!("{{}}");
        return;
    }
    let now = Utc::now().to_rfc3339();
    if claim.get("conversation_uuid").and_then(Value::as_str) != Some(conversation) {
        if !is_foreground_conversation_rollover(event, &payload)
            || !rotate_cursor_conversation(
                &root,
                &claim_path,
                &mut claim,
                &session_id,
                conversation,
                &now,
            )
        {
            println!("{{}}");
            return;
        }
    }
    let phase = match event {
        "beforeSubmitPrompt"
        | "afterAgentThought"
        | "preToolUse"
        | "beforeShellExecution"
        | "beforeMCPExecution" => Some("active"),
        "sessionStart" | "stop" | "afterAgentResponse" => Some("idle"),
        "sessionEnd" => Some("ended"),
        _ => None,
    };
    // A hook from the exact launched process is the binding observation. Keep
    // the launch fields intact so resume and permission checks retain the same
    // identity; only promote a matching pending reservation.
    let registration_ready = std::env::var("LONGHOUSE_CURSOR_REGISTRATION_READY").as_deref()
        == Ok("1")
        || std::fs::read(root.join(format!("{session_id}.json")))
            .ok()
            .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
            .is_some_and(|state| {
                state.get("registration").and_then(Value::as_str) == Some("registered")
            });
    if registration_ready && claim.get("status").and_then(Value::as_str) == Some("pending") {
        let mut observed = claim.clone();
        observed["status"] = json!("observed");
        observed["hook_observed_at"] = json!(now);
        if write(claim_path, &observed) {
            let _ = std::fs::remove_file(
                root.join("binding-probes")
                    .join(format!("{session_id}.observed-backup.json")),
            );
        }
    }
    let event_path = root
        .join("hook-events")
        .join(format!("{session_id}.ndjson"));
    if let Some(parent) = event_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = append_json_line(
        &event_path,
        &json!({"event":event,"observed_at":now,"session_id":session_id,"conversation_id":conversation,"payload":payload}),
    );
    if let Some(phase) = phase {
        let _ = write(
            root.join(format!("{session_id}.phase.json")),
            &json!({"session_id":session_id,"conversation_id":conversation,"launch_id":launch_id,"phase":phase,"generation_id":payload.get("generation_id"),"observed_at":now}),
        );
        let state = if phase == "active" {
            "thinking"
        } else {
            "idle"
        };
        let _ = write(
            home()
                .join("agent/outbox")
                .join(format!("prs.{}.json", Uuid::new_v4())),
            &json!({"session_id":session_id,"state":state,"tool_name":payload.get("tool_name"),"cwd":payload.get("cwd"),"provider":"cursor","control_path":"managed"}),
        );
    }
    if matches!(event, "afterAgentResponse" | "stop" | "sessionEnd") {
        wake_transcript(&session_id, conversation, payload.get("generation_id"));
    }
    println!("{{}}");
}

fn is_foreground_conversation_rollover(event: &str, payload: &Value) -> bool {
    match event {
        "sessionStart" => {
            payload.get("is_background_agent").and_then(Value::as_bool) == Some(false)
        }
        // Cursor 2026.07.23 does not emit sessionStart after `/clear`. Its
        // first event for the replacement top-level conversation is the
        // human prompt, and that foreground payload omits is_background_agent.
        // One cursor-agent Helm process owns one foreground conversation;
        // background hooks explicitly set the flag. Reject those and empty
        // prompts; later thought/tool/response hooks cannot rotate identity.
        "beforeSubmitPrompt" => {
            payload.get("is_background_agent").and_then(Value::as_bool) != Some(true)
                && payload
                    .get("prompt")
                    .and_then(Value::as_str)
                    .is_some_and(|value| !value.trim().is_empty())
        }
        _ => false,
    }
}

fn rotate_cursor_conversation(
    root: &std::path::Path,
    claim_path: &std::path::Path,
    claim: &mut Value,
    session_id: &str,
    conversation: &str,
    observed_at: &str,
) -> bool {
    let Some(previous) = claim
        .get("conversation_uuid")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_string)
    else {
        return false;
    };
    let history = claim.as_object_mut().and_then(|object| {
        object
            .entry("previous_conversation_uuids")
            .or_insert_with(|| json!([]))
            .as_array_mut()
    });
    let Some(history) = history else {
        return false;
    };
    // This is ordered transition history, not a set. A -> B -> A -> B must
    // retain A as the final predecessor so the first post-upgrade archive can
    // emit a distinct boundary even before source-epoch identity is populated.
    history.push(Value::String(previous));
    claim["conversation_uuid"] = Value::String(conversation.to_string());
    claim["status"] = Value::String("observed".to_string());
    claim["hook_observed_at"] = Value::String(observed_at.to_string());
    claim["updated_at"] = Value::String(observed_at.to_string());
    if !write(claim_path.to_path_buf(), claim) {
        return false;
    }

    let state_path = root.join(format!("{session_id}.json"));
    let Some(mut state) = std::fs::read(&state_path)
        .ok()
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
    else {
        return true;
    };
    if state.get("session_id").and_then(Value::as_str) == Some(session_id) {
        state["provider_session_id"] = Value::String(conversation.to_string());
        state["updated_at"] = Value::String(observed_at.to_string());
        let _ = write(state_path, &state);
    }
    true
}

fn wake_transcript(session_id: &str, conversation: &str, generation_id: Option<&Value>) {
    let cursor_home = std::env::var_os("CURSOR_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into())).join(".cursor")
        });
    let Ok(workspaces) = std::fs::read_dir(cursor_home.join("chats")) else {
        return;
    };
    let stores: Vec<PathBuf> = workspaces
        .flatten()
        .map(|entry| entry.path().join(conversation).join("store.db"))
        .filter(|path| path.is_file())
        .collect();
    let [store] = stores.as_slice() else {
        return;
    };
    let socket = home().join("agent/transcript-wake.sock");
    let Ok(mut stream) = UnixStream::connect(socket) else {
        return;
    };
    let _ = stream.set_write_timeout(Some(Duration::from_millis(75)));
    let observed_at_ms = Utc::now().timestamp_millis();
    let file_len_hint = store
        .metadata()
        .map(|value| value.len())
        .unwrap_or_default();
    let _ = stream.write_all(
        json!({
            "provider":"cursor",
            "path":store,
            "phase":"idle",
            "session_id":session_id,
            "turn_id":generation_id,
            "wake_reason":"turn_completed",
            "observed_at_ms":observed_at_ms,
            "file_len_hint":file_len_hint,
        })
        .to_string()
        .as_bytes(),
    );
}
pub fn permission(event: &str) -> anyhow::Result<()> {
    if std::env::var("LONGHOUSE_PERMISSION_HOOK_ENABLED").as_deref() != Ok("1") {
        println!("{{}}");
        return Ok(());
    }
    let deny = |message: &str| {
        println!("{}", json!({"permission":"deny","user_message":message}));
    };
    let payload = match input() {
        Ok(payload) => payload,
        Err(_) => {
            deny("Longhouse received malformed Cursor hook input; command blocked");
            return Ok(());
        }
    };
    let session_id = std::env::var("LONGHOUSE_SESSION_ID").unwrap_or_default();
    let conversation = payload
        .get("conversation_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let launch_id = std::env::var("LONGHOUSE_CURSOR_LAUNCH_ID").unwrap_or_default();
    let root = home().join("managed-local/cursor-helm");
    if session_id.is_empty() || conversation.is_empty() || launch_id.is_empty() {
        deny("Longhouse launch identity is incomplete; command blocked");
        return Ok(());
    }
    let claim: Value = match std::fs::read(
        root.join("binding-probes")
            .join(format!("{session_id}.json")),
    ) {
        Ok(raw) => match serde_json::from_slice(&raw) {
            Ok(claim) => claim,
            Err(_) => {
                deny("Longhouse launch identity could not be verified; command blocked");
                return Ok(());
            }
        },
        Err(_) => {
            deny("Longhouse launch identity could not be verified; command blocked");
            return Ok(());
        }
    };
    let policy = claim.get("permission_policy").and_then(Value::as_str);
    if policy.is_none() {
        println!("{{}}");
        return Ok(());
    }
    let remote_policy = matches!(policy, Some("remote_human" | "remote_approve"));
    let identity_matches = claim.get("session_id").and_then(Value::as_str)
        == Some(session_id.as_str())
        && claim.get("conversation_uuid").and_then(Value::as_str) == Some(conversation)
        && claim.get("launch_id").and_then(Value::as_str) == Some(launch_id.as_str())
        && matches!(
            claim.get("status").and_then(Value::as_str),
            Some("pending" | "observed")
        );
    if !identity_matches {
        deny("Longhouse launch identity or permission policy does not match; command blocked");
        return Ok(());
    }
    if !remote_policy {
        deny("Longhouse launch identity or permission policy does not match; command blocked");
        return Ok(());
    }
    let base = std::env::var("LONGHOUSE_HOOK_URL").unwrap_or_default();
    let token = std::env::var("LONGHOUSE_HOOK_TOKEN").unwrap_or_default();
    if base.trim().is_empty() || token.trim().is_empty() {
        deny("Longhouse approval service is not configured; command blocked");
        return Ok(());
    }
    let timeout = match std::env::var("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S") {
        Ok(value) => match value.parse::<f64>() {
            Ok(value) if value.is_finite() => Duration::from_secs_f64(value.clamp(1.0, 120.0)),
            _ => {
                deny("Longhouse approval timeout is invalid; command blocked");
                return Ok(());
            }
        },
        Err(_) => Duration::from_secs(20),
    };
    let string_field = |names: &[&str]| {
        names.iter().find_map(|name| {
            payload
                .get(*name)
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
        })
    };
    let invocation = string_field(&["tool_call_id", "toolCallId", "invocation_id", "call_id"])
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let material = format!(
        "{conversation}|{}|{event}|{invocation}|{}",
        string_field(&["generation_id"]).unwrap_or_default(),
        string_field(&["command", "tool_name"]).unwrap_or_default()
    );
    let request_id = format!("{:x}", Sha256::digest(material.as_bytes()));
    let tool_name = if event == "beforeShellExecution" {
        "Shell"
    } else {
        payload
            .get("tool_name")
            .and_then(Value::as_str)
            .unwrap_or("MCP")
    };
    let tool_input = if event == "beforeShellExecution" {
        json!({"command": payload.get("command")})
    } else {
        payload
            .get("tool_input")
            .or_else(|| payload.get("arguments"))
            .cloned()
            .unwrap_or_else(|| json!({}))
    };
    let outcome = remote_decision(
        base.trim_end_matches('/'),
        token.trim(),
        &session_id,
        &request_id,
        tool_name,
        tool_input,
        timeout,
    );
    match outcome.as_deref() {
        Some("allow") => println!("{}", json!({"permission":"allow"})),
        Some("deny") => println!("{}", json!({"permission":"deny"})),
        _ => deny("No human approval was received before the Longhouse deadline; command blocked"),
    }
    Ok(())
}

fn remote_decision(
    base: &str,
    token: &str,
    session_id: &str,
    request_id: &str,
    tool_name: &str,
    tool_input: Value,
    timeout: Duration,
) -> Option<String> {
    let runtime = tokio::runtime::Runtime::new().ok()?;
    runtime.block_on(async {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .ok()?;
        let ack: Value = client
            .post(format!("{base}/api/agents/permission-requests"))
            .header("X-Agents-Token", token)
            .header("User-Agent", "Longhouse-Cursor-Permission-Hook/1")
            .json(&json!({
                "session_id": session_id,
                "tool_use_id": request_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "provider": "cursor",
                "wait_timeout_seconds": timeout.as_secs_f64(),
            }))
            .send()
            .await
            .ok()?
            .error_for_status()
            .ok()?
            .json()
            .await
            .ok()?;
        let pause_id = ack.get("pause_request_id")?.as_str()?.trim();
        if pause_id.is_empty() {
            return None;
        }
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            let result: Value = client
                .get(format!("{base}/api/agents/permission-decision"))
                .header("X-Agents-Token", token)
                .query(&[
                    ("session_id", session_id),
                    ("tool_use_id", request_id),
                    ("pause_request_id", pause_id),
                    ("provider", "cursor"),
                ])
                .send()
                .await
                .ok()?
                .error_for_status()
                .ok()?
                .json()
                .await
                .ok()?;
            if result.get("resolved").is_some_and(|value| value == true) {
                let decision = result.get("decision")?.as_str()?.to_ascii_lowercase();
                return matches!(decision.as_str(), "allow" | "deny").then_some(decision);
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        let _ = client
            .post(format!(
                "{base}/api/agents/permission-requests/{pause_id}/expire"
            ))
            .header("X-Agents-Token", token)
            .json(&json!({
                "session_id": session_id,
                "reason": "Approval deadline expired before a human decision",
            }))
            .timeout(Duration::from_secs(2))
            .send()
            .await;
        None
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn conversation_rotation_preserves_history_and_updates_active_state() {
        let temp = tempfile::tempdir().unwrap();
        let claim_path = temp.path().join("binding-probes/session.json");
        let state_path = temp.path().join("session.json");
        let mut claim = json!({
            "session_id": "session",
            "conversation_uuid": "conversation-old",
            "launch_id": "launch",
            "status": "observed"
        });
        write(claim_path.clone(), &claim);
        write(
            state_path.clone(),
            &json!({
                "session_id": "session",
                "provider_session_id": "conversation-old",
                "updated_at": "before"
            }),
        );

        assert!(rotate_cursor_conversation(
            temp.path(),
            &claim_path,
            &mut claim,
            "session",
            "conversation-new",
            "2026-07-31T12:00:00Z",
        ));

        let durable_claim: Value =
            serde_json::from_slice(&std::fs::read(&claim_path).unwrap()).unwrap();
        let durable_state: Value =
            serde_json::from_slice(&std::fs::read(state_path).unwrap()).unwrap();
        assert_eq!(durable_claim["conversation_uuid"], "conversation-new");
        assert_eq!(
            durable_claim["previous_conversation_uuids"],
            json!(["conversation-old"])
        );
        assert_eq!(durable_state["provider_session_id"], "conversation-new");
        assert_eq!(durable_state["updated_at"], "2026-07-31T12:00:00Z");

        assert!(rotate_cursor_conversation(
            temp.path(),
            &claim_path,
            &mut claim,
            "session",
            "conversation-old",
            "2026-07-31T12:01:00Z",
        ));
        assert!(rotate_cursor_conversation(
            temp.path(),
            &claim_path,
            &mut claim,
            "session",
            "conversation-new",
            "2026-07-31T12:02:00Z",
        ));
        let repeated: Value = serde_json::from_slice(&std::fs::read(claim_path).unwrap()).unwrap();
        assert_eq!(
            repeated["previous_conversation_uuids"],
            json!(["conversation-old", "conversation-new", "conversation-old"])
        );
    }

    #[test]
    fn conversation_rotation_requires_explicit_foreground_evidence() {
        assert!(is_foreground_conversation_rollover(
            "sessionStart",
            &json!({"is_background_agent": false})
        ));
        assert!(!is_foreground_conversation_rollover(
            "sessionStart",
            &json!({"is_background_agent": true})
        ));
        assert!(!is_foreground_conversation_rollover(
            "sessionStart",
            &json!({})
        ));
        assert!(is_foreground_conversation_rollover(
            "beforeSubmitPrompt",
            &json!({"prompt": "post-reset prompt"})
        ));
        assert!(!is_foreground_conversation_rollover(
            "beforeSubmitPrompt",
            &json!({"prompt": "background", "is_background_agent": true})
        ));
        assert!(!is_foreground_conversation_rollover(
            "beforeSubmitPrompt",
            &json!({"prompt": ""})
        ));
        assert!(!is_foreground_conversation_rollover(
            "afterAgentThought",
            &json!({})
        ));
    }
}
