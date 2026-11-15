//! The one place that states how every Console provider runs with respect to
//! permissions.
//!
//! Rule: wherever Longhouse itself chooses the provider's flags (a Console
//! turn the Machine Agent launches), the provider runs in its most permissive
//! mode. There is no per-session choice and no remote approval for Console.
//! Helm passes through exactly the user's own flags and settings and never
//! reads this table; Shadow observes only.
//!
//! Every Console launcher takes its flags or protocol settings from
//! [`console_posture`]. A test checks that each Console-capable provider in
//! `schemas/managed_providers.yml` has an entry here.

/// How a provider is put into bypass.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mechanism {
    /// Extra argv on the provider command line.
    Flags,
    /// Settings sent over the provider's own protocol (Codex app-server).
    ProtocolSettings,
    /// The provider asks over its protocol and the adapter approves each ask.
    ProtocolApproval,
    /// The provider has no permission system, so there is nothing to bypass.
    NoPermissionSystem,
}

#[derive(Clone, Copy, Debug)]
pub struct ConsolePosture {
    pub provider: &'static str,
    pub mechanism: Mechanism,
    /// Argv the Console launcher always adds.
    pub argv: &'static [&'static str],
    /// Codex-style approval policy and sandbox, when set over a protocol.
    pub approval_policy: Option<&'static str>,
    pub sandbox: Option<&'static str>,
    pub note: &'static str,
}

pub const CODEX_APPROVAL_POLICY: &str = "never";
pub const CODEX_SANDBOX: &str = "danger-full-access";

pub const CONSOLE_POSTURES: &[ConsolePosture] = &[
    ConsolePosture {
        provider: "claude",
        mechanism: Mechanism::Flags,
        argv: &["--dangerously-skip-permissions"],
        approval_policy: None,
        sandbox: None,
        note: "claude --print bypassPermissions",
    },
    ConsolePosture {
        provider: "codex",
        mechanism: Mechanism::ProtocolSettings,
        argv: &[],
        approval_policy: Some(CODEX_APPROVAL_POLICY),
        sandbox: Some(CODEX_SANDBOX),
        note: "app-server approval_policy never + sandbox danger-full-access; any approval request that still arrives is accepted",
    },
    ConsolePosture {
        provider: "opencode",
        mechanism: Mechanism::ProtocolApproval,
        argv: &[],
        approval_policy: None,
        sandbox: None,
        note: "every permission.asked is replied once; questions are rejected (nobody to answer)",
    },
    ConsolePosture {
        provider: "cursor",
        mechanism: Mechanism::Flags,
        argv: &["--trust", "--force", "--approve-mcps"],
        approval_policy: None,
        sandbox: None,
        note: "cursor-agent --print: trust workspace, force-allow commands, approve MCP servers",
    },
    ConsolePosture {
        provider: "pi",
        mechanism: Mechanism::NoPermissionSystem,
        argv: &[],
        approval_policy: None,
        sandbox: None,
        note: "Pi has no permission prompts or sandbox; project trust only gates loading project extensions and is not a tool permission",
    },
    ConsolePosture {
        provider: "omp",
        mechanism: Mechanism::Flags,
        argv: &["--auto-approve"],
        approval_policy: None,
        sandbox: None,
        note: "omp --auto-approve (alias --yolo) skips tool approval prompts; same as tools.approvalMode yolo",
    },
    ConsolePosture {
        provider: "antigravity",
        mechanism: Mechanism::Flags,
        argv: &["--dangerously-skip-permissions"],
        approval_policy: None,
        sandbox: None,
        note: "agy --dangerously-skip-permissions",
    },
];

pub fn console_posture(provider: &str) -> Option<&'static ConsolePosture> {
    CONSOLE_POSTURES
        .iter()
        .find(|posture| posture.provider == provider)
}

/// The posture a Console launcher must use. A provider with no entry is a
/// programming error: Console must never start without a stated posture.
pub fn require_console_posture(provider: &str) -> &'static ConsolePosture {
    console_posture(provider)
        .unwrap_or_else(|| panic!("Console provider {provider} has no permission posture"))
}

