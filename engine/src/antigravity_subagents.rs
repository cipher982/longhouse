//! Antigravity subagent lineage, read from the parent's own record of it.
//!
//! When an Antigravity conversation spawns a worker (`invoke_subagent`, or a
//! `swarm` member), the CLI writes one file into the PARENT's brain directory:
//!
//! `brain/<parent>/.system_generated/subagents/<child>.json`
//!
//! ```json
//! {"conversationId":"<child>",
//!  "subagentDescriptor":{"typeName":"self","role":"Compute & Skills Doc Auditor"},
//!  "state":"SUBAGENT_STATE_ALIVE","spawnStepIndex":6211,"workspaceUris":["file:///..."]}
//! ```
//!
//! `typeName` is `self` for an `invoke_subagent` child and `worker` for a swarm
//! member; `state` turns `SUBAGENT_STATE_KILLED` when a swarm member is retired.
//! The file lands before the child's own transcript directory exists (measured
//! 0.45 s ahead on agy 1.5.x), and on a 114-conversation store it named exactly
//! the 64 conversations that open with a system message and none of the 50 that
//! open with user input. It is the only place the parent is named reliably: the
//! child's first record carries `sender=<id>`, but for a swarm member that is
//! whichever peer posted the board message that woke it, not the spawner.
//!
//! `spawnStepIndex` is not a key: siblings from one batch share it (six swarm
//! members carried 91), and its offset to the spawning planner step varies.
//! The child cannot name its spawning tool call from here. That binding still
//! comes from the parent's `delegation.spawn` facts.

use std::path::{Path, PathBuf};

use serde_json::Value;
use uuid::Uuid;

/// A sidecar is a few hundred bytes; anything larger is not one.
const MAX_SIDECAR_BYTES: u64 = 64 * 1024;

/// What the parent's sidecar says about one child conversation.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct AntigravityChild {
    /// The spawning conversation's id (its brain directory name).
    pub parent_conversation_id: String,
    /// `self` (`invoke_subagent`) or `worker` (swarm member).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub type_name: Option<String>,
    /// The role the parent gave the child ("Compute & Skills Doc Auditor").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// `SUBAGENT_STATE_ALIVE` or `SUBAGENT_STATE_KILLED`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub state: Option<String>,
}

impl AntigravityChild {
    /// The provider retired this child. An `invoke_subagent` child stays
    /// `ALIVE` after it answers, so only `KILLED` is a terminal statement.
    pub fn killed(&self) -> bool {
        self.state.as_deref() == Some("SUBAGENT_STATE_KILLED")
    }

    /// The agent-type label Longhouse shows: a swarm member says so; an
    /// `invoke_subagent` child is the plain case and carries no badge.
    pub fn agent_type(&self) -> Option<&'static str> {
        match self.type_name.as_deref() {
            Some("worker") => Some("swarm"),
            _ => None,
        }
    }
}

/// `(brain dir, conversation id)` for a transcript at
/// `brain/<id>/.system_generated/logs/<file>`.
fn brain_and_conversation(transcript: &Path) -> Option<(PathBuf, String)> {
    let logs = transcript
        .parent()
        .filter(|dir| dir.file_name().is_some_and(|name| name == "logs"))?;
    let system = logs.parent().filter(|dir| {
        dir.file_name()
            .is_some_and(|name| name == ".system_generated")
    })?;
    let conversation = system.parent()?;
    let id = conversation
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|id| Uuid::parse_str(id).is_ok())?;
    let brain = conversation
        .parent()
        .filter(|dir| dir.file_name().is_some_and(|name| name == "brain"))?;
    Some((brain.to_path_buf(), id.to_string()))
}

fn read_sidecar(path: &Path, child: &str, parent: &str) -> Option<AntigravityChild> {
    let meta = std::fs::metadata(path).ok()?;
    if !meta.is_file() || meta.len() > MAX_SIDECAR_BYTES {
        return None;
    }
    let value: Value = serde_json::from_slice(&std::fs::read(path).ok()?).ok()?;
    // The file names its child; a sidecar that names another is not this one's.
    if value.get("conversationId").and_then(Value::as_str) != Some(child) {
        return None;
    }
    let text = |value: Option<&Value>| {
        value
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|text| !text.is_empty())
            .map(|text| text.chars().take(255).collect::<String>())
    };
    let descriptor = value.get("subagentDescriptor");
    Some(AntigravityChild {
        parent_conversation_id: parent.to_string(),
        type_name: text(descriptor.and_then(|d| d.get("typeName"))),
        role: text(descriptor.and_then(|d| d.get("role"))),
        state: text(value.get("state")),
    })
}

