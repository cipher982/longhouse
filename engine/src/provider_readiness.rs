//! Why a provider cannot run here, rather than only whether it can.
//!
//! The Machine Agent used to advertise a provider on one fact: its binary was
//! found on PATH. Present is not authenticated and not able to run, so a CLI
//! that was installed but signed out was offered in the launch picker and then
//! failed at turn time as `adapter_unavailable` -- a dead end whose cause the
//! machine already knew and threw away.
//!
//! The probe that answers "is this credential real" is declared per provider in
//! `schemas/managed_providers.yml` and travels in the embedded contract
//! manifest, so the engine never decides for itself what authenticated means.
//! Providers with no safe way to ask report `unknown`; nothing here guesses.

use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::ffi::OsString;
use std::time::Duration;

use serde_json::Value;

/// A probe is a liveness question, not work. If a provider CLI cannot answer
/// within this budget it is treated as unanswerable rather than waited on --
/// readiness is computed on the hello path and must not hold it open.
const PROBE_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadinessState {
    /// Installed and holding a credential the provider itself accepts.
    Ready,
    /// No binary. The user has not installed this provider here.
    CliMissing,
    /// Binary present, provider says it is not signed in.
    NotAuthenticated,
    /// No safe way to ask. Never inferred from binary presence.
    Unknown,
}

impl ReadinessState {
    pub fn as_str(self) -> &'static str {
        match self {
            ReadinessState::Ready => "ready",
            ReadinessState::CliMissing => "cli_missing",
            ReadinessState::NotAuthenticated => "not_authenticated",
            ReadinessState::Unknown => "unknown",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProviderReadiness {
    pub provider: String,
    pub state: ReadinessState,
    /// Human-facing description of the credential in use. Never the credential.
    pub detail: Option<String>,
    /// Name of an environment credential that outranks the interactive login.
    pub credential_override: Option<String>,
    /// What the user would do next. Absent when there is nothing honest to say.
    pub remediation: Option<String>,
}

impl ProviderReadiness {
    pub fn to_json(&self) -> Value {
        let mut map = serde_json::Map::new();
        map.insert("provider".into(), Value::String(self.provider.clone()));
        map.insert("state".into(), Value::String(self.state.as_str().into()));
        for (key, value) in [
            ("detail", &self.detail),
            ("credential_override", &self.credential_override),
            ("remediation", &self.remediation),
        ] {
            if let Some(value) = value {
                map.insert(key.into(), Value::String(value.clone()));
            }
        }
        Value::Object(map)
    }
}

/// What the manifest says about asking this provider.
enum ProbePlan<'a> {
    ExitCode { argv: Vec<&'a str> },
    Json { argv: Vec<&'a str>, fields: JsonFields<'a> },
    /// No probe to run, and the manifest's stated reason why.
    Unavailable { reason: Option<&'a str> },
}

struct JsonFields<'a> {
    logged_in: &'a str,
    plan: Option<&'a str>,
    credential_override: Option<&'a str>,
}

fn probe_plan(contract: &Value) -> ProbePlan<'_> {
    let Some(probe) = contract.get("auth_probe") else {
        return ProbePlan::Unavailable { reason: None };
    };
    let disposition = probe.get("disposition").and_then(Value::as_str);
    if disposition != Some("implemented") {
        // `not_implemented` carries the observation that would close it;
        // `upstream_absent` and `policy_disabled` carry why they are decisions.
        // Either way the manifest already wrote the sentence a user should see.
        let reason = probe
            .get("owner_action")
            .and_then(Value::as_str)
            .or_else(|| probe.get("reason").and_then(Value::as_str));
        return ProbePlan::Unavailable { reason };
    }
    let argv: Vec<&str> = probe
        .get("argv")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    match probe.get("format").and_then(Value::as_str) {
        Some("json") => {
            let Some(logged_in) = probe.get("logged_in_field").and_then(Value::as_str) else {
                return ProbePlan::Unavailable { reason: None };
            };
            ProbePlan::Json {
                argv,
                fields: JsonFields {
                    logged_in,
                    plan: probe.get("plan_field").and_then(Value::as_str),
                    credential_override: probe.get("credential_override_field").and_then(Value::as_str),
                },
            }
        }
        _ => ProbePlan::ExitCode { argv },
    }
}

/// Read a JSON probe's stdout.
///
/// Deliberately never returns the raw document. `claude auth status` includes
/// the account email and organization id, and a probe result is fanned out to
/// the Runtime Host -- only the derived facts leave this function.
fn interpret_json(fields: &JsonFields<'_>, stdout: &str) -> (ReadinessState, Option<String>, Option<String>) {
    let Ok(payload) = serde_json::from_str::<Value>(stdout.trim()) else {
        return (ReadinessState::Unknown, None, None);
    };
    let logged_in = payload.get(fields.logged_in).and_then(Value::as_bool);
    let credential_override = fields
        .credential_override
        .and_then(|field| payload.get(field))
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_string);
    let plan = fields
        .plan
        .and_then(|field| payload.get(field))
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_string);
    match logged_in {
        Some(true) => {
            // The case worth catching: an environment credential silently
            // outranks the interactive login. `loggedIn` stays true and the
            // auth method still names the subscription, so reporting only
            // "ready" would tell someone they were spending a plan they were
            // not. Say which credential actually wins.
            let detail = match (&credential_override, &plan) {
                (Some(name), _) => Some(format!("billing to {name}, overriding the signed-in account")),
                (None, Some(plan)) => Some(format!("{plan} plan")),
                (None, None) => None,
            };
            (ReadinessState::Ready, detail, credential_override)
        }
        Some(false) => (ReadinessState::NotAuthenticated, None, None),
        None => (ReadinessState::Unknown, None, None),
    }
}

