//! Negative-control fault injection for provider qualification producers.
//!
//! Compiled only with the `qa-fault-injection` Cargo feature. A release engine
//! has no code path that reads `LONGHOUSE_QA_FAULT`, so a stray environment
//! variable on a user machine can never turn a steer into a no-op.
//!
//! A producer runs once against a QA-built engine with a named fault, and its
//! target assertion must fail. The receipt proves the fault actually fired, so
//! a producer that "fails" for an unrelated reason (timeout, crash, missing
//! credentials) is inconclusive rather than a passed negative control.

/// Where the provider factory leaves a fault selection for a QA engine, inside the sandbox it runs the producer in.
/// This is the engine-only channel: the producer is never told a fault is active, it only sees an engine that
/// misbehaves, and its own oracle has to notice (spec multi-agent-dev-loop-speed.md, change 6, blind fault injection).
#[cfg(feature = "qa-fault-injection")]
pub const FAULT_CONFIG_PATH: &str = "/run/longhouse-qa/fault.json";

/// Read `FAULT_CONFIG_PATH` once, at process start, and publish its values as the `LONGHOUSE_QA_FAULT*` variables
/// every reader below (and the child processes and provider extensions an engine starts) already consults. A missing
/// or unreadable file changes nothing. Call before any thread starts: it sets process environment.
///
/// Keys: `fault` (name), `receipt` (path of the JSONL receipt), `marker` (exact marker text) and `marker_prefix` with
/// `marker_hex_len` (redact every `<prefix><hex digits>` token, for a marker the engine cannot know in advance).
#[cfg(feature = "qa-fault-injection")]
pub fn load_config() {
    let Ok(text) = std::fs::read_to_string(FAULT_CONFIG_PATH) else {
        return;
    };
    let Ok(config) = serde_json::from_str::<serde_json::Value>(&text) else {
        return;
    };
    for (key, variable) in [
        ("fault", "LONGHOUSE_QA_FAULT"),
        ("receipt", "LONGHOUSE_QA_FAULT_RECEIPT"),
        ("marker", "LONGHOUSE_QA_FAULT_MARKER"),
        ("marker_prefix", "LONGHOUSE_QA_FAULT_MARKER_PREFIX"),
        ("marker_hex_len", "LONGHOUSE_QA_FAULT_MARKER_HEX_LEN"),
    ] {
        let value = match config.get(key) {
            Some(serde_json::Value::String(text)) => text.clone(),
            Some(serde_json::Value::Number(number)) => number.to_string(),
            _ => continue,
        };
        if key == "fault" {
            // Cursor's launcher reads its own name for the same selection.
            std::env::set_var("LH_QA_FAULT", &value);
        }
        std::env::set_var(variable, value);
    }
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn load_config() {}

/// Faults injected at the Codex bridge dispatch boundary shared by
/// `codex-bridge <verb>` and the control channel.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CodexFault {
    /// Wait for the active turn to finish, then deliver the steer text as a
    /// new turn: the queued-follow-up shape a steer oracle must reject.
    SteerAsFollowUp,
    /// Report interrupt success without sending `turn/interrupt`.
    InterruptNoop,
    /// Report send success with a fabricated turn id without starting a turn.
    SendNoop,
}

