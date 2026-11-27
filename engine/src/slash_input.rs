//! Slash commands the owner sends from the Longhouse app.
//!
//! The owner's text runs exactly as if they had typed it in the provider's
//! terminal: `/name args` runs the provider's command. Two exceptions, decided
//! here for every provider alike:
//!
//! - A command that opens a picker when it has no argument (the provider
//!   contract's `slash_commands.pickers`, such as a bare `/model`) is refused:
//!   the picker would open in a terminal nobody is watching and swallow the
//!   next message.
//! - Text that starts with `//` is the owner saying "this is a message, not a
//!   command": one slash is dropped and the rest goes to the model as text.
//!
//! How each provider runs a command is the provider adapter's business; it asks
//! [`command`] whether the text is one.

use serde_json::Value;

/// A slash command as the owner typed it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SlashCommand {
    /// The command's name without the slash (`model`, `skill:review`).
    pub name: String,
    /// Everything after the name, trimmed (`""` when nothing was).
    pub args: String,
}

/// What the owner's text is, before any provider sees it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OwnerText {
    /// Ordinary text for the model.
    Text,
    /// `//...`: a message that starts with a slash. The literal text to send.
    Literal(String),
    /// `/name args`: run the provider's command.
    Command(SlashCommand),
}

/// How the provider contract says a provider treats slash commands.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SlashPolicy {
    /// Commands that open a picker when sent with no argument.
    pub pickers: Vec<String>,
    /// Per session mode (`helm`, `console`): `run` or `refuse`.
    pub helm: Option<String>,
    pub console: Option<String>,
    /// Why a mode refuses, shown to the owner.
    pub refuse_reason: Option<String>,
}

impl SlashPolicy {
    pub fn from_contract(contract: &Value) -> Self {
        let section = contract.get("slash_commands");
        let string = |key: &str| {
            section
                .and_then(|s| s.get(key))
                .and_then(Value::as_str)
                .map(str::to_owned)
        };
        Self {
            pickers: section
                .and_then(|s| s.get("pickers"))
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default(),
            helm: string("helm"),
            console: string("console"),
            refuse_reason: string("refuse_reason"),
        }
    }

    fn mode(&self, mode: &str) -> Option<&str> {
        match mode {
            "helm" => self.helm.as_deref(),
            "console" => self.console.as_deref(),
            _ => None,
        }
    }
}

/// Is `text` a slash command, a literal `//` message, or text?
///
/// A command name is letters, digits and `_ - : .`, as every provider's
/// commands are; `/tmp/x is broken` is text, not a command named `tmp/x`.
pub fn classify(text: &str) -> OwnerText {
    let trimmed = text.trim_start();
    let Some(rest) = trimmed.strip_prefix('/') else {
        return OwnerText::Text;
    };
    if rest.starts_with('/') {
        return OwnerText::Literal(rest.to_string());
    }
    let end = rest.find(char::is_whitespace).unwrap_or(rest.len());
    let name = &rest[..end];
    let valid = !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | ':' | '.'));
    if !valid {
        return OwnerText::Text;
    }
    OwnerText::Command(SlashCommand {
        name: name.to_string(),
        args: rest[end..].trim().to_string(),
    })
}

/// The slash command in `text`, if it is one.
pub fn command(text: &str) -> Option<SlashCommand> {
    match classify(text) {
        OwnerText::Command(command) => Some(command),
        _ => None,
    }
}

/// A refusal the owner sees, with the code the server and app key on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    pub code: &'static str,
    pub message: String,
}

pub const REFUSED_CODE: &str = "slash_command_refused";

/// Decide whether the owner's `text` may go to the provider in `mode`.
///
/// `Ok(text)` hands the text on unchanged, a `//` literal included: each
/// adapter decides how to deliver one (most call [`message_text`]). `Err` is a
/// refusal to show the owner.
pub fn admit(
    provider: &str,
    mode: &str,
    policy: &SlashPolicy,
    text: &str,
) -> Result<String, Refusal> {
    match classify(text) {
        OwnerText::Text => Ok(text.to_string()),
        OwnerText::Literal(_) => Ok(text.to_string()),
        OwnerText::Command(command) => {
            if policy.mode(mode) == Some("refuse") {
                let why = policy.refuse_reason.clone().unwrap_or_else(|| {
                    format!("{provider} cannot run a command sent from the app")
                });
                return Err(Refusal {
                    code: REFUSED_CODE,
                    message: format!(
                        "/{} was not sent: {why}. Run it in the terminal, or start the message with // to send it as text.",
                        command.name
                    ),
                });
            }
            if command.args.is_empty() && policy.pickers.iter().any(|p| p == &command.name) {
                return Err(Refusal {
                    code: REFUSED_CODE,
                    message: format!(
                        "/{} was not sent: it opens a picker in the terminal, which nobody can answer from the app. Run it in the terminal, or add an argument (for example /{} <choice>).",
                        command.name, command.name
                    ),
                });
            }
            Ok(text.to_string())
        }
    }
}

