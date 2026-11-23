//! Native, local-only Claude lifecycle hook.
//!
//! Claude invokes this once per hook event. It must stay small: parse stdin,
//! enqueue a presence record, and exit 0. The daemon owns durable local
//! projections such as managed transcript bindings.

use std::collections::{HashMap, HashSet};
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::sync::{LazyLock, Mutex};

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::pipeline::parser::{ParsedEvent, Role};

pub fn run() -> anyhow::Result<()> {
    // Claude treats hook failures as an interactive interruption. This command
    // is observability-only, so every local failure is deliberately swallowed.
    let _ = run_inner();
    Ok(())
}

/// Whether the inherited managed-session claim actually belongs to Claude.
///
/// `LONGHOUSE_MANAGED_SESSION_ID` is ambient authority: every child process
/// inherits it, and nothing in the value binds it to the process that should
/// hold it. A `claude` launched from inside a managed Codex session therefore
/// inherited that session's id and bound its transcripts to it — four Claude
/// transcripts ended up owned by one Codex session on the author's machine, and
/// the Runtime Host correctly refused every upload because the envelope claimed
/// `provider=claude` for a session it knows is `provider=codex`. That refusal
/// held local health red indefinitely, and deleting the queued rows only made
/// the engine rebuild the identical envelope from the same binding.
///
/// Every launcher now tags the claim with its owner, so a mismatch is provable.
/// Absence is not: an older launcher predates the tag, and treating "no tag" as
/// "not mine" would silently unmanage live sessions across an upgrade. So this
/// fails closed only on a contradiction, which is the case that caused harm.
#[cfg(test)]
fn managed_claim_belongs_to_claude() -> bool {
    crate::managed_identity::managed_claim_belongs_to(
        crate::managed_identity_contract::ManagedProvider::Claude,
    )
}

fn run_inner() -> anyhow::Result<()> {
    let mut raw = String::new();
    if std::io::stdin().read_to_string(&mut raw).is_err() {
        return Ok(());
    }
    let Ok(input) = serde_json::from_str::<Value>(&raw) else {
        return Ok(());
    };
    handle_input(&input)
}

/// Everything the hook does once its input is parsed.
///
/// Split from `run_inner` so tests can hand it a payload instead of stdin.
fn handle_input(input: &Value) -> anyhow::Result<()> {
    let (output, result) =
        evaluate_input_outcome(input, crate::warp_cli_agent::is_warp_terminal(), true);
    if let Some(output) = output {
        println!("{output}");
    }
    result
}

#[cfg_attr(not(test), allow(dead_code))]
fn evaluate_input(
    input: &Value,
    warp_enabled: bool,
    try_tty: bool,
) -> anyhow::Result<Option<Value>> {
    let (output, result) = evaluate_input_outcome(input, warp_enabled, try_tty);
    match result {
        Ok(()) => Ok(output),
        Err(_) if output.is_some() => Ok(output),
        Err(error) => Err(error),
    }
}

fn non_empty_output(map: serde_json::Map<String, Value>) -> Option<Value> {
    (!map.is_empty()).then_some(Value::Object(map))
}

fn persist_presence_and_edges(
    home: &Path,
    session_id: &str,
    event: &str,
    child_origin: bool,
    input: &Value,
    payload: &Value,
    observation: &HookObservation,
) -> anyhow::Result<()> {
    if !child_origin
        && event == "PreToolUse"
        && string(input, "tool_name").as_deref() == Some("Monitor")
    {
        record_monitor_invocation(home, session_id, input);
    } else if event == "SessionEnd" {
        clear_monitor_ledger(home, session_id);
    }
    if (!child_origin && observation.status.is_some())
        || payload.get("delegation").is_some()
        || payload.get("delegation_update").is_some()
        || payload.get("session_usage").is_some()
        || payload.get("session_crons").is_some()
        || payload.get("permission_mode").is_some()
        || payload.get("git_branch").is_some()
        || matches!(event, "CwdChanged" | "SessionEnd")
    {
        crate::hook_outbox::enqueue_presence(home, payload)?;
    }
    if !child_origin {
        if let Some(edge) = observation.edge.as_ref() {
            enqueue_interaction_edge(session_id, edge)?;
        }
    }
    Ok(())
}

fn evaluate_input_outcome(
    input: &Value,
    warp_enabled: bool,
    try_tty: bool,
) -> (Option<Value>, anyhow::Result<()>) {
    let event = string(input, "hook_event_name").unwrap_or_default();
    let observation = observation_for_event(&event, input);
    let occurred_at = Utc::now().to_rfc3339();
    let cwd = if event == "CwdChanged" {
        string(input, "new_cwd").or_else(|| string(input, "cwd"))
    } else {
        string(input, "cwd")
    };
    let managed_session_id = crate::managed_identity::managed_session_id_for(
        crate::managed_identity_contract::ManagedProvider::Claude,
    );
    let provider_session_id = string(input, "session_id");
    let session_id = managed_session_id
        .clone()
        .or_else(|| provider_session_id.clone());
    // Stop and SubagentStop carry a complete registry scoped to the parent
    // session. ClaudeModState only carries a complete registry when the sender
    // explicitly includes `background_tasks`; otherwise `$.agent.list()` covers
    // subagents/teammates only and must sync via `delegation_update` so active
    // shell/monitor tasks are never wiped.
    let is_complete_registry = matches!(event.as_str(), "Stop" | "SubagentStop")
        || (event == "ClaudeModState" && input.get("background_tasks").is_some());
    let monitor_descriptions = if is_complete_registry {
        longhouse_home()
            .ok()
            .zip(session_id.as_deref())
            .map(|(home, sid)| load_monitor_descriptions(&home, sid))
            .unwrap_or_default()
    } else {
        HashSet::new()
    };
    let mut registry = if is_complete_registry {
        delegation_snapshot_with_monitors(input, &occurred_at, Some(&monitor_descriptions))
    } else {
        None
    };
    if event == "SessionEnd" && registry.is_none() {
        registry = Some(empty_delegation_snapshot(&occurred_at));
    }
    let lifecycle_update = lifecycle_update_for_event(&event, input, &occurred_at);
    let session_usage = extract_session_usage(input);
    let session_crons = extract_session_crons(input);
    let permission_mode =
        bounded_required_task_text(input.get("permission_mode"), DELEGATION_STATUS_MAX_CHARS);
    let git_branch = string(input, "git_branch").or_else(|| {
        if matches!(event.as_str(), "SessionStart" | "CwdChanged") {
            git_head_branch(cwd.as_deref())
        } else {
            None
        }
    });
    if !matches!(
        event.as_str(),
        "SessionEnd" | "CwdChanged" | "ClaudeModState" | "PermissionRequest"
    ) && observation.status.is_none()
        && observation.edge.is_none()
        && registry.is_none()
        && lifecycle_update.is_none()
        && session_usage.is_none()
        && session_crons.is_none()
        && permission_mode.is_none()
        && git_branch.is_none()
    {
        return (None, Ok(()));
    }
    let Some(session_id) = session_id else {
        return (None, Ok(()));
    };
    let mut hook_output = serde_json::Map::new();
    let mut turn_control_blocked_stop = false;
    // Turn control runs before observability, which may fail and return early.
    if let Some(managed) = managed_session_id.as_deref() {
        if let Some(Value::Object(map)) =
            crate::claude_channel_control::lifecycle_hook_turn_control(managed, &event, input)
        {
            turn_control_blocked_stop =
                event == "Stop" && map.get("decision").and_then(Value::as_str) == Some("block");
            hook_output.extend(map);
        }
    }
    // A subagent's tool events fire these same hooks and carry `agent_id`
    // (with `agent_type` beside it). Presence is keyed by session id, and a
    // managed child inherits `LONGHOUSE_MANAGED_SESSION_ID`, so without this
    // guard a child's PreToolUse writes the *parent's* activity head: the
    // parent reads "Using Bash" while its own turn sits idle waiting on that
    // child. Suppress rather than retarget, because at PreToolUse there is
    // usually no child run to key to yet — the child arrives as a nested
    // session after ingest. Turn control above stays: it is what honours an
    // interrupt at a tool boundary, and it writes control-plane markers
    // rather than served state.
    let child_origin = string(input, "agent_id").is_some();
    if child_origin && registry.is_none() && lifecycle_update.is_none() {
        return (non_empty_output(hook_output), Ok(()));
    }
    let transcript_path = string(input, "transcript_path");
    // A bare Claude session that started before the machine's import scope is
    // not shipped, so it is not announced either: presence carries the
    // session's folder and tool names. A session Longhouse launched is the
    // owner's own doing and always reports.
    let admit_presence = if managed_session_id.is_none() {
        transcript_path
            .as_deref()
            .map(|path| crate::config::import_scope().admits_file(Path::new(path)))
            .unwrap_or(true)
    } else {
        true
    };
    if event == "SessionStart" {
        if let (Some(managed), Some(native)) = (
            managed_session_id.as_deref(),
            provider_session_id.as_deref(),
        ) {
            let _ =
                crate::claude_channel_server::update_managed_provider_session_id(managed, native);
        }
    }
    let child_tool_event = child_origin
        && matches!(
            event.as_str(),
            "PreToolUse" | "PostToolUse" | "PostToolUseFailure"
        );
    let mut payload = json!({
        "session_id": session_id,
        "tool_name": if child_origin { None } else { string(input, "tool_name") },
        "cwd": cwd,
        "provider": "claude",
        "transcript_path": if child_tool_event { None } else { transcript_path },
        "control_path": if managed_session_id.is_some() { "managed" } else { "unmanaged" },
        // This is the hook's observation time, not the daemon's delivery time.
        "occurred_at": occurred_at,
    });
    if matches!(event.as_str(), "CwdChanged" | "ClaudeModState") {
        payload["hook_event_name"] = json!(event);
    }
    // Child-origin lifecycle updates are source evidence only. Never carry an
    // idle/running phase from the child into the parent's activity head.
    if !child_origin {
        payload["state"] = json!(observation.status);
    }
    // Managed launchers carry the exact durable run generation in the
    // environment. Preserve it on the hook event so Runtime Host can bind
    // provider facts to the run without a timestamp or session join.
    if managed_session_id.is_some() {
        if let Some(run_id) = std::env::var("LONGHOUSE_RUN_ID")
            .ok()
            .filter(|value| !value.trim().is_empty())
        {
            payload["run_id"] = json!(run_id);
        }
    }
    // The complete registry is independent of activity and is retained by
    // the daemon even when a later phase observation has no registry.
    if let Some(snapshot) = registry {
        payload["delegation"] = snapshot;
    }
    // Start/stop hooks are partial, source-bound changes. They must not be
    // mistaken for a complete registry or renew unrelated items.
    if let Some(update) = lifecycle_update {
        payload["delegation_update"] = update;
    }
    if let Some(usage) = session_usage {
        payload["session_usage"] = usage;
    }
    if let Some(crons) = session_crons {
        payload["session_crons"] = crons;
    }
    if let Some(mode) = permission_mode {
        payload["permission_mode"] = json!(mode);
    }
    if let Some(branch) = git_branch {
        payload["git_branch"] = json!(branch);
    }
    attach_provider_session_id(
        &mut payload,
        managed_session_id.is_some(),
        provider_session_id.as_deref(),
    );
    if crate::qa_fault::claude_background_writer_disabled()
        && (payload.get("delegation").is_some() || payload.get("delegation_update").is_some())
    {
        crate::qa_fault::record_fired_named(
            "claude_background_writer_disabled",
            &session_id,
            json!({
                "hook_event_name": event,
                "had_registry": payload.get("delegation").is_some(),
                "had_lifecycle_update": payload.get("delegation_update").is_some(),
            }),
        );
        if let Some(fields) = payload.as_object_mut() {
            fields.remove("delegation");
            fields.remove("delegation_update");
        }
    }
    if managed_session_id.is_none() {
        if let Some((provider_pid, provider_process_start_time)) = unmanaged_provider_identity() {
            payload["provider_pid"] = json!(provider_pid);
            payload["provider_process_start_time"] = json!(provider_process_start_time);
        }
    }
    // Registry and lifecycle evidence share the native callback's observation
    // time but remain separate fact families downstream. Keep both fields on
    // the producer payload: the daemon preserves the complete snapshot while
    // reducing the exact child edge independently.
    let home = longhouse_home();
    let observe_result = match home.as_ref() {
        Ok(home) if admit_presence => persist_presence_and_edges(
            home,
            &session_id,
            &event,
            child_origin,
            input,
            &payload,
            &observation,
        ),
        Ok(_) => Ok(()),
        Err(error) => Err(anyhow::anyhow!("{error}")),
    };
    let record_state = crate::warp_cli_agent::has_warp_state_file();
    if warp_enabled || record_state {
        let stopping_agent_id = (event == "SubagentStop")
            .then(|| string(input, "agent_id"))
            .flatten();
        let notification_type = (event == "Notification")
            .then(|| string(input, "notification_type"))
            .flatten();
        if let Some(marker_event) = warp_marker_for_event_at(
            home.as_deref().ok(),
            &session_id,
            &event,
            child_origin,
            stopping_agent_id.as_deref(),
            payload.get("delegation"),
            notification_type.as_deref(),
            turn_control_blocked_stop,
        ) {
            if record_state {
                crate::warp_cli_agent::record_warp_state(
                    "claude",
                    marker_event,
                    &session_id,
                    Path::new(cwd.as_deref().unwrap_or(".")),
                    None,
                );
            }
            if warp_enabled {
                let extra_owned = warp_extra_fields(marker_event, input);
                let extra_refs: Vec<(&str, &str)> = extra_owned
                    .iter()
                    .map(|(key, value)| (*key, value.as_str()))
                    .collect();
                let marker = crate::warp_cli_agent::session_event_marker_with_fields(
                    "claude",
                    marker_event,
                    &session_id,
                    Path::new(cwd.as_deref().unwrap_or(".")),
                    None,
                    &extra_refs,
                );
                let delivered_via_tty = try_tty && crate::warp_cli_agent::write_tty_marker(&marker);
                if !delivered_via_tty {
                    hook_output.insert("terminalSequence".to_string(), Value::String(marker));
                }
            }
        }
    }
    if event == "SessionStart"
        && managed_session_id.is_some()
        && coordination_bootstrap_enabled()
        && !hook_output.contains_key("hookSpecificOutput")
    {
        hook_output.insert(
            "hookSpecificOutput".to_string(),
            json!({
                "hookEventName": "SessionStart",
                "additionalContext": session_start_awareness(),
            }),
        );
    }
    (non_empty_output(hook_output), observe_result)
}

/// Managed sessions report under the Longhouse id, so without this the
/// provider-native id only reaches the server when a transcript ships. Carrying
/// it on every managed presence record lets the server re-bind the alias
/// immediately (e.g. after an out-of-band `claude --resume` rotates the native
/// id). Unmanaged payloads skip it: their `session_id` is already the native id.
fn attach_provider_session_id(
    payload: &mut Value,
    managed: bool,
    provider_session_id: Option<&str>,
) {
    if !managed {
        return;
    }
    if let Some(native) = provider_session_id {
        payload["provider_session_id"] = json!(native);
    }
}