#[cfg(feature = "qa-fault-injection")]
pub fn codex_fault() -> Option<CodexFault> {
    // Terminal Truth's steer mutation check switches the same defect on by its own name.
    if crate::qa_fault::mutant("codex-helm-steer-as-follow-up") {
        return Some(CodexFault::SteerAsFollowUp);
    }
    match std::env::var("LONGHOUSE_QA_FAULT").ok()?.as_str() {
        "codex_steer_as_follow_up" => Some(CodexFault::SteerAsFollowUp),
        "codex_interrupt_noop" => Some(CodexFault::InterruptNoop),
        "codex_send_noop" => Some(CodexFault::SendNoop),
        _ => None,
    }
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn codex_fault() -> Option<CodexFault> {
    None
}

/// Faults injected where the Pi and OMP Helm launchers forward an authorized
/// remote command to the provider extension.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HelmExtensionFault {
    /// Forward a steer as a plain send. While a turn is active the extension
    /// delivers that as `followUp`: the queued shape a steer oracle must reject.
    SteerAsFollowUp,
    /// Acknowledge abort without forwarding it to the provider.
    AbortNoop,
    /// Acknowledge a send without forwarding it: the caller is told the message
    /// landed and no turn ever starts, the silent-drop shape a send oracle must
    /// reject.
    SendNoop,
    /// Acknowledge terminate without forwarding it, so the recorded owners stay
    /// alive while the caller is told the session is gone.
    TerminateNoop,
}