fn remediation_for(state: ReadinessState, binary: &str) -> Option<String> {
    match state {
        ReadinessState::Ready => None,
        ReadinessState::CliMissing => Some(format!("Install the {binary} CLI on this machine")),
        ReadinessState::NotAuthenticated => Some(format!("Sign in to {binary} on this machine")),
        // There is no honest instruction for a provider we cannot even ask
        // about. The manifest's sentence explains the gap and belongs in
        // `detail`; putting it here would hand a user our own backlog note as
        // if it were their next step.
        ReadinessState::Unknown => None,
    }
}

/// Resolve one provider's readiness, running its declared probe if it has one.
pub async fn readiness_for_contract(
    contract: &Value,
    binary: Option<OsString>,
    binary_on_path: bool,
) -> ProviderReadiness {
    let provider = contract
        .get("provider")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let display_binary = contract
        .get("provider_cli_binary")
        .and_then(Value::as_str)
        .unwrap_or(provider.as_str())
        .to_string();

    if !binary_on_path {
        return ProviderReadiness {
            provider,
            state: ReadinessState::CliMissing,
            detail: None,
            credential_override: None,
            remediation: remediation_for(ReadinessState::CliMissing, &display_binary),
        };
    }

    let plan = probe_plan(contract);
    let (argv, json_fields) = match &plan {
        ProbePlan::Unavailable { reason } => {
            return ProviderReadiness {
                provider,
                state: ReadinessState::Unknown,
                detail: reason.map(str::to_string),
                credential_override: None,
                remediation: None,
            };
        }
        ProbePlan::ExitCode { argv } => (argv, None),
        ProbePlan::Json { argv, fields } => (argv, Some(fields)),
    };

    let binary = binary.unwrap_or_else(|| OsString::from(&display_binary));
    let output = run_probe(&binary, argv).await;
    let (state, detail, credential_override) = match output {
        // A probe that could not be executed says nothing about the credential.
        None => (ReadinessState::Unknown, None, None),
        Some((exit_ok, stdout)) => match json_fields {
            Some(fields) => interpret_json(fields, &stdout),
            None if exit_ok => (ReadinessState::Ready, None, None),
            None => (ReadinessState::NotAuthenticated, None, None),
        },
    };

    ProviderReadiness {
        provider,
        remediation: remediation_for(state, &display_binary),
        state,
        detail,
        credential_override,
    }
}

async fn run_probe(binary: &OsStr, argv: &[&str]) -> Option<(bool, String)> {
    let mut command = tokio::process::Command::new(binary);
    command.args(argv);
    command.stdin(std::process::Stdio::null());
    command.kill_on_drop(true);
    let output = tokio::time::timeout(PROBE_TIMEOUT, command.output()).await;
    match output {
        Ok(Ok(output)) => Some((
            output.status.success(),
            String::from_utf8_lossy(&output.stdout).into_owned(),
        )),
        // Both a spawn failure and a timeout mean unanswered, not signed out.
        // Reporting NotAuthenticated here would tell users to re-run a login
        // that was never the problem.
        _ => None,
    }
}