/// Claude executes hooks through a shell, so its direct parent is not reliably
/// the provider. Walk a short parent chain and report only an actual `claude`
/// process; this preserves the engine's PID-reuse protection for Shadow runs.
fn unmanaged_provider_pid() -> Option<u32> {
    let mut pid = unsafe { libc::getppid() } as u32;
    for _ in 0..16 {
        if pid == 0 {
            return None;
        }
        let (command, parent) = crate::process_identity::process_comm_and_parent(pid)?;
        if std::path::Path::new(&command)
            .file_name()
            .and_then(|name| name.to_str())
            == Some("claude")
        {
            return Some(pid);
        }
        pid = parent;
    }
    None
}

/// Capture the provider's raw birth identity while the hook producer still
/// observes the process. If that probe is unavailable, omit the binding claim;
/// a later drain must not invent an identity for a reused PID.
fn unmanaged_provider_identity() -> Option<(u32, String)> {
    let pid = unmanaged_provider_pid()?;
    let crate::process_identity::ProcessFactLookup::Present(fact) =
        crate::process_identity::inspect_process_fact(pid)
    else {
        return None;
    };
    if crate::unmanaged_bindings::is_provider_process(&fact.command) != Some("claude") {
        return None;
    }
    let start_time = fact.lstart.trim();
    (!start_time.is_empty()).then(|| (pid, start_time.to_string()))
}

/// Bounded registry from Claude's parent Stop hook.
///
/// `background_tasks[]` is how Claude distinguishes "session is done" from
/// "session is paused waiting for background work to wake it back up", and it
/// is scoped to the parent session. The server reduces this into the
/// `delegation` fact family — a separate axis, because activity expires in
/// 90-600s while a background agent outlives turns.
///
/// `session_crons[]` is deliberately not folded in here: a scheduled wakeup is
/// future work, not work in flight, and the two are separate signals
/// (`delegation.background` and `delegation.scheduled`).
const DELEGATION_KIND_LIMIT: usize = 8;
const DELEGATION_TASK_LIMIT: usize = 256;
const DELEGATION_ID_MAX_CHARS: usize = 256;
const DELEGATION_STATUS_MAX_CHARS: usize = 32;
const DELEGATION_DESCRIPTION_MAX_CHARS: usize = 256;
const DELEGATION_TOOL_CALL_ID_MAX_CHARS: usize = 256;
const DELEGATION_SIDECAR_MAX_BYTES: u64 = 64 * 1024;

fn bounded_task_text(value: Option<&Value>, limit: usize) -> Option<String> {
    let text = value?.as_str()?.trim();
    if text.is_empty() {
        return None;
    }
    Some(text.chars().take(limit).collect())
}

fn bounded_required_task_text(value: Option<&Value>, limit: usize) -> Option<String> {
    let text = value?.as_str()?.trim();
    if text.is_empty() || text.chars().count() > limit {
        return None;
    }
    Some(text.to_owned())
}

fn task_id(value: Option<&Value>) -> Option<String> {
    let text = value?.as_str()?.trim();
    if text.is_empty() || text.chars().count() > DELEGATION_ID_MAX_CHARS {
        return None;
    }
    Some(text.to_owned())
}

fn delegation_kind(raw: &str) -> &'static str {
    match raw.to_ascii_lowercase().replace([' ', '-'], "_").as_str() {
        "shell" => "shell",
        "subagent" => "subagent",
        "monitor" | "monitor_task" => "monitor",
        "workflow" => "workflow",
        "teammate" => "teammate",
        "cloud_session" => "cloud_session",
        "mcp_task" => "mcp_task",
        _ => "other",
    }
}

fn safe_task_path_component(task_id: &str) -> bool {
    !task_id.is_empty()
        && task_id
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_'))
}

fn bounded_tool_use_id(value: Option<&Value>) -> Option<String> {
    let value = bounded_required_task_text(value, DELEGATION_TOOL_CALL_ID_MAX_CHARS)?;
    (!value.chars().any(char::is_control)).then_some(value)
}

#[derive(Default)]
struct AgentMeta {
    parent_tool_call_id: Option<String>,
    agent_type: Option<String>,
    description: Option<String>,
    model: Option<String>,
    is_fork: Option<bool>,
    parent_agent_id: Option<String>,
    spawn_depth: Option<u64>,
    finished: Option<bool>,
}

/// Read only the exact provider sidecar named by a lifecycle or child tool
/// event (`Stop`, `SubagentStart`, `SubagentStop`, `PreToolUse`,
/// `PostToolUse`, `PostToolUseFailure`, or `ClaudeModState`).
fn read_agent_meta(input: &Value, task_id: &str) -> Option<AgentMeta> {
    if !matches!(
        input.get("hook_event_name").and_then(Value::as_str),
        Some(
            "Stop"
                | "SubagentStart"
                | "SubagentStop"
                | "PreToolUse"
                | "PostToolUse"
                | "PostToolUseFailure"
                | "ClaudeModState"
        )
    ) || !safe_task_path_component(task_id)
    {
        return None;
    }
    let transcript_path = input
        .get("transcript_path")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(Path::new)?;
    if !transcript_path.is_absolute()
        || transcript_path
            .components()
            .any(|component| matches!(component, Component::ParentDir))
        || transcript_path.extension().and_then(|value| value.to_str()) != Some("jsonl")
        || !std::fs::metadata(transcript_path)
            .map(|metadata| metadata.is_file())
            .unwrap_or(false)
    {
        return None;
    }
    let parent_stem = transcript_path.with_extension("");
    let subagents = parent_stem.join("subagents");
    let sidecar = subagents.join(format!("agent-{task_id}.meta.json"));
    let sidecar_metadata = std::fs::metadata(&sidecar).ok()?;
    if !sidecar_metadata.is_file() || sidecar_metadata.len() > DELEGATION_SIDECAR_MAX_BYTES {
        return None;
    }
    let canonical_subagents = std::fs::canonicalize(&subagents).ok()?;
    let canonical_sidecar = std::fs::canonicalize(&sidecar).ok()?;
    if !canonical_sidecar.starts_with(&canonical_subagents) {
        return None;
    }
    let bytes = std::fs::read(sidecar).ok()?;
    let sidecar: Value = serde_json::from_slice(&bytes).ok()?;
    Some(AgentMeta {
        parent_tool_call_id: bounded_tool_use_id(sidecar.get("toolUseId")),
        agent_type: bounded_required_task_text(
            sidecar.get("agentType"),
            DELEGATION_STATUS_MAX_CHARS,
        ),
        description: bounded_task_text(
            sidecar.get("description"),
            DELEGATION_DESCRIPTION_MAX_CHARS,
        ),
        model: bounded_required_task_text(
            sidecar.get("pickedEffortModel"),
            DELEGATION_PROGRESS_TOOL_MAX_CHARS,
        )
        .or_else(|| {
            bounded_required_task_text(sidecar.get("model"), DELEGATION_PROGRESS_TOOL_MAX_CHARS)
        }),
        is_fork: sidecar.get("isFork").and_then(Value::as_bool),
        parent_agent_id: crate::claude_lifecycle_hook::task_id(sidecar.get("parentAgentId")),
        spawn_depth: sidecar.get("spawnDepth").and_then(Value::as_u64),
        finished: sidecar.get("finished").and_then(Value::as_bool),
    })
}

fn parent_tool_call_id(input: &Value, task_id: &str) -> Option<String> {
    read_agent_meta(input, task_id).and_then(|meta| meta.parent_tool_call_id)
}

fn apply_agent_meta(item: &mut Value, meta: &AgentMeta) {
    if item.get("parent_tool_call_id").is_none() {
        if let Some(parent_tool_call_id) = meta.parent_tool_call_id.as_deref() {
            item["parent_tool_call_id"] = json!(parent_tool_call_id);
        }
    }
    if item.get("agent_type").is_none() {
        if let Some(agent_type) = meta.agent_type.as_deref() {
            item["agent_type"] = json!(agent_type);
        }
    }
    if item.get("description").is_none_or(Value::is_null) {
        if let Some(description) = meta.description.as_deref() {
            item["description"] = json!(description);
        }
    }
    if item.get("model").is_none() {
        if let Some(model) = meta.model.as_deref() {
            item["model"] = json!(model);
        }
    }
    if item.get("is_fork").is_none() {
        if let Some(is_fork) = meta.is_fork {
            item["is_fork"] = json!(is_fork);
        }
    }
    if item.get("parent_agent_id").is_none() {
        if let Some(parent_agent_id) = meta.parent_agent_id.as_deref() {
            item["parent_agent_id"] = json!(parent_agent_id);
        }
    }
    if item.get("spawn_depth").is_none() {
        if let Some(spawn_depth) = meta.spawn_depth {
            item["spawn_depth"] = json!(spawn_depth);
        }
    }
}

/// A single lifecycle edge observed between complete parent registry snapshots.
///
/// The provider gives `SubagentStart`/`SubagentStop` an agent identity, but
/// neither event is a complete registry, so these updates carry an explicit
/// operation and source fields; the consumer must not treat a start as a
/// replacement snapshot or refresh an unrelated item.
const DELEGATION_UPDATE_PATH_MAX_CHARS: usize = 1024;
const DELEGATION_PROGRESS_TOOL_MAX_CHARS: usize = 128;

fn warp_deferred_stop_path(home: &Path, session_id: &str) -> PathBuf {
    let digest = Sha256::digest(session_id.as_bytes());
    home.join("agent")
        .join("turn-claims")
        .join(format!("warp-deferred-stop-{digest:x}"))
}

fn remaining_active_subagents(delegation: Option<&Value>, stopping_agent_id: Option<&str>) -> u64 {
    let Some(snapshot) = delegation else {
        return 0;
    };
    if let Some(items) = snapshot.get("items").and_then(Value::as_array) {
        return items
            .iter()
            .filter(|item| item.get("kind").and_then(Value::as_str) == Some("subagent"))
            .filter(|item| {
                stopping_agent_id.map_or(true, |stop_id| {
                    item.get("id").and_then(Value::as_str) != Some(stop_id)
                })
            })
            .count() as u64;
    }
    let count = snapshot
        .get("kinds")
        .and_then(|kinds| kinds.get("subagent"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    if stopping_agent_id.is_some() {
        count.saturating_sub(1)
    } else {
        count
    }
}

#[allow(clippy::too_many_arguments)]
fn warp_marker_for_event_at(
    home: Option<&Path>,
    session_id: &str,
    event: &str,
    child_origin: bool,
    stopping_agent_id: Option<&str>,
    delegation: Option<&Value>,
    notification_type: Option<&str>,
    turn_control_blocked_stop: bool,
) -> Option<&'static str> {
    let deferred_path = home.map(|root| warp_deferred_stop_path(root, session_id));
    let clear_deferred = || {
        if let Some(path) = deferred_path.as_ref() {
            let _ = std::fs::remove_file(path);
        }
    };
    if event == "SubagentStop" {
        if let Some(path) = deferred_path.as_ref() {
            if path.exists() && remaining_active_subagents(delegation, stopping_agent_id) == 0 {
                let _ = std::fs::remove_file(path);
                return Some("stop");
            }
        }
        return None;
    }
    if child_origin {
        return None;
    }
    match event {
        "SessionStart" => {
            clear_deferred();
            Some("session_start")
        }
        "UserPromptSubmit" => {
            clear_deferred();
            Some("prompt_submit")
        }
        "PermissionRequest" => Some("permission_request"),
        "PostToolUse" | "PostToolUseFailure" => Some("tool_complete"),
        "Notification" if notification_type == Some("idle_prompt") => Some("idle_prompt"),
        "SessionEnd" => {
            clear_deferred();
            Some("stop")
        }
        "Stop" => {
            if turn_control_blocked_stop {
                return None;
            }
            if remaining_active_subagents(delegation, None) == 0 {
                clear_deferred();
                Some("stop")
            } else {
                if let Some(path) = deferred_path.as_ref() {
                    if let Some(parent) = path.parent() {
                        let _ = std::fs::create_dir_all(parent);
                    }
                    let _ = std::fs::write(path, b"1");
                }
                None
            }
        }
        _ => None,
    }
}

#[cfg_attr(not(test), allow(dead_code))]
#[allow(clippy::too_many_arguments)]
fn warp_marker_for_event(
    home: &Path,
    session_id: &str,
    event: &str,
    child_origin: bool,
    stopping_agent_id: Option<&str>,
    delegation: Option<&Value>,
    notification_type: Option<&str>,
    turn_control_blocked_stop: bool,
) -> Option<&'static str> {
    warp_marker_for_event_at(
        Some(home),
        session_id,
        event,
        child_origin,
        stopping_agent_id,
        delegation,
        notification_type,
        turn_control_blocked_stop,
    )
}

fn permission_summary(tool_name: Option<&str>, tool_input: Option<&Value>) -> String {
    let tool = tool_name.unwrap_or("tool");
    let preview = tool_input.and_then(|value| {
        ["command", "file_path", "pattern", "url"]
            .into_iter()
            .find_map(|key| {
                value
                    .get(key)
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
            })
    });
    match preview {
        Some(preview) => format!("Wants to run {tool}: {preview}"),
        None => format!("Wants to run {tool}"),
    }
}

fn warp_extra_fields(marker_event: &str, input: &Value) -> Vec<(&'static str, String)> {
    let mut fields = Vec::new();
    match marker_event {
        "prompt_submit" => {
            if let Some(prompt) = string(input, "prompt") {
                fields.push(("query", prompt));
            }
        }
        "tool_complete" => {
            if let Some(tool_name) = string(input, "tool_name") {
                fields.push(("tool_name", tool_name));
            }
        }
        "permission_request" => {
            let tool_name = string(input, "tool_name");
            let summary = permission_summary(tool_name.as_deref(), input.get("tool_input"));
            if let Some(tool_name) = tool_name {
                fields.push(("tool_name", tool_name));
            }
            fields.push(("summary", summary));
        }
        "idle_prompt" => {
            let summary = string(input, "message")
                .unwrap_or_else(|| "Claude is waiting for your input".to_string());
            fields.push(("summary", summary));
        }
        _ => {}
    }
    fields
}

fn safe_single_line_intent(raw: &str) -> Option<String> {
    let trimmed = raw.lines().map(str::trim).find(|line| !line.is_empty())?;
    let cleaned: String = trimmed
        .chars()
        .filter(|ch| !ch.is_control())
        .take(DELEGATION_PROGRESS_TOOL_MAX_CHARS)
        .collect();
    let trimmed_cleaned = cleaned.trim();
    (!trimmed_cleaned.is_empty()).then(|| trimmed_cleaned.to_string())
}

fn extract_tool_intent(tool_name: Option<&str>, tool_input: Option<&Value>) -> Option<String> {
    let input = tool_input?;
    if let Some(desc) = input.get("description").and_then(Value::as_str) {
        if let Some(intent) = safe_single_line_intent(desc) {
            return Some(intent);
        }
    }
    let tool = tool_name?.trim();
    match tool {
        "Read" | "Edit" | "Write" | "MultiEdit" | "NotebookEdit" => {
            let path = input
                .get("file_path")
                .or_else(|| input.get("notebook_path"))
                .and_then(Value::as_str)?;
            let name = Path::new(path)
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or(path);
            safe_single_line_intent(&format!("{tool} {name}"))
        }
        "Grep" | "Glob" => {
            let pattern = input.get("pattern").and_then(Value::as_str)?;
            safe_single_line_intent(&format!("{tool} {pattern}"))
        }
        "WebSearch" => input
            .get("query")
            .and_then(Value::as_str)
            .and_then(safe_single_line_intent),
        _ => None,
    }
}