/// The parent that recorded spawning this transcript's conversation, if any.
///
/// Looks for `<brain>/<parent>/.system_generated/subagents/<child>.json` under
/// every sibling conversation. Exactly one parent must claim the child; two
/// claims are ambiguous and name none. Top-level conversations (no sidecar
/// anywhere) return `None`, which is the common case and costs one directory
/// read plus a failed stat per sibling.
pub fn child_of(transcript: &Path) -> Option<AntigravityChild> {
    let (brain, child) = brain_and_conversation(transcript)?;
    let mut found: Option<AntigravityChild> = None;
    for entry in std::fs::read_dir(&brain).ok()?.flatten() {
        let name = entry.file_name();
        let Some(parent) = name.to_str() else {
            continue;
        };
        if parent == child || Uuid::parse_str(parent).is_err() {
            continue;
        }
        let sidecar = entry
            .path()
            .join(".system_generated")
            .join("subagents")
            .join(format!("{child}.json"));
        if let Some(record) = read_sidecar(&sidecar, &child, parent) {
            if found.is_some() {
                return None;
            }
            found = Some(record);
        }
    }
    found
}

/// One worker a parent recorded spawning that is still at work.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkingChild {
    pub conversation_id: String,
    pub record: AntigravityChild,
}

/// How much of a transcript's end is read to find its last record.
const TAIL_BYTES: u64 = 256 * 1024;

/// Whether a transcript's last record is the model's answer: a planner step
/// that called no tool. `None` when the file or its last record is unreadable.
fn ends_in_answer(transcript: &Path) -> Option<bool> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file = std::fs::File::open(transcript).ok()?;
    let len = file.metadata().ok()?.len();
    let start = len.saturating_sub(TAIL_BYTES);
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut tail = Vec::new();
    file.read_to_end(&mut tail).ok()?;
    let last = tail
        .split(|byte| *byte == b'\n')
        .rev()
        .find(|line| !line.iter().all(u8::is_ascii_whitespace))?;
    let record: Value = serde_json::from_slice(last).ok()?;
    let planner = record.get("type").and_then(Value::as_str) == Some("PLANNER_RESPONSE");
    let calls = record
        .get("tool_calls")
        .and_then(Value::as_array)
        .is_some_and(|calls| !calls.is_empty());
    Some(planner && !calls)
}