/// The text to put in front of the model: a `//` literal loses one slash,
/// anything else is unchanged. Only for text that is not a command.
pub fn message_text(text: &str) -> String {
    match classify(text) {
        OwnerText::Literal(literal) => {
            let leading = &text[..text.len() - text.trim_start().len()];
            format!("{leading}{literal}")
        }
        _ => text.to_string(),
    }
}

/// The fields every provider's send result carries for a command, so the
/// server can tell a command that started no model turn from a message: it
/// releases the session at once instead of waiting for a turn to end.
pub fn result_fields(command: &SlashCommand, turn_started: bool) -> Value {
    serde_json::json!({
        "command": command.name,
        "command_args": command.args,
        "turn_started": turn_started,
    })
}

/// Mark a send result as a command whose provider cannot tell whether it
/// opened a model turn (it was typed into the provider's terminal). The server
/// then frees the session once it stays idle after the command.
pub fn mark_command(result: &mut Value, command: &SlashCommand) {
    if let Some(target) = result.as_object_mut() {
        target.insert("command".into(), Value::String(command.name.clone()));
        target.insert("command_args".into(), Value::String(command.args.clone()));
    }
}

/// Merge [`result_fields`] into a send result object.
pub fn mark_result(result: &mut Value, command: &SlashCommand, turn_started: bool) {
    if let (Some(target), Value::Object(fields)) =
        (result.as_object_mut(), result_fields(command, turn_started))
    {
        target.extend(fields);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy() -> SlashPolicy {
        SlashPolicy {
            pickers: vec!["model".into(), "resume".into()],
            helm: Some("run".into()),
            console: Some("refuse".into()),
            refuse_reason: Some("headless mode has no commands".into()),
        }
    }

    #[test]
    fn classifies_commands_literals_and_text() {
        assert_eq!(classify("hello"), OwnerText::Text);
        assert_eq!(classify("/tmp/x is broken"), OwnerText::Text);
        assert_eq!(classify("/"), OwnerText::Text);
        assert_eq!(classify("//goal x"), OwnerText::Literal("/goal x".into()));
        assert_eq!(
            classify("  /goal ship it\nnow"),
            OwnerText::Command(SlashCommand {
                name: "goal".into(),
                args: "ship it\nnow".into()
            })
        );
        assert_eq!(
            command("/skill:review"),
            Some(SlashCommand {
                name: "skill:review".into(),
                args: String::new()
            })
        );
    }

    #[test]
    fn refuses_bare_pickers_and_refusing_modes() {
        let p = policy();
        assert_eq!(
            admit("x", "helm", &p, "/model opus").unwrap(),
            "/model opus"
        );
        let bare = admit("x", "helm", &p, "/model").unwrap_err();
        assert_eq!(bare.code, REFUSED_CODE);
        assert!(bare.message.contains("opens a picker"), "{}", bare.message);
        let console = admit("x", "console", &p, "/goal x").unwrap_err();
        assert!(
            console.message.contains("headless mode"),
            "{}",
            console.message
        );
        assert_eq!(admit("x", "console", &p, "//goal x").unwrap(), "//goal x");
        assert_eq!(message_text("//goal x"), "/goal x");
        assert_eq!(message_text("plain"), "plain");
        assert_eq!(admit("x", "helm", &p, "plain").unwrap(), "plain");
    }

    #[test]
    fn marks_results() {
        let mut result = serde_json::json!({"exit_code": 0});
        mark_result(&mut result, &command("/rename x").unwrap(), false);
        assert_eq!(result["command"], "rename");
        assert_eq!(result["command_args"], "x");
        assert_eq!(result["turn_started"], false);
    }
}
