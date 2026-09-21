//! Provider-receipt evidence for managed Cursor turns.
//!
//! Cursor's store is a lossless artifact archive, not a presentation log.  The
//! managed Helm hooks and Console stream both supply stronger turn boundaries
//! and committed response receipts used by the renderer.

use std::collections::HashMap;
use std::fs;
use std::io::Write;
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::path::PathBuf;
use std::time::Duration as StdDuration;

use anyhow::{Context, Result};
use chrono::{DateTime, Duration, Utc};
use serde_json::{json, Value};

const COMPLETED_RECEIPT_GRACE: Duration = Duration::seconds(30);

/// Hook events that represent provider work for a generation.  Session
/// lifecycle events (`sessionStart`/`sessionEnd`) are excluded: only evidence
/// that the provider actually worked on a generation may let a resumed launch
/// take over that generation's unanswered turn.
const PROVIDER_WORK_EVENTS: &[&str] = &[
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

fn is_provider_work_event(event: &str) -> bool {
    PROVIDER_WORK_EVENTS.contains(&event)
}


#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CursorEvidenceWait {
    InFlight,
    CompletedReceiptGrace,
}

impl CursorEvidenceWait {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::InFlight => "in_flight",
            Self::CompletedReceiptGrace => "completed_receipt_grace",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CursorProviderTurn {
    pub generation_id: String,
    pub launch_id: Option<String>,
    pub prompt: String,
    pub response_text: Option<String>,
    pub stop_status: Option<String>,
    pub stop_observed_at: Option<DateTime<Utc>>,
}

impl CursorProviderTurn {
    fn unsettled_reason(
        &self,
        session_ended: bool,
        now: DateTime<Utc>,
    ) -> Option<CursorEvidenceWait> {
        // afterAgentResponse is itself the provider's semantic commit receipt;
        // a separately dropped stop hook must not wedge raw archival.
        if self.response_text.is_some() {
            return None;
        }
        match self.stop_status.as_deref() {
            Some("completed") if session_ended => None,
            Some("completed")
                if self.stop_observed_at.is_some_and(|observed| {
                    now.signed_duration_since(observed) >= COMPLETED_RECEIPT_GRACE
                }) =>
            {
                None
            }
            Some("completed") => Some(CursorEvidenceWait::CompletedReceiptGrace),
            Some(_) => None,
            None if session_ended => None,
            None => Some(CursorEvidenceWait::InFlight),
        }
    }
}

/// A generation Cursor reported as failed. Kept separately from `turns`
/// because the failure that matters most arrives without a prompt receipt:
/// Cursor's own auto-continuations emit `afterAgentThought` and `stop(error)`
/// and never call `beforeSubmitPrompt`, so anchoring outcomes to prompts threw
/// the interesting ones away.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CursorTurnFailure {
    pub generation_id: String,
    pub launch_id: Option<String>,
    pub status: String,
    pub observed_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct CursorVisibilityEvidence {
    pub turns: Vec<CursorProviderTurn>,
    pub failures: Vec<CursorTurnFailure>,
    /// Generation of the newest terminal receipt, and whether it failed.
    /// A failure is worth telling the user about only while nothing has
    /// superseded it.
    pub latest_terminal: Option<(String, bool)>,
    /// Execution authority comes from the managed binding, not prompt history.
    pub current_launch_id: Option<String>,
    pub session_ended: bool,
    pub ambiguous: bool,
    pub first_activity_at: Option<DateTime<Utc>>,
    pub last_activity_at: Option<DateTime<Utc>>,
}

impl CursorVisibilityEvidence {
    /// The failure to surface, if the session's most recent turn is the one
    /// that failed. A later completed turn supersedes it, so the row clears
    /// itself without anyone tracking acknowledgement.
    pub(crate) fn unsuperseded_failure(&self) -> Option<&CursorTurnFailure> {
        let (generation_id, failed) = self.latest_terminal.as_ref()?;
        failed
            .then(|| {
                self.failures
                    .iter()
                    .find(|failure| {
                        &failure.generation_id == generation_id
                            && failure.launch_id == self.current_launch_id
                    })
            })
            .flatten()
    }
}

impl CursorVisibilityEvidence {
    pub(crate) fn unsettled_reason(&self) -> Option<CursorEvidenceWait> {
        self.unsettled_reason_at(Utc::now())
    }