fn lifecycle_update_for_event(event: &str, input: &Value, observed_at: &str) -> Option<Value> {
    if event == "ClaudeModState" {
        if input.get("background_tasks").is_some() {
            return None;
        }
        let agent_list = input.get("agents").and_then(Value::as_array)?;
        let mut seen_ids = HashSet::new();
        let mut items = Vec::new();
        let mut kinds = std::collections::BTreeMap::new();
        collect_agent_items(
            input,
            agent_list,
            observed_at,
            &mut seen_ids,
            &mut items,
            &mut kinds,
        );
        return Some(json!({
            "operation": "sync_agents",
            "items": items,
            "observed_at": observed_at,
            "source_event": event,
        }));
    }
    if event == "TaskCreated" {
        let id = task_id(input.get("task_id"))?;
        if id.chars().count() > DELEGATION_ID_MAX_CHARS || id.chars().any(char::is_control) {
            return None;
        }
        let kind = if string(input, "teammate_name").is_some() {
            "teammate"
        } else {
            "other"
        };
        let description =
            bounded_task_text(input.get("task_subject"), DELEGATION_DESCRIPTION_MAX_CHARS).or_else(
                || {
                    bounded_task_text(
                        input.get("task_description"),
                        DELEGATION_DESCRIPTION_MAX_CHARS,
                    )
                },
            );
        let item = json!({
            "id": id,
            "kind": kind,
            "status": "running",
            "description": description,
        });
        return Some(json!({
            "operation": "observe",
            "item": item,
            "observed_at": observed_at,
            "source_event": event,
            "membership": "upsert",
        }));
    }
    if event == "TaskCompleted" {
        let id = task_id(input.get("task_id"))?;
        if id.chars().count() > DELEGATION_ID_MAX_CHARS || id.chars().any(char::is_control) {
            return None;
        }
        return Some(json!({
            "operation": "remove",
            "item": {
                "id": id,
                "kind": if string(input, "teammate_name").is_some() { "teammate" } else { "other" },
                "status": "completed",
                "description": Value::Null,
            },
            "observed_at": observed_at,
            "source_event": event,
            "membership": "existing_exact_link_only",
        }));
    }

    let meta = string(input, "agent_id").and_then(|id| read_agent_meta(input, &id));
    let (operation, id, kind, status, native_progress) = match event {
        // A start proves native child identity, not background membership.
        // The consumer may enrich an already-linked item, but must not add a
        // count from this event alone.
        "SubagentStart" => (
            "observe",
            string(input, "agent_id")?,
            "subagent",
            "running",
            None,
        ),
        "SubagentStop" => {
            let id = string(input, "agent_id")?;
            if meta.as_ref().and_then(|m| m.finished) == Some(false) {
                (
                    "observe",
                    id,
                    "subagent",
                    "waiting",
                    Some(json!({
                        "observed_at": observed_at,
                        "status": "waiting",
                    })),
                )
            } else {
                ("remove", id, "subagent", "completed", None)
            }
        }
        "PreToolUse" => {
            let id = string(input, "agent_id")?;
            let tool = bounded_required_task_text(
                input.get("tool_name"),
                DELEGATION_PROGRESS_TOOL_MAX_CHARS,
            );
            let mut progress = json!({
                "observed_at": observed_at,
                "status": "running",
            });
            if let Some(tool) = tool.as_deref() {
                progress["current_tool"] = json!(tool);
            }
            if let Some(intent) = extract_tool_intent(tool.as_deref(), input.get("tool_input")) {
                progress["last_intent"] = json!(intent);
            }
            ("observe", id, "subagent", "running", Some(progress))
        }
        "PostToolUse" | "PostToolUseFailure" => (
            "observe",
            string(input, "agent_id")?,
            "subagent",
            "running",
            Some(json!({
                "observed_at": observed_at,
                "status": "thinking",
            })),
        ),
        _ => return None,
    };
    if id.chars().count() > DELEGATION_ID_MAX_CHARS || id.chars().any(char::is_control) {
        return None;
    }

    let mut item = json!({
        "id": id,
        "kind": kind,
        "status": status,
        "description": Value::Null,
    });
    if kind == "subagent" {
        if let Some(agent_type) =
            bounded_required_task_text(input.get("agent_type"), DELEGATION_STATUS_MAX_CHARS)
        {
            item["agent_type"] = json!(agent_type);
        }
        if let Some(ref meta) = meta {
            apply_agent_meta(&mut item, meta);
        }
        if let Some(progress) = native_progress {
            item["native_progress"] = progress;
        }
    }

    let mut update = json!({
        "operation": operation,
        "item": item,
        "observed_at": observed_at,
        "source_event": event,
        "membership": "existing_exact_link_only",
    });
    let source = update.as_object_mut()?;
    if let Some(agent_id) = string(input, "agent_id") {
        source.insert("source_agent_id".to_string(), json!(agent_id));
    }
    if let Some(agent_type) =
        bounded_required_task_text(input.get("agent_type"), DELEGATION_STATUS_MAX_CHARS)
    {
        source.insert("source_agent_type".to_string(), json!(agent_type));
    }
    if let Some(path) = bounded_required_task_text(
        input.get("agent_transcript_path"),
        DELEGATION_UPDATE_PATH_MAX_CHARS,
    ) {
        if !path.chars().any(char::is_control) {
            source.insert("source_agent_transcript_path".to_string(), json!(path));
        }
    }
    Some(update)
}

fn empty_delegation_snapshot(observed_at: &str) -> Value {
    json!({
        "count": 0,
        "kinds": {},
        "items": [],
        "observed_at": observed_at,
    })
}

fn monitor_ledger_path(home: &Path, session_id: &str) -> PathBuf {
    let digest = Sha256::digest(session_id.as_bytes());
    home.join("agent")
        .join("monitors")
        .join(format!("{digest:x}.json"))
}

fn record_monitor_invocation(home: &Path, session_id: &str, input: &Value) {
    let Some(tool_input) = input.get("tool_input") else {
        return;
    };
    let description = bounded_task_text(
        tool_input.get("description"),
        DELEGATION_DESCRIPTION_MAX_CHARS,
    )
    .or_else(|| bounded_task_text(tool_input.get("name"), DELEGATION_DESCRIPTION_MAX_CHARS));
    let Some(description) = description else {
        return;
    };
    let ledger_path = monitor_ledger_path(home, session_id);
    if let Some(parent) = ledger_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let mut entries: Vec<String> = std::fs::read(&ledger_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default();
    if !entries.iter().any(|existing| existing == &description) {
        entries.push(description);
        if entries.len() > 32 {
            let overflow = entries.len() - 32;
            entries.drain(0..overflow);
        }
        if let Ok(bytes) = serde_json::to_vec(&entries) {
            let _ = std::fs::write(&ledger_path, bytes);
        }
    }
}

fn load_monitor_descriptions(home: &Path, session_id: &str) -> HashSet<String> {
    let ledger_path = monitor_ledger_path(home, session_id);
    std::fs::read(ledger_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Vec<String>>(&bytes).ok())
        .unwrap_or_default()
        .into_iter()
        .collect()
}

fn clear_monitor_ledger(home: &Path, session_id: &str) {
    let _ = std::fs::remove_file(monitor_ledger_path(home, session_id));
}

fn normalize_agent_status(raw: &str) -> &'static str {
    match raw.trim().to_ascii_lowercase().as_str() {
        "waiting" | "idle" | "paused" => "waiting",
        "queued" | "pending" | "starting" => "queued",
        "completed" | "stopped" | "failed" | "killed" | "finished" | "cancelled" | "canceled" => {
            "completed"
        }
        _ => "running",
    }
}

fn extract_agent_progress(agent: &Value, status: &str, observed_at: &str) -> Option<Value> {
    let tool_count = agent
        .get("toolCount")
        .or_else(|| agent.get("tool_count"))
        .and_then(Value::as_u64);
    let context_tokens = agent
        .get("contextTokens")
        .or_else(|| agent.get("context_tokens"))
        .and_then(Value::as_u64);
    let tokens = agent.get("tokens").and_then(Value::as_u64);
    let duration_ms = agent
        .get("durationMs")
        .or_else(|| agent.get("duration_ms"))
        .and_then(Value::as_u64);
    let last_intent = agent
        .get("lastIntent")
        .or_else(|| agent.get("last_intent"))
        .and_then(Value::as_str)
        .and_then(safe_single_line_intent);
    let current_tool = agent
        .get("currentTool")
        .or_else(|| agent.get("current_tool"))
        .and_then(|value| {
            bounded_required_task_text(Some(value), DELEGATION_PROGRESS_TOOL_MAX_CHARS)
        });
    if tool_count.is_none()
        && context_tokens.is_none()
        && tokens.is_none()
        && duration_ms.is_none()
        && last_intent.is_none()
        && current_tool.is_none()
        && status != "waiting"
    {
        return None;
    }
    let mut progress = json!({
        "observed_at": observed_at,
        "status": status,
    });
    if let Some(count) = tool_count {
        progress["tool_count"] = json!(count);
    }
    if let Some(ct) = context_tokens {
        progress["context_tokens"] = json!(ct);
    }
    if let Some(tok) = tokens {
        progress["tokens"] = json!(tok);
    }
    if let Some(dur) = duration_ms {
        progress["duration_ms"] = json!(dur);
    }
    if let Some(intent) = last_intent {
        progress["last_intent"] = json!(intent);
    }
    if let Some(tool) = current_tool {
        progress["current_tool"] = json!(tool);
    }
    Some(progress)
}

fn collect_agent_items(
    input: &Value,
    agent_list: &[Value],
    observed_at: &str,
    seen_ids: &mut HashSet<String>,
    items: &mut Vec<Value>,
    kinds: &mut std::collections::BTreeMap<String, u64>,
) {
    for agent in agent_list {
        if items.len() >= DELEGATION_TASK_LIMIT {
            break;
        }
        let Some(id) = task_id(agent.get("id")) else {
            continue;
        };
        let raw_status = agent
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("running");
        let meta = read_agent_meta(input, &id);
        let normalized_raw = raw_status.trim().to_ascii_lowercase();
        let mut status = normalize_agent_status(raw_status);
        if status == "completed" {
            let explicit_terminal = matches!(
                normalized_raw.as_str(),
                "failed" | "killed" | "stopped" | "cancelled" | "canceled"
            );
            if !explicit_terminal && meta.as_ref().and_then(|m| m.finished) == Some(false) {
                status = "waiting";
            } else {
                continue;
            }
        }
        if !seen_ids.insert(id.clone()) {
            continue;
        }
        let agent_type = bounded_required_task_text(agent.get("type"), DELEGATION_STATUS_MAX_CHARS);
        let is_teammate = agent
            .get("teammateId")
            .and_then(Value::as_str)
            .is_some_and(|v| !v.trim().is_empty())
            || agent_type
                .as_deref()
                .is_some_and(|t| t.eq_ignore_ascii_case("teammate"));
        let kind = if is_teammate { "teammate" } else { "subagent" };
        *kinds.entry(kind.to_string()).or_insert(0) += 1;
        let description =
            bounded_task_text(agent.get("description"), DELEGATION_DESCRIPTION_MAX_CHARS)
                .or_else(|| bounded_task_text(agent.get("name"), DELEGATION_DESCRIPTION_MAX_CHARS));
        let mut item = json!({
            "id": id,
            "kind": kind,
            "status": status,
            "description": description,
        });
        if let Some(ref at) = agent_type {
            item["agent_type"] = json!(at);
        }
        if let Some(parent_id) =
            task_id(agent.get("parentId")).or_else(|| task_id(agent.get("spawnedBy")))
        {
            item["parent_agent_id"] = json!(parent_id);
        }
        if let Some(ref meta) = meta {
            apply_agent_meta(&mut item, meta);
        }
        if let Some(progress) = extract_agent_progress(agent, status, observed_at) {
            item["native_progress"] = progress;
        }
        items.push(item);
    }
}

fn delegation_snapshot(input: &Value, observed_at: &str) -> Option<Value> {
    delegation_snapshot_with_monitors(input, observed_at, None)
}

fn delegation_snapshot_with_monitors(
    input: &Value,
    observed_at: &str,
    monitor_descriptions: Option<&HashSet<String>>,
) -> Option<Value> {
    let aborted = input.get("aborted").and_then(Value::as_bool) == Some(true);
    let background_tasks = match input.get("background_tasks") {
        Some(value) => Some(value.as_array()?),
        None => None,
    };
    let agents = if aborted {
        None
    } else {
        input.get("agents").and_then(Value::as_array)
    };
    if background_tasks.is_none() && agents.is_none() {
        return aborted.then(|| empty_delegation_snapshot(observed_at));
    }

    let mut seen_ids = HashSet::new();
    let mut items = Vec::new();
    let mut kinds: std::collections::BTreeMap<String, u64> = std::collections::BTreeMap::new();

    if let Some(tasks) = background_tasks {
        if tasks.len() > DELEGATION_TASK_LIMIT {
            return None;
        }
        for task in tasks {
            // A nonempty registry is authoritative only when every entry needed
            // to interpret it is well formed. Do not turn a malformed list into
            // an authoritative empty/partial snapshot.
            let Some(id) = task_id(task.get("id")) else {
                return None;
            };
            let Some(status) =
                bounded_required_task_text(task.get("status"), DELEGATION_STATUS_MAX_CHARS)
            else {
                return None;
            };
            let Some(raw_kind) = task
                .get("type")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
            else {
                return None;
            };
            if raw_kind.chars().count() > DELEGATION_STATUS_MAX_CHARS {
                return None;
            }
            if !seen_ids.insert(id.clone()) {
                continue;
            }

            // `description` and `name` are provider labels. Deliberately do not
            // inspect `command`, which may contain credentials or user data.
            let description =
                bounded_task_text(task.get("description"), DELEGATION_DESCRIPTION_MAX_CHARS)
                    .or_else(|| {
                        bounded_task_text(task.get("name"), DELEGATION_DESCRIPTION_MAX_CHARS)
                    });
            let mut kind = delegation_kind(raw_kind);
            if kind == "shell" {
                if let (Some(monitors), Some(desc)) = (monitor_descriptions, description.as_deref())
                {
                    if monitors.contains(desc) {
                        kind = "monitor";
                    }
                }
            }
            *kinds.entry(kind.to_string()).or_insert(0) += 1;
            let mut item = json!({
                "id": id,
                "kind": kind,
                "status": status,
                "description": description,
            });
            if let Some(agent_type) =
                bounded_required_task_text(task.get("agent_type"), DELEGATION_STATUS_MAX_CHARS)
            {
                item["agent_type"] = json!(agent_type);
            }
            if kind == "subagent" {
                if let Some(meta) = read_agent_meta(input, &id) {
                    apply_agent_meta(&mut item, &meta);
                }
            }
            items.push(item);
        }
    }

    if let Some(agent_list) = agents {
        collect_agent_items(
            input,
            agent_list,
            observed_at,
            &mut seen_ids,
            &mut items,
            &mut kinds,
        );
    }

    Some(json!({
        "count": items.len(),
        "kinds": kinds.into_iter().take(DELEGATION_KIND_LIMIT).collect::<std::collections::BTreeMap<_, _>>(),
        "items": items,
        "observed_at": observed_at,
    }))
}

