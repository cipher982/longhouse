//! One registration payload for every managed launch.
//!
//! `POST /api/sessions/managed-local/this-device` had four independent
//! hand-written JSON literals — one per provider, three in `longhouse.rs` and
//! one in `cursor_helm_launcher.rs`. They drifted:
//!
//! - claude and codex sent `git_repo`/`git_branch`; opencode and cursor did not,
//!   so 99% of managed OpenCode and 97% of managed Cursor sessions on hosted
//!   `david010` have no repo or branch, and `live_catalog_launch` only ever sets
//!   those fields from the launch payload.
//! - codex hardcoded `"permission_mode": "bypass"` while the binary receives
//!   `--dangerously-bypass-approvals-and-sandbox` only when the opt-in flag is
//!   passed, so Longhouse recorded "this session bypasses approvals" for every
//!   managed Codex session including the ones where Codex was enforcing them.
//!
//! A provider can still contribute its own fields through `extra`, but it
//! cannot forget a shared one: shared fields are built here, from the cwd, for
//! everybody.

use serde_json::{json, Value};
use std::io::IsTerminal;
use std::path::Path;
use std::process::Command;
use std::time::Duration;

/// Permission posture Longhouse reports for a managed launch.
///
/// `Bypass` means Longhouse told the provider to skip its own prompts;
/// `ProviderLocal` means the provider keeps and owns its local approval UI;
/// `RemoteApprove` means Longhouse can answer that approval surface remotely.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PermissionMode {
    Bypass,
    ProviderLocal,
    RemoteApprove,
}

impl PermissionMode {
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn as_wire(self) -> &'static str {
        match self {
            PermissionMode::Bypass => "bypass",
            PermissionMode::ProviderLocal => "provider_local",
            PermissionMode::RemoteApprove => "remote_approve",
        }
    }

    /// The posture implied by whether the launcher actually passes its
    /// provider's skip-permissions flag. Every launcher must derive its wire
    /// value from the same boolean it uses to build argv, so the two cannot
    /// disagree.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn from_bypass_flag(bypassing: bool) -> Self {
        if bypassing {
            PermissionMode::Bypass
        } else {
            PermissionMode::RemoteApprove
        }
    }
}

/// Return provenance for a provider launched from an attached human terminal.
/// Managed Helm launchers share this contract so durable ingest can safely
/// promote their first content into the default timeline.
pub fn interactive_human_shell_provenance() -> (Option<&'static str>, Option<&'static str>) {
    let hidden = std::env::var("LONGHOUSE_ORIGIN_KIND")
        .ok()
        .is_some_and(|value| !value.trim().is_empty());
    let sidechain = matches!(
        std::env::var("LONGHOUSE_IS_SIDECHAIN")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    );
    human_shell_provenance_for_terminal(
        std::io::stdin().is_terminal(),
        std::io::stdout().is_terminal(),
        hidden,
        sidechain,
    )
}

fn human_shell_provenance_for_terminal(
    stdin_is_terminal: bool,
    stdout_is_terminal: bool,
    hidden: bool,
    sidechain: bool,
) -> (Option<&'static str>, Option<&'static str>) {
    if stdin_is_terminal && stdout_is_terminal && !hidden && !sidechain {
        (Some("human_shell"), Some("terminal"))
    } else {
        (None, None)
    }
}

pub struct ManagedLaunchRegistration<'a> {
    pub provider: &'a str,
    pub cwd: &'a Path,
    pub project: Option<&'a str>,
    pub display_name: Option<&'a str>,
    pub loop_mode: &'a str,
    pub machine_name: &'a str,
    pub permission_mode: PermissionMode,
    /// Provider-specific fields (`session_id`, `native_claude_channels_available`).
    pub extra: Vec<(&'static str, Value)>,
}

impl<'a> ManagedLaunchRegistration<'a> {
    pub fn to_json(&self) -> Value {
        let (git_repo, git_branch) = git_context(self.cwd);
        let mut payload = json!({
            "cwd": self.cwd,
            "provider": self.provider,
            "project": self.project,
            "git_repo": git_repo,
            "git_branch": git_branch,
            "display_name": self.display_name,
            "loop_mode": self.loop_mode,
            "machine_name": self.machine_name,
            "permission_mode": self.permission_mode.as_wire(),
        });
        let map = payload.as_object_mut().expect("object literal");
        for (key, value) in &self.extra {
            map.insert((*key).to_string(), value.clone());
        }
        payload
    }
}

