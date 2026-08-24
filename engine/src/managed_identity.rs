//! The identity a managed provider process carries, and the keys it never inherits.
//!
//! Thirteen launch sites each hand-wrote this block. Five of them got it wrong,
//! in three different ways, and every one of those was found by a person rather
//! than a check:
//!
//! - Cursor Helm exported `LONGHOUSE_SESSION_ID`, a name no consumer reads, so a
//!   helmed Cursor session could not name itself (`d5eb907d8`, 2026-08-24).
//! - Cursor Helm then handed its child the *parent* session's channel and run
//!   ids, because it only dropped the keys it went on to set (`9d9999f6e`).
//! - OpenCode Helm and Codex Console set the session id but never the owner tag,
//!   so a hook could not tell whose claim it was reading.
//! - Cursor Console and OpenCode Console set no identity at all.
//!
//! The contract, in one sentence: **a `LONGHOUSE_*` identity key is set by this
//! launcher or it is absent — never inherited.** Applying the overlay scrubs
//! every such key, then writes the two the launcher must always carry. A
//! launcher that legitimately owns more (Claude's channel ids, Antigravity's
//! inbox dirs, the hook credentials) sets them back afterwards.
//!
//! This is deliberately an overlay and not a launch-env constructor. The
//! launchers are not one function: Cursor Helm builds `Vec<CString>` for a raw
//! `execve` after `forkpty`, Codex attach uses `command.exec()`, and the rest
//! spawn a `std::process::Command`. Each keeps owning its argv, cwd, PTY and
//! credentials; only identity is shared.

use crate::managed_identity_contract::{
    ManagedProvider, NEVER_INHERITED_KEYS, REQUIRED_IDENTITY_KEYS,
};

/// Somewhere an environment can be written. The launchers spawn through three
/// different mechanisms -- `std::process::Command`, `tokio::process::Command`,
/// and a raw `execve` -- and the overlay has to reach all of them without
/// forcing them into one spawn path.
pub trait EnvSink {
    fn set_var(&mut self, key: &str, value: &str);
    fn unset_var(&mut self, key: &str);
}

impl EnvSink for std::process::Command {
    fn set_var(&mut self, key: &str, value: &str) {
        self.env(key, value);
    }
    fn unset_var(&mut self, key: &str) {
        self.env_remove(key);
    }
}

impl EnvSink for tokio::process::Command {
    fn set_var(&mut self, key: &str, value: &str) {
        self.env(key, value);
    }
    fn unset_var(&mut self, key: &str) {
        self.env_remove(key);
    }
}

/// The session a provider process belongs to, and the provider that owns the claim.
#[derive(Debug, Clone)]
pub struct ManagedIdentity {
    provider: ManagedProvider,
    session_id: String,
    run_id: Option<String>,
}

impl ManagedIdentity {
    /// A provider cannot be a free string here: it comes from the schema-derived
    /// enum, so `LONGHOUSE_MANAGED_PROVIDER` cannot disagree with the registry.
    pub fn new(provider: ManagedProvider, session_id: impl Into<String>) -> Self {
        Self {
            provider,
            session_id: session_id.into(),
            run_id: None,
        }
    }

    /// Console adapters carry a run id; Helm launchers mostly do not. Absent is a
    /// valid state — what is never valid is inheriting someone else's.
    pub fn with_run_id(mut self, run_id: impl Into<String>) -> Self {
        self.run_id = Some(run_id.into());
        self
    }

    pub fn provider(&self) -> ManagedProvider {
        self.provider
    }

    pub fn session_id(&self) -> &str {
        &self.session_id
    }

    /// The keys this identity sets, in order. Everything else in
    /// `NEVER_INHERITED_KEYS` is scrubbed.
    fn overlay(&self) -> Vec<(&'static str, String)> {
        let mut pairs = vec![
            ("LONGHOUSE_MANAGED_SESSION_ID", self.session_id.clone()),
            ("LONGHOUSE_MANAGED_PROVIDER", self.provider.as_str().into()),
        ];
        if let Some(run_id) = &self.run_id {
            pairs.push(("LONGHOUSE_RUN_ID", run_id.clone()));
        }
        pairs
    }

    /// Scrub every inheritable identity key without claiming one. An anonymous
    /// worker is meant to carry no session; without this it carries whichever
    /// session happened to spawn it.
    pub fn scrub<S: EnvSink>(sink: &mut S) {
        for key in NEVER_INHERITED_KEYS {
            sink.unset_var(key);
        }
    }

    /// Scrub every inheritable identity key, then set this launcher's own. Call
    /// this **before** the launcher sets the keys it owns, or the scrub will
    /// remove them.
    pub fn apply<S: EnvSink>(&self, sink: &mut S) {
        for key in NEVER_INHERITED_KEYS {
            sink.unset_var(key);
        }
        for (key, value) in self.overlay() {
            sink.set_var(key, &value);
        }
    }

    /// Apply to a raw environment list for `execve`, which cannot go through
    /// `Command`. Returns the scrubbed inherited pairs with the overlay appended:
    /// a duplicate key in an exec environment resolves to whichever copy comes
    /// first, so the inherited copies must be dropped rather than shadowed.
    pub fn apply_to_pairs(
        &self,
        inherited: impl Iterator<Item = (Vec<u8>, Vec<u8>)>,
    ) -> Vec<(Vec<u8>, Vec<u8>)> {
        let mut pairs: Vec<(Vec<u8>, Vec<u8>)> = inherited
            .filter(|(key, _)| {
                !NEVER_INHERITED_KEYS
                    .iter()
                    .any(|scrubbed| key.as_slice() == scrubbed.as_bytes())
            })
            .collect();
        for (key, value) in self.overlay() {
            pairs.push((key.as_bytes().to_vec(), value.into_bytes()));
        }
        pairs
    }
}