fn extract_session_usage(input: &Value) -> Option<Value> {
    let usage = input.get("session_usage").and_then(Value::as_object)?;
    let mut out = serde_json::Map::new();
    if let Some(context) = usage.get("context").and_then(Value::as_object) {
        let mut ctx = serde_json::Map::new();
        if let Some(tokens) = context.get("tokens").and_then(Value::as_u64) {
            ctx.insert("tokens".to_string(), json!(tokens));
        }
        if let Some(window) = context.get("window").and_then(Value::as_u64) {
            ctx.insert("window".to_string(), json!(window));
        }
        if let Some(percent) = context.get("percent").and_then(Value::as_f64) {
            ctx.insert("percent".to_string(), json!(percent));
        }
        if !ctx.is_empty() {
            out.insert("context".to_string(), Value::Object(ctx));
        }
    }
    if let Some(cost_usd) = usage
        .get("cost")
        .and_then(|c| c.get("usd"))
        .and_then(Value::as_f64)
        .or_else(|| usage.get("cost_usd").and_then(Value::as_f64))
    {
        out.insert("cost_usd".to_string(), json!(cost_usd));
    }
    if let Some(started_at) = usage
        .get("startedAt")
        .or_else(|| usage.get("started_at"))
        .and_then(Value::as_u64)
    {
        out.insert("started_at".to_string(), json!(started_at));
    }
    if let Some(rate_limits) = usage
        .get("rateLimits")
        .or_else(|| usage.get("rate_limits"))
        .and_then(Value::as_array)
    {
        let bounded: Vec<Value> = rate_limits
            .iter()
            .take(8)
            .filter_map(|item| {
                let kind = bounded_required_task_text(item.get("kind"), 64)?;
                let percent = item
                    .get("percentUsed")
                    .or_else(|| item.get("percent_used"))
                    .and_then(Value::as_f64)?;
                let mut entry = json!({ "kind": kind, "percent_used": percent });
                if let Some(resets) = item
                    .get("resetsAt")
                    .or_else(|| item.get("resets_at"))
                    .and_then(|v| bounded_required_task_text(Some(v), 64))
                {
                    entry["resets_at"] = json!(resets);
                }
                Some(entry)
            })
            .collect();
        if !bounded.is_empty() {
            out.insert("rate_limits".to_string(), Value::Array(bounded));
        }
    }
    (!out.is_empty()).then_some(Value::Object(out))
}

fn extract_session_crons(input: &Value) -> Option<Value> {
    let crons = input.get("session_crons").and_then(Value::as_array)?;
    let bounded: Vec<Value> = crons
        .iter()
        .take(32)
        .filter_map(|cron| {
            let id = bounded_required_task_text(cron.get("id"), DELEGATION_ID_MAX_CHARS)?;
            let schedule = bounded_required_task_text(cron.get("schedule"), 64)?;
            let recurring = cron
                .get("recurring")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let mut item = json!({
                "id": id,
                "schedule": schedule,
                "recurring": recurring,
            });
            if let Some(prompt) =
                bounded_task_text(cron.get("prompt"), DELEGATION_DESCRIPTION_MAX_CHARS)
            {
                item["prompt"] = json!(prompt);
            }
            Some(item)
        })
        .collect();
    Some(Value::Array(bounded))
}

fn git_head_branch(cwd: Option<&str>) -> Option<String> {
    let mut dir = Path::new(cwd?).to_path_buf();
    for _ in 0..8 {
        let dot_git = dir.join(".git");
        let head_path = if dot_git.is_file() {
            let content = std::fs::read_to_string(&dot_git).ok()?;
            let gitdir = content.strip_prefix("gitdir:")?.trim();
            let resolved = if Path::new(gitdir).is_absolute() {
                PathBuf::from(gitdir)
            } else {
                dir.join(gitdir)
            };
            resolved.join("HEAD")
        } else if dot_git.is_dir() {
            dot_git.join("HEAD")
        } else {
            if !dir.pop() {
                return None;
            }
            continue;
        };
        let head = std::fs::read_to_string(head_path).ok()?;
        let line = head.trim();
        if let Some(branch) = line.strip_prefix("ref: refs/heads/") {
            let branch = branch.trim();
            if !branch.is_empty()
                && branch.chars().count() <= 128
                && !branch.chars().any(char::is_control)
            {
                return Some(branch.to_string());
            }
        }
        return None;
    }
    None
}

