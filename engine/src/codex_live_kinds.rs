//! What the Codex app-server's item notifications say about a running tool or subagent, in the shape the Runtime Host
//! publishes as a live preview.
//!
//! Console (`codex_exec`) and Helm (`codex_bridge`) read the same notifications. Both post a tool as a
//! `console_live_tool_item` progress event carrying the tool's real name, and reasoning as the raw
//! `item/reasoning/*` notification (`codex_app_server_jsonrpc`), which the host already turns into a `thinking` preview.
//! The host derives the kind from the tool name, so a `collabAgentToolCall` is posted as `spawn_agent` and shows as a
//! subagent.

use serde_json::Value;

/// A tool or subagent item as the live lane shows it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiveToolItem {
    pub item_id: String,
    /// The name the host derives the kind from (`shell`, `edit`, `spawn_agent`, an MCP tool's own name).
    pub name: String,
    /// One line saying what the call is: the command, a subagent's task, a file path or a query.
    pub summary: String,
}

/// The notification methods whose text is the model's reasoning summary.
pub fn is_reasoning_method(method: &str) -> bool {
    matches!(
        method,
        "item/reasoning/summaryTextDelta" | "item/reasoning/summaryPartAdded"
    )
}

/// `spawnAgent` and `sendInput` are what the app-server calls its collaboration tools; the host knows them as
/// `spawn_agent` and so on.
pub fn snake_case_tool_name(name: &str) -> String {
    let mut out = String::with_capacity(name.len() + 2);
    for (index, character) in name.chars().enumerate() {
        if character.is_ascii_uppercase() {
            if index > 0 {
                out.push('_');
            }
            out.push(character.to_ascii_lowercase());
        } else {
            out.push(character);
        }
    }
    out
}

fn string_field(item: &Value, key: &str) -> Option<String> {
    item.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(str::to_string)
}

fn clip(text: &str, max_chars: usize) -> String {
    text.chars().take(max_chars).collect()
}

/// The tool or subagent behind an `item/started` or `item/completed` item, or `None` for any other item type
/// (messages, reasoning, plans).
pub fn live_tool_item(item: &Value) -> Option<LiveToolItem> {
    let item_id = string_field(item, "id")?;
    let (name, summary) = match item.get("type").and_then(Value::as_str)? {
        "commandExecution" => (
            "shell".to_string(),
            string_field(item, "command").unwrap_or_default(),
        ),
        "fileChange" => (
            "edit".to_string(),
            item.get("changes")
                .and_then(Value::as_array)
                .map(|changes| {
                    changes
                        .iter()
                        .filter_map(|change| string_field(change, "path"))
                        .collect::<Vec<_>>()
                        .join(", ")
                })
                .unwrap_or_default(),
        ),
        "collabAgentToolCall" => {
            let tool = string_field(item, "tool")?;
            (
                snake_case_tool_name(&tool),
                clip(
                    &string_field(item, "prompt").unwrap_or_else(|| tool.clone()),
                    120,
                ),
            )
        }
        "mcpToolCall" | "dynamicToolCall" => {
            let tool = string_field(item, "tool")?;
            let summary = item
                .get("arguments")
                .filter(|arguments| arguments.is_object())
                .map(|arguments| crate::status_slot::tool_arguments_summary(&tool, arguments))
                .unwrap_or_default();
            (tool, clip(&summary, 240))
        }
        "webSearch" => (
            "web_search".to_string(),
            string_field(item, "query").unwrap_or_default(),
        ),
        _ => return None,
    };
    Some(LiveToolItem {
        item_id,
        name,
        summary,
    })
}

/// The output a finished item carries (`aggregatedOutput` for a command), when it has any.
pub fn item_output(item: &Value) -> Option<String> {
    string_field(item, "aggregatedOutput")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_collab_spawn_is_named_for_the_host() {
        let item = json!({"id": "c1", "type": "collabAgentToolCall", "tool": "spawnAgent", "prompt": "check the logs"});
        let tool = live_tool_item(&item).unwrap();
        assert_eq!(tool.name, "spawn_agent");
        assert_eq!(tool.summary, "check the logs");
        assert_eq!(
            crate::status_slot::tool_preview_kind(&tool.name),
            "subagent"
        );
    }

    #[test]
    fn a_command_is_a_shell_tool_with_its_command() {
        let item = json!({"id": "x", "type": "commandExecution", "command": "ls -la"});
        let tool = live_tool_item(&item).unwrap();
        assert_eq!(
            (tool.name.as_str(), tool.summary.as_str()),
            ("shell", "ls -la")
        );
        assert_eq!(crate::status_slot::tool_preview_kind(&tool.name), "tool");
    }

    #[test]
    fn messages_and_reasoning_are_not_tools() {
        for item_type in ["agentMessage", "reasoning", "userMessage", "plan"] {
            assert!(live_tool_item(&json!({"id": "m", "type": item_type})).is_none());
        }
    }

    #[test]
    fn an_mcp_call_uses_its_tool_and_arguments() {
        let item = json!({"id": "m1", "type": "mcpToolCall", "tool": "search", "arguments": {"query": "rust"}});
        let tool = live_tool_item(&item).unwrap();
        assert_eq!(
            (tool.name.as_str(), tool.summary.as_str()),
            ("search", "rust")
        );
    }

    #[test]
    fn only_summary_reasoning_methods_are_thinking() {
        assert!(is_reasoning_method("item/reasoning/summaryTextDelta"));
        assert!(!is_reasoning_method("item/agentMessage/delta"));
        assert_eq!(snake_case_tool_name("closeAgent"), "close_agent");
    }
}
