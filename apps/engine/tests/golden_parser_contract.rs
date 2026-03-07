//! Golden file tests for the session parser.
//!
//! Invokes `longhouse-engine parse --dump-events` and snapshots the output
//! as a committed `*.expected.json` file. Only stable contract fields are
//! retained (role, raw_type, content_text, tool_name) — unstable fields like
//! uuid and fallback-generated timestamps are stripped before comparison so
//! that fixture evolution doesn't cause spurious failures.
//!
//! # Regenerating snapshots
//!
//! ```bash
//! UPDATE_GOLDENS=1 cargo test -p longhouse-engine --test golden_parser_contract
//! ```

use std::path::{Path, PathBuf};
use std::process::Command;

use pretty_assertions::assert_eq;
use serde::{Deserialize, Serialize};
use serde_json::Value;

// ---------------------------------------------------------------------------
// Snapshot types — only stable contract fields
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize, Deserialize, PartialEq)]
struct SnapshotEvent {
    role: String,
    raw_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    content_text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_input_json: Option<Value>,
}

#[derive(Debug, Serialize, Deserialize, PartialEq)]
struct Snapshot {
    event_count: usize,
    events: Vec<SnapshotEvent>,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn engine_bin() -> PathBuf {
    // Always use the repo-local binary — never the one on PATH.
    // This ensures golden tests catch stale-binary regressions.
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("target")
        .join("release")
        .join("longhouse-engine")
}

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
}

fn parse_to_snapshot(input_path: &Path) -> Snapshot {
    let bin = engine_bin();
    assert!(
        bin.exists(),
        "Engine binary not found at {}. Run: cargo build --release",
        bin.display()
    );

    let output = Command::new(&bin)
        .args(["parse", "--dump-events"])
        .arg(input_path)
        .output()
        .unwrap_or_else(|e| panic!("Failed to run engine: {}", e));

    assert!(
        output.status.success(),
        "Engine parse failed for {}:\n{}",
        input_path.display(),
        String::from_utf8_lossy(&output.stderr)
    );

    // stderr has human-readable summary; stdout has one JSON event per line
    let stdout = String::from_utf8_lossy(&output.stdout);

    let mut events: Vec<SnapshotEvent> = Vec::new();

    for line in stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let v: Value = serde_json::from_str(line)
            .unwrap_or_else(|e| panic!("Invalid event JSON: {}\nLine: {}", e, line));

        events.push(SnapshotEvent {
            role: v["role"].as_str().unwrap_or("").to_string(),
            raw_type: v["raw_type"].as_str().unwrap_or("").to_string(),
            content_text: v["content_text"].as_str().map(|s| s.to_string()),
            tool_name: v["tool_name"].as_str().map(|s| s.to_string()),
            tool_input_json: if v["tool_input_json"].is_null() {
                None
            } else {
                Some(v["tool_input_json"].clone())
            },
        });
    }

    Snapshot {
        event_count: events.len(),
        events,
    }
}

fn run_golden_test(input_path: &Path, expected_path: &Path) {
    let actual = parse_to_snapshot(input_path);

    if std::env::var("UPDATE_GOLDENS").is_ok() {
        let json = serde_json::to_string_pretty(&actual).expect("serialize snapshot");
        std::fs::write(expected_path, json + "\n").expect("write golden file");
        println!("Updated golden: {}", expected_path.display());
        return;
    }

    assert!(
        expected_path.exists(),
        "Golden file missing: {}\nRun: UPDATE_GOLDENS=1 cargo test -p longhouse-engine --test golden_parser_contract",
        expected_path.display()
    );

    let expected_json = std::fs::read_to_string(expected_path).expect("read golden file");
    let expected: Snapshot = serde_json::from_str(&expected_json).expect("parse golden file");

    assert_eq!(expected, actual);
}

// ---------------------------------------------------------------------------
// Golden tests — one per provider
// ---------------------------------------------------------------------------

#[test]
fn golden_claude_basic() {
    let base = fixtures_dir().join("golden").join("claude");
    run_golden_test(&base.join("basic.jsonl"), &base.join("basic.expected.json"));
}

#[test]
fn golden_codex_basic() {
    let base = fixtures_dir().join("golden").join("codex");
    run_golden_test(&base.join("basic.jsonl"), &base.join("basic.expected.json"));
}

#[test]
fn golden_gemini_basic() {
    let base = fixtures_dir().join("golden").join("gemini");
    run_golden_test(&base.join("basic.json"), &base.join("basic.expected.json"));
}