/// `<provider>_steer_as_follow_up`, `<provider>_abort_noop`,
/// `<provider>_send_noop` or `<provider>_terminate_noop`.
#[cfg(feature = "qa-fault-injection")]
pub fn helm_extension_fault(provider: &str) -> Option<HelmExtensionFault> {
    let value = std::env::var("LONGHOUSE_QA_FAULT").ok()?;
    match value.strip_prefix(provider)?.strip_prefix('_')? {
        "steer_as_follow_up" => Some(HelmExtensionFault::SteerAsFollowUp),
        "abort_noop" => Some(HelmExtensionFault::AbortNoop),
        "send_noop" => Some(HelmExtensionFault::SendNoop),
        "terminate_noop" => Some(HelmExtensionFault::TerminateNoop),
        _ => None,
    }
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn helm_extension_fault(_provider: &str) -> Option<HelmExtensionFault> {
    None
}

/// Console fault: acknowledge a mid-turn steer without delivering it into the
/// running turn, so the caller is told it landed and the turn never sees it --
/// the silent-drop shape the Console steer oracle must reject.
#[cfg(feature = "qa-fault-injection")]
pub fn console_steer_noop() -> bool {
    std::env::var("LONGHOUSE_QA_FAULT").ok().as_deref() == Some("console_steer_noop")
        || crate::qa_fault::mutant("console-steer-dropped")
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn console_steer_noop() -> bool {
    false
}

/// Remove only Claude background membership at the parent lifecycle writer.
#[cfg(feature = "qa-fault-injection")]
pub fn claude_background_writer_disabled() -> bool {
    std::env::var("LONGHOUSE_QA_FAULT").ok().as_deref() == Some("claude_background_writer_disabled")
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn claude_background_writer_disabled() -> bool {
    false
}

/// Ingest fault: the Machine Agent ships the transcript normally but blanks one
/// marker token out of every render record before the envelope is persisted.
/// The raw bytes, envelope identity and acknowledgement are untouched, so the
/// session looks healthy while served search can no longer find the marker --
/// the silent-ingest-loss shape a search oracle must catch.
///
/// The marker is either the exact text in `LONGHOUSE_QA_FAULT_MARKER` or every token made of
/// `LONGHOUSE_QA_FAULT_MARKER_PREFIX` followed by `LONGHOUSE_QA_FAULT_MARKER_HEX_LEN` hex digits (the factory's blind
/// control cannot know a marker the producer draws at run time). Returns the redacted text and the number of tokens
/// blanked, or `None` when the fault is off or nothing matched.
#[cfg(feature = "qa-fault-injection")]
pub fn redact_ingest_markers(text: &str) -> Option<(String, usize)> {
    if std::env::var("LONGHOUSE_QA_FAULT").ok()?.as_str() != "ingest_redact_marker" {
        return None;
    }
    let exact = std::env::var("LONGHOUSE_QA_FAULT_MARKER")
        .ok()
        .map(|marker| marker.trim().to_string())
        .filter(|marker| !marker.is_empty());
    let prefix = std::env::var("LONGHOUSE_QA_FAULT_MARKER_PREFIX")
        .ok()
        .filter(|prefix| !prefix.is_empty());
    let hex_len = std::env::var("LONGHOUSE_QA_FAULT_MARKER_HEX_LEN")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(16);
    redact_markers(text, exact.as_deref(), prefix.as_deref(), hex_len)
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn redact_ingest_markers(_text: &str) -> Option<(String, usize)> {
    None
}

#[cfg(feature = "qa-fault-injection")]
fn redact_markers(
    text: &str,
    exact: Option<&str>,
    prefix: Option<&str>,
    hex_len: usize,
) -> Option<(String, usize)> {
    let mut out = text.to_string();
    let mut redacted = 0usize;
    if let Some(marker) = exact {
        let count = out.matches(marker).count();
        if count > 0 {
            redacted += count;
            out = out.replace(marker, &"x".repeat(marker.len()));
        }
    }
    if let Some(prefix) = prefix {
        let mut result = String::with_capacity(out.len());
        let mut rest = out.as_str();
        while let Some(at) = rest.find(prefix) {
            let after = &rest[at + prefix.len()..];
            let digits = after
                .bytes()
                .take_while(|byte| byte.is_ascii_hexdigit())
                .count();
            result.push_str(&rest[..at]);
            if digits >= hex_len {
                let length = prefix.len() + hex_len;
                result.push_str(&"x".repeat(length));
                redacted += 1;
                rest = &rest[at + length..];
            } else {
                result.push_str(prefix);
                rest = after;
            }
        }
        result.push_str(rest);
        out = result;
    }
    (redacted > 0).then_some((out, redacted))
}

/// Append one JSON line to `LONGHOUSE_QA_FAULT_RECEIPT`. A fault that cannot
/// write its receipt still fires; the producer then reports inconclusive.
#[cfg(feature = "qa-fault-injection")]
pub fn record_fired(fault: CodexFault, session_id: &str, detail: serde_json::Value) {
    record_fired_named(&format!("{fault:?}"), session_id, detail);
}

#[cfg(feature = "qa-fault-injection")]
pub fn record_fired_named(fault: &str, session_id: &str, detail: serde_json::Value) {
    use std::io::Write;
    let Ok(path) = std::env::var("LONGHOUSE_QA_FAULT_RECEIPT") else {
        return;
    };
    let line = serde_json::json!({
        "schema_version": 1,
        "fault": fault,
        "session_id": session_id,
        "fired_at": chrono::Utc::now().to_rfc3339(),
        "detail": detail,
    });
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = writeln!(file, "{line}");
    }
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn record_fired(_fault: CodexFault, _session_id: &str, _detail: serde_json::Value) {}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn record_fired_named(_fault: &str, _session_id: &str, _detail: serde_json::Value) {}

/// Whether `name` is one of the comma-separated faults in `LONGHOUSE_QA_FAULT`.
#[cfg(feature = "qa-fault-injection")]
fn fault_listed(name: &str) -> bool {
    std::env::var("LONGHOUSE_QA_FAULT")
        .ok()
        .is_some_and(|value| value.split(',').any(|fault| fault.trim() == name))
}

/// Watcher fault: deliver the first file event for each transcript and drop
/// every later one, the shape of an overflowing watcher channel or a coalesced
/// FSEvents stream. Only the hot-source re-check or a reconciliation scan can
/// then see an append.
#[cfg(feature = "qa-fault-injection")]
pub fn watcher_drops_repeat_transcript_event(path: &std::path::Path) -> bool {
    static SEEN: std::sync::LazyLock<
        std::sync::Mutex<std::collections::HashSet<std::path::PathBuf>>,
    > = std::sync::LazyLock::new(Default::default);
    if !fault_listed("watcher_drop_repeat_events") {
        return false;
    }
    SEEN.lock()
        .map(|mut seen| !seen.insert(path.to_path_buf()))
        .unwrap_or(false)
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn watcher_drops_repeat_transcript_event(_path: &std::path::Path) -> bool {
    false
}

/// The hot-source re-check does nothing: the behaviour before it existed, as
/// the negative control for a lost-event latency measurement.
#[cfg(feature = "qa-fault-injection")]
pub fn hot_recheck_off() -> bool {
    fault_listed("hot_recheck_off")
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn hot_recheck_off() -> bool {
    false
}

/// Terminal-truth mutation checks: one engine binary carries every known live-text defect, and a check switches one on.
///
/// `LONGHOUSE_QA_MUTANT` names the defect (comma separated). Each call site keeps the shipped behavior in its `else`
/// branch and puts the defect, the edit the check used to compile into a binary of its own, in the branch `mutant`
/// guards. The unmutated cell and the mutated cell of a check therefore run the same binary and differ in this one
/// variable. `server/zerg/qa/terminal_truth_mutations.py` owns the names and what each is expected to break.
#[cfg(feature = "qa-fault-injection")]
pub fn mutant(name: &str) -> bool {
    static ACTIVE: std::sync::LazyLock<Vec<String>> = std::sync::LazyLock::new(|| {
        std::env::var("LONGHOUSE_QA_MUTANT")
            .map(|value| {
                value
                    .split(',')
                    .map(|part| part.trim().to_string())
                    .collect()
            })
            .unwrap_or_default()
    });
    ACTIVE.iter().any(|active| active == name)
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn mutant(_name: &str) -> bool {
    false
}

/// An embedded asset (a JS or TS file the engine writes for a provider to load) with the one text edit a mutation check
/// names in `LONGHOUSE_QA_ASSET_PATCH`, a JSON list of `{"asset", "old", "new"}`. The edit must land in exactly one
/// place, as the source edit it replaces had to; a patch that does not stops the engine, so a check never runs a defect
/// that was not put back.
#[cfg(feature = "qa-fault-injection")]
pub fn patch_asset<'a>(asset: &str, text: &'a str) -> std::borrow::Cow<'a, str> {
    let Ok(raw) = std::env::var("LONGHOUSE_QA_ASSET_PATCH") else {
        return std::borrow::Cow::Borrowed(text);
    };
    let patches: Vec<serde_json::Value> =
        serde_json::from_str(&raw).expect("LONGHOUSE_QA_ASSET_PATCH is not a JSON list");
    let mut patched = std::borrow::Cow::Borrowed(text);
    for patch in patches {
        if patch["asset"].as_str() != Some(asset) {
            continue;
        }
        let old = patch["old"].as_str().expect("asset patch has no `old`");
        let new = patch["new"].as_str().expect("asset patch has no `new`");
        let count = patched.matches(old).count();
        assert_eq!(
            count, 1,
            "asset patch for {asset}: the text to edit occurs {count} times"
        );
        patched = std::borrow::Cow::Owned(patched.replacen(old, new, 1));
    }
    patched
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn patch_asset<'a>(_asset: &str, text: &'a str) -> std::borrow::Cow<'a, str> {
    std::borrow::Cow::Borrowed(text)
}

#[cfg(all(test, feature = "qa-fault-injection"))]
mod blind_tests {
    use super::redact_markers;

    #[test]
    fn prefix_token_is_blanked_to_the_same_length() {
        let text =
            "found lhsearch0123456789abcdef and lhsearch00ff and lhsearchfedcba9876543210 end";
        let (out, count) = redact_markers(text, None, Some("lhsearch"), 16).unwrap();
        assert_eq!(count, 2);
        assert_eq!(out.len(), text.len());
        assert_eq!(
            out,
            format!(
                "found {} and lhsearch00ff and {} end",
                "x".repeat(24),
                "x".repeat(24)
            )
        );
    }

    #[test]
    fn exact_marker_and_no_match() {
        let (out, count) = redact_markers("a MARK b MARK", Some("MARK"), None, 16).unwrap();
        assert_eq!((out.as_str(), count), ("a xxxx b xxxx", 2));
        assert!(redact_markers("nothing here", Some("MARK"), Some("lhsearch"), 16).is_none());
    }
}