/// Every key the overlay guarantees is present. Exposed for conformance tests.
pub fn required_keys() -> &'static [&'static str] {
    REQUIRED_IDENTITY_KEYS
}

/// Every key a provider process must never inherit. Exposed for conformance tests.
pub fn never_inherited_keys() -> &'static [&'static str] {
    NEVER_INHERITED_KEYS
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn pairs(input: &[(&str, &str)]) -> Vec<(Vec<u8>, Vec<u8>)> {
        input
            .iter()
            .map(|(k, v)| (k.as_bytes().to_vec(), v.as_bytes().to_vec()))
            .collect()
    }

    fn applied(identity: &ManagedIdentity, inherited: &[(&str, &str)]) -> HashMap<String, String> {
        identity
            .apply_to_pairs(pairs(inherited).into_iter())
            .into_iter()
            .map(|(k, v)| {
                (
                    String::from_utf8(k).unwrap(),
                    String::from_utf8(v).unwrap(),
                )
            })
            .collect()
    }

    #[test]
    fn overlay_sets_the_two_keys_every_managed_process_carries() {
        let identity = ManagedIdentity::new(ManagedProvider::Cursor, "session-123");
        let env = applied(&identity, &[("PATH", "/usr/bin")]);
        assert_eq!(env["LONGHOUSE_MANAGED_SESSION_ID"], "session-123");
        assert_eq!(env["LONGHOUSE_MANAGED_PROVIDER"], "cursor");
        assert_eq!(env["PATH"], "/usr/bin");
        for key in required_keys() {
            assert!(env.contains_key(*key), "{key} must always be present");
        }
    }

    #[test]
    fn no_identity_key_is_ever_inherited() {
        // The whole bug class in one assertion: whatever the parent had, the
        // child sees this launcher's values or nothing.
        let inherited: Vec<(&str, &str)> = never_inherited_keys()
            .iter()
            .map(|key| (*key, "parent-value"))
            .chain(std::iter::once(("PATH", "/usr/bin")))
            .collect();
        let identity = ManagedIdentity::new(ManagedProvider::Opencode, "session-123");
        let env = applied(&identity, &inherited);

        assert_eq!(env["LONGHOUSE_MANAGED_SESSION_ID"], "session-123");
        assert_eq!(env["LONGHOUSE_MANAGED_PROVIDER"], "opencode");
        for key in never_inherited_keys() {
            let inherited_survived = env.get(*key).map(String::as_str) == Some("parent-value");
            assert!(!inherited_survived, "{key} was inherited from the parent");
        }
        assert!(!env.contains_key("LONGHOUSE_SESSION_ID"), "retired key was set");
        assert_eq!(env["PATH"], "/usr/bin");
    }

    #[test]
    fn every_identity_key_appears_exactly_once() {
        // A duplicate key in an exec environment resolves to whichever copy comes
        // first, which would hand the child its parent's identity.
        let inherited: Vec<(&str, &str)> = never_inherited_keys()
            .iter()
            .map(|key| (*key, "parent-value"))
            .collect();
        let identity =
            ManagedIdentity::new(ManagedProvider::Claude, "session-123").with_run_id("run-456");
        let result = identity.apply_to_pairs(pairs(&inherited).into_iter());
        for key in never_inherited_keys() {
            let count = result
                .iter()
                .filter(|(name, _)| name.as_slice() == key.as_bytes())
                .count();
            assert!(count <= 1, "{key} appears {count} times");
        }
        let env = applied(&identity, &inherited);
        assert_eq!(env["LONGHOUSE_RUN_ID"], "run-456");
    }

    #[test]
    fn an_absent_run_id_is_absent_rather_than_inherited() {
        let identity = ManagedIdentity::new(ManagedProvider::Codex, "session-123");
        let env = applied(&identity, &[("LONGHOUSE_RUN_ID", "parent-run")]);
        assert!(!env.contains_key("LONGHOUSE_RUN_ID"));
    }

    #[derive(Default)]
    struct RecordingSink {
        set: Vec<(String, String)>,
        unset: Vec<String>,
    }

    impl EnvSink for RecordingSink {
        fn set_var(&mut self, key: &str, value: &str) {
            self.set.push((key.into(), value.into()));
        }
        fn unset_var(&mut self, key: &str) {
            self.unset.push(key.into());
        }
    }

    #[test]
    fn apply_scrubs_before_it_sets() {
        // Order is load-bearing: a launcher calls apply() and then sets the keys
        // it owns. If the scrub ran last it would erase them.
        let mut sink = RecordingSink::default();
        ManagedIdentity::new(ManagedProvider::Antigravity, "session-123").apply(&mut sink);
        for key in never_inherited_keys() {
            assert!(sink.unset.contains(&(*key).to_string()), "{key} not scrubbed");
        }
        assert_eq!(
            sink.set,
            vec![
                ("LONGHOUSE_MANAGED_SESSION_ID".to_string(), "session-123".to_string()),
                ("LONGHOUSE_MANAGED_PROVIDER".to_string(), "antigravity".to_string()),
            ]
        );
    }

    #[test]
    fn provider_tag_comes_from_the_schema_derived_enum() {
        for provider in crate::managed_identity_contract::ALL_MANAGED_PROVIDERS {
            let identity = ManagedIdentity::new(*provider, "session-123");
            let env = applied(&identity, &[]);
            assert_eq!(env["LONGHOUSE_MANAGED_PROVIDER"], provider.as_str());
        }
    }
}