    fn unsettled_reason_at(&self, now: DateTime<Utc>) -> Option<CursorEvidenceWait> {
        let turn = self.turns.last()?;
        if !self.session_ended
            && self.current_launch_id.is_some()
            && turn.launch_id != self.current_launch_id
            && turn.response_text.is_none()
        {
            // An old aborted/ended attempt cannot settle an unanswered turn
            // being resumed by the currently bound launch.
            return Some(CursorEvidenceWait::InFlight);
        }
        turn.unsettled_reason(self.session_ended, now)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum CursorProviderReceipt<'a> {
    Prompt(&'a str),
    Response(&'a str),
    Stop(&'a str),
}

fn receipt_events_path(root: &Path, session_id: &str) -> PathBuf {
    root.join("hook-events")
        .join(format!("{session_id}.ndjson"))
}

pub(crate) fn append_cursor_provider_receipt(
    root: &Path,
    session_id: &str,
    conversation_id: &str,
    generation_id: &str,
    launch_id: &str,
    source: &str,
    receipt: CursorProviderReceipt<'_>,
) -> Result<()> {
    let (event, payload) = match receipt {
        CursorProviderReceipt::Prompt(prompt) => (
            "beforeSubmitPrompt",
            json!({"generation_id": generation_id, "prompt": prompt}),
        ),
        CursorProviderReceipt::Response(text) => (
            "afterAgentResponse",
            json!({"generation_id": generation_id, "text": text}),
        ),
        CursorProviderReceipt::Stop(status) => (
            "stop",
            json!({"generation_id": generation_id, "status": status}),
        ),
    };
    let path = receipt_events_path(root, session_id);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let row = json!({
        "event": event,
        "observed_at": Utc::now().to_rfc3339(),
        "session_id": session_id,
        "conversation_id": conversation_id,
        "launch_id": launch_id,
        "source": source,
        "payload": payload,
    });
    let mut line = serde_json::to_vec(&row)?;
    line.push(b'\n');
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?
        .write_all(&line)?;
    Ok(())
}

pub(crate) fn has_cursor_prompt_receipt(
    root: &Path,
    session_id: &str,
    conversation_id: &str,
    generation_id: &str,
    launch_id: &str,
) -> bool {
    let Ok(contents) = fs::read_to_string(receipt_events_path(root, session_id)) else {
        return false;
    };
    contents.lines().any(|line| {
        serde_json::from_str::<Value>(line).ok().is_some_and(|row| {
            row.get("event").and_then(Value::as_str) == Some("beforeSubmitPrompt")
                && row.get("conversation_id").and_then(Value::as_str) == Some(conversation_id)
                && row.get("launch_id").and_then(Value::as_str) == Some(launch_id)
                && row
                    .get("payload")
                    .and_then(|payload| payload.get("generation_id"))
                    .and_then(Value::as_str)
                    == Some(generation_id)
        })
    })
}

/// Whether Cursor has a store for this conversation at all, however many
/// workspaces hold one. Distinct from `configured_cursor_store`, which needs a
/// single unambiguous path to wake; here the question is only whether an
/// authoritative source exists, so ambiguity still answers yes.
pub(crate) fn cursor_store_exists(conversation_id: &str) -> bool {
    !cursor_store_candidates(conversation_id).is_empty()
}

pub(crate) fn configured_cursor_store(conversation_id: &str) -> Option<PathBuf> {
    let candidates = cursor_store_candidates(conversation_id);
    let [store] = candidates.as_slice() else {
        return None;
    };
    Some(store.clone())
}

/// The provider's own words for why a turn failed.
///
/// Cursor's hook stream reports *that* a turn ended in error but never why; the
/// message only ever reaches the agent-transcripts projection, as a trailing
/// `{"type":"turn_ended","status":"error","error":"..."}` lifecycle line. That
/// file is not a transcript source — it is rejected as one — but a typed
/// lifecycle record in it is still evidence, and it is the only place the
/// string exists. Read the tail only, and answer `None` unless the newest
/// lifecycle line is itself the failure, so a later successful turn cannot
/// lend its predecessor an error message.
pub(crate) fn cursor_projection_turn_error(conversation_id: &str) -> Option<String> {
    let home = PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into()));
    let cursor_home = std::env::var_os("CURSOR_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".cursor"));
    cursor_projection_turn_error_in(&cursor_home, conversation_id)
}

fn cursor_projection_turn_error_in(cursor_home: &Path, conversation_id: &str) -> Option<String> {
    const TAIL_BYTES: u64 = 64 * 1024;
    let path = cursor_projection_candidates(cursor_home, conversation_id)
        .into_iter()
        .next()?;
    let mut file = fs::File::open(&path).ok()?;
    let length = file.metadata().ok()?.len();
    if length > TAIL_BYTES {
        use std::io::Seek;
        file.seek(std::io::SeekFrom::Start(length - TAIL_BYTES))
            .ok()?;
    }
    let mut contents = String::new();
    {
        use std::io::Read as _;
        file.read_to_string(&mut contents).ok()?;
    }
    let last_lifecycle = contents
        .lines()
        .rev()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find(|row| row.get("type").and_then(Value::as_str) == Some("turn_ended"))?;
    let status = last_lifecycle.get("status").and_then(Value::as_str)?;
    if !matches!(status, "error" | "aborted") {
        return None;
    }
    let message = last_lifecycle.get("error").and_then(Value::as_str)?.trim();
    // Cursor writes provider text here; keep it short enough to read as a
    // status line rather than a transcript.
    (!message.is_empty() && message.chars().count() <= 200).then(|| message.to_string())
}

fn cursor_projection_candidates(cursor_home: &Path, conversation_id: &str) -> Vec<PathBuf> {
    let Ok(projects) = fs::read_dir(cursor_home.join("projects")) else {
        return Vec::new();
    };
    projects
        .flatten()
        .flat_map(|project| {
            let transcripts = project.path().join("agent-transcripts");
            // Cursor has used both a flat file and a per-conversation
            // directory; accept either rather than guessing a version.
            [
                transcripts.join(format!("{conversation_id}.jsonl")),
                transcripts
                    .join(conversation_id)
                    .join(format!("{conversation_id}.jsonl")),
            ]
        })
        .filter(|path| path.is_file())
        .collect()
}

fn cursor_store_candidates(conversation_id: &str) -> Vec<PathBuf> {
    let home = PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into()));
    let cursor_home = std::env::var_os("CURSOR_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".cursor"));
    let xdg_config_home = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"));
    [
        xdg_config_home.join("cursor/chats"),
        cursor_home.join("chats"),
    ]
    .into_iter()
    .find_map(|root| {
        let stores = fs::read_dir(root)
            .ok()?
            .flatten()
            .map(|entry| entry.path().join(conversation_id).join("store.db"))
            .filter(|path| path.is_file())
            .collect::<Vec<_>>();
        (!stores.is_empty()).then_some(stores)
    })
    .unwrap_or_default()
}

#[cfg(unix)]
pub(crate) fn wake_cursor_transcript(
    session_id: &str,
    conversation_id: &str,
    generation_id: &str,
    run_id: Option<&str>,
    transcript_path: Option<&Path>,
) {
    // store.db is the render authority; Cursor's hook payload points at the
    // lossy agent-transcripts projection. Waking only the hook's path is how a
    // turn-completion wake kept refreshing the projection nobody should read
    // while the store went unrefreshed between scans. Wake the store first,
    // then the projection so its raw archive stays current too.
    let store = configured_cursor_store(conversation_id);
    let projection = transcript_path
        .filter(|path| path.is_file())
        .map(Path::to_path_buf)
        .filter(|path| store.as_deref() != Some(path.as_path()));
    let targets: Vec<PathBuf> = [store, projection].into_iter().flatten().collect();
    if targets.is_empty() {
        return;
    }
    let Ok(socket) = crate::config::get_agent_transcript_wake_socket_path() else {
        return;
    };
    for transcript in targets {
        // One failed connect must not cost the other target its wake.
        let Ok(mut stream) = UnixStream::connect(&socket) else {
            continue;
        };
        let _ = stream.set_write_timeout(Some(StdDuration::from_millis(75)));
        let payload = json!({
            "provider": "cursor",
            "path": transcript,
            "phase": "idle",
            "session_id": session_id,
            "run_id": run_id,
            "turn_id": generation_id,
            "provider_turn_id": conversation_id,
            "wake_reason": "turn_completed",
            "observed_at_ms": Utc::now().timestamp_millis(),
            "file_len_hint": transcript.metadata().ok().map(|metadata| metadata.len()),
        });
        let _ = stream.write_all(payload.to_string().as_bytes());
    }
}

pub(crate) fn load_cursor_visibility_evidence(
    session_id: &str,
    conversation_id: &str,
) -> Result<Option<CursorVisibilityEvidence>> {
    load_cursor_visibility_evidence_in(
        &crate::config::get_longhouse_home()?.join("managed-local/cursor-helm"),
        session_id,
        conversation_id,
    )
}

fn load_cursor_visibility_evidence_in(
    root: &Path,
    session_id: &str,
    conversation_id: &str,
) -> Result<Option<CursorVisibilityEvidence>> {
    let contents = match fs::read_to_string(receipt_events_path(root, session_id)) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error).context("read Cursor provider receipt journal"),
    };
    // Resume replaces this claim before starting the provider. The last prompt
    // can still belong to the previous launch and is not lifecycle authority.
    let claim = fs::read(root.join("binding-probes").join(format!("{session_id}.json")))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .filter(|claim| {
            claim.get("session_id").and_then(Value::as_str) == Some(session_id)
                && claim.get("conversation_uuid").and_then(Value::as_str) == Some(conversation_id)
        });
    let current_launch_id = claim.as_ref()
        .and_then(|claim| claim.get("launch_id"))
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty());
    let mut evidence = parse_cursor_visibility_evidence(&contents, conversation_id, current_launch_id)?;
    evidence.session_ended |= session_lifecycle_ended(root, session_id, conversation_id, current_launch_id);
    Ok(Some(evidence))
}