pub fn console_argv(provider: &str) -> Vec<String> {
    require_console_posture(provider)
        .argv
        .iter()
        .map(|arg| (*arg).to_string())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Console-capable providers come from the managed-provider schema, so a
    /// new Console provider cannot ship without a posture.
    fn console_providers() -> Vec<String> {
        let contracts: serde_json::Value =
            serde_json::from_str(include_str!("managed_provider_contracts.generated.json"))
                .unwrap();
        contracts["providers"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|entry| {
                entry["console_adapter"]
                    .as_str()
                    .is_some_and(|adapter| !adapter.trim().is_empty())
            })
            .map(|entry| entry["provider"].as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn every_console_provider_has_a_bypass_posture() {
        let providers = console_providers();
        assert!(providers.len() >= 7, "schema lists {providers:?}");
        for provider in &providers {
            let posture = console_posture(provider)
                .unwrap_or_else(|| panic!("{provider} is Console-capable but has no posture"));
            let stated = match posture.mechanism {
                Mechanism::Flags => !posture.argv.is_empty(),
                Mechanism::ProtocolSettings => {
                    posture.approval_policy.is_some() && posture.sandbox.is_some()
                }
                Mechanism::ProtocolApproval | Mechanism::NoPermissionSystem => {
                    !posture.note.is_empty()
                }
            };
            assert!(stated, "{provider} posture states no bypass");
        }
        for posture in CONSOLE_POSTURES {
            assert!(
                providers.iter().any(|p| p == posture.provider),
                "posture for {} which is not Console-capable",
                posture.provider
            );
        }
    }

    #[test]
    fn postures_are_the_most_permissive_settings() {
        let codex = require_console_posture("codex");
        assert_eq!(codex.approval_policy, Some("never"));
        assert_eq!(codex.sandbox, Some("danger-full-access"));
        for provider in ["claude", "antigravity"] {
            assert_eq!(
                console_argv(provider),
                vec!["--dangerously-skip-permissions".to_string()]
            );
        }
        assert_eq!(console_argv("omp"), vec!["--auto-approve".to_string()]);
    }

    /// Helm passes through the user's own flags and adds no permission flag.
    /// The Helm launchers build argv inline, so this scans their non-test
    /// source for the bypass flags that belong to Console. (Cursor Helm's
    /// explicit `auto_approve` choice is covered by a behavior test beside its
    /// argv builder; Codex Helm's own `--dangerously-bypass-approvals-and-sandbox`
    /// is only ever the user's typed flag, forwarded.)
    #[test]
    fn helm_launchers_never_add_a_console_bypass_flag() {
        let mut forbidden: Vec<&str> = CONSOLE_POSTURES
            .iter()
            .flat_map(|posture| posture.argv.iter().copied())
            .filter(|arg| !matches!(*arg, "--trust" | "--force"))
            .collect();
        forbidden.extend(["--yolo", "--approval-mode"]);
        let helm_sources: [(&str, &str); 8] = [
            ("longhouse.rs", include_str!("longhouse.rs")),
            ("pi_helm_launcher.rs", include_str!("pi_helm_launcher.rs")),
            ("omp_helm_launcher.rs", include_str!("omp_helm_launcher.rs")),
            ("opencode_bridge.rs", include_str!("opencode_bridge.rs")),
            ("claude_helm_live.rs", include_str!("claude_helm_live.rs")),
            (
                "antigravity_helm_live.rs",
                include_str!("antigravity_helm_live.rs"),
            ),
            (
                "cursor_helm_launcher.rs",
                include_str!("cursor_helm_launcher.rs"),
            ),
            ("codex_bridge.rs", include_str!("codex_bridge.rs")),
        ];
        for (name, source) in helm_sources {
            let production = source.split("#[cfg(test)]").next().unwrap();
            for flag in &forbidden {
                let literal = format!("\"{flag}\"");
                let found = production
                    .lines()
                    .filter(|line| !line.trim_start().starts_with("//"))
                    .any(|line| line.contains(&literal));
                // Cursor Helm keeps one explicit-request branch, tested apart.
                let allowed = name == "cursor_helm_launcher.rs" && *flag == "--approve-mcps";
                // The Codex bridge's own CLI takes --auto-approve from its
                // caller (a QA canary), never from a Helm launch.
                let bridge = name == "codex_bridge.rs" && *flag == "--auto-approve";
                assert!(
                    !found || allowed || bridge,
                    "{name} adds {flag}: Helm must pass through only the user's own flags"
                );
            }
        }
    }
}