fn string(input: &Value, key: &str) -> Option<String> {
    input
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

/// Claude's structured question tool.
///
/// `schemas/managed_providers.yml` owns each provider's `pause_tool_name`; this
/// restates Claude's because this file is the only place that decides which
/// hook event is a question, and the engine does not read that schema.
const PAUSE_TOOL_NAME: &str = "AskUserQuestion";

const PAUSE_SOURCE: &str = "claude_hook";

/// A durable interaction edge: a wait the *user* owes, or the retirement of one.
///
/// This is deliberately separate from `status`. A phase is replaceable evidence
/// about what the provider is doing; an interaction is a semantic record with an
/// owner and a lifetime. Collapsing them is what produced a session headlined
/// "Blocked" with nothing behind it, so the hook now states the obligation
/// itself instead of leaving a client to infer one from the last phase.
///
/// Claude's hooks reference settles the keys this needs: `PreToolUse` and
/// `PostToolUse` carry `tool_use_id`, and `PermissionRequest` explicitly does
/// *not*. So a question is keyed by the tool call, and an approval — which has
/// no id anywhere in its own payload — is keyed by a coordinate that both the
/// request and the resolving tool event can compute without stored state.
#[derive(Debug, PartialEq)]
enum InteractionEdge {
    OpenQuestion {
        tool_use_id: String,
        request_payload: Value,
    },
    ResolveQuestion {
        tool_use_id: String,
    },
    OpenPermission {
        request_key: String,
        tool_name: String,
        request_payload: Value,
    },
    ResolvePermission {
        request_key: String,
        tool_name: String,
    },
}

/// One hook event's replaceable status and its optional interaction edge.
///
/// `status: None` is not "nothing happened" — `PermissionRequest` states an
/// obligation without stating a phase, and the notification types that carry no
/// tool identity state neither.
struct HookObservation {
    status: Option<&'static str>,
    edge: Option<InteractionEdge>,
}

fn observation_for_event(event: &str, input: &Value) -> HookObservation {
    let tool_name = string(input, "tool_name");
    let tool_use_id = string(input, "tool_use_id");
    let tool_input = input.get("tool_input").cloned().unwrap_or(Value::Null);
    let is_pause_tool = tool_name.as_deref() == Some(PAUSE_TOOL_NAME);
    match event {
        "SessionStart" | "Stop" => HookObservation {
            status: Some("idle"),
            edge: None,
        },
        "UserPromptSubmit" => HookObservation {
            status: Some("thinking"),
            edge: None,
        },
        "PreToolUse" => HookObservation {
            status: Some("running"),
            edge: match (is_pause_tool, tool_use_id) {
                (true, Some(tool_use_id)) => Some(InteractionEdge::OpenQuestion {
                    tool_use_id,
                    request_payload: tool_input,
                }),
                _ => None,
            },
        },
        "PostToolUse" | "PostToolUseFailure" => {
            // A tool event is the only place a wait is provably over: it names
            // the call for a question, and repeats the tool and its input for an
            // approval whose own payload never carried an id.
            //
            // The arms are exclusive, and ordered by what the event actually
            // proves. `PostToolUse` carries `tool_use_id` for *every* tool, so
            // keying on the id first would emit a question resolution for a
            // `Bash` call: the id matches no question, while the approval that
            // `PermissionRequest` opened for that same call went unretired and
            // stayed pending after the user had answered it. Only the pause
            // tool's own completion resolves a question.
            let edge = if is_pause_tool {
                tool_use_id
                    .clone()
                    .map(|tool_use_id| InteractionEdge::ResolveQuestion { tool_use_id })
            } else {
                tool_name.clone().and_then(|tool_name| {
                    if tool_input.is_null() {
                        None
                    } else {
                        Some(InteractionEdge::ResolvePermission {
                            request_key: permission_request_key(&tool_name, &tool_input),
                            tool_name,
                        })
                    }
                })
            };
            HookObservation {
                status: Some("thinking"),
                edge,
            }
        }
        "PermissionRequest" => HookObservation {
            // No phase. "Blocked" was this hook asserting that the user owes
            // something, on a signal whose own payload carries no tool identity
            // to key it by; the request is the obligation, and the badge reads
            // the obligation rather than the phase.
            status: None,
            edge: match tool_name {
                Some(tool_name) if !is_pause_tool && !tool_input.is_null() => {
                    Some(InteractionEdge::OpenPermission {
                        request_key: permission_request_key(&tool_name, &tool_input),
                        tool_name,
                        request_payload: tool_input,
                    })
                }
                // A question opens at its own PreToolUse, keyed by the id that
                // event carries. Opening a second, id-less claim for the same
                // wait is how one dialog becomes two obligations.
                _ => None,
            },
        },
        "Notification" => match string(input, "notification_type").as_deref() {
            // Claude is back at its input prompt, so no dialog is up. This is
            // quiescence, not a claim about the user: the old mapping to
            // `needs_user` made "Claude has been idle for a minute" read as an
            // open question for the next ten.
            Some("idle_prompt") | Some("elicitation_dialog") => HookObservation {
                status: Some("idle"),
                edge: None,
            },
            // Repeats an obligation already opened by `PermissionRequest`, and
            // carries no tool identity of its own.
            _ => HookObservation {
                status: None,
                edge: None,
            },
        },
        _ => HookObservation {
            status: None,
            edge: None,
        },
    }
}

/// The coordinate an approval is held under.
///
/// `PermissionRequest` carries `tool_name` and `tool_input` and no id, and the
/// `PostToolUse`/`PostToolUseFailure` that ends the wait carries both as well,
/// so hashing the input gives both ends the same key without the hook holding
/// state. Two identical tool calls prompting at the same moment collapse to one
/// key; that is strictly narrower than today, where neither is keyed at all, and
/// the server's single-active and last-seen ordering already pick a winner.
fn permission_request_key(tool_name: &str, tool_input: &Value) -> String {
    let canonical = serde_json::to_vec(tool_input).unwrap_or_default();
    format!(
        "claude-hook:permission:{tool_name}:{:x}",
        Sha256::digest(&canonical)
    )
}

/// Build the runtime event that opens or retires one interaction.
///
/// It rides the durable runtime-events lane the Codex bridge's `pause_request`
/// already uses, which is what makes a resolution survive a dropped hook, a
/// killed process, or a restart: replaceable status may be lost, an obligation
/// may not.
///
/// `answerable` says Longhouse's Claude Code mod is racing this question's
/// dialog against an answer from the app, so the app may answer it; otherwise
/// only the terminal can.
fn interaction_runtime_event(session_id: &str, edge: &InteractionEdge, answerable: bool) -> Value {
    let runtime_key = format!("claude:{session_id}");
    let occurred_at = chrono::Utc::now().to_rfc3339();
    let (kind, tool_name, payload) = match edge {
        InteractionEdge::OpenQuestion {
            tool_use_id,
            request_payload,
        } => (
            "pause_request",
            PAUSE_TOOL_NAME.to_string(),
            json!({
                "provider_request_id": tool_use_id,
                "provider_ref": {
                    "source": PAUSE_SOURCE,
                    "reply_transport": if answerable { "managed_push" } else { "terminal" },
                },
                // The Runtime Host's word for a structured question; any other
                // kind is dropped from the session's pending interaction.
                "kind": "structured_question",
                "title": "Claude needs an answer",
                "summary": if answerable {
                    "Answer here or in the terminal."
                } else {
                    "Answer this in the original terminal."
                },
                "request_payload": request_payload,
                // Without the mod the dialog lives only in the provider's
                // terminal: Longhouse surfaces it but cannot answer it.
                "can_respond": answerable,
                "single_active": true,
            }),
        ),
        InteractionEdge::ResolveQuestion { tool_use_id } => (
            "pause_resolution",
            PAUSE_TOOL_NAME.to_string(),
            json!({
                "provider_request_id": tool_use_id,
                "status": "resolved",
                "response_text": "Answered.",
            }),
        ),
        InteractionEdge::OpenPermission {
            request_key,
            tool_name,
            request_payload,
        } => (
            "pause_request",
            tool_name.clone(),
            json!({
                "request_key": request_key,
                "provider_ref": {"source": PAUSE_SOURCE, "reply_transport": "terminal"},
                "kind": "permission",
                "title": "Claude needs permission",
                "summary": format!("Approve or deny {tool_name} in the original terminal."),
                "request_payload": request_payload,
                "can_respond": false,
                "single_active": true,
            }),
        ),
        InteractionEdge::ResolvePermission {
            request_key,
            tool_name,
        } => (
            "pause_resolution",
            tool_name.clone(),
            json!({
                "request_key": request_key,
                "status": "resolved",
                "response_text": "Decided in the original terminal.",
            }),
        ),
    };
    // A request is deduped on its own identity so a re-delivery cannot open a
    // second obligation. A resolution is an event, not a state, and carries a
    // nonce for the same reason the Codex bridge's does.
    let dedupe_key = match edge {
        InteractionEdge::OpenQuestion { tool_use_id, .. } => {
            format!("claude-hook:open-question:{session_id}:{tool_use_id}")
        }
        InteractionEdge::OpenPermission { request_key, .. } => {
            format!("claude-hook:open-permission:{session_id}:{request_key}")
        }
        _ => format!("claude-hook:{kind}:{session_id}:{}", uuid::Uuid::new_v4()),
    };
    json!({
        "runtime_key": runtime_key,
        "session_id": session_id,
        "provider": "claude",
        "source": PAUSE_SOURCE,
        "kind": kind,
        "phase": Value::Null,
        "tool_name": tool_name,
        "occurred_at": occurred_at,
        "dedupe_key": dedupe_key,
        "payload": payload,
    })
}

fn enqueue_interaction_edge(session_id: &str, edge: &InteractionEdge) -> anyhow::Result<()> {
    let answerable = match edge {
        InteractionEdge::OpenQuestion { tool_use_id, .. } => {
            crate::claude_channel_control::open_question(session_id, None)
                .ok()
                .flatten()
                .is_some_and(|open| &open.tool_use_id == tool_use_id)
        }
        _ => false,
    };
    let event = interaction_runtime_event(session_id, edge, answerable);
    crate::outbox::enqueue_runtime_event(
        &crate::config::get_agent_runtime_events_outbox_dir()?,
        &event,
    )?;
    Ok(())
}

/// Where a resolution that came from the transcript, not the hook, says it came
/// from. The two are distinguishable on purpose: the hook is the authority, and
/// a reader deciding whether a wait was closed by the provider's callback or by
/// the transcript catching up should not have to guess.
const TRANSCRIPT_SOURCE: &str = "claude_transcript";

/// Question calls a shipped transcript has shown but not yet closed.
///
/// The hook can miss: an Esc, a dropped write, a killed process, a machine that
/// never registered the hook at all. The transcript carries the same outcome —
/// the tool result names the call the wait was keyed by — so the shipping path
/// closes the wait there rather than leaving a badge claiming an answer the
/// user has already given.
///
/// Only a *question* is tracked, and only between its own two events. A result
/// cannot say which tool it belongs to, and a question's call and its result are
/// by definition in different batches: the user is answering in between. So the
/// id is remembered when the call ships and dropped when the result does. What
/// is never closed is bounded by the questions one session asked, and a restart
/// loses it — which costs one backstop, never a wrong state.
static OPEN_QUESTION_CALLS: LazyLock<Mutex<HashMap<String, HashSet<String>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// The resolutions a shipped batch proves, for the caller to enqueue.
///
/// Kept out of `enqueue_interaction_edge` because a hook resolution is an event
/// (a nonce) and this one is a statement about a named call: the watcher
/// re-reads ranges, so the same resolution must restate under the same key
/// instead of arriving as a second one.
pub(crate) fn transcript_resolutions_for_events(
    session_id: &str,
    events: &[ParsedEvent],
) -> Vec<Value> {
    let mut resolutions = Vec::new();
    let Ok(mut open) = OPEN_QUESTION_CALLS.lock() else {
        return resolutions;
    };
    let session_open = open.entry(session_id.to_string()).or_default();
    for event in events {
        let Some(tool_call_id) = event.tool_call_id.as_deref() else {
            continue;
        };
        match event.role {
            Role::Assistant if event.tool_name.as_deref() == Some(PAUSE_TOOL_NAME) => {
                session_open.insert(tool_call_id.to_string());
            }
            Role::Tool if session_open.remove(tool_call_id) => {
                resolutions.push(transcript_resolution_event(
                    session_id,
                    tool_call_id,
                    event.timestamp,
                ));
            }
            _ => {}
        }
    }
    if session_open.is_empty() {
        open.remove(session_id);
    }
    resolutions
}

/// The resolution a transcript tool end proves, in the shape the hook's rides.
fn transcript_resolution_event(
    session_id: &str,
    tool_use_id: &str,
    occurred_at: DateTime<Utc>,
) -> Value {
    json!({
        "runtime_key": format!("claude:{session_id}"),
        "session_id": session_id,
        "provider": "claude",
        "source": TRANSCRIPT_SOURCE,
        "kind": "pause_resolution",
        "phase": Value::Null,
        "tool_name": PAUSE_TOOL_NAME,
        "occurred_at": occurred_at.to_rfc3339(),
        "dedupe_key": format!("claude-transcript:resolve:{session_id}:{tool_use_id}"),
        "payload": {
            "provider_request_id": tool_use_id,
            "status": "resolved",
            "response_text": "Answered.",
        },
    })
}

fn longhouse_home() -> anyhow::Result<PathBuf> {
    if let Some(home) = std::env::var_os("LONGHOUSE_HOME") {
        return Ok(PathBuf::from(home));
    }
    Ok(PathBuf::from(std::env::var("HOME")?).join(".longhouse"))
}

/// The session-start awareness note, from the coordination contract
/// (`schemas/coordination_contract.yml`, rendered by the generator) so the
/// engine hook and the shipper-installed hook cannot say different things.
fn session_start_awareness() -> String {
    static NOTE: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    NOTE.get_or_init(|| {
        let contract: serde_json::Value =
            serde_json::from_str(include_str!("coordination_contract.generated.json"))
                .expect("the generated coordination contract is valid JSON");
        contract["session_start"]
            .as_str()
            .expect("the coordination contract carries session_start")
            .to_string()
    })
    .clone()
}

fn coordination_bootstrap_enabled() -> bool {
    !matches!(
        std::env::var("LONGHOUSE_COORDINATION_BOOTSTRAP")
            .unwrap_or_else(|_| "1".into())
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "0" | "false" | "no" | "off"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The engine hook and the shipper hook share one awareness note: the
    /// coordination contract's session_start, byte for byte.
    #[test]
    fn session_start_awareness_is_the_contract_text() {
        let contract: serde_json::Value =
            serde_json::from_str(include_str!("coordination_contract.generated.json")).unwrap();
        assert_eq!(
            session_start_awareness(),
            contract["session_start"].as_str().unwrap()
        );
        assert!(session_start_awareness().contains("`peers`"));
    }

    /// Serialized because these mutate process-wide environment.
    // Poison-tolerant on purpose: every mutation under this lock is made through an
    // RAII guard whose Drop restores the previous value, and Drop runs while unwinding.
    // So a panicking test leaves the environment clean, and the poison flag carries no
    // information -- it only converts one real failure into a wall of PoisonError noise
    // from every other test that shares the lock.

    fn with_provider<T>(value: Option<&str>, body: impl FnOnce() -> T) -> T {
        let _guard = crate::console_adapter::agent_state_guard();
        let previous = std::env::var_os("LONGHOUSE_MANAGED_PROVIDER");
        match value {
            Some(value) => std::env::set_var("LONGHOUSE_MANAGED_PROVIDER", value),
            None => std::env::remove_var("LONGHOUSE_MANAGED_PROVIDER"),
        }
        let result = body();
        match previous {
            Some(value) => std::env::set_var("LONGHOUSE_MANAGED_PROVIDER", value),
            None => std::env::remove_var("LONGHOUSE_MANAGED_PROVIDER"),
        }
        result
    }

    fn retained_native_lifecycle_rows() -> Vec<Value> {
        include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/claude_hooks/lifecycle_native.stdin.jsonl"
        ))
        .lines()
        .map(|line| serde_json::from_str(line).expect("native lifecycle fixture JSON"))
        .collect()
    }

    #[test]
    fn a_claude_run_inside_a_managed_codex_session_is_not_that_session() {
        // The exact impersonation: `claude` launched from inside managed Codex
        // inherits LONGHOUSE_MANAGED_SESSION_ID and, before this check, bound
        // its transcripts to the Codex session. The Runtime Host then refused
        // every upload because the envelope claimed provider=claude for a
        // session it knows is provider=codex.
        assert!(
            !with_provider(Some("codex"), managed_claim_belongs_to_claude),
            "a Codex-owned managed claim must not be adopted by Claude"
        );
        assert!(with_provider(
            Some("claude"),
            managed_claim_belongs_to_claude
        ));
        assert!(with_provider(
            Some("CLAUDE"),
            managed_claim_belongs_to_claude
        ));
    }

    #[test]
    fn an_untagged_claim_is_still_honoured() {
        // Absence means "launcher older than the tag", not "not mine". Treating
        // it as a mismatch would silently unmanage live sessions on upgrade.
        assert!(with_provider(None, managed_claim_belongs_to_claude));
        assert!(with_provider(Some("  "), managed_claim_belongs_to_claude));
    }

    #[test]
    fn maps_claude_events_without_guessing_unknown_notifications() {
        assert_eq!(
            observation_for_event("PreToolUse", &json!({})).status,
            Some("running")
        );
        assert_eq!(
            observation_for_event("Notification", &json!({"notification_type":"idle_prompt"}))
                .status,
            Some("idle")
        );
        assert_eq!(
            observation_for_event("Notification", &json!({"notification_type":"other"})).status,
            None
        );
    }

    #[test]
    fn a_permission_request_states_an_obligation_and_no_phase() {
        // `PermissionRequest` carries no `tool_use_id`, and that absence is
        // exactly why it must not become a phase: an id-less "blocked" activity
        // was rendered as a session-wide attention claim with nothing to key it
        // by, which is the defect this mapping removes.
        let observation = observation_for_event(
            "PermissionRequest",
            &json!({"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}),
        );
        assert_eq!(observation.status, None);
        match observation.edge {
            Some(InteractionEdge::OpenPermission {
                request_key,
                tool_name,
                ..
            }) => {
                assert_eq!(tool_name, "Bash");
                assert!(
                    request_key.starts_with("claude-hook:permission:Bash:"),
                    "{request_key}"
                );
            }
            other => panic!("expected an approval edge, got {other:?}"),
        }
    }

    #[test]
    fn a_question_opens_at_pretooluse_keyed_by_the_tool_call() {
        let observation = observation_for_event(
            "PreToolUse",
            &json!({"tool_name": "AskUserQuestion", "tool_use_id": "toolu_01abc"}),
        );
        assert_eq!(observation.status, Some("running"));
        assert_eq!(
            observation.edge,
            Some(InteractionEdge::OpenQuestion {
                tool_use_id: "toolu_01abc".to_string(),
                request_payload: Value::Null,
            })
        );
    }

    #[test]
    fn a_question_does_not_also_open_an_approval() {
        // Claude may ask for a permission decision on the question tool itself.
        // Opening a second, id-less claim for the same dialog is how one wait
        // becomes two obligations and neither resolution matches.
        let observation = observation_for_event(
            "PermissionRequest",
            &json!({"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}),
        );
        assert_eq!(observation.status, None);
        assert_eq!(observation.edge, None);
    }

    #[test]
    fn a_tool_end_retires_both_a_question_and_an_approval() {
        let question = observation_for_event(
            "PostToolUse",
            &json!({"tool_name": "AskUserQuestion", "tool_use_id": "toolu_01abc"}),
        );
        assert_eq!(
            question.edge,
            Some(InteractionEdge::ResolveQuestion {
                tool_use_id: "toolu_01abc".to_string(),
            })
        );
        // The approval coordinate is recomputed from the same two fields
        // `PermissionRequest` supplied, so no state has to be held between them.
        // `PostToolUse` carries `tool_use_id` for *every* tool, and the approval
        // case below keeps one for exactly that reason: an id-first mapping
        // resolved the question instead, whose id matches nothing, and left the
        // approval `PermissionRequest` had opened pending after the user had
        // answered it.
        let permission = observation_for_event(
            "PostToolUseFailure",
            &json!({
                "tool_name": "Bash",
                "tool_use_id": "toolu_01bash",
                "tool_input": {"command": "rm -rf /tmp/x"},
            }),
        );
        let opened = observation_for_event(
            "PermissionRequest",
            &json!({"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}),
        );
        let opened_key = match opened.edge {
            Some(InteractionEdge::OpenPermission { request_key, .. }) => request_key,
            other => panic!("expected an approval edge, got {other:?}"),
        };
        match permission.edge {
            Some(InteractionEdge::ResolvePermission { request_key, .. }) => {
                assert_eq!(request_key, opened_key);
            }
            other => panic!("expected an approval resolution, got {other:?}"),
        }
    }

    fn parsed_event(
        role: Role,
        tool_name: Option<&str>,
        tool_call_id: Option<&str>,
    ) -> ParsedEvent {
        ParsedEvent {
            item: Default::default(),
            uuid: format!("uuid-{}", tool_call_id.unwrap_or("none")),
            parent_uuid: None,
            session_id: "session-transcript".to_string(),
            timestamp: DateTime::from_timestamp(1_700_000_000, 0).unwrap(),
            role,
            content_text: None,
            tool_name: tool_name.map(str::to_string),
            tool_input_json: None,
            tool_output_text: None,
            tool_call_id: tool_call_id.map(str::to_string),
            source_offset: 0,
            raw_type: "test".to_string(),
            raw_line: None,
        }
    }

    #[test]
    fn a_shipped_question_result_closes_the_wait_its_call_opened() {
        let session = "session-transcript";
        let call = parsed_event(
            Role::Assistant,
            Some(PAUSE_TOOL_NAME),
            Some("toolu_01shipped"),
        );
        let result = parsed_event(Role::Tool, None, Some("toolu_01shipped"));

        // An ordinary tool end proves nothing about a question: the result does
        // not say which tool it belongs to, so only a call this watched can
        // close a wait.
        assert!(transcript_resolutions_for_events(
            session,
            &[parsed_event(Role::Tool, None, Some("toolu_01other"))]
        )
        .is_empty());

        let resolutions =
            transcript_resolutions_for_events(session, &[call.clone(), result.clone()]);
        assert_eq!(resolutions.len(), 1);
        assert_eq!(resolutions[0]["kind"], "pause_resolution");
        assert_eq!(
            resolutions[0]["payload"]["provider_request_id"],
            "toolu_01shipped"
        );
        assert_eq!(resolutions[0]["source"], TRANSCRIPT_SOURCE);
        assert_eq!(resolutions[0]["runtime_key"], "claude:session-transcript");
        // Deterministic rather than a nonce: the watcher re-reads ranges, so a
        // replayed pass must restate this resolution, not add a second one.
        assert_eq!(
            resolutions[0]["dedupe_key"],
            "claude-transcript:resolve:session-transcript:toolu_01shipped"
        );

        // The entry is spent, so the same result cannot close a later wait.
        assert!(transcript_resolutions_for_events(session, &[result.clone()]).is_empty());
    }

    #[test]
    fn an_opened_question_is_a_durable_record_not_a_phase() {
        let event = interaction_runtime_event(
            "session-1",
            &InteractionEdge::OpenQuestion {
                tool_use_id: "toolu_01abc".to_string(),
                request_payload: json!({"questions": [{"question": "which?"}]}),
            },
            false,
        );
        assert_eq!(event["kind"], "pause_request");
        assert_eq!(event["runtime_key"], "claude:session-1");
        assert_eq!(event["phase"], Value::Null);
        assert_eq!(event["payload"]["kind"], "structured_question");
        assert_eq!(event["payload"]["provider_request_id"], "toolu_01abc");
        // The dialog is the provider's. Longhouse surfaced a wait; it did not
        // gain the authority to answer it.
        assert_eq!(event["payload"]["can_respond"], false);
        // Stable, so a re-delivery cannot open a second obligation.
        assert_eq!(
            event["dedupe_key"],
            "claude-hook:open-question:session-1:toolu_01abc"
        );
        assert_eq!(
            event["payload"]["provider_ref"]["reply_transport"],
            "terminal"
        );
    }

    #[test]
    fn a_question_the_mod_races_is_answerable_from_longhouse() {
        let event = interaction_runtime_event(
            "session-1",
            &InteractionEdge::OpenQuestion {
                tool_use_id: "toolu_01abc".to_string(),
                request_payload: json!({"questions": [{"question": "which?"}]}),
            },
            true,
        );
        assert_eq!(event["payload"]["can_respond"], true);
        assert_eq!(
            event["payload"]["provider_ref"]["reply_transport"],
            "managed_push"
        );
        assert_eq!(event["payload"]["provider_ref"]["source"], PAUSE_SOURCE);
    }

    #[test]
    fn managed_presence_carries_provider_session_id() {
        let mut managed = json!({"session_id": "lh-id", "state": "idle"});
        attach_provider_session_id(&mut managed, true, Some("native-id"));
        assert_eq!(managed["provider_session_id"], json!("native-id"));

        let mut unmanaged = json!({"session_id": "native-id", "state": "idle"});
        attach_provider_session_id(&mut unmanaged, false, Some("native-id"));
        assert!(unmanaged.get("provider_session_id").is_none());

        let mut missing = json!({"session_id": "lh-id", "state": "idle"});
        attach_provider_session_id(&mut missing, true, None);
        assert!(missing.get("provider_session_id").is_none());
    }

    #[test]
    fn a_stop_payload_reports_its_in_flight_registry() {
        // Claude publishes this on Stop and SubagentStop so a hook can tell
        // "session is done" from "session is paused waiting for background
        // work". Before this it reached the machine and was discarded.
        let input = json!({
            "hook_event_name": "Stop",
            "background_tasks": [
                {"id": "t1", "type": "subagent", "status": "running", "agent_type": "Explore"},
                {"id": "t2", "type": "shell", "status": "running", "command": "tail -f /var/log/syslog"},
                {"id": "t3", "type": "cloud session", "status": "running"},
                {"id": "t4", "type": "MCP task", "status": "running"},
            ],
            "session_crons": [{"id": "c1", "schedule": "0 9 * * 1-5", "recurring": true}],
        });

        let snapshot = delegation_snapshot(&input, "2026-09-25T16:39:00Z")
            .expect("a registry must be reported");

        // Multi-word labels normalize; crons are future work, not in flight,
        // and belong to a separate signal.
        assert_eq!(snapshot["count"], json!(4));
        assert_eq!(snapshot["kinds"]["subagent"], json!(1));
        assert_eq!(snapshot["kinds"]["shell"], json!(1));
        assert_eq!(snapshot["kinds"]["cloud_session"], json!(1));
        assert_eq!(snapshot["kinds"]["mcp_task"], json!(1));
        assert_eq!(snapshot["items"].as_array().unwrap().len(), 4);
        assert_eq!(snapshot["items"][0]["id"], "t1");
        assert_eq!(snapshot["items"][0]["kind"], "subagent");
        assert_eq!(snapshot["items"][0]["status"], "running");
        assert_eq!(snapshot["items"][0]["description"], Value::Null);
        assert_eq!(snapshot["observed_at"], "2026-09-25T16:39:00Z");
        assert!(snapshot["kinds"].get("scheduled").is_none());
    }

    #[test]
    fn an_empty_registry_is_an_observation_and_a_missing_one_is_not() {
        // `count: 0` is a positive observation that the task registry was
        // reachable and empty; no array at all is the absence of a claim. The
        // server serves those as `none` and `unknown` respectively.
        assert!(delegation_snapshot(
            &json!({"hook_event_name": "PreToolUse"}),
            "2026-09-25T16:39:00Z"
        )
        .is_none());

        let empty =
            delegation_snapshot(&json!({"background_tasks": []}), "2026-09-25T16:39:00Z").unwrap();
        assert_eq!(empty["count"], json!(0));
        assert_eq!(empty["kinds"], json!({}));
        assert_eq!(empty["items"], json!([]));
        assert_eq!(empty["observed_at"], "2026-09-25T16:39:00Z");
    }

    #[test]
    fn malformed_tasks_omit_the_authoritative_snapshot() {
        let snapshot = delegation_snapshot(
            &json!({
                "background_tasks": [
                    {"id": "dup", "type": "subagent", "status": "running", "description": "first"},
                    {"id": "missing-status", "type": "shell"},
                    {"id": "missing-type", "status": "running"},
                    {"id": 42, "type": "monitor", "status": "running"},
                ]
            }),
            "2026-09-25T16:39:00Z",
        );
        assert!(
            snapshot.is_none(),
            "malformed registry must not become authoritative zero/partial state"
        );
    }

    #[test]
    fn oversized_registry_does_not_claim_a_partial_count() {
        let tasks: Vec<Value> = (0..=DELEGATION_TASK_LIMIT)
            .map(|id| json!({"id": id.to_string(), "type": "shell", "status": "running"}))
            .collect();
        assert!(
            delegation_snapshot(&json!({"background_tasks": tasks}), "2026-09-25T16:39:00Z",)
                .is_none()
        );
    }

    #[test]
    fn valid_duplicate_tasks_are_counted_once() {
        let snapshot = delegation_snapshot(
            &json!({
                "background_tasks": [
                    {"id": "dup", "type": "subagent", "status": "running", "description": "first"},
                    {"id": "dup", "type": "shell", "status": "completed", "description": "second"},
                    {"id": "named", "type": "monitor", "status": "queued", "name": "Provider monitor", "command": "secret"},
                ]
            }),
            "2026-09-25T16:39:00Z",
        )
        .unwrap();

        assert_eq!(snapshot["count"], json!(2));
        assert_eq!(snapshot["kinds"], json!({"monitor": 1, "subagent": 1}));
        assert_eq!(snapshot["items"][0]["description"], "first");
        assert_eq!(snapshot["items"][1]["description"], "Provider monitor");
        assert!(snapshot["items"][1].get("command").is_none());
    }

    #[test]
    fn stop_subagent_snapshot_uses_exact_parent_sidecar_tool_use_id() {
        let root = tempfile::tempdir().unwrap();
        let transcript = root.path().join("parent.jsonl");
        std::fs::write(&transcript, b"").unwrap();
        let subagents = transcript.with_extension("").join("subagents");
        std::fs::create_dir_all(&subagents).unwrap();
        std::fs::write(
            subagents.join("agent-a9b75dc9dc1004699.meta.json"),
            br#"{"toolUseId":"toolu_parent_proof"}"#,
        )
        .unwrap();

        let snapshot = delegation_snapshot(
            &json!({
                "hook_event_name": "Stop",
                "transcript_path": transcript,
                "background_tasks": [{
                    "id": "a9b75dc9dc1004699",
                    "type": "subagent",
                    "status": "running",
                    "description": "Sleep and report completion",
                }],
            }),
            "2026-09-25T17:34:21.637638Z",
        )
        .unwrap();

        assert_eq!(
            snapshot["items"][0]["parent_tool_call_id"],
            "toolu_parent_proof"
        );
    }

    #[test]
    fn sidecar_link_rejects_missing_malformed_and_traversal_inputs() {
        let root = tempfile::tempdir().unwrap();
        let transcript = root.path().join("parent.jsonl");
        std::fs::write(&transcript, b"").unwrap();
        let subagents = transcript.with_extension("").join("subagents");
        std::fs::create_dir_all(&subagents).unwrap();
        std::fs::write(
            subagents.join("agent-malformed.meta.json"),
            br#"{"toolUseId":7}"#,
        )
        .unwrap();
        let transcript_path = transcript.to_string_lossy().into_owned();
        let snapshot_for = |id: &str| {
            delegation_snapshot(
                &json!({
                    "hook_event_name": "Stop",
                    "transcript_path": transcript_path.clone(),
                    "background_tasks": [{
                        "id": id,
                        "type": "subagent",
                        "status": "running",
                    }],
                }),
                "2026-09-25T17:34:21.637638Z",
            )
            .unwrap()
        };

        for id in ["missing", "malformed", "../escape"] {
            assert!(
                snapshot_for(id)["items"][0]
                    .get("parent_tool_call_id")
                    .is_none(),
                "untrusted sidecar input must not create a parent link: {id}"
            );
        }
    }

    #[test]
    fn lifecycle_edges_preserve_native_identity_and_parent_sidecar() {
        let root = tempfile::tempdir().unwrap();
        let transcript = root.path().join("parent.jsonl");
        std::fs::write(&transcript, b"").unwrap();
        let subagents = transcript.with_extension("").join("subagents");
        std::fs::create_dir_all(&subagents).unwrap();
        std::fs::write(
            subagents.join("agent-agent-start.meta.json"),
            br#"{"toolUseId":"toolu_lifecycle_parent"}"#,
        )
        .unwrap();

        let start = lifecycle_update_for_event(
            "SubagentStart",
            &json!({
                "hook_event_name": "SubagentStart",
                "transcript_path": transcript,
                "agent_id": "agent-start",
                "agent_type": "Explore",
            }),
            "2026-09-30T10:00:00Z",
        )
        .unwrap();
        assert_eq!(start["operation"], "observe");
        assert_eq!(start["item"]["id"], "agent-start");
        assert_eq!(start["item"]["kind"], "subagent");
        assert_eq!(start["item"]["status"], "running");
        assert_eq!(start["item"]["agent_type"], "Explore");
        assert_eq!(
            start["item"]["parent_tool_call_id"],
            "toolu_lifecycle_parent"
        );
        assert_eq!(start["source_event"], "SubagentStart");
        assert_eq!(start["source_agent_id"], "agent-start");
        assert_eq!(start["membership"], "existing_exact_link_only");

        let stop = lifecycle_update_for_event(
            "SubagentStop",
            &json!({
                "hook_event_name": "SubagentStop",
                "transcript_path": root.path().join("parent.jsonl"),
                "agent_id": "agent-start",
                "agent_type": "Explore",
                "agent_transcript_path": root.path().join("child.jsonl"),
            }),
            "2026-09-30T10:00:01Z",
        )
        .unwrap();
        assert_eq!(stop["operation"], "remove");
        assert_eq!(stop["item"]["id"], "agent-start");
        assert_eq!(stop["item"]["status"], "completed");
        assert_eq!(
            stop["source_agent_transcript_path"].as_str(),
            Some(root.path().join("child.jsonl").to_str().unwrap())
        );
        assert_eq!(stop["observed_at"], "2026-09-30T10:00:01Z");
    }

    #[test]
    fn retained_native_hooks_keep_callback_identity_and_registry_truth() {
        let rows = retained_native_lifecycle_rows();
        let start = rows
            .iter()
            .find(|row| row["hook_event_name"] == "SubagentStart")
            .expect("native SubagentStart");
        let agent_id = string(start, "agent_id").expect("native start agent id");
        let stop = rows
            .iter()
            .find(|row| {
                row["hook_event_name"] == "SubagentStop"
                    && string(row, "agent_id").as_deref() == Some(agent_id.as_str())
            })
            .expect("matching native SubagentStop");
        let unpaired_stop = rows
            .iter()
            .find(|row| {
                row["hook_event_name"] == "SubagentStop"
                    && string(row, "agent_id").as_deref() != Some(agent_id.as_str())
            })
            .expect("native unpaired SubagentStop");
        let unpaired_update =
            lifecycle_update_for_event("SubagentStop", unpaired_stop, "2026-09-30T10:00:01Z")
                .expect("unpaired native stop edge");
        assert_eq!(
            unpaired_update["source_agent_id"],
            unpaired_stop["agent_id"]
        );
        assert_eq!(
            unpaired_update["membership"], "existing_exact_link_only",
            "an unpaired terminal identity cannot create registry membership"
        );
        let active_parent_stop = rows
            .iter()
            .find(|row| {
                row["hook_event_name"] == "Stop"
                    && row["background_tasks"]
                        .as_array()
                        .is_some_and(|tasks| tasks.len() > 1)
            })
            .expect("native parent Stop with active registry");
        let empty_parent_stop = rows
            .iter()
            .find(|row| row["hook_event_name"] == "Stop" && row["background_tasks"] == json!([]))
            .expect("native parent Stop with explicit empty registry");

        let root = tempfile::tempdir().unwrap();
        let transcript = root.path().join("parent.jsonl");
        std::fs::write(&transcript, b"").unwrap();
        let subagents = transcript.with_extension("").join("subagents");
        std::fs::create_dir_all(&subagents).unwrap();
        let sidecar: Value = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/claude_hooks/lifecycle_native.sidecar.meta.json"
        )))
        .unwrap();
        std::fs::write(
            subagents.join(format!("agent-{agent_id}.meta.json")),
            serde_json::to_vec(&sidecar).unwrap(),
        )
        .unwrap();

        let mut start_input = start.clone();
        start_input["transcript_path"] = json!(transcript);
        let start_update =
            lifecycle_update_for_event("SubagentStart", &start_input, "2026-09-30T10:00:00Z")
                .expect("native start edge");
        assert_eq!(start_update["operation"], "observe");
        assert_eq!(start_update["item"]["id"], start["agent_id"]);
        assert_eq!(start_update["item"]["kind"], "subagent");
        assert_eq!(start_update["item"]["agent_type"], start["agent_type"]);
        assert_eq!(
            start_update["item"]["parent_tool_call_id"],
            sidecar["toolUseId"]
        );
        assert_eq!(start_update["membership"], "existing_exact_link_only");

        let mut stop_input = stop.clone();
        stop_input["transcript_path"] = json!(transcript);
        let stop_update =
            lifecycle_update_for_event("SubagentStop", &stop_input, "2026-09-30T10:00:01Z")
                .expect("native stop edge");
        assert_eq!(stop_update["operation"], "remove");
        assert_eq!(stop_update["item"]["id"], stop["agent_id"]);
        assert_eq!(stop_update["source_agent_id"], stop["agent_id"]);
        assert_eq!(
            stop["background_tasks"][0]["id"], start["agent_id"],
            "terminal callback must match the prior running registry identity"
        );
        let carrier = with_managed_home(
            root.path(),
            "managed-native-parent",
            "run-native-parent",
            || {
                handle_input(&stop_input).unwrap();
                let path = root
                    .path()
                    .join("agent/outbox")
                    .read_dir()
                    .unwrap()
                    .next()
                    .unwrap()
                    .unwrap()
                    .path();
                serde_json::from_slice::<Value>(&std::fs::read(path).unwrap()).unwrap()
            },
        );
        assert!(carrier["delegation"].is_object());
        assert!(carrier["delegation_update"].is_object());
        assert!(carrier.get("state").is_none());

        // The provider's terminal callback is a completion edge, but its
        // simultaneously captured registry still says running. Do not invent
        // a completed registry row from the callback.
        let callback_registry =
            delegation_snapshot(stop, "2026-09-30T10:00:01Z").expect("callback registry");
        assert_eq!(callback_registry["count"], json!(1));
        assert_eq!(callback_registry["items"][0]["id"], start["agent_id"]);
        assert_eq!(callback_registry["items"][0]["status"], "running");

        let active_registry =
            delegation_snapshot(active_parent_stop, "2026-09-30T10:00:02Z").unwrap();
        assert_eq!(active_registry["count"], json!(2));
        assert_eq!(active_registry["items"][0]["id"], start["agent_id"]);
        assert_eq!(active_registry["items"][0]["status"], "running");

        let empty_registry =
            delegation_snapshot(empty_parent_stop, "2026-09-30T10:00:03Z").unwrap();
        assert_eq!(empty_registry["count"], json!(0));
        assert_eq!(empty_registry["items"], json!([]));
        assert!(
            delegation_snapshot(&json!({"hook_event_name": "Stop"}), "2026-09-30T10:00:03Z")
                .is_none()
        );
    }

    #[test]
    fn lifecycle_edges_reject_missing_or_unbounded_identity() {
        assert!(lifecycle_update_for_event(
            "SubagentStart",
            &json!({"agent_type": "Explore"}),
            "2026-09-30T10:00:00Z",
        )
        .is_none());
        assert!(lifecycle_update_for_event(
            "SubagentStop",
            &json!({"agent_id": "bad\u{0000}id"}),
            "2026-09-30T10:00:00Z",
        )
        .is_none());
    }

    #[test]
    fn child_stop_carries_parent_registry_without_parent_phase_or_run_loss() {
        let home = tempfile::tempdir().unwrap();
        let transcript = home.path().join("parent.jsonl");
        std::fs::write(&transcript, b"").unwrap();
        let subagents = transcript.with_extension("").join("subagents");
        std::fs::create_dir_all(&subagents).unwrap();
        std::fs::write(
            subagents.join("agent-child-1.meta.json"),
            br#"{"toolUseId":"toolu_parent_child_1"}"#,
        )
        .unwrap();
        let payload = with_managed_home(home.path(), "longhouse-parent", "run-parent-1", || {
            handle_input(&json!({
                "hook_event_name": "SubagentStop",
                "session_id": "native-parent",
                "transcript_path": transcript,
                "agent_id": "child-1",
                "agent_type": "Explore",
                "agent_transcript_path": home.path().join("child-1.jsonl"),
                "background_tasks": [{
                    "id": "child-1",
                    "type": "subagent",
                    "status": "running",
                }],
                "session_crons": [],
            }))
            .unwrap();
            let path = home
                .path()
                .join("agent/outbox")
                .read_dir()
                .unwrap()
                .next()
                .unwrap()
                .unwrap()
                .path();
            serde_json::from_slice::<Value>(&std::fs::read(path).unwrap()).unwrap()
        });
        assert_eq!(payload["session_id"], "longhouse-parent");
        assert_eq!(payload["provider_session_id"], "native-parent");
        assert_eq!(payload["run_id"], "run-parent-1");
        assert!(payload.get("state").is_none());
        assert_eq!(payload["delegation"]["count"], 1);
        assert_eq!(
            payload["delegation"]["items"][0]["parent_tool_call_id"],
            "toolu_parent_child_1"
        );
        assert_eq!(payload["delegation_update"]["operation"], "remove");
        assert_eq!(payload["delegation_update"]["source_agent_id"], "child-1");
    }

    fn with_managed_home<T>(
        home: &std::path::Path,
        session_id: &str,
        run_id: &str,
        body: impl FnOnce() -> T,
    ) -> T {
        let _guard = crate::console_adapter::agent_state_guard();
        temp_env::with_vars(
            [
                ("LONGHOUSE_HOME", Some(home.as_os_str())),
                (
                    "LONGHOUSE_MANAGED_SESSION_ID",
                    Some(std::ffi::OsStr::new(session_id)),
                ),
                ("LONGHOUSE_RUN_ID", Some(std::ffi::OsStr::new(run_id))),
                (
                    "LONGHOUSE_MANAGED_PROVIDER",
                    Some(std::ffi::OsStr::new("claude")),
                ),
            ],
            body,
        )
    }

    /// Run with a scratch Longhouse home and none of the caller's Longhouse
    /// identity. A test run from inside a managed agent session inherits
    /// LONGHOUSE_MANAGED_SESSION_ID and friends, which make a "bare" session
    /// look managed and change what the hook announces; every ambient
    /// `LONGHOUSE_*` variable is set aside for the body and restored after.
    fn with_home<T>(home: &std::path::Path, body: impl FnOnce() -> T) -> T {
        let _guard = crate::console_adapter::agent_state_guard();
        let ambient: Vec<(std::ffi::OsString, std::ffi::OsString)> = std::env::vars_os()
            .filter(|(key, _)| key.to_string_lossy().starts_with("LONGHOUSE_"))
            .collect();
        for (key, _) in &ambient {
            std::env::remove_var(key);
        }
        std::env::set_var("LONGHOUSE_HOME", home);
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(body));
        // Clear whatever the body set too, so nothing leaks to the next test.
        for (key, _) in std::env::vars_os()
            .filter(|(key, _)| key.to_string_lossy().starts_with("LONGHOUSE_"))
            .collect::<Vec<_>>()
        {
            std::env::remove_var(key);
        }
        for (key, value) in &ambient {
            std::env::set_var(key, value);
        }
        match result {
            Ok(value) => value,
            Err(panic) => std::panic::resume_unwind(panic),
        }
    }

    fn outbox_files(home: &std::path::Path) -> Vec<String> {
        let outbox = home.join("agent").join("outbox");
        let Ok(entries) = std::fs::read_dir(&outbox) else {
            return Vec::new();
        };
        let mut names: Vec<String> = entries
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    fn tool_use_payload(extra: Value) -> Value {
        let mut payload = json!({
            "hook_event_name": "PreToolUse",
            "session_id": "sess-unmanaged",
            "tool_name": "Bash",
            "cwd": "/tmp",
        });
        if let Value::Object(extra) = extra {
            for (key, value) in extra {
                payload[key] = value;
            }
        }
        payload
    }

    #[test]
    fn registry_presence_uses_one_observation_clock() {
        let home = tempfile::tempdir().unwrap();
        let payload = with_home(home.path(), || {
            handle_input(&json!({
                "hook_event_name": "Stop",
                "session_id": "sess-clock",
                "background_tasks": [{
                    "id": "task-clock",
                    "type": "subagent",
                    "status": "mystery",
                    "description": "provider label",
                }],
            }))
            .unwrap();
            let file = home
                .path()
                .join("agent/outbox")
                .read_dir()
                .unwrap()
                .next()
                .unwrap()
                .unwrap()
                .path();
            serde_json::from_slice::<Value>(&std::fs::read(file).unwrap()).unwrap()
        });

        assert_eq!(
            payload["occurred_at"], payload["delegation"]["observed_at"],
            "carrier and snapshot must share the hook observation clock"
        );
        assert_eq!(payload["delegation"]["items"][0]["status"], "mystery");
    }

    #[test]
    fn background_writer_fault_preserves_foreground_and_provider_binding() {
        let _guard = crate::console_adapter::agent_state_guard();
        let home = tempfile::tempdir().unwrap();
        let receipt = home.path().join("fault.jsonl");
        temp_env::with_vars(
            [
                ("LONGHOUSE_HOME", Some(home.path().to_str().unwrap())),
                ("LONGHOUSE_MANAGED_PROVIDER", None),
                ("LONGHOUSE_MANAGED_SESSION_ID", None),
                (
                    "LONGHOUSE_QA_FAULT",
                    Some("claude_background_writer_disabled"),
                ),
                (
                    "LONGHOUSE_QA_FAULT_RECEIPT",
                    Some(receipt.to_str().unwrap()),
                ),
            ],
            || {
                handle_input(&json!({
                    "hook_event_name": "Stop",
                    "session_id": "fault-parent-native",
                    "background_tasks": [{
                        "id": "native-worker", "type": "subagent", "status": "running"
                    }],
                }))
                .unwrap();
                let files: Vec<_> = home
                    .path()
                    .join("agent/outbox")
                    .read_dir()
                    .unwrap()
                    .map(|entry| entry.unwrap().path())
                    .collect();
                let payload: Value =
                    serde_json::from_slice(&std::fs::read(&files[0]).unwrap()).unwrap();
                assert_eq!(payload["session_id"], "fault-parent-native");
                assert_eq!(payload["provider"], "claude");
                assert_eq!(payload["state"], "idle");
                if cfg!(feature = "qa-fault-injection") {
                    assert!(payload.get("delegation").is_none());
                    let fired: Value = serde_json::from_str(
                        std::fs::read_to_string(&receipt)
                            .unwrap()
                            .lines()
                            .next()
                            .unwrap(),
                    )
                    .unwrap();
                    assert_eq!(fired["fault"], "claude_background_writer_disabled");
                    assert_eq!(fired["session_id"], "fault-parent-native");
                    assert_eq!(fired["detail"]["had_registry"], true);
                } else {
                    assert_eq!(payload["delegation"]["count"], 1);
                    assert!(!receipt.exists());
                }
            },
        );
    }

    #[test]
    fn a_subagent_tool_event_does_not_write_the_parents_presence() {
        // Claude fires the configured hooks inside subagents, and the input
        // carries agent_id/agent_type. Presence is keyed by session id and a
        // managed child inherits the parent's, so before this guard a child's
        // PreToolUse landed on the parent's activity head and the parent read
        // "Using Bash" while its own turn sat idle waiting on that child.
        let home = tempfile::tempdir().unwrap();

        let parent_wrote = with_home(home.path(), || {
            handle_input(&tool_use_payload(json!({}))).unwrap();
            outbox_files(home.path()).len()
        });
        assert_eq!(
            parent_wrote, 1,
            "a parent-thread tool event must still write presence"
        );

        let subagent_files = with_home(home.path(), || {
            handle_input(&tool_use_payload(
                json!({"agent_id": "a123", "agent_type": "Explore"}),
            ))
            .unwrap();
            handle_input(&json!({
                "hook_event_name": "PostToolUse",
                "session_id": "sess-1",
                "tool_name": "Bash",
                "cwd": "/tmp",
                "agent_id": "a123",
                "agent_type": "Explore",
            }))
            .unwrap();
            let runtime_outbox = crate::config::get_agent_runtime_events_outbox_dir().unwrap();
            let runtime_files: Vec<_> = std::fs::read_dir(&runtime_outbox)
                .map(|entries| entries.flatten().collect())
                .unwrap_or_default();
            assert!(
                runtime_files.is_empty(),
                "child tool completions must not emit parent pause_resolution runtime events"
            );
            outbox_files(home.path())
        });
        assert_eq!(
            subagent_files.len(),
            3,
            "child tool events emit bounded delegation_update records alongside parent presence"
        );
        let outbox_dir = home.path().join("agent").join("outbox");
        let payloads: Vec<Value> = subagent_files
            .iter()
            .map(|name| {
                serde_json::from_slice(&std::fs::read(outbox_dir.join(name)).unwrap()).unwrap()
            })
            .collect();
        let child_updates: Vec<&Value> = payloads
            .iter()
            .filter(|payload| payload.get("delegation_update").is_some())
            .collect();
        assert_eq!(child_updates.len(), 2);
        for payload in &child_updates {
            assert!(
                payload.get("state").is_none(),
                "child tool events must never set parent state"
            );
            assert!(payload["tool_name"].is_null());
            assert!(payload["transcript_path"].is_null());
            assert_eq!(
                payload["delegation_update"]["membership"],
                "existing_exact_link_only"
            );
            assert_eq!(payload["delegation_update"]["operation"], "observe");
            assert_eq!(payload["delegation_update"]["source_agent_id"], "a123");
        }
        let pre = child_updates
            .iter()
            .find(|p| p["delegation_update"]["source_event"] == "PreToolUse")
            .expect("PreToolUse update");
        assert_eq!(
            pre["delegation_update"]["item"]["native_progress"]["status"],
            "running"
        );
        assert_eq!(
            pre["delegation_update"]["item"]["native_progress"]["current_tool"],
            "Bash"
        );
        let post = child_updates
            .iter()
            .find(|p| p["delegation_update"]["source_event"] == "PostToolUse")
            .expect("PostToolUse update");
        assert_eq!(
            post["delegation_update"]["item"]["native_progress"]["status"],
            "thinking"
        );
        assert!(post["delegation_update"]["item"]["native_progress"]
            .get("current_tool")
            .is_none());
    }

    #[test]
    fn warp_stop_marker_is_suppressed_while_subagents_run_and_released_on_final_stop_or_exit() {
        let home = tempfile::tempdir().unwrap();
        let sid = "sess-warp-1";
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "SessionStart",
                false,
                None,
                None,
                None,
                false
            ),
            Some("session_start")
        );
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "PostToolUse",
                false,
                None,
                None,
                None,
                false
            ),
            Some("tool_complete")
        );
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "Notification",
                false,
                None,
                None,
                Some("idle_prompt"),
                false
            ),
            Some("idle_prompt")
        );
        assert_eq!(
            warp_marker_for_event(home.path(), sid, "Stop", false, None, None, None, true),
            None,
            "Stop blocked by turn control keeps the turn alive"
        );
        assert_eq!(
            warp_marker_for_event(home.path(), sid, "Stop", false, None, None, None, false),
            Some("stop")
        );
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "Stop",
                false,
                None,
                Some(&json!({"count": 0, "kinds": {}, "items": []})),
                None,
                false
            ),
            Some("stop")
        );
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "Stop",
                false,
                None,
                Some(&json!({
                    "count": 2,
                    "kinds": {"shell": 2},
                    "items": [
                        {"id": "sh-1", "kind": "shell", "status": "running"},
                        {"id": "sh-2", "kind": "shell", "status": "running"}
                    ]
                })),
                None,
                false
            ),
            Some("stop"),
            "background shells like dev servers must not keep Warp spinning"
        );
        // Foreground SubagentStop when parent never suppressed Stop emits nothing.
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "SubagentStop",
                true,
                Some("fg-1"),
                Some(&json!({"count": 0, "kinds": {}, "items": []})),
                None,
                false
            ),
            None
        );
        // Parent Stop with 2 running background subagents defers Warp stop.
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "Stop",
                false,
                None,
                Some(&json!({
                    "count": 2,
                    "kinds": {"subagent": 2},
                    "items": [
                        {"id": "bg-1", "kind": "subagent", "status": "running"},
                        {"id": "bg-2", "kind": "subagent", "status": "running"}
                    ]
                })),
                None,
                false
            ),
            None
        );
        // First SubagentStop still has bg-2 running, so Warp stays active.
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "SubagentStop",
                true,
                Some("bg-1"),
                Some(&json!({
                    "count": 2,
                    "kinds": {"subagent": 2},
                    "items": [
                        {"id": "bg-1", "kind": "subagent", "status": "running"},
                        {"id": "bg-2", "kind": "subagent", "status": "running"}
                    ]
                })),
                None,
                false
            ),
            None
        );
        // Final SubagentStop clears the deferred Warp stop even if no later parent Stop fires.
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "SubagentStop",
                true,
                Some("bg-2"),
                Some(&json!({
                    "count": 1,
                    "kinds": {"subagent": 1},
                    "items": [
                        {"id": "bg-2", "kind": "subagent", "status": "running"}
                    ]
                })),
                None,
                false
            ),
            Some("stop")
        );
        // SessionEnd also clears any deferred stop unconditionally.
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "Stop",
                false,
                None,
                Some(&json!({"count": 1, "kinds": {"subagent": 1}})),
                None,
                false
            ),
            None
        );
        assert_eq!(
            warp_marker_for_event(
                home.path(),
                sid,
                "SessionEnd",
                false,
                None,
                None,
                None,
                false
            ),
            Some("stop")
        );
    }

    fn parse_warp_terminal_sequence(output: &Value) -> Value {
        let seq = output["terminalSequence"]
            .as_str()
            .expect("terminalSequence string");
        let payload = seq
            .strip_prefix("\x1b]777;notify;warp://cli-agent;")
            .and_then(|value| value.strip_suffix('\x07'))
            .expect("Warp OSC 777 framing");
        serde_json::from_str::<Value>(payload).unwrap()
    }

    #[test]
    fn warp_terminal_sequence_outputs_single_valid_json_when_tty_unavailable() {
        let home = tempfile::tempdir().unwrap();
        with_managed_home(home.path(), "lh-warp-sess", "run-warp-1", || {
            let start = evaluate_input(
                &json!({
                    "hook_event_name": "SessionStart",
                    "session_id": "native-warp",
                    "cwd": "/tmp/demo-proj",
                }),
                true,
                false,
            )
            .unwrap()
            .expect("SessionStart output");
            assert_eq!(start["hookSpecificOutput"]["hookEventName"], "SessionStart");
            let start_marker = parse_warp_terminal_sequence(&start);
            assert_eq!(start_marker["agent"], "claude");
            assert_eq!(start_marker["event"], "session_start");
            assert_eq!(start_marker["session_id"], "lh-warp-sess");
            assert_eq!(start_marker["project"], "demo-proj");

            let prompt = evaluate_input(
                &json!({
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "native-warp",
                    "cwd": "/tmp/demo-proj",
                    "prompt": "fix the warp hooks",
                }),
                true,
                false,
            )
            .unwrap()
            .expect("UserPromptSubmit output");
            let prompt_marker = parse_warp_terminal_sequence(&prompt);
            assert_eq!(prompt_marker["event"], "prompt_submit");
            assert_eq!(prompt_marker["query"], "fix the warp hooks");

            let perm = evaluate_input(
                &json!({
                    "hook_event_name": "PermissionRequest",
                    "session_id": "native-warp",
                    "cwd": "/tmp/demo-proj",
                    "tool_name": "Bash",
                    "tool_input": { "command": "cargo test" },
                }),
                true,
                false,
            )
            .unwrap()
            .expect("PermissionRequest output");
            let perm_marker = parse_warp_terminal_sequence(&perm);
            assert_eq!(perm_marker["event"], "permission_request");
            assert_eq!(perm_marker["tool_name"], "Bash");
            assert_eq!(perm_marker["summary"], "Wants to run Bash: cargo test");

            let post = evaluate_input(
                &json!({
                    "hook_event_name": "PostToolUse",
                    "session_id": "native-warp",
                    "cwd": "/tmp/demo-proj",
                    "tool_name": "Bash",
                    "tool_input": { "command": "cargo test" },
                }),
                true,
                false,
            )
            .unwrap()
            .expect("PostToolUse output");
            let post_marker = parse_warp_terminal_sequence(&post);
            assert_eq!(post_marker["event"], "tool_complete");
            assert_eq!(post_marker["tool_name"], "Bash");

            let idle = evaluate_input(
                &json!({
                    "hook_event_name": "Notification",
                    "notification_type": "idle_prompt",
                    "session_id": "native-warp",
                    "cwd": "/tmp/demo-proj",
                    "message": "Claude is waiting for your input",
                }),
                true,
                false,
            )
            .unwrap()
            .expect("Notification output");
            let idle_marker = parse_warp_terminal_sequence(&idle);
            assert_eq!(idle_marker["event"], "idle_prompt");
            assert_eq!(idle_marker["summary"], "Claude is waiting for your input");

            let stop = evaluate_input(
                &json!({
                    "hook_event_name": "Stop",
                    "session_id": "native-warp",
                    "cwd": "/tmp/demo-proj",
                }),
                true,
                false,
            )
            .unwrap()
            .expect("Stop output");
            let stop_marker = parse_warp_terminal_sequence(&stop);
            assert_eq!(stop_marker["event"], "stop");

            let deferred = evaluate_input(
                &json!({
                    "hook_event_name": "Stop",
                    "session_id": "native-warp",
                    "cwd": "/tmp/demo-proj",
                    "background_tasks": [{
                        "id": "bg-1",
                        "type": "subagent",
                        "status": "running",
                    }],
                }),
                true,
                false,
            )
            .unwrap();
            assert!(
                deferred.is_none(),
                "Stop with running subagent must defer Warp stop"
            );

            let released = evaluate_input(
                &json!({
                    "hook_event_name": "SubagentStop",
                    "session_id": "native-warp",
                    "cwd": "/tmp/demo-proj",
                    "agent_id": "bg-1",
                    "background_tasks": [{
                        "id": "bg-1",
                        "type": "subagent",
                        "status": "running",
                    }],
                }),
                true,
                false,
            )
            .unwrap()
            .expect("Final SubagentStop output");
            assert_eq!(parse_warp_terminal_sequence(&released)["event"], "stop");

            let outside_warp = evaluate_input(
                &json!({
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "native-warp",
                    "cwd": "/tmp/demo-proj",
                    "prompt": "ignored outside warp",
                }),
                false,
                false,
            )
            .unwrap();
            assert!(
                outside_warp.is_none(),
                "outside Warp must emit no terminalSequence"
            );
        });
    }

    #[test]
    fn warp_state_file_records_claude_session_outside_warp() {
        let home = tempfile::tempdir().unwrap();
        let warp_state_path = home.path().join("remote-claude.warp.json");
        with_managed_home(home.path(), "lh-remote-claude", "run-remote-1", || {
            temp_env::with_var(
                crate::warp_cli_agent::WARP_STATE_FILE_ENV,
                Some(warp_state_path.to_str().unwrap()),
                || {
                    let out = evaluate_input(
                        &json!({
                            "hook_event_name": "UserPromptSubmit",
                            "session_id": "native-id",
                            "cwd": "/tmp/demo-proj",
                            "prompt": "start outside warp",
                        }),
                        false,
                        false,
                    )
                    .unwrap();
                    assert!(out.is_none());
                    let state = crate::warp_cli_agent::read_warp_state(&warp_state_path)
                        .expect("warp state file written outside Warp");
                    assert_eq!(state.agent, "claude");
                    assert_eq!(state.session_id, "lh-remote-claude");
                    assert_eq!(state.last_event, "prompt_submit");

                    let _ = evaluate_input(
                        &json!({
                            "hook_event_name": "Stop",
                            "session_id": "native-id",
                            "cwd": "/tmp/demo-proj",
                        }),
                        false,
                        false,
                    )
                    .unwrap();
                    let stopped = crate::warp_cli_agent::read_warp_state(&warp_state_path)
                        .expect("stop state written");
                    assert_eq!(stopped.last_event, "stop");
                },
            );
        });
    }

    fn write_transcript(dir: &std::path::Path, name: &str) -> std::path::PathBuf {
        let path = dir.join(name);
        std::fs::write(&path, "{\"cwd\":\"/tmp\",\"type\":\"user\"}\n").unwrap();
        path
    }

    fn presence_for(transcript: &std::path::Path) {
        // Distinct session ids keep one test's presence from deduplicating another's.
        let session = format!("sess-{}", uuid::Uuid::new_v4());
        handle_input(&tool_use_payload(json!({
            "session_id": session,
            "transcript_path": transcript,
        })))
        .unwrap();
    }

    /// A bare Claude session that began before the machine's import scope is not
    /// announced: presence carries its folder and tool names.
    #[test]
    fn a_bare_claude_session_older_than_the_import_scope_is_not_announced() {
        let home = tempfile::tempdir().unwrap();
        let old = write_transcript(home.path(), "old.jsonl");
        std::thread::sleep(std::time::Duration::from_millis(60));
        let since = chrono::Utc::now();
        std::thread::sleep(std::time::Duration::from_millis(60));
        let new = write_transcript(home.path(), "new.jsonl");
        crate::import_scope::ImportScope::starting(since, "cli")
            .save(&home.path().join("machine"))
            .unwrap();
        with_home(home.path(), || {
            presence_for(&old);
            assert!(
                outbox_files(home.path()).is_empty(),
                "an old session was announced"
            );
            presence_for(&new);
            assert_eq!(
                outbox_files(home.path()).len(),
                1,
                "a new session is announced"
            );
        });
    }

    /// The hook is its own process and never ran the daemon's first-run
    /// resolution. With no scope file on a connected machine that never shipped,
    /// it must not read the missing file as "everything".
    #[test]
    fn with_no_scope_file_a_connected_new_machine_announces_no_old_session() {
        let home = tempfile::tempdir().unwrap();
        let machine = home.path().join("machine");
        std::fs::create_dir_all(&machine).unwrap();
        std::fs::write(
            machine.join("state.json"),
            "{\"runtime_url\":\"https://you.longhouse.ai\",\"machine_name\":\"laptop\"}",
        )
        .unwrap();
        let old = write_transcript(home.path(), "old.jsonl");
        std::thread::sleep(std::time::Duration::from_millis(60));
        with_home(home.path(), || {
            presence_for(&old);
            assert!(outbox_files(home.path()).is_empty());
        });
    }

    /// Machines that already ship keep announcing everything they always did,
    /// scope file or not.
    #[test]
    fn a_machine_that_shipped_before_scopes_still_announces_with_no_scope_file() {
        let home = tempfile::tempdir().unwrap();
        let machine = home.path().join("machine");
        std::fs::create_dir_all(&machine).unwrap();
        std::fs::write(
            machine.join("state.json"),
            "{\"runtime_url\":\"https://you.longhouse.ai\",\"machine_name\":\"laptop\"}",
        )
        .unwrap();
        std::fs::create_dir_all(home.path().join("agent")).unwrap();
        let state = crate::state::db::open_db(Some(
            &home.path().join("agent").join("longhouse-shipper.db"),
        ))
        .unwrap();
        state
            .execute(
                "INSERT INTO file_state (path, provider, queued_offset, acked_offset, last_updated)
                 VALUES ('/x.jsonl', 'claude', 10, 10, '2026-01-01T00:00:00Z')",
                [],
            )
            .unwrap();
        drop(state);
        let old = write_transcript(home.path(), "old.jsonl");
        std::thread::sleep(std::time::Duration::from_millis(60));
        with_home(home.path(), || {
            presence_for(&old);
            assert_eq!(outbox_files(home.path()).len(), 1);
        });
    }

    #[test]
    fn mod_state_agents_and_session_usage_and_monitors_produce_rich_presence() {
        let home = tempfile::tempdir().unwrap();
        let transcript = home.path().join("parent.jsonl");
        std::fs::write(&transcript, b"").unwrap();
        let subagents = transcript.with_extension("").join("subagents");
        std::fs::create_dir_all(&subagents).unwrap();
        std::fs::write(
            subagents.join("agent-a1.meta.json"),
            br#"{"agentType":"Explore","description":"Scan hooks","toolUseId":"toolu_a1","pickedEffortModel":"claude-opus-5-5","isFork":true,"spawnDepth":2,"parentAgentId":"a0","finished":false}"#,
        )
        .unwrap();

        with_managed_home(home.path(), "longhouse-rich", "run-rich", || {
            // Monitor PreToolUse records its description for later upgrade.
            handle_input(&json!({
                "hook_event_name": "PreToolUse",
                "session_id": "native-rich",
                "tool_name": "Monitor",
                "tool_input": { "description": "Watch canary rollout" },
            }))
            .unwrap();

            // SubagentStop when finished=false emits observe/waiting rather than remove.
            handle_input(&json!({
                "hook_event_name": "SubagentStop",
                "session_id": "native-rich",
                "transcript_path": transcript,
                "agent_id": "a1",
            }))
            .unwrap();

            // Child PreToolUse populates last_intent and clears on PostToolUse.
            handle_input(&json!({
                "hook_event_name": "PreToolUse",
                "session_id": "native-rich",
                "transcript_path": transcript,
                "agent_id": "a1",
                "tool_name": "Read",
                "tool_input": { "file_path": "/tmp/hooks.py" },
            }))
            .unwrap();

            // TaskCreated upserts; TaskCompleted removes.
            handle_input(&json!({
                "hook_event_name": "TaskCreated",
                "session_id": "native-rich",
                "task_id": "task-9",
                "task_subject": "Run verification",
            }))
            .unwrap();

            // CwdChanged picks up new_cwd + permission_mode + session_usage + ClaudeModState.
            handle_input(&json!({
                "hook_event_name": "ClaudeModState",
                "session_id": "native-rich",
                "transcript_path": transcript,
                "cwd": "/repo/sub",
                "agents": [
                    {
                        "id": "a1",
                        "type": "Explore",
                        "status": "idle",
                        "toolCount": 14,
                        "tokens": 8200,
                        "durationMs": 45000,
                        "lastIntent": "Read hooks.py",
                    }
                ],
                "background_tasks": [
                    {
                        "id": "sh-mon",
                        "type": "shell",
                        "status": "running",
                        "description": "Watch canary rollout",
                    }
                ],
                "session_usage": {
                    "startedAt": 1700000000000u64,
                    "context": { "tokens": 120000, "window": 1000000, "percent": 12 },
                    "cost": { "usd": 14.5 },
                    "rateLimits": [{ "kind": "five_hour", "percentUsed": 22.0, "resetsAt": "2026-11-15T08:00:00Z" }]
                },
                "session_crons": [{ "id": "cron-1", "schedule": "*/5 * * * *", "recurring": true, "prompt": "check" }],
                "permission_mode": "bypassPermissions",
            }))
            .unwrap();

            let files: Vec<Value> = std::fs::read_dir(home.path().join("agent/outbox"))
                .unwrap()
                .map(|entry| {
                    let bytes = std::fs::read(entry.unwrap().path()).unwrap();
                    serde_json::from_slice(&bytes).unwrap()
                })
                .collect();

            let waiting_stop = files
                .iter()
                .find(|v| {
                    v.get("delegation_update")
                        .and_then(|u| u.get("source_event"))
                        == Some(&json!("SubagentStop"))
                })
                .unwrap();
            assert_eq!(waiting_stop["delegation_update"]["operation"], "observe");
            assert_eq!(
                waiting_stop["delegation_update"]["item"]["status"],
                "waiting"
            );
            assert_eq!(waiting_stop["delegation_update"]["item"]["spawn_depth"], 2);
            assert_eq!(waiting_stop["delegation_update"]["item"]["is_fork"], true);

            let child_pre = files
                .iter()
                .find(|v| {
                    v.get("delegation_update")
                        .and_then(|u| u.get("source_event"))
                        == Some(&json!("PreToolUse"))
                })
                .unwrap();
            assert_eq!(
                child_pre["delegation_update"]["item"]["native_progress"]["last_intent"],
                "Read hooks.py"
            );

            let created = files
                .iter()
                .find(|v| {
                    v.get("delegation_update")
                        .and_then(|u| u.get("source_event"))
                        == Some(&json!("TaskCreated"))
                })
                .unwrap();
            assert_eq!(created["delegation_update"]["membership"], "upsert");
            assert_eq!(
                created["delegation_update"]["item"]["description"],
                "Run verification"
            );

            let mod_state = files
                .iter()
                .find(|v| v.get("hook_event_name") == Some(&json!("ClaudeModState")))
                .unwrap();
            assert_eq!(mod_state["delegation"]["count"], 2);
            assert_eq!(mod_state["delegation"]["kinds"]["monitor"], 1);
            assert_eq!(mod_state["delegation"]["kinds"]["subagent"], 1);
            assert_eq!(mod_state["delegation"]["items"][0]["kind"], "monitor");
            assert_eq!(mod_state["delegation"]["items"][1]["status"], "waiting");
            assert_eq!(
                mod_state["delegation"]["items"][1]["model"],
                "claude-opus-5-5"
            );
            assert_eq!(
                mod_state["delegation"]["items"][1]["native_progress"]["tool_count"],
                14
            );
            assert_eq!(mod_state["session_usage"]["cost_usd"], 14.5);
            assert_eq!(mod_state["session_usage"]["context"]["window"], 1000000);
            assert_eq!(mod_state["permission_mode"], "bypassPermissions");
            assert_eq!(mod_state["session_crons"][0]["id"], "cron-1");
        });
    }

    #[test]
    fn claude_mod_state_without_background_tasks_syncs_agents_without_emitting_full_snapshot() {
        let home = tempfile::tempdir().unwrap();
        let transcript = home.path().join("sess-sync.jsonl");
        std::fs::write(&transcript, "").unwrap();
        let subagents = home.path().join("sess-sync/subagents");
        std::fs::create_dir_all(&subagents).unwrap();
        std::fs::write(
            subagents.join("agent-a1.meta.json"),
            json!({
                "agentType": "Explore",
                "description": "Waiting on reply",
                "finished": false,
            })
            .to_string(),
        )
        .unwrap();

        with_managed_home(home.path(), "longhouse-sync", "run-sync", || {
            handle_input(&json!({
                "hook_event_name": "ClaudeModState",
                "session_id": "sess-sync",
                "transcript_path": transcript,
                "agents": [
                    {
                        "id": "a1",
                        "type": "Explore",
                        "status": "completed",
                    },
                    {
                        "id": "a2",
                        "type": "general-purpose",
                        "status": "completed",
                    }
                ],
            }))
            .unwrap();

            let files: Vec<Value> = std::fs::read_dir(home.path().join("agent/outbox"))
                .unwrap()
                .map(|entry| {
                    let bytes = std::fs::read(entry.unwrap().path()).unwrap();
                    serde_json::from_slice(&bytes).unwrap()
                })
                .collect();
            assert_eq!(files.len(), 1);
            let payload = &files[0];
            assert!(payload.get("delegation").is_none());
            assert_eq!(payload["delegation_update"]["operation"], "sync_agents");
            assert_eq!(
                payload["delegation_update"]["source_event"],
                "ClaudeModState"
            );
            let items = payload["delegation_update"]["items"].as_array().unwrap();
            assert_eq!(items.len(), 1);
            assert_eq!(items[0]["id"], "a1");
            assert_eq!(items[0]["status"], "waiting");
        });
    }
}
