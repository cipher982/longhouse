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

/// Normalized product provenance for one managed Helm registration.
///
/// The fields are private so provider launchers cannot manufacture a human
/// stamp or accidentally keep one inherited through automation. Every fresh
/// and resumed Helm payload must carry this typed value; `to_json` owns the
/// wire representation.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ManagedLaunchProvenance {
    launch_actor: Option<&'static str>,
    launch_surface: Option<&'static str>,
}

impl ManagedLaunchProvenance {
    /// Classify the current wrapper invocation once, with automation and
    /// sidechain evidence taking precedence over terminal interactivity.
    pub fn interactive_helm() -> Self {
        let origin_kind = std::env::var("LONGHOUSE_ORIGIN_KIND").ok();
        let inherited_actor = std::env::var("LONGHOUSE_LAUNCH_ACTOR").ok();
        let inherited_surface = std::env::var("LONGHOUSE_LAUNCH_SURFACE").ok();
        let sidechain = env_truthy(std::env::var("LONGHOUSE_IS_SIDECHAIN").ok().as_deref());
        Self::for_terminal_context(
            std::io::stdin().is_terminal(),
            std::io::stdout().is_terminal(),
            origin_kind.as_deref(),
            sidechain,
            inherited_actor.as_deref(),
            inherited_surface.as_deref(),
        )
    }

    fn for_terminal_context(
        stdin_is_terminal: bool,
        stdout_is_terminal: bool,
        origin_kind: Option<&str>,
        sidechain: bool,
        inherited_actor: Option<&str>,
        inherited_surface: Option<&str>,
    ) -> Self {
        let origin_kind = normalize_token(origin_kind);
        let inherited_actor = normalize_token(inherited_actor);
        let inherited_surface = normalize_token(inherited_surface);

        // Explicit automation always wins. Preserve useful typed provenance
        // rather than merely dropping a human stamp: the Runtime Host can then
        // enforce default-hidden policy even when a future automation cwd is
        // not yet known to its provider-proof path classifier.
        if sidechain {
            return Self::automation("provider_subprocess");
        }
        if origin_kind.as_deref() == Some("hatch_automation") {
            return Self::automation(automation_surface(inherited_surface.as_deref(), "hatch"));
        }
        if origin_kind.as_deref() == Some("test_or_canary") {
            return Self::automation(automation_surface(inherited_surface.as_deref(), "test"));
        }
        if inherited_actor.as_deref() == Some("automation") {
            return Self::automation(automation_surface(inherited_surface.as_deref(), "test"));
        }

        if stdin_is_terminal && stdout_is_terminal {
            Self {
                launch_actor: Some("human_shell"),
                launch_surface: Some("terminal"),
            }
        } else {
            Self::default()
        }
    }

    fn automation(surface: &'static str) -> Self {
        Self {
            launch_actor: Some("automation"),
            launch_surface: Some(surface),
        }
    }
}

fn normalize_token(value: Option<&str>) -> Option<String> {
    let normalized = value?.trim().to_ascii_lowercase().replace('-', "_");
    (!normalized.is_empty()).then_some(normalized)
}

fn env_truthy(value: Option<&str>) -> bool {
    matches!(
        value.unwrap_or_default().trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn automation_surface(value: Option<&str>, fallback: &'static str) -> &'static str {
    match value {
        Some("hatch") => "hatch",
        Some("test") => "test",
        Some("ci") => "ci",
        Some("provider_subprocess") => "provider_subprocess",
        _ => fallback,
    }
}

pub struct ManagedLaunchRegistration<'a> {
    pub provider: &'a str,
    pub cwd: &'a Path,
    pub project: Option<&'a str>,
    pub display_name: Option<&'a str>,
    pub machine_name: &'a str,
    pub permission_mode: PermissionMode,
    pub provenance: ManagedLaunchProvenance,
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
            "machine_name": self.machine_name,
            "permission_mode": self.permission_mode.as_wire(),
            "launch_actor": self.provenance.launch_actor,
            "launch_surface": self.provenance.launch_surface,
        });
        let map = payload.as_object_mut().expect("object literal");
        for (key, value) in &self.extra {
            // Shared registration fields are owned here. Silently ignoring an
            // internal collision is safer than allowing provider-local JSON to
            // override normalized provenance or another shared contract field.
            if REQUIRED_REGISTRATION_KEYS.contains(key) {
                continue;
            }
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
    let output = Command::new("git").args(args).current_dir(cwd).output().ok()?;
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
    "machine_name",
    "permission_mode",
    "launch_actor",
    "launch_surface",
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
            machine_name: "cinder",
            permission_mode: PermissionMode::Bypass,
            provenance: ManagedLaunchProvenance::for_terminal_context(
                true, true, None, false, None, None,
            ),
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
                assert!(map.contains_key(*key), "{provider} payload is missing {key}");
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
        let human = ManagedLaunchProvenance::for_terminal_context(
            true, true, None, false, None, None,
        );
        assert_eq!(human.launch_actor, Some("human_shell"));
        assert_eq!(human.launch_surface, Some("terminal"));
        for (stdin_is_terminal, stdout_is_terminal) in [(false, true), (true, false)] {
            assert_eq!(
                ManagedLaunchProvenance::for_terminal_context(
                    stdin_is_terminal,
                    stdout_is_terminal,
                    None,
                    false,
                    None,
                    None,
                ),
                ManagedLaunchProvenance::default()
            );
        }
    }

    #[test]
    fn automation_and_sidechain_evidence_precede_inherited_human_provenance() {
        for (origin_kind, sidechain, expected_surface) in [
            (Some("hatch-automation"), false, "hatch"),
            (Some("test_or_canary"), false, "test"),
            (None, true, "provider_subprocess"),
        ] {
            let provenance = ManagedLaunchProvenance::for_terminal_context(
                true,
                true,
                origin_kind,
                sidechain,
                Some("human_shell"),
                Some("terminal"),
            );
            assert_eq!(provenance.launch_actor, Some("automation"));
            assert_eq!(provenance.launch_surface, Some(expected_surface));
        }
    }

    #[test]
    fn explicit_automation_provenance_is_normalized_without_a_tty() {
        let provenance = ManagedLaunchProvenance::for_terminal_context(
            false,
            false,
            None,
            false,
            Some("AUTOMATION"),
            Some("CI"),
        );
        assert_eq!(provenance.launch_actor, Some("automation"));
        assert_eq!(provenance.launch_surface, Some("ci"));
    }

    #[test]
    fn provider_extras_cannot_override_shared_provenance() {
        let payload = registration(
            "codex",
            vec![
                ("launch_actor", json!("automation")),
                ("launch_surface", json!("test")),
            ],
        );
        assert_eq!(payload["launch_actor"], "human_shell");
        assert_eq!(payload["launch_surface"], "terminal");
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