/// The workers `parent` recorded spawning that are still at work: not retired
/// by the provider, not ended in an answer, and written to within `window`.
/// The window is what keeps a worker that died mid-step from being waited on
/// forever; it has no other clock.
pub fn working_children(
    brain: &Path,
    parent: &str,
    now: std::time::SystemTime,
    window: std::time::Duration,
) -> Vec<WorkingChild> {
    let dir = brain
        .join(parent)
        .join(".system_generated")
        .join("subagents");
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return Vec::new();
    };
    let mut working = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(child) = name
            .to_str()
            .and_then(|name| name.strip_suffix(".json"))
            .filter(|id| Uuid::parse_str(id).is_ok() && *id != parent)
        else {
            continue;
        };
        let Some(record) = read_sidecar(&entry.path(), child, parent) else {
            continue;
        };
        if record.killed() {
            continue;
        }
        let logs = brain.join(child).join(".system_generated").join("logs");
        let Some(transcript) = ["transcript_full.jsonl", "transcript.jsonl"]
            .into_iter()
            .map(|name| logs.join(name))
            .find(|path| path.is_file())
        else {
            continue;
        };
        let fresh = std::fs::metadata(&transcript)
            .and_then(|meta| meta.modified())
            .ok()
            .and_then(|modified| now.duration_since(modified).ok())
            .map_or(true, |age| age <= window);
        if fresh && ends_in_answer(&transcript) == Some(false) {
            working.push(WorkingChild {
                conversation_id: child.to_string(),
                record,
            });
        }
    }
    working.sort_by(|left, right| left.conversation_id.cmp(&right.conversation_id));
    working
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn working_children_are_the_unretired_unanswered_and_recent_ones() {
        let dir = tempfile::tempdir().unwrap();
        let brain = dir.path().join("brain");
        let working = "11111111-1111-4111-8111-111111111111";
        let answered = "22222222-2222-4222-8222-222222222222";
        let killed = "33333333-3333-4333-8333-333333333333";
        let stale = "44444444-4444-4444-8444-444444444444";
        let tool_call = r#"{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","tool_calls":[{"name":"read_file"}]}"#;
        let answer =
            r#"{"step_index":3,"source":"MODEL","type":"PLANNER_RESPONSE","content":"done"}"#;
        for (id, last, state) in [
            (working, tool_call, "SUBAGENT_STATE_ALIVE"),
            (answered, answer, "SUBAGENT_STATE_ALIVE"),
            (killed, tool_call, "SUBAGENT_STATE_KILLED"),
            (stale, tool_call, "SUBAGENT_STATE_ALIVE"),
        ] {
            let path = transcript(&brain, id);
            std::fs::write(&path, format!("{{\"step_index\":0}}\n{last}\n")).unwrap();
            sidecar(
                &brain,
                PARENT,
                id,
                &format!(
                    r#"{{"conversationId":"{id}","subagentDescriptor":{{"typeName":"self","role":"r-{id}"}},"state":"{state}"}}"#
                ),
            );
        }
        let old = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
        std::fs::File::options()
            .write(true)
            .open(transcript_path(&brain, stale))
            .unwrap()
            .set_modified(old)
            .unwrap();

        let found = working_children(
            &brain,
            PARENT,
            std::time::SystemTime::now(),
            std::time::Duration::from_secs(900),
        );
        assert_eq!(
            found
                .iter()
                .map(|child| child.conversation_id.as_str())
                .collect::<Vec<_>>(),
            vec![working]
        );
        assert_eq!(
            found[0].record.role.as_deref(),
            Some(format!("r-{working}").as_str())
        );
    }

    fn transcript_path(brain: &Path, id: &str) -> PathBuf {
        brain
            .join(id)
            .join(".system_generated/logs/transcript_full.jsonl")
    }

    const PARENT: &str = "0b7e6a3c-1d2f-4e5a-9b8c-7d6e5f4a3b2c";
    const CHILD: &str = "1c8f7b4d-2e3a-4f6b-8c9d-0e1f2a3b4c5d";
    const OTHER: &str = "2d9a8c5e-3f4b-4a7c-9d0e-1f2a3b4c5d6e";

    fn transcript(brain: &Path, id: &str) -> PathBuf {
        let path = brain
            .join(id)
            .join(".system_generated/logs/transcript_full.jsonl");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "{}\n").unwrap();
        path
    }

    fn sidecar(brain: &Path, parent: &str, child: &str, body: &str) {
        let dir = brain.join(parent).join(".system_generated/subagents");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(format!("{child}.json")), body).unwrap();
    }

    #[test]
    fn a_child_is_named_by_its_parents_sidecar() {
        let dir = tempfile::tempdir().unwrap();
        let brain = dir.path().join("brain");
        transcript(&brain, PARENT);
        let child = transcript(&brain, CHILD);
        sidecar(
            &brain,
            PARENT,
            CHILD,
            &format!(
                r#"{{"conversationId":"{CHILD}","subagentDescriptor":{{"typeName":"self","role":"Doc Auditor"}},"state":"SUBAGENT_STATE_ALIVE","spawnStepIndex":6211}}"#
            ),
        );

        let found = child_of(&child).expect("sidecar names the parent");
        assert_eq!(found.parent_conversation_id, PARENT);
        assert_eq!(found.type_name.as_deref(), Some("self"));
        assert_eq!(found.role.as_deref(), Some("Doc Auditor"));
        assert!(!found.killed());
        assert_eq!(found.agent_type(), None);
    }

    #[test]
    fn a_swarm_worker_reads_as_swarm_and_killed_is_terminal() {
        let dir = tempfile::tempdir().unwrap();
        let brain = dir.path().join("brain");
        let child = transcript(&brain, OTHER);
        sidecar(
            &brain,
            PARENT,
            OTHER,
            &format!(
                r#"{{"conversationId":"{OTHER}","subagentDescriptor":{{"typeName":"worker","role":"Swarm Worker (monitor)"}},"state":"SUBAGENT_STATE_KILLED"}}"#
            ),
        );

        let found = child_of(&child).unwrap();
        assert!(found.killed());
        assert_eq!(found.agent_type(), Some("swarm"));
    }

    #[test]
    fn top_level_mismatched_and_ambiguous_conversations_name_no_parent() {
        let dir = tempfile::tempdir().unwrap();
        let brain = dir.path().join("brain");
        let parent = transcript(&brain, PARENT);
        assert_eq!(child_of(&parent), None, "no sidecar anywhere: top level");

        let child = transcript(&brain, CHILD);
        sidecar(
            &brain,
            PARENT,
            CHILD,
            r#"{"conversationId":"someone-else"}"#,
        );
        assert_eq!(child_of(&child), None, "a sidecar must name this child");

        let body = format!(r#"{{"conversationId":"{CHILD}"}}"#);
        sidecar(&brain, PARENT, CHILD, &body);
        sidecar(&brain, OTHER, CHILD, &body);
        assert_eq!(
            child_of(&child),
            None,
            "two parents claiming one child is ambiguous"
        );

        assert_eq!(
            child_of(&dir.path().join("elsewhere/transcript.jsonl")),
            None,
            "not a brain transcript"
        );
    }
}