/// Readiness keyed by provider, for the hello frame.
pub fn readiness_map(entries: Vec<ProviderReadiness>) -> Value {
    let mut map = BTreeMap::new();
    for entry in entries {
        map.insert(entry.provider.clone(), entry.to_json());
    }
    Value::Object(map.into_iter().collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn json_fields() -> JsonFields<'static> {
        JsonFields {
            logged_in: "loggedIn",
            plan: Some("subscriptionType"),
            credential_override: Some("apiKeySource"),
        }
    }

    #[test]
    fn an_environment_credential_that_outranks_the_login_is_named() {
        // Observed against the real CLI: with ANTHROPIC_API_KEY set, loggedIn
        // stays true and authMethod still reports claude.ai, while
        // subscriptionType goes null. Reporting a bare "ready" here would tell
        // someone they were spending a plan they were not.
        let stdout = r#"{"loggedIn":true,"authMethod":"claude.ai","apiKeySource":"ANTHROPIC_API_KEY","subscriptionType":null}"#;
        let (state, detail, override_name) = interpret_json(&json_fields(), stdout);
        assert_eq!(state, ReadinessState::Ready);
        assert_eq!(override_name.as_deref(), Some("ANTHROPIC_API_KEY"));
        assert!(detail.unwrap().contains("ANTHROPIC_API_KEY"));
    }

    #[test]
    fn a_plain_subscription_login_reports_its_plan() {
        let stdout = r#"{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"max"}"#;
        let (state, detail, override_name) = interpret_json(&json_fields(), stdout);
        assert_eq!(state, ReadinessState::Ready);
        assert_eq!(detail.as_deref(), Some("max plan"));
        assert_eq!(override_name, None);
    }

    #[test]
    fn a_signed_out_cli_is_not_authenticated_rather_than_missing() {
        let stdout = r#"{"loggedIn":false,"authMethod":"none"}"#;
        let (state, _, _) = interpret_json(&json_fields(), stdout);
        assert_eq!(state, ReadinessState::NotAuthenticated);
    }

    #[test]
    fn unreadable_probe_output_is_unknown_not_a_verdict() {
        // A CLI that changed its output shape must not silently start
        // reporting every machine as signed out.
        for stdout in ["", "not json at all", "{}", r#"{"loggedIn":"yes"}"#] {
            let (state, _, _) = interpret_json(&json_fields(), stdout);
            assert_eq!(state, ReadinessState::Unknown, "stdout={stdout:?}");
        }
    }

    #[test]
    fn probe_output_never_leaks_identity_into_the_result() {
        // claude auth status carries the account email and organization id.
        // Readiness is fanned out to the Runtime Host, so only derived facts
        // may leave the interpreter.
        let stdout = r#"{"loggedIn":true,"subscriptionType":"max","email":"someone@example.com","orgId":"5e0bd722-654f-42eb-ae93-58de2bac0876"}"#;
        let (_, detail, override_name) = interpret_json(&json_fields(), stdout);
        let rendered = format!("{detail:?}{override_name:?}");
        assert!(!rendered.contains("example.com"), "{rendered}");
        assert!(!rendered.contains("5e0bd722"), "{rendered}");
    }

    #[test]
    fn a_provider_with_no_declared_probe_is_unknown_and_says_why() {
        let contract = serde_json::json!({
            "provider": "antigravity",
            "provider_cli_binary": "agy",
            "auth_probe": {
                "disposition": "upstream_absent",
                "reason": "the agy CLI has no auth/login/status subcommand",
            },
        });
        match probe_plan(&contract) {
            ProbePlan::Unavailable { reason } => {
                assert_eq!(reason, Some("the agy CLI has no auth/login/status subcommand"));
            }
            _ => panic!("a provider without an implemented probe must not be run"),
        }
    }

    #[test]
    fn a_policy_refusal_never_produces_a_command_to_run() {
        // pi's only auth surface prints the credential itself to stdout.
        // Nothing in this module may turn that into an executable probe.
        let contract = serde_json::json!({
            "provider": "pi",
            "provider_cli_binary": "pi",
            "auth_probe": {
                "disposition": "policy_disabled",
                "reason": "pi's only auth surface prints the credential to stdout",
            },
        });
        assert!(matches!(probe_plan(&contract), ProbePlan::Unavailable { .. }));
    }

    #[tokio::test]
    async fn an_unprobeable_provider_explains_itself_without_prescribing_a_fix() {
        // The manifest sentence for a gap is Longhouse's own backlog note. It
        // belongs in detail as an explanation, never in remediation, where a
        // user would read it as the step they are supposed to take.
        let contract = serde_json::json!({
            "provider": "opencode",
            "provider_cli_binary": "opencode",
            "auth_probe": {
                "disposition": "not_implemented",
                "owner_action": "observe `opencode auth list` with an empty credential store",
            },
        });
        let readiness = readiness_for_contract(&contract, Some(OsString::from("opencode")), true).await;
        assert_eq!(readiness.state, ReadinessState::Unknown);
        assert!(readiness.detail.unwrap().contains("empty credential store"));
        assert_eq!(readiness.remediation, None);
    }

    #[tokio::test]
    async fn a_missing_binary_reports_installation_not_a_login_problem() {
        let contract = serde_json::json!({
            "provider": "codex",
            "provider_cli_binary": "codex",
            "auth_probe": {"disposition": "implemented", "argv": ["login", "status"], "format": "exit_code"},
        });
        let readiness = readiness_for_contract(&contract, None, false).await;
        assert_eq!(readiness.state, ReadinessState::CliMissing);
        assert!(readiness.remediation.unwrap().contains("Install"));
    }

    #[tokio::test]
    async fn a_probe_that_cannot_run_is_unknown_rather_than_signed_out() {
        let contract = serde_json::json!({
            "provider": "codex",
            "provider_cli_binary": "codex",
            "auth_probe": {"disposition": "implemented", "argv": ["login", "status"], "format": "exit_code"},
        });
        let missing = OsString::from("/nonexistent/longhouse-probe-should-not-exist");
        let readiness = readiness_for_contract(&contract, Some(missing), true).await;
        assert_eq!(readiness.state, ReadinessState::Unknown);
    }
}
