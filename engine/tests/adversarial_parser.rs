//! Adversarial parser tests.
//!
//! Verifies the parser's behaviour on malformed and edge-case inputs:
//!   - Never panics
//!   - Never silently drops an entire session due to one bad record
//!   - Produces 0 events only for genuinely empty input (not parse errors)
//!   - Unknown/future message types are skipped, not fatal

use std::path::{Path, PathBuf};
use std::process::Command;

fn engine_bin() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("target")
        .join("release")
        .join("longhouse-engine")
}

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("adversarial")
}

/// Run the engine parser on a fixture. Returns (event_count, success).
fn parse_events(input_path: &Path) -> (usize, bool) {
    let bin = engine_bin();
    assert!(
        bin.exists(),
        "Engine binary not found. Run: cargo build --release"
    );

    let output = Command::new(&bin)
        .args(["parse", "--dump-events"])
        .arg(input_path)
        .output()
        .unwrap_or_else(|e| panic!("Failed to run engine: {}", e));

    let count = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .count();

    (count, output.status.success())
}

// ---------------------------------------------------------------------------
// Gemini adversarial cases
// ---------------------------------------------------------------------------

/// Non-string content (object) in one message must NOT drop the entire session.
/// The messages with valid string content must still be shipped.
#[test]
fn gemini_object_content_keeps_string_messages() {
    let path = fixtures_dir().join("gemini").join("object_content.json");
    let (count, ok) = parse_events(&path);
    assert!(ok, "engine must exit 0 even with malformed content");
    // 3 messages: user (string), gemini (object — skip), user (string)
    // Should preserve the 2 string-content messages
    assert!(
        count >= 2,
        "Expected ≥2 events from object_content fixture, got {}. \
         A non-string content field must not drop the entire session.",
        count
    );
}

/// Unknown message type is silently skipped; surrounding valid messages are kept.
#[test]
fn gemini_unknown_type_skipped_not_fatal() {
    let path = fixtures_dir().join("gemini").join("unknown_type.json");
    let (count, ok) = parse_events(&path);
    assert!(ok, "engine must exit 0 on unknown message type");
    // 3 messages: user, system (unknown—skip), gemini
    assert_eq!(
        count, 2,
        "Expected exactly 2 events (unknown type skipped), got {}",
        count
    );
}

/// Empty messages array is legitimate — 0 events, exit 0.
#[test]
fn gemini_empty_messages_is_not_an_error() {
    let path = fixtures_dir().join("gemini").join("empty_messages.json");
    let (count, ok) = parse_events(&path);
    assert!(ok, "engine must exit 0 on empty messages array");
    assert_eq!(count, 0, "Expected 0 events for empty messages");
}

/// Truncated JSON (partial write mid-flush) — engine must exit 0, 0 events.
/// The file is invalid JSON so parse fails; this is expected and must be silent.
#[test]
fn gemini_truncated_json_exits_cleanly() {
    // .broken extension bypasses JSON linters while keeping the file clearly
    // associated with the Gemini format it simulates.
    let path = fixtures_dir().join("gemini").join("truncated.json.broken");
    let (count, ok) = parse_events(&path);
    assert!(ok, "engine must exit 0 on truncated (invalid) JSON");
    assert_eq!(count, 0, "Expected 0 events from truncated JSON");
}

// ---------------------------------------------------------------------------
// Claude adversarial cases
// ---------------------------------------------------------------------------

/// A malformed line in the middle of a JSONL file must not drop surrounding events.
#[test]
fn claude_malformed_middle_line_keeps_valid_events() {
    let path = fixtures_dir().join("claude").join("malformed_middle.jsonl");
    let (count, ok) = parse_events(&path);
    assert!(ok, "engine must exit 0 with malformed middle line");
    // 3 lines: valid user, broken JSON, valid assistant
    assert!(
        count >= 2,
        "Expected ≥2 events (bad line skipped, valid lines kept), got {}",
        count
    );
}

// ---------------------------------------------------------------------------
// Codex adversarial cases
// ---------------------------------------------------------------------------

/// Unknown payload type in the middle is skipped; surrounding events kept.
#[test]
fn codex_unknown_payload_type_skipped() {
    let path = fixtures_dir()
        .join("codex")
        .join("unknown_payload_type.jsonl");
    let (count, ok) = parse_events(&path);
    assert!(ok, "engine must exit 0 on unknown payload type");
    // 4 lines: session_meta, user message, unknown type (skip), assistant message
    assert!(
        count >= 2,
        "Expected ≥2 events (unknown type skipped), got {}",
        count
    );
}
