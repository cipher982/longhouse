//! Native Cursor hook commands. Cursor invokes these through hooks.json; they
//! intentionally keep lifecycle evidence best-effort and permission decisions
//! fail-closed when remote authority is enabled.
use chrono::Utc;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant};

fn home() -> PathBuf {
    std::env::var_os("LONGHOUSE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into())).join(".longhouse")
        })
}
fn input() -> Value {
    let mut raw = Vec::new();
    let _ = std::io::stdin().read_to_end(&mut raw);
    serde_json::from_slice(&raw).unwrap_or_else(|_| json!({}))
}
fn write(path: PathBuf, value: &Value) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let temporary = path.with_extension(format!("tmp.{}", std::process::id()));
    if std::fs::write(&temporary, value.to_string()).is_ok() {
        let _ = std::fs::rename(temporary, path);
    }
}
pub fn lifecycle(event: &str) {
    let payload = input();
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
    let claim: Value = std::fs::read(&claim_path)
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_else(|| json!({}));
    if claim.get("session_id").and_then(Value::as_str) != Some(&session_id)
        || claim.get("conversation_uuid").and_then(Value::as_str) != Some(conversation)
        || claim.get("launch_id").and_then(Value::as_str) != Some(&launch_id)
    {
        println!("{{}}");
        return;
    }
    let now = Utc::now().to_rfc3339();
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
    if claim.get("status").and_then(Value::as_str) == Some("pending") {
        let mut observed = claim.clone();
        observed["status"] = json!("observed");
        observed["hook_observed_at"] = json!(now);
        write(claim_path, &observed);
    }
    let event_path = root
        .join("hook-events")
        .join(format!("{session_id}.ndjson"));
    if let Some(parent) = event_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(event_path)
    {
        let _ = writeln!(
            file,
            "{}",
            json!({"event":event,"observed_at":now,"session_id":session_id,"conversation_id":conversation,"payload":payload})
        );
    }
    if let Some(phase) = phase {
        write(
            root.join(format!("{session_id}.phase.json")),
            &json!({"session_id":session_id,"conversation_id":conversation,"launch_id":launch_id,"phase":phase,"generation_id":payload.get("generation_id"),"observed_at":now}),
        );
    }
    println!("{{}}");
}
pub fn permission(event: &str) -> anyhow::Result<()> {
    if std::env::var("LONGHOUSE_PERMISSION_HOOK_ENABLED").as_deref() != Ok("1") {
        println!("{{}}");
        return Ok(());
    }
    let payload = input();
    let session_id = std::env::var("LONGHOUSE_SESSION_ID").unwrap_or_default();
    let conversation = payload
        .get("conversation_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let launch_id = std::env::var("LONGHOUSE_CURSOR_LAUNCH_ID").unwrap_or_default();
    let root = home().join("managed-local/cursor-helm");
    let claim: Value = std::fs::read(
        root.join("binding-probes")
            .join(format!("{session_id}.json")),
    )
    .ok()
    .and_then(|raw| serde_json::from_slice(&raw).ok())
    .unwrap_or_else(|| json!({}));
    let deny = |message: &str| {
        println!("{}", json!({"permission":"deny","user_message":message}));
    };
    let remote_policy = matches!(
        claim.get("permission_policy").and_then(Value::as_str),
        Some("remote_human" | "remote_approve")
    );
    if !claim.get("permission_policy").is_some() {
        println!("{{}}");
        return Ok(());
    }
    if session_id.is_empty()
        || conversation.is_empty()
        || launch_id.is_empty()
        || !remote_policy
        || claim.get("session_id").and_then(Value::as_str) != Some(&session_id)
        || claim.get("conversation_uuid").and_then(Value::as_str) != Some(conversation)
        || claim.get("launch_id").and_then(Value::as_str) != Some(&launch_id)
        || !matches!(
            claim.get("status").and_then(Value::as_str),
            Some("pending" | "observed")
        )
    {
        deny("Longhouse launch identity or permission policy does not match; command blocked");
        return Ok(());
    }
    let base = std::env::var("LONGHOUSE_HOOK_URL").unwrap_or_default();
    let token = std::env::var("LONGHOUSE_HOOK_TOKEN").unwrap_or_default();
    if base.trim().is_empty() || token.trim().is_empty() {
        deny("Longhouse approval service is not configured; command blocked");
        return Ok(());
    }
    let timeout = std::env::var("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S")
        .ok()
        .and_then(|value| value.parse::<f64>().ok())
        .filter(|value| value.is_finite())
        .map(|value| Duration::from_secs_f64(value.clamp(1.0, 120.0)))
        .unwrap_or(Duration::from_secs(20));
    let invocation = payload
        .get("tool_call_id")
        .or_else(|| payload.get("toolCallId"))
        .or_else(|| payload.get("invocation_id"))
        .or_else(|| payload.get("call_id"))
        .map(Value::to_string)
        .unwrap_or_else(|| {
            format!(
                "{}:{}",
                Utc::now().timestamp_nanos_opt().unwrap_or_default(),
                std::process::id()
            )
        });
    let material = format!(
        "{conversation}|{}|{event}|{invocation}|{}",
        payload
            .get("generation_id")
            .map(Value::to_string)
            .unwrap_or_default(),
        payload
            .get("command")
            .or_else(|| payload.get("tool_name"))
            .map(Value::to_string)
            .unwrap_or_default()
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
        let client = reqwest::Client::builder().timeout(Duration::from_secs(5)).build().ok()?;
        let ack: Value = client.post(format!("{base}/api/agents/permission-requests")).header("X-Agents-Token", token).header("User-Agent", "Longhouse-Cursor-Permission-Hook/1").json(&json!({"session_id":session_id,"tool_use_id":request_id,"tool_name":tool_name,"tool_input":tool_input,"provider":"cursor","wait_timeout_seconds":timeout.as_secs_f64()})).send().await.ok()?.error_for_status().ok()?.json().await.ok()?;
        let pause_id = ack.get("pause_request_id")?.as_str()?.trim(); if pause_id.is_empty() { return None; }
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            let result: Value = client.get(format!("{base}/api/agents/permission-decision")).header("X-Agents-Token", token).query(&[("session_id", session_id), ("tool_use_id", request_id), ("pause_request_id", pause_id), ("provider", "cursor")]).send().await.ok()?.error_for_status().ok()?.json().await.ok()?;
            if result.get("resolved").is_some_and(|value| value == true) { let decision = result.get("decision")?.as_str()?.to_ascii_lowercase(); return matches!(decision.as_str(), "allow" | "deny").then_some(decision); }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        let _ = client.post(format!("{base}/api/agents/permission-requests/{pause_id}/expire")).header("X-Agents-Token", token).json(&json!({"session_id":session_id,"reason":"Approval deadline expired before a human decision"})).timeout(Duration::from_secs(2)).send().await;
        None
    })
}
