//! Codex slash commands sent from the Longhouse app.
//!
//! Codex parses slash commands only in its terminal UI; Longhouse talks to
//! `codex app-server`, which has no parser. So each command is mapped to the
//! app-server request the TUI itself sends for it (codex-rs/tui
//! `chatwidget/slash_dispatch.rs`, rust-v0.171.0). A command that only drives
//! the TUI (a picker, a display toggle, a new conversation the terminal would
//! have to switch to) has no request to send and is refused with the reason.

use serde_json::{json, Value};

use crate::slash_input::SlashCommand;

/// What to do with a slash command for a Codex thread.
#[derive(Debug, PartialEq)]
pub enum CodexSlash {
    /// Send this app-server request. `turn` says whether a model turn follows:
    /// `Some(false)` never, `None` when the request may start one.
    Request {
        method: &'static str,
        params: Value,
        turn: Option<bool>,
    },
    /// Not a Codex command: the TUI sends unknown text as a message.
    Message,
    /// A Codex command the app cannot run, and why.
    Refuse(String),
}

/// Every built-in name the pinned Codex TUI knows (aliases included).
const CODEX_COMMANDS: &[&str] = &[
    "model",
    "presets",
    "daybreak",
    "ide",
    "permissions",
    "keymap",
    "vim",
    "setup-default-sandbox",
    "experimental",
    "approve",
    "memories",
    "skills",
    "import",
    "hooks",
    "review",
    "rename",
    "new",
    "archive",
    "delete",
    "resume",
    "fork",
    "worktree",
    "app",
    "init",
    "compact",
    "config",
    "recap",
    "plan",
    "voice",
    "goal",
    "agents",
    "side",
    "btw",
    "copy",
    "export",
    "raw",
    "tui",
    "diff",
    "mention",
    "status",
    "daemon",
    "rc",
    "remote",
    "warnings",
    "cd",
    "pwd",
    "cwd",
    "usage",
    "debug-config",
    "title",
    "statusline",
    "theme",
    "pets",
    "pet",
    "mcp",
    "apps",
    "plugins",
    "logout",
    "quit",
    "exit",
    "feedback",
    "rollout",
    "ps",
    "stop",
    "clean",
    "clear",
    "test-approval",
    "subagents",
    "debug-m-drop",
    "debug-m-update",
];

pub fn route(thread_id: &str, command: &SlashCommand) -> CodexSlash {
    let name = command.name.as_str();
    let args = command.args.trim();
    let request = |method, params, turn| CodexSlash::Request {
        method,
        params,
        turn,
    };
    match (name, args) {
        ("rename", title) if !title.is_empty() => request(
            "thread/name/set",
            json!({"threadId": thread_id, "name": title}),
            Some(false),
        ),
        ("goal", "clear") => request(
            "thread/goal/clear",
            json!({"threadId": thread_id, "origin": "user"}),
            Some(false),
        ),
        ("goal", "pause") => request(
            "thread/goal/set",
            json!({"threadId": thread_id, "origin": "user", "status": "paused"}),
            Some(false),
        ),
        ("goal", "resume") => request(
            "thread/goal/set",
            json!({"threadId": thread_id, "origin": "user", "status": "active"}),
            None,
        ),
        // An active goal makes Codex keep working on it, so a turn may follow.
        ("goal", objective) if !objective.is_empty() => request(
            "thread/goal/set",
            json!({"threadId": thread_id, "origin": "user", "objective": objective, "status": "active"}),
            None,
        ),
        ("compact", _) => request(
            "thread/compact/start",
            json!({"threadId": thread_id}),
            None,
        ),
        ("review", instructions) if !instructions.is_empty() => request(
            "review/start",
            json!({"threadId": thread_id, "target": {"type": "custom", "instructions": instructions}}),
            None,
        ),
        ("model", model) if !model.is_empty() => request(
            "thread/settings/update",
            json!({"threadId": thread_id, "model": model}),
            Some(false),
        ),
        ("stop" | "clean", _) => request(
            "thread/backgroundTerminals/clean",
            json!({"threadId": thread_id}),
            Some(false),
        ),
        ("new" | "clear" | "resume" | "fork" | "side" | "btw" | "worktree", _) => {
            CodexSlash::Refuse(format!(
                "/{name} starts or switches the conversation Codex's terminal shows, which only the terminal can do; run it there"
            ))
        }
        ("archive" | "delete", _) => CodexSlash::Refuse(format!(
            "/{name} would remove this session's thread from under Codex's terminal; run it there"
        )),
        ("quit" | "exit" | "logout", _) => CodexSlash::Refuse(format!(
            "/{name} works on Codex's terminal itself; run it there, or stop the session from the app"
        )),
        ("init" | "plan" | "recap", _) => CodexSlash::Refuse(format!(
            "/{name} is built into Codex's terminal (its prompt and mode live there, not in codex app-server); run it there"
        )),
        _ if CODEX_COMMANDS.contains(&name) => CodexSlash::Refuse(format!(
            "/{name} only changes or shows Codex's terminal, so it has no effect sent from the app; run it there"
        )),
        _ => CodexSlash::Message,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::slash_input::command;

    #[test]
    fn maps_commands_to_the_requests_the_tui_sends() {
        assert_eq!(
            route("t1", &command("/rename Proof run").unwrap()),
            CodexSlash::Request {
                method: "thread/name/set",
                params: json!({"threadId": "t1", "name": "Proof run"}),
                turn: Some(false)
            }
        );
        match route("t1", &command("/goal ship the fix").unwrap()) {
            CodexSlash::Request {
                method,
                params,
                turn,
            } => {
                assert_eq!(method, "thread/goal/set");
                assert_eq!(params["objective"], "ship the fix");
                assert_eq!(turn, None);
            }
            other => panic!("{other:?}"),
        }
        assert!(matches!(
            route("t1", &command("/goal clear").unwrap()),
            CodexSlash::Request {
                method: "thread/goal/clear",
                ..
            }
        ));
        assert!(matches!(
            route("t1", &command("/model gpt-6").unwrap()),
            CodexSlash::Request {
                method: "thread/settings/update",
                ..
            }
        ));
    }

    #[test]
    fn refuses_terminal_only_commands_and_passes_unknown_text() {
        assert!(matches!(
            route("t", &command("/new").unwrap()),
            CodexSlash::Refuse(_)
        ));
        assert!(matches!(
            route("t", &command("/theme").unwrap()),
            CodexSlash::Refuse(_)
        ));
        assert_eq!(
            route("t", &command("/frobnicate x").unwrap()),
            CodexSlash::Message
        );
    }
}
