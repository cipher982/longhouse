use std::io::{BufRead, BufReader};
use std::path::Path;

use serde_json::Value;

const CODEX_SESSION_META_SCAN_LIMIT_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CodexSubagentSource {
    pub parent_thread_id: Option<String>,
    /// How deep the worker sits below the root agent. Present in every real
    /// subagent rollout on disk and dropped until now, which is why no nesting
    /// was representable.
    pub depth: Option<u32>,
    /// The provider's own display name for the worker, e.g. "Avicenna". A label
    /// the provider chose beats anything Longhouse can infer from a prompt.
    pub agent_nickname: Option<String>,
    /// The worker's role, e.g. "default". Frequently null in practice.
    pub agent_role: Option<String>,
    /// The worker's path in the agent tree, e.g. "/root/claude_cursor".
    pub agent_path: Option<String>,
}

pub fn parse_codex_subagent_source(source: &Value) -> Option<CodexSubagentSource> {
    let subagent = source
        .get("subagent")
        .or_else(|| source.get("subAgent"))
        .or_else(|| source.get("sub_agent"))?;

    if subagent.is_string() {
        // A bare string says "this is a subagent" and nothing more: no spawn
        // record to read a depth or a name from.
        return Some(CodexSubagentSource::default());
    }

    let thread_spawn = subagent
        .get("thread_spawn")
        .or_else(|| subagent.get("threadSpawn"))
        .or_else(|| subagent.get("threadspawn"));

    Some(CodexSubagentSource {
        parent_thread_id: thread_spawn.and_then(extract_parent_thread_id),
        depth: thread_spawn.and_then(extract_thread_spawn_depth),
        agent_nickname: thread_spawn
            .and_then(|spawn| extract_optional_string(spawn, "agent_nickname", "agentNickname")),
        agent_role: thread_spawn
            .and_then(|spawn| extract_optional_string(spawn, "agent_role", "agentRole")),
        agent_path: thread_spawn
            .and_then(|spawn| extract_optional_string(spawn, "agent_path", "agentPath")),
    })
}

pub fn parse_codex_subagent_source_str(source: &str) -> Option<CodexSubagentSource> {
    let value: Value = serde_json::from_str(source).ok()?;
    parse_codex_subagent_source(&value)
}

pub fn codex_thread_value_is_subagent(thread: &Value) -> bool {
    codex_thread_value_subagent_source(thread).is_some()
}

pub fn codex_thread_value_subagent_source(thread: &Value) -> Option<CodexSubagentSource> {
    thread.get("source").and_then(parse_codex_subagent_source)
}

pub fn codex_rollout_file_is_subagent(path: &Path) -> bool {
    scan_codex_rollout_source(path)
        .as_deref()
        .and_then(parse_codex_subagent_source_str)
        .is_some()
}

fn extract_parent_thread_id(thread_spawn: &Value) -> Option<String> {
    thread_spawn
        .get("parent_thread_id")
        .or_else(|| thread_spawn.get("parentThreadId"))
        .or_else(|| thread_spawn.get("parentThreadID"))
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(ToString::to_string)
}

fn extract_thread_spawn_depth(thread_spawn: &Value) -> Option<u32> {
    thread_spawn
        .get("depth")
        .and_then(Value::as_u64)
        .and_then(|depth| u32::try_from(depth).ok())
}

/// First present, non-empty string among the given keys.
///
/// Codex ships the same field under snake_case from the CLI and camelCase from
/// the app server, and `agent_role` is legitimately null in most real rollouts,
/// so absence must stay absence rather than becoming an empty label.
fn extract_optional_string(thread_spawn: &Value, snake: &str, camel: &str) -> Option<String> {
    thread_spawn
        .get(snake)
        .or_else(|| thread_spawn.get(camel))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
}