fn session_lifecycle_ended(
    root: &Path,
    session_id: &str,
    conversation_id: &str,
    current_launch_id: Option<&str>,
) -> bool {
    let Some(current_launch_id) = current_launch_id else {
        return false;
    };
    fs::read(root.join(format!("{session_id}.phase.json")))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|phase| {
            phase.get("session_id").and_then(Value::as_str) == Some(session_id)
                && phase.get("conversation_id").and_then(Value::as_str) == Some(conversation_id)
                && phase.get("launch_id").and_then(Value::as_str) == Some(current_launch_id)
                && phase.get("phase").and_then(Value::as_str) == Some("ended")
        })
}

pub(crate) fn parse_cursor_visibility_evidence(
    contents: &str,
    conversation_id: &str,
    current_launch_id: Option<&str>,
) -> Result<CursorVisibilityEvidence> {
    let current_launch_id = current_launch_id.map(str::trim).filter(|id| !id.is_empty());
    let mut turns = Vec::<CursorProviderTurn>::new();
    let mut failures = Vec::<CursorTurnFailure>::new();
    let mut latest_terminal: Option<(String, bool)> = None;
    let mut failure_indices = HashMap::<String, HashMap<String, usize>>::new();
    let mut indices = HashMap::<String, HashMap<String, usize>>::new();
    let mut session_ended_for_current_launch = false;
    let mut ambiguous = false;
    let mut first_activity_at = None;
    let mut last_activity_at = None;
    for (line_index, line) in contents.lines().enumerate() {
        let row: Value = match serde_json::from_str(line) {
            Ok(row) => row,
            Err(_) => continue,
        };
        if row.get("conversation_id").and_then(Value::as_str) != Some(conversation_id) {
            continue;
        }
        let event = row.get("event").and_then(Value::as_str).unwrap_or_default();
        let provider_work = is_provider_work_event(event);
        let row_launch_id = row
            .get("launch_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty());
        let is_current_launch = current_launch_id
            .zip(row_launch_id)
            .is_some_and(|(current, observed)| current == observed);
        if event == "sessionEnd" {
            // Late history cannot settle, or unset an end for, the live binding.
            session_ended_for_current_launch |= is_current_launch;
            continue;
        }
        let payload = row.get("payload").and_then(Value::as_object);
        let generation_id = payload
            .and_then(|payload| payload.get("generation_id"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim();
        if generation_id.is_empty() {
            continue;
        }
        let observed_at = row
            .get("observed_at")
            .and_then(Value::as_str)
            .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
            .map(|value| value.with_timezone(&Utc));
        if event == "beforeSubmitPrompt" {
            let prompt = payload
                .and_then(|payload| payload.get("prompt"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .trim();
            if prompt.is_empty() {
                continue;
            }
            // Cursor emits lifecycle receipts for local TUI commands too.
            // They change provider state but are not model turns and have no
            // corresponding message in the Cursor store. Keeping them out of
            // the turn sequence prevents teardown/reset commands from making
            // an otherwise unique transcript alignment look ambiguous.
            if matches!(
                prompt,
                "/exit" | "/clear" | "/new" | "/new-chat" | "/newchat"
            ) {
                continue;
            }
            if let Some(index) = indices.get(generation_id)
                .and_then(|launches| launches.get(row_launch_id.unwrap_or_default())).copied()
            {
                ambiguous |= turns.get(index).is_some_and(|turn| turn.prompt != prompt);
                continue;
            }
            if is_current_launch {
                session_ended_for_current_launch = false;
                latest_terminal = None;
            }
            indices.entry(generation_id.to_owned()).or_default()
                .insert(row_launch_id.unwrap_or_default().to_owned(), turns.len());
            turns.push(CursorProviderTurn {
                generation_id: generation_id.to_string(),
                launch_id: row_launch_id.map(str::to_owned),
                prompt: prompt.to_string(),
                response_text: None,
                stop_status: None,
                stop_observed_at: None,
            });
            if let Some(observed_at) = observed_at {
                first_activity_at = Some(
                    first_activity_at
                        .map_or(observed_at, |first: DateTime<Utc>| first.min(observed_at)),
                );
                last_activity_at = Some(
                    last_activity_at
                        .map_or(observed_at, |last: DateTime<Utc>| last.max(observed_at)),
                );
            }
            continue;
        }
        // Outcome first: a terminal failure is evidence about the session even
        // when its generation never produced a prompt receipt, and the lookup
        // below would otherwise drop the whole generation on the floor.
        // A committed response is itself a successful terminal receipt, so a
        // turn that answers after a failed one supersedes it even when its own
        // stop hook is dropped. A late callback from an older launch must not
        // become the current launch's terminal receipt.
        // A missing launch id is not correlation. Keep the receipt as
        // historical evidence, but never let it become current authority.
        if event == "afterAgentResponse" && is_current_launch {
            latest_terminal = Some((generation_id.to_string(), false));
        }
        if event == "stop" {
            if let Some(status) = payload
                .and_then(|payload| payload.get("status"))
                .and_then(Value::as_str)
                .map(str::trim)
            {
                if is_current_launch {
                    latest_terminal = Some((
                        generation_id.to_string(),
                        matches!(status, "error" | "aborted"),
                    ));
                }
            }
            if let Some(status) = payload
                .and_then(|payload| payload.get("status"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|status| matches!(*status, "error" | "aborted"))
            {
                match failure_indices.get(generation_id)
                    .and_then(|launches| launches.get(row_launch_id.unwrap_or_default())).copied()
                {
                    Some(index) => {
                        if let Some(existing) = failures.get_mut(index) {
                            existing.status = status.to_string();
                            existing.observed_at = observed_at.or(existing.observed_at);
                        }
                    }
                    None => {
                        failure_indices.entry(generation_id.to_owned()).or_default()
                            .insert(row_launch_id.unwrap_or_default().to_owned(), failures.len());
                        failures.push(CursorTurnFailure {
                            generation_id: generation_id.to_string(),
                            launch_id: row_launch_id.map(str::to_owned),
                            status: status.to_string(),
                            observed_at,
                        });
                    }
                }
            }
        }
        let latest_turn_index = turns.len().checked_sub(1);
        let index = indices.get(generation_id)
            .and_then(|launches| launches.get(row_launch_id.unwrap_or_default())).copied()
            .or_else(|| {
                // Resume may continue the exact unanswered provider generation
                // without a new prompt. Only provider work from the bound launch
                // can take it over; a lifecycle event, an unrelated generation,
                // or an answered historical turn cannot.
                let index = latest_turn_index?;
                let turn = &turns[index];
                (provider_work
                    && is_current_launch
                    && turn.generation_id == generation_id
                    && turn.response_text.is_none()
                    && turn.launch_id.is_some())
                    .then_some(index)
            });
        let Some(index) = index else { continue; };
        let turn = turns.get_mut(index).with_context(|| {
            format!("Cursor hook turn index was invalid at evidence line {}", line_index + 1)
        })?;
        let turn_launch_matches = turn.launch_id.as_deref().zip(row_launch_id)
            .is_some_and(|(expected, observed)| expected == observed);
        if !turn_launch_matches {
            if !is_current_launch || Some(index) != latest_turn_index
                || turn.response_text.is_some() || turn.launch_id.is_none()
            {
                continue;
            }
            turn.launch_id = row_launch_id.map(str::to_owned);
            turn.stop_status = None;
            turn.stop_observed_at = None;
            indices.entry(generation_id.to_owned()).or_default()
                .insert(row_launch_id.unwrap_or_default().to_owned(), index);
        }
        // Only provider work belonging to an accepted prompt advances activity.
        // Session teardown, binding refresh and local commands are not turns.
        if matches!(
            event,
            "afterAgentResponse"
                | "afterAgentThought"
                | "preToolUse"
                | "postToolUse"
                | "postToolUseFailure"
                | "stop"
        ) {
            if let Some(observed_at) = observed_at {
                first_activity_at =
                    Some(first_activity_at.map_or(observed_at, |first| first.min(observed_at)));
                last_activity_at =
                    Some(last_activity_at.map_or(observed_at, |last| last.max(observed_at)));
            }
        }
        match event {
            "afterAgentResponse" => {
                let response_text = payload
                    .and_then(|payload| payload.get("text"))
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                if let (Some(existing), Some(next)) = (&turn.response_text, &response_text) {
                    ambiguous |= existing != next;
                } else if turn.response_text.is_none() {
                    turn.response_text = response_text;
                }
            }
            "stop" => {
                let stop_status = payload
                    .and_then(|payload| payload.get("status"))
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                if stop_status.is_some() {
                    turn.stop_status = stop_status;
                    turn.stop_observed_at = row
                        .get("observed_at")
                        .and_then(Value::as_str)
                        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
                        .map(|value| value.with_timezone(&Utc));
                }
            }
            _ => {}
        }
    }
    Ok(CursorVisibilityEvidence {
        turns,
        failures,
        latest_terminal,
        current_launch_id: current_launch_id.map(str::to_owned),
        session_ended: session_ended_for_current_launch,
        ambiguous,
        first_activity_at,
        last_activity_at,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The failure that matters most has no prompt receipt: Cursor's own
    /// auto-continuation runs a generation that emits thoughts and then
    /// `stop(error)` without ever calling `beforeSubmitPrompt`. Anchoring
    /// outcomes to prompts dropped exactly those, so the phone showed a
    /// session that quietly stopped talking while the terminal showed an error.
    #[test]
    fn failed_generations_survive_without_a_prompt_receipt() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","observed_at":"2026-09-09T20:50:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"human","prompt":"build it"}}
{"event":"afterAgentResponse","observed_at":"2026-09-09T20:56:50Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"human","text":"done"}}
{"event":"stop","observed_at":"2026-09-09T20:56:50Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"human","status":"completed"}}
{"event":"afterAgentThought","observed_at":"2026-09-09T20:57:45Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"continuation","text":"thinking"}}
{"event":"stop","observed_at":"2026-09-09T20:57:46Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"continuation","status":"error"}}
{"event":"stop","observed_at":"2026-09-09T20:57:46Z","conversation_id":"other","payload":{"generation_id":"elsewhere","status":"error"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();

        assert_eq!(evidence.turns.len(), 1);
        assert_eq!(evidence.failures.len(), 1);
        let failure = &evidence.failures[0];
        assert_eq!(failure.generation_id, "continuation");
        assert_eq!(failure.status, "error");
        assert_eq!(
            failure.observed_at.map(|value| value.to_rfc3339()),
            Some("2026-09-09T20:57:46+00:00".to_string())
        );
        // A completed turn is not a failure, and another conversation's
        // failure is not this session's.
        assert!(!evidence
            .failures
            .iter()
            .any(|entry| entry.generation_id == "human" || entry.generation_id == "elsewhere"));
        // Nothing has answered since, so this failure is the session's outcome.
        assert_eq!(
            evidence
                .unsuperseded_failure()
                .map(|entry| entry.generation_id.as_str()),
            Some("continuation")
        );
    }

    /// A turn that answers after a failed one is the session's outcome now, so
    /// the failure row clears itself.
    #[test]
    fn a_later_completed_turn_supersedes_an_earlier_failure() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"stop","observed_at":"2026-09-09T20:57:46Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"failed","status":"error"}}
{"event":"beforeSubmitPrompt","observed_at":"2026-09-09T21:10:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"next","prompt":"try again"}}
{"event":"afterAgentResponse","observed_at":"2026-09-09T21:11:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"next","text":"fixed"}}
{"event":"stop","observed_at":"2026-09-09T21:11:01Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"next","status":"completed"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();

        assert_eq!(evidence.failures.len(), 1);
        assert!(evidence.unsuperseded_failure().is_none());
    }

    /// A dropped stop hook must not resurrect a superseded failure: a committed
    /// response is itself a successful terminal receipt.
    #[test]
    fn a_committed_response_supersedes_without_its_stop_hook() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"stop","observed_at":"2026-09-09T20:57:46Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"failed","status":"error"}}
{"event":"beforeSubmitPrompt","observed_at":"2026-09-09T21:10:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"next","prompt":"try again"}}
{"event":"afterAgentResponse","observed_at":"2026-09-09T21:11:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"next","text":"fixed"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();

        assert!(evidence.unsuperseded_failure().is_none());
    }

    #[test]
    fn duplicate_stop_receipts_report_one_failure() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"stop","observed_at":"2026-09-09T20:57:46Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g","status":"error"}}
{"event":"stop","observed_at":"2026-09-09T20:57:46Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g","status":"error"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert_eq!(evidence.failures.len(), 1);
    }

    #[test]
    fn activity_tracks_provider_work_not_teardown_or_unrelated_receipts() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","observed_at":"2026-07-20T14:00:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"postToolUse","observed_at":"2026-07-20T14:01:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1"}}
{"event":"afterAgentResponse","observed_at":"2026-07-20T14:02:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","text":"done"}}
{"event":"stop","observed_at":"2026-07-20T14:02:01Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"completed"}}
{"event":"stop","observed_at":"2026-09-04T16:51:00Z","conversation_id":"other","payload":{"generation_id":"g1","status":"completed"}}
{"event":"stop","observed_at":"2026-09-04T16:51:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"unknown","status":"completed"}}
{"event":"beforeSubmitPrompt","observed_at":"2026-09-04T16:51:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"exit","prompt":"/exit"}}
{"event":"stop","observed_at":"2026-09-04T16:51:01Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"exit","status":"completed"}}
{"event":"sessionEnd","observed_at":"2026-09-04T16:51:02Z","conversation_id":"conversation","launch_id":"launch","payload":{}}"#,
            "conversation",
            Some("launch"),
        ).unwrap();
        let (started_at, last_activity_at) =
            crate::cursor_store::cursor_session_timestamps(None, None, Some(&evidence)).unwrap();
        assert_eq!(started_at.to_rfc3339(), "2026-07-20T14:00:00+00:00");
        assert_eq!(last_activity_at.to_rfc3339(), "2026-07-20T14:02:01+00:00");

        let running = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","observed_at":"2026-07-23T10:00:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2","prompt":"keep working"}}
{"event":"postToolUse","observed_at":"2026-07-23T10:05:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2"}}"#,
            "conversation",
            Some("launch"),
        ).unwrap();
        let (_, progressed_at) =
            crate::cursor_store::cursor_session_timestamps(None, None, Some(&running)).unwrap();
        assert_eq!(progressed_at.to_rfc3339(), "2026-07-23T10:05:00+00:00");
        assert_eq!(
            running.unsettled_reason(),
            Some(CursorEvidenceWait::InFlight)
        );
    }

    #[test]
    fn completed_turn_waits_for_response_when_stop_arrives_first() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"stop","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"completed"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert_eq!(
            evidence.unsettled_reason(),
            Some(CursorEvidenceWait::CompletedReceiptGrace)
        );

        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"stop","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"completed"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","text":"world"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert_eq!(evidence.unsettled_reason(), None);
        assert_eq!(evidence.turns[0].response_text.as_deref(), Some("world"));
    }

    #[test]
    fn failed_turn_without_response_is_settled_raw_only_evidence() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"stop","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"error"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert_eq!(evidence.unsettled_reason(), None);
        assert_eq!(evidence.turns[0].response_text, None);
    }

    #[test]
    fn duplicate_hooks_and_other_conversations_do_not_duplicate_turns() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"other","payload":{"generation_id":"g0","prompt":"ignore"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","text":"world"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","text":"world"}}
{"event":"stop","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"completed"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert_eq!(evidence.turns.len(), 1);
        assert_eq!(evidence.turns[0].response_text.as_deref(), Some("world"));
    }

    #[test]
    fn local_cursor_commands_do_not_shift_provider_turn_alignment() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","text":"world"}}
{"event":"stop","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"completed"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2","prompt":"/exit"}}
{"event":"stop","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2","status":"completed"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert!(!evidence.ambiguous);
        assert_eq!(evidence.turns.len(), 1);
        assert_eq!(evidence.turns[0].prompt, "hello");
        assert_eq!(evidence.turns[0].response_text.as_deref(), Some("world"));
    }

    #[test]
    fn an_old_incomplete_turn_does_not_block_a_later_settled_turn() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"crashed"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2","prompt":"recovered"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2","text":"done"}}
{"event":"stop","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2","status":"completed"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn interrupt_stop_transition_does_not_hide_a_later_recovery() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"sleep"}}
{"event":"stop","observed_at":"2026-07-21T12:00:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"aborted"}}
{"event":"stop","observed_at":"2026-07-21T12:00:01Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"error"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2","prompt":"recover"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2","text":"done"}}
{"event":"stop","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g2","status":"completed"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert!(!evidence.ambiguous);
        assert_eq!(evidence.turns[0].stop_status.as_deref(), Some("error"));
        assert_eq!(evidence.turns[1].response_text.as_deref(), Some("done"));
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn response_receipt_settles_when_stop_hook_is_missing() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","text":"world"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn completed_turn_without_receipt_degrades_to_raw_only_after_grace() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"stop","observed_at":"2026-07-21T12:00:00Z","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"completed"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert_eq!(
            evidence.unsettled_reason_at(
                DateTime::parse_from_rfc3339("2026-07-21T12:00:31Z")
                    .unwrap()
                    .with_timezone(&Utc)
            ),
            None
        );
    }

    #[test]
    fn session_end_settles_incomplete_turn_raw_only() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"sessionEnd","conversation_id":"conversation","launch_id":"launch","payload":{}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert!(evidence.session_ended);
        assert_eq!(evidence.current_launch_id.as_deref(), Some("launch"));
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn session_end_without_launch_identity_does_not_settle() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"sessionEnd","conversation_id":"conversation","payload":{}}"#,
            "conversation",
            None,
        )
        .unwrap();
        assert!(!evidence.session_ended);
        assert_eq!(evidence.current_launch_id, None);
        assert_eq!(
            evidence.unsettled_reason(),
            Some(CursorEvidenceWait::InFlight)
        );
    }

    #[test]
    fn conflicting_duplicate_receipts_are_ambiguous() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"hello"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","text":"first"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","text":"second"}}"#,
            "conversation",
            Some("launch"),
        )
        .unwrap();
        assert!(evidence.ambiguous);
    }

    #[test]
    fn console_provider_receipts_form_one_settled_idempotent_turn() {
        let root = tempfile::tempdir().unwrap();
        for _ in 0..2 {
            append_cursor_provider_receipt(
                root.path(),
                "session",
                "conversation",
                "run-1",
                "launch-1",
                "cursor_print",
                CursorProviderReceipt::Prompt("hello"),
            )
            .unwrap();
            append_cursor_provider_receipt(
                root.path(),
                "session",
                "conversation",
                "run-1",
                "launch-1",
                "cursor_print",
                CursorProviderReceipt::Response("world"),
            )
            .unwrap();
            append_cursor_provider_receipt(
                root.path(),
                "session",
                "conversation",
                "run-1",
                "launch-1",
                "cursor_print",
                CursorProviderReceipt::Stop("completed"),
            )
            .unwrap();
        }
        let contents = fs::read_to_string(receipt_events_path(root.path(), "session")).unwrap();
        let evidence =
            parse_cursor_visibility_evidence(&contents, "conversation", Some("launch-1")).unwrap();
        assert_eq!(evidence.turns.len(), 1);
        assert_eq!(evidence.turns[0].prompt, "hello");
        assert_eq!(evidence.turns[0].response_text.as_deref(), Some("world"));
        assert_eq!(evidence.turns[0].stop_status.as_deref(), Some("completed"));
        assert!(!evidence.ambiguous);
        assert_eq!(evidence.unsettled_reason(), None);
    }

    #[test]
    fn old_ended_launch_does_not_settle_new_launch() {
        let contents = r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"old-generation","prompt":"old"}}
{"event":"stop","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"old-generation","status":"error"}}
{"event":"sessionEnd","conversation_id":"conversation","launch_id":"old-launch","payload":{}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"new-launch","payload":{"generation_id":"new-generation","prompt":"new"}}
{"event":"stop","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"old-generation","status":"error"}}"#;
        let evidence =
            parse_cursor_visibility_evidence(contents, "conversation", Some("new-launch")).unwrap();
        assert_eq!(evidence.turns.len(), 2);
        assert_eq!(evidence.turns[0].generation_id, "old-generation");
        assert_eq!(evidence.turns[1].generation_id, "new-generation");
        assert!(evidence.unsuperseded_failure().is_none());
        assert!(!evidence.session_ended);
        assert_eq!(
            evidence.unsettled_reason_at(
                DateTime::parse_from_rfc3339("2026-09-21T00:00:00Z")
                    .unwrap()
                    .with_timezone(&Utc),
            ),
            Some(CursorEvidenceWait::InFlight),
        );
    }

    #[test]
    fn response_from_another_launch_does_not_enter_current_turn() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"new-launch","payload":{"generation_id":"generation","prompt":"new"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"generation","text":"old answer"}}"#,
            "conversation",
            Some("new-launch"),
        )
        .unwrap();
        assert_eq!(evidence.turns.len(), 1);
        assert_eq!(evidence.turns[0].response_text, None);
        assert_eq!(evidence.latest_terminal, None);
        assert_eq!(
            evidence.unsettled_reason(),
            Some(CursorEvidenceWait::InFlight)
        );
    }

    #[test]
    fn current_session_end_survives_a_late_end_from_the_old_launch() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"old-generation","prompt":"old"}}
{"event":"sessionEnd","conversation_id":"conversation","launch_id":"old-launch","payload":{}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"new-launch","payload":{"generation_id":"new-generation","prompt":"new"}}
{"event":"sessionEnd","conversation_id":"conversation","launch_id":"new-launch","payload":{}}
{"event":"sessionEnd","conversation_id":"conversation","launch_id":"old-launch","payload":{}}"#,
            "conversation",
            Some("new-launch"),
        )
        .unwrap();
        assert!(evidence.session_ended);
        assert_eq!(
            evidence.current_launch_id.as_deref(),
            Some("new-launch")
        );
        assert_eq!(evidence.unsettled_reason(), None);
    }

    /// The launcher's phase file is lifecycle evidence only for the launch it
    /// names: another launch's phase cannot settle the current one, and a
    /// missing binding is not terminal evidence at all.
    #[test]
    fn phase_file_settles_only_the_launch_it_names() {
        let root = tempfile::tempdir().unwrap();
        fs::write(
            root.path().join("session.phase.json"),
            r#"{"session_id":"session","conversation_id":"conversation","launch_id":"old-launch","phase":"ended"}"#,
        )
        .unwrap();
        assert!(!session_lifecycle_ended(
            root.path(),
            "session",
            "conversation",
            Some("new-launch"),
        ));

        fs::write(
            root.path().join("session.phase.json"),
            r#"{"session_id":"session","conversation_id":"conversation","launch_id":"new-launch","phase":"ended"}"#,
        )
        .unwrap();
        assert!(session_lifecycle_ended(
            root.path(),
            "session",
            "conversation",
            Some("new-launch"),
        ));
        assert!(!session_lifecycle_ended(
            root.path(),
            "session",
            "conversation",
            None,
        ));
    }

    /// A resumed launch may continue the exact unanswered generation without
    /// emitting a new prompt, so its response adopts that turn.
    #[test]
    fn resume_without_a_new_prompt_adopts_the_unanswered_turn() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"g1","prompt":"work"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"new-launch","payload":{"generation_id":"g1","text":"resumed answer"}}
{"event":"stop","conversation_id":"conversation","launch_id":"new-launch","payload":{"generation_id":"g1","status":"completed"}}"#,
            "conversation",
            Some("new-launch"),
        )
        .unwrap();
        assert_eq!(evidence.turns.len(), 1);
        assert_eq!(evidence.turns[0].launch_id.as_deref(), Some("new-launch"));
        assert_eq!(
            evidence.turns[0].response_text.as_deref(),
            Some("resumed answer")
        );
        assert_eq!(evidence.unsettled_reason(), None);
    }

    /// A generation id is unique only within a launch: the same id under a
    /// later launch is a second attempt, not a duplicate of the first.
    #[test]
    fn a_generation_id_reused_by_a_later_launch_is_a_second_turn() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"g1","prompt":"first attempt"}}
{"event":"stop","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"g1","status":"error"}}
{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"new-launch","payload":{"generation_id":"g1","prompt":"second attempt"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"new-launch","payload":{"generation_id":"g1","text":"second answer"}}"#,
            "conversation",
            Some("new-launch"),
        )
        .unwrap();
        assert_eq!(evidence.turns.len(), 2);
        assert_eq!(evidence.turns[0].prompt, "first attempt");
        assert_eq!(evidence.turns[1].prompt, "second attempt");
        assert_eq!(
            evidence.turns[1].response_text.as_deref(),
            Some("second answer")
        );
        assert!(!evidence.ambiguous);
    }

    /// Once the bound launch has adopted the turn, a late callback carrying the
    /// old launch id is history: it can neither rewrite the answer nor become
    /// the session's terminal state.
    #[test]
    fn a_late_old_launch_callback_cannot_mutate_the_adopted_turn() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"g1","prompt":"work"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"new-launch","payload":{"generation_id":"g1","text":"resumed answer"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"g1","text":"stale answer"}}
{"event":"stop","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"g1","status":"error"}}"#,
            "conversation",
            Some("new-launch"),
        )
        .unwrap();
        assert_eq!(
            evidence.turns[0].response_text.as_deref(),
            Some("resumed answer")
        );
        assert_eq!(evidence.turns[0].stop_status, None);
        assert!(evidence.unsuperseded_failure().is_none());
    }

    /// Without a current launch binding there is no execution authority: the
    /// journal stays readable history, but nothing in it is a terminal verdict.
    #[test]
    fn missing_current_authority_never_authorizes_terminal_state() {
        let evidence = parse_cursor_visibility_evidence(
            r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","prompt":"work"}}
{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","text":"answer"}}
{"event":"stop","conversation_id":"conversation","launch_id":"launch","payload":{"generation_id":"g1","status":"error"}}
{"event":"sessionEnd","conversation_id":"conversation","launch_id":"launch","payload":{}}"#,
            "conversation",
            None,
        )
        .unwrap();
        assert_eq!(evidence.current_launch_id, None);
        assert!(evidence.latest_terminal.is_none());
        assert!(evidence.unsuperseded_failure().is_none());
        assert!(!evidence.session_ended);
    }
    /// The load path is the authority boundary: the binding claim supplies the
    /// current launch, and a claim naming another conversation is not authority.
    #[test]
    fn load_takes_current_launch_authority_from_the_binding_claim() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir_all(root.path().join("binding-probes")).unwrap();
        fs::create_dir_all(root.path().join("hook-events")).unwrap();
        fs::write(
            root.path().join("binding-probes/session.json"),
            r#"{"session_id":"session","conversation_uuid":"conversation","launch_id":"new-launch","run_id":"run"}"#,
        )
        .unwrap();
        fs::write(
            receipt_events_path(root.path(), "session"),
            concat!(
                r#"{"event":"beforeSubmitPrompt","conversation_id":"conversation","launch_id":"old-launch","payload":{"generation_id":"g1","prompt":"work"}}"#,
                "\n",
                r#"{"event":"afterAgentResponse","conversation_id":"conversation","launch_id":"new-launch","payload":{"generation_id":"g1","text":"resumed answer"}}"#,
                "\n",
                r#"{"event":"sessionEnd","conversation_id":"conversation","launch_id":"old-launch","payload":{}}"#,
                "\n",
            ),
        )
        .unwrap();

        let evidence = load_cursor_visibility_evidence_in(root.path(), "session", "conversation")
            .unwrap()
            .expect("a receipt journal exists");
        assert_eq!(evidence.current_launch_id.as_deref(), Some("new-launch"));
        assert_eq!(
            evidence.turns[0].response_text.as_deref(),
            Some("resumed answer")
        );
        assert!(!evidence.session_ended);
        assert_eq!(evidence.unsettled_reason(), None);

        // A claim naming another conversation is not execution authority.
        fs::write(
            root.path().join("binding-probes/session.json"),
            r#"{"session_id":"session","conversation_uuid":"other","launch_id":"new-launch","run_id":"run"}"#,
        )
        .unwrap();
        let evidence = load_cursor_visibility_evidence_in(root.path(), "session", "conversation")
            .unwrap()
            .expect("a receipt journal exists");
        assert_eq!(evidence.current_launch_id, None);
        assert_eq!(evidence.turns[0].response_text, None);
    }
}

