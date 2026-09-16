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

/// Ingest fault: the Machine Agent ships the transcript normally but blanks one
/// marker token out of every render record before the envelope is persisted.
/// The raw bytes, envelope identity and acknowledgement are untouched, so the
/// session looks healthy while served search can no longer find the marker --
/// the silent-ingest-loss shape a search oracle must catch.
#[cfg(feature = "qa-fault-injection")]
pub fn ingest_redact_marker() -> Option<String> {
    if std::env::var("LONGHOUSE_QA_FAULT").ok()?.as_str() != "ingest_redact_marker" {
        return None;
    }
    std::env::var("LONGHOUSE_QA_FAULT_MARKER")
        .ok()
        .map(|marker| marker.trim().to_string())
        .filter(|marker| !marker.is_empty())
}

#[cfg(not(feature = "qa-fault-injection"))]
pub fn ingest_redact_marker() -> Option<String> {
    None
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