fn scan_codex_rollout_source(path: &Path) -> Option<String> {
    let file = std::fs::File::open(path).ok()?;
    let mut reader = BufReader::with_capacity(16 * 1024, file);
    let mut line = String::new();
    let mut bytes_scanned = 0usize;

    while bytes_scanned < CODEX_SESSION_META_SCAN_LIMIT_BYTES {
        line.clear();
        let n = reader.read_line(&mut line).ok()?;
        if n == 0 {
            break;
        }
        bytes_scanned += n;

        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }

        let value: Value = match serde_json::from_str(trimmed) {
            Ok(value) => value,
            Err(_) => continue,
        };
        if value.get("type").and_then(Value::as_str) != Some("session_meta") {
            continue;
        }
        return value
            .get("payload")
            .and_then(|payload| payload.get("source"))
            .map(Value::to_string);
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_raw_rollout_subagent_thread_spawn_source() {
        let source = json!({
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": "019dd708-573a-7131-a4d9-9ee855520483",
                    "depth": 1,
                    "agent_nickname": "Ptolemy",
                    "agent_role": "default"
                }
            }
        });

        let parsed = parse_codex_subagent_source(&source).unwrap();
        assert_eq!(
            parsed.parent_thread_id.as_deref(),
            Some("019dd708-573a-7131-a4d9-9ee855520483")
        );
        // The rest of the spawn record was parsed and thrown away until now,
        // which is why no nesting was representable and a child had no name.
        assert_eq!(parsed.depth, Some(1));
        assert_eq!(parsed.agent_nickname.as_deref(), Some("Ptolemy"));
        assert_eq!(parsed.agent_role.as_deref(), Some("default"));
    }

    #[test]
    fn a_bare_string_source_carries_no_spawn_detail() {
        // Some writers say only "this is a subagent". Absence must stay absence
        // rather than becoming a zero depth or an empty name.
        let parsed = parse_codex_subagent_source(&json!({"subagent": "thread_spawn"})).unwrap();
        assert_eq!(parsed.parent_thread_id, None);
        assert_eq!(parsed.depth, None);
        assert_eq!(parsed.agent_nickname, None);
        assert_eq!(parsed.agent_role, None);
        assert_eq!(parsed.agent_path, None);
    }

    #[test]
    fn a_null_agent_role_is_absent_not_empty() {
        // Real rollouts carry `"agent_role": null`; a name must never come out
        // of that, or the UI labels a worker with nothing.
        let source = json!({
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": "019f7628-12e8-76c2-b4d4-9edd0210d001",
                    "depth": 2,
                    "agent_path": "/root/claude_cursor",
                    "agent_nickname": "Avicenna",
                    "agent_role": null
                }
            }
        });

        let parsed = parse_codex_subagent_source(&source).unwrap();
        assert_eq!(parsed.depth, Some(2));
        assert_eq!(parsed.agent_nickname.as_deref(), Some("Avicenna"));
        assert_eq!(parsed.agent_role, None);
        assert_eq!(parsed.agent_path.as_deref(), Some("/root/claude_cursor"));
    }

    #[test]
    fn parses_app_server_camel_subagent_thread_spawn_source() {
        let source = json!({
            "subAgent": {
                "threadSpawn": {
                    "parentThreadId": "019dd708-573a-7131-a4d9-9ee855520483",
                    "depth": 1
                }
            }
        });

        let parsed = parse_codex_subagent_source(&source).unwrap();
        assert_eq!(
            parsed.parent_thread_id.as_deref(),
            Some("019dd708-573a-7131-a4d9-9ee855520483")
        );
    }

    #[test]
    fn root_string_source_is_not_subagent() {
        assert!(parse_codex_subagent_source(&json!("vscode")).is_none());
    }

    #[test]
    fn parses_non_thread_spawn_subagent_without_parent() {
        let source = json!({
            "subagent": {
                "review": {}
            }
        });

        let parsed = parse_codex_subagent_source(&source).unwrap();
        assert_eq!(parsed.parent_thread_id, None);
    }
}