#[cfg(test)]
mod projection_error_tests {
    use super::*;

    fn write_projection(cursor_home: &Path, conversation_id: &str, lines: &[&str]) {
        let dir = cursor_home
            .join("projects/Users-davidrose-git-zeta/agent-transcripts")
            .join(conversation_id);
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join(format!("{conversation_id}.jsonl")),
            lines.join("\n"),
        )
        .unwrap();
    }

    /// Cursor's hook stream says a turn failed; the error string exists only in
    /// the projection, as a trailing lifecycle line.
    #[test]
    fn the_newest_lifecycle_failure_supplies_its_message() {
        let dir = tempfile::tempdir().unwrap();
        let conversation = "f4fe32b7-4986-478d-b715-c1688f659046";
        write_projection(
            dir.path(),
            conversation,
            &[
                r#"{"role":"assistant","message":{"content":"done"}}"#,
                r#"{"type":"turn_ended","status":"error","error":"WritableIterable is closed"}"#,
            ],
        );

        assert_eq!(
            cursor_projection_turn_error_in(dir.path(), conversation).as_deref(),
            Some("WritableIterable is closed")
        );
    }

    /// A turn that succeeded after the failure must not lend its predecessor an
    /// error message.
    #[test]
    fn a_later_success_withholds_the_message() {
        let dir = tempfile::tempdir().unwrap();
        let conversation = "aaaaaaaa-0000-0000-0000-000000000001";
        write_projection(
            dir.path(),
            conversation,
            &[
                r#"{"type":"turn_ended","status":"error","error":"WritableIterable is closed"}"#,
                r#"{"type":"turn_ended","status":"success"}"#,
            ],
        );

        assert_eq!(
            cursor_projection_turn_error_in(dir.path(), conversation),
            None
        );
    }

    #[test]
    fn an_absent_projection_is_not_an_error() {
        let dir = tempfile::tempdir().unwrap();

        assert_eq!(
            cursor_projection_turn_error_in(dir.path(), "aaaaaaaa-0000-0000-0000-000000000002"),
            None
        );
    }

    /// Cursor has used both a flat file and a per-conversation directory.
    #[test]
    fn a_flat_projection_file_is_found_too() {
        let dir = tempfile::tempdir().unwrap();
        let conversation = "aaaaaaaa-0000-0000-0000-000000000003";
        let transcripts = dir
            .path()
            .join("projects/Users-davidrose-git-zeta/agent-transcripts");
        fs::create_dir_all(&transcripts).unwrap();
        fs::write(
            transcripts.join(format!("{conversation}.jsonl")),
            r#"{"type":"turn_ended","status":"aborted","error":"interrupted"}"#,
        )
        .unwrap();

        assert_eq!(
            cursor_projection_turn_error_in(dir.path(), conversation).as_deref(),
            Some("interrupted")
        );
    }

    /// Provider text, not a transcript: an essay in the error field is not a
    /// status line.
    #[test]
    fn an_oversized_message_is_declined() {
        let dir = tempfile::tempdir().unwrap();
        let conversation = "aaaaaaaa-0000-0000-0000-000000000004";
        let long = "x".repeat(500);
        write_projection(
            dir.path(),
            conversation,
            &[&format!(
                r#"{{"type":"turn_ended","status":"error","error":"{long}"}}"#
            )],
        );

        assert_eq!(
            cursor_projection_turn_error_in(dir.path(), conversation),
            None
        );
    }
}