/// Repo root and branch for `cwd`, or `(None, None)` outside a work tree.
pub fn git_context(cwd: &Path) -> (Option<String>, Option<String>) {
    (
        git_output(cwd, &["rev-parse", "--show-toplevel"]),
        git_output(cwd, &["rev-parse", "--abbrev-ref", "HEAD"]),
    )
}

fn git_output(cwd: &Path, args: &[&str]) -> Option<String> {
    let mut command = Command::new("git");
    command.args(args).current_dir(cwd);
    let output = crate::process_identity::output_with_timeout(command, Duration::from_secs(5))?;
    if !output.status.success() {
        return None;
    }
    let value = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}

/// The shared keys every managed registration must carry. Used by the contract
/// test so a new launcher cannot ship with a narrower payload.
#[cfg_attr(not(test), allow(dead_code))]
pub const REQUIRED_REGISTRATION_KEYS: &[&str] = &[
    "cwd",
    "provider",
    "project",
    "git_repo",
    "git_branch",
    "display_name",
    "loop_mode",
    "machine_name",
    "permission_mode",
];

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Map as JsonMap;

    fn registration(provider: &str, extra: Vec<(&'static str, Value)>) -> Value {
        ManagedLaunchRegistration {
            provider,
            cwd: Path::new("/tmp"),
            project: Some("proj"),
            display_name: None,
            loop_mode: "assist",
            machine_name: "cinder",
            permission_mode: PermissionMode::Bypass,
            extra,
        }
        .to_json()
    }

    #[test]
    fn every_provider_payload_carries_the_shared_keys() {
        // The exact regression: opencode and cursor shipped without git context
        // for a month because each launcher wrote its own literal.
        for provider in ["claude", "codex", "cursor", "opencode"] {
            let payload = registration(provider, vec![]);
            let map: &JsonMap<String, Value> = payload.as_object().unwrap();
            for key in REQUIRED_REGISTRATION_KEYS {
                assert!(
                    map.contains_key(*key),
                    "{provider} payload is missing {key}"
                );
            }
            assert_eq!(map["provider"], provider);
        }
    }

    #[test]
    fn provider_extras_do_not_displace_shared_keys() {
        let payload = registration(
            "claude",
            vec![("native_claude_channels_available", json!(true))],
        );
        let map = payload.as_object().unwrap();
        assert_eq!(map["native_claude_channels_available"], json!(true));
        assert!(map.contains_key("git_repo"));
    }

    #[test]
    fn permission_mode_follows_the_flag_that_builds_argv() {
        // Codex reported "bypass" unconditionally while passing
        // --dangerously-bypass-approvals-and-sandbox only on request.
        assert_eq!(PermissionMode::from_bypass_flag(true).as_wire(), "bypass");
        assert_eq!(
            PermissionMode::from_bypass_flag(false).as_wire(),
            "remote_approve"
        );
        assert_eq!(PermissionMode::ProviderLocal.as_wire(), "provider_local");
    }

    #[test]
    fn human_shell_provenance_requires_visible_terminal_and_no_hidden_origin() {
        assert_eq!(
            human_shell_provenance_for_terminal(true, true, false, false),
            (Some("human_shell"), Some("terminal"))
        );
        for (stdin_is_terminal, stdout_is_terminal, hidden, sidechain) in [
            (false, true, false, false),
            (true, false, false, false),
            (true, true, true, false),
            (true, true, false, true),
        ] {
            assert_eq!(
                human_shell_provenance_for_terminal(
                    stdin_is_terminal,
                    stdout_is_terminal,
                    hidden,
                    sidechain,
                ),
                (None, None)
            );
        }
    }

    #[test]
    fn git_context_is_none_outside_a_work_tree() {
        let dir = std::env::temp_dir().join(format!("lh-git-ctx-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (repo, branch) = git_context(&dir);
        // A temp dir may sit inside an unrelated repo on some machines; the
        // contract is only that both fields resolve together.
        assert_eq!(repo.is_none(), branch.is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
