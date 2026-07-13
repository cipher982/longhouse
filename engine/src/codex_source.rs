use std::io::{BufRead, BufReader};
use std::path::Path;

use serde_json::Value;

const CODEX_SESSION_META_SCAN_LIMIT_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodexSubagentSource {
    pub parent_thread_id: Option<String>,
}

pub fn parse_codex_subagent_source(source: &Value) -> Option<CodexSubagentSource> {
    let subagent = source
        .get("subagent")
        .or_else(|| source.get("subAgent"))
        .or_else(|| source.get("sub_agent"))?;

    if subagent.is_string() {
        return Some(CodexSubagentSource {
            parent_thread_id: None,
        });
    }

    let thread_spawn = subagent
        .get("thread_spawn")
        .or_else(|| subagent.get("threadSpawn"))
        .or_else(|| subagent.get("threadspawn"));

    let parent_thread_id = thread_spawn.and_then(extract_parent_thread_id);
    Some(CodexSubagentSource { parent_thread_id })
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
