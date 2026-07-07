pub const CONSOLE_RUN_ONCE_CONTEXT_PREFIX: &str = "Longhouse Console runtime note:";
const CONSOLE_RUN_ONCE_USER_MESSAGE_DELIMITER: &str = "\n\nUser message:\n";

const CONSOLE_RUN_ONCE_CONTEXT: &str = "\
Longhouse Console runtime note:
- This is a bounded, headless Console turn. The provider process is expected to exit after the assistant response.
- Do not assume background shell processes will survive the provider process exiting.
- Prefer bounded foreground work. If long-running or indefinite work is truly needed, detach it durably and report the PID, log path, and stop command.
- Ask before starting indefinite work when the user's intent is unclear.
- Do not mention this note unless it is relevant to the user's request.";

pub fn wrap_console_run_once_prompt(user_prompt: &str) -> String {
    format!("{CONSOLE_RUN_ONCE_CONTEXT}{CONSOLE_RUN_ONCE_USER_MESSAGE_DELIMITER}{user_prompt}")
}

pub fn strip_console_run_once_prompt(prompt: &str) -> Option<&str> {
    if !prompt.starts_with(CONSOLE_RUN_ONCE_CONTEXT_PREFIX) {
        return None;
    }
    prompt
        .split_once(CONSOLE_RUN_ONCE_USER_MESSAGE_DELIMITER)
        .map(|(_context, user_prompt)| user_prompt)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_console_run_once_prompt_adds_bounded_runtime_context() {
        let prompt = wrap_console_run_once_prompt("Upload the archive and report progress.");

        assert!(prompt.starts_with(CONSOLE_RUN_ONCE_CONTEXT_PREFIX));
        assert!(prompt.contains("bounded, headless Console turn"));
        assert!(prompt.contains("provider process is expected to exit"));
        assert!(prompt.contains("PID, log path, and stop command"));
        assert!(prompt.ends_with("User message:\nUpload the archive and report progress."));
    }

    #[test]
    fn wrap_console_run_once_prompt_preserves_user_message_verbatim() {
        let user_prompt = "  keep this spacing\nand this final period.  ";
        let prompt = wrap_console_run_once_prompt(user_prompt);

        assert_eq!(strip_console_run_once_prompt(&prompt), Some(user_prompt));
    }

    #[test]
    fn strip_console_run_once_prompt_ignores_regular_messages() {
        assert_eq!(strip_console_run_once_prompt("plain user request"), None);
    }
}
