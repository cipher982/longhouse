//! Golden file tests for the session parser.
//!
//! Invokes `longhouse-engine parse --dump-events` and snapshots the output
//! as a committed `*.expected.json` file. Only stable contract fields are
//! retained (role, raw_type, content_text, tool_name, item) — unstable fields like
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
    kind: String,
    role: String,
    raw_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    action_kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    content_text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_input_json: Option<Value>,
    /// Canonical item facts: phase and its source, reasoning visibility, tool
    /// intent, turn end reason.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    item: Option<Value>,
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
    // Prefer Cargo's freshly-built test binary, but fall back to the
    // release artifact for standalone snapshot refresh/debug flows.
    if let Some(bin) = option_env!("CARGO_BIN_EXE_longhouse-engine") {
        let path = PathBuf::from(bin);
        if path.exists() {
            return path;
        }
    }

    std::env::var_os("CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .expect("engine/ must live below the repo root")
                .join(".build")
                .join("cargo-target")
        })
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
        "Engine binary not found at {}. Run `make test-engine` or build through scripts/build/cargo.py.",
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
        let raw_type = v["raw_type"].as_str().unwrap_or("").to_string();
        let action_kind = action_kind_for_raw_type(&raw_type);

        events.push(SnapshotEvent {
            kind: if action_kind.is_some() {
                "action".to_string()
            } else {
                "event".to_string()
            },
            role: v["role"].as_str().unwrap_or("").to_string(),
            raw_type,
            action_kind,
            content_text: v["content_text"].as_str().map(|s| s.to_string()),
            tool_name: v["tool_name"].as_str().map(|s| s.to_string()),
            tool_input_json: if v["tool_input_json"].is_null() {
                None
            } else {
                Some(v["tool_input_json"].clone())
            },
            item: v.get("item").filter(|item| !item.is_null()).cloned(),
        });
    }

    Snapshot {
        event_count: events.len(),
        events,
    }
}

fn action_kind_for_raw_type(raw_type: &str) -> Option<String> {
    match raw_type {
        "codex_turn_interrupted" | "codex_turn_interrupted_marker" => {
            Some("turn_interrupted".to_string())
        }
        _ => None,
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
fn golden_claude_task_notification() {
    let base = fixtures_dir().join("golden").join("claude");
    run_golden_test(
        &base.join("task_notification.jsonl"),
        &base.join("task_notification.expected.json"),
    );
}

#[test]
fn claude_preserves_parent_links_for_sibling_head_projection() {
    let path = fixtures_dir()
        .join("golden")
        .join("claude")
        .join("branch_siblings.jsonl");
    let output = Command::new(engine_bin())
        .args(["parse", "--dump-events"])
        .arg(path)
        .output()
        .expect("run engine parser");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );

    let events: Vec<Value> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| serde_json::from_str(line).expect("parse event JSON"))
        .collect();
    let users: Vec<(&str, Option<&str>)> = events
        .iter()
        .filter(|event| event["role"] == "user")
        .map(|event| {
            (
                event["uuid"].as_str().expect("user uuid"),
                event["parent_uuid"].as_str(),
            )
        })
        .collect();

    assert_eq!(
        users,
        vec![
            ("root-0000-0000-0000-000000000001", None),
            (
                "abandoned-0000-0000-0000-000000000003",
                Some("assistant-0000-0000-0000-000000000002")
            ),
            (
                "resend-0000-0000-0000-000000000004",
                Some("assistant-0000-0000-0000-000000000002")
            ),
            (
                "followup-0000-0000-0000-000000000005",
                Some("resend-0000-0000-0000-000000000004")
            ),
        ]
    );
}

#[test]
fn golden_codex_basic() {
    let base = fixtures_dir().join("golden").join("codex");
    run_golden_test(&base.join("basic.jsonl"), &base.join("basic.expected.json"));
}

#[test]
fn golden_codex_search_tools() {
    let base = fixtures_dir().join("golden").join("codex");
    run_golden_test(
        &base.join("search_tools.jsonl"),
        &base.join("search_tools.expected.json"),
    );
}

#[test]
fn golden_codex_turn_interrupted() {
    let base = fixtures_dir().join("golden").join("codex");
    run_golden_test(
        &base.join("turn_interrupted.jsonl"),
        &base.join("turn_interrupted.expected.json"),
    );
}

#[test]
fn golden_antigravity_legacy_json_basic() {
    let base = fixtures_dir()
        .join("golden")
        .join("antigravity_legacy_json");
    run_golden_test(&base.join("basic.json"), &base.join("basic.expected.json"));
}

// ---------------------------------------------------------------------------
// Provider facts — the side channel beside the transcript
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
struct SnapshotFact {
    kind: String,
    at: String,
    source_offset: u64,
    payload: Value,
}

#[derive(Debug, Serialize, Deserialize, PartialEq)]
struct FactsSnapshot {
    fact_count: usize,
    facts: Vec<SnapshotFact>,
}

fn parse_to_facts_snapshot(input_path: &Path) -> FactsSnapshot {
    parse_to_facts_snapshot_from(input_path, 0)
}

fn parse_to_facts_snapshot_from(input_path: &Path, offset: u64) -> FactsSnapshot {
    let bin = engine_bin();
    let output = Command::new(&bin)
        .args(["parse", "--dump-facts", "--offset", &offset.to_string()])
        .arg(input_path)
        .output()
        .unwrap_or_else(|e| panic!("Failed to run engine: {}", e));
    assert!(
        output.status.success(),
        "Engine parse failed for {}:\n{}",
        input_path.display(),
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    let facts = stdout
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(|line| {
            let v: Value = serde_json::from_str(line)
                .unwrap_or_else(|e| panic!("Invalid fact JSON: {}\nLine: {}", e, line));
            SnapshotFact {
                kind: v["kind"].as_str().unwrap_or("").to_string(),
                at: v["at"].as_str().unwrap_or("").to_string(),
                source_offset: v["source_offset"].as_u64().unwrap_or(0),
                payload: v["payload"].clone(),
            }
        })
        .collect::<Vec<_>>();
    FactsSnapshot {
        fact_count: facts.len(),
        facts,
    }
}

fn run_golden_facts_test(input_path: &Path, expected_path: &Path) {
    let actual = parse_to_facts_snapshot(input_path);
    if std::env::var("UPDATE_GOLDENS").is_ok() {
        let json = serde_json::to_string_pretty(&actual).expect("serialize facts snapshot");
        std::fs::write(expected_path, json + "\n").expect("write golden facts file");
        println!("Updated golden facts: {}", expected_path.display());
        return;
    }
    assert!(
        expected_path.exists(),
        "Golden facts file missing: {}. Run with UPDATE_GOLDENS=1 to create it.",
        expected_path.display()
    );
    let expected_json = std::fs::read_to_string(expected_path).expect("read golden facts file");
    let expected: FactsSnapshot =
        serde_json::from_str(&expected_json).expect("parse golden facts file");
    assert_eq!(expected, actual);
}

/// Antigravity's own records carry usage (on the planner step), the model's
/// reasoning, the failed model calls and the compaction checkpoint. Each must
/// reach the facts side channel or the render stream, never be dropped.
#[test]
fn golden_antigravity_native_facts_and_thinking() {
    let base = fixtures_dir().join("golden").join("antigravity");
    run_golden_facts_test(
        &base.join("native.jsonl"),
        &base.join("native.facts.expected.json"),
    );
    let events = parse_to_snapshot(&base.join("native.jsonl"));
    assert!(
        events
            .events
            .iter()
            .any(|event| event.raw_type == "antigravity_thinking"),
        "planner thinking must become a render event"
    );
    assert!(
        events
            .events
            .iter()
            .all(|event| event.raw_type != "antigravity_error"),
        "ERROR_MESSAGE is a fact, not a render event"
    );
}

/// The planner's `thinking` is a render event, the way Pi's thinking blocks are.
#[test]
fn golden_antigravity_thinking() {
    let base = fixtures_dir().join("golden").join("antigravity");
    run_golden_test(
        &base.join("thinking.jsonl"),
        &base.join("thinking.expected.json"),
    );
    let events = parse_to_snapshot(&base.join("thinking.jsonl"));
    assert!(events
        .events
        .iter()
        .any(|event| event.raw_type == "antigravity_thinking"));
}

/// A Claude turn ends on a `system/turn_duration` line the transcript never
/// shows. The parser must surface it as a provider fact, not a render event,
/// with the provider's own wall-clock accounting intact.
#[test]
fn golden_claude_turn_duration_facts() {
    let base = fixtures_dir().join("golden").join("claude");
    run_golden_facts_test(
        &base.join("turn_duration.jsonl"),
        &base.join("turn_duration.facts.expected.json"),
    );
    // The same fixture must not leak the fact line into the event stream.
    let events = parse_to_snapshot(&base.join("turn_duration.jsonl"));
    assert!(
        events
            .events
            .iter()
            .all(|event| event.raw_type != "turn_duration"),
        "turn_duration must not become a render event"
    );
}

/// The provider's own title and its away recap are facts, not transcript
/// rows: the title has no clock, the recap sheds its terminal chrome.
#[test]
fn golden_claude_recap_and_title_facts() {
    let base = fixtures_dir().join("golden").join("claude");
    run_golden_facts_test(
        &base.join("recap_title.jsonl"),
        &base.join("recap_title.facts.expected.json"),
    );
    let events = parse_to_snapshot(&base.join("recap_title.jsonl"));
    assert_eq!(
        events.event_count, 3,
        "only the user and assistant rows and the turn end are events"
    );
}

/// Codex closes a turn on `task_complete` (or `turn_aborted`, which unlike
/// Claude still carries the duration). The usage fact is stamped on that same
/// line from the last `token_count`, with the model and effort the turn's
/// `turn_context` named, and Codex's own context accounting (`total_tokens`)
/// rather than a sum of overlapping input classes. Stream and terminal errors
/// and the compaction record are facts beside the transcript.
#[test]
fn golden_codex_turn_signal_facts() {
    let base = fixtures_dir().join("golden").join("codex");
    run_golden_facts_test(
        &base.join("rollout-turn_signals.jsonl"),
        &base.join("rollout-turn_signals.facts.expected.json"),
    );
    let events = parse_to_snapshot(&base.join("rollout-turn_signals.jsonl"));
    assert!(
        events.events.iter().all(|event| !matches!(
            event.raw_type.as_str(),
            "task_complete" | "token_count" | "turn_context" | "stream_error"
        )),
        "turn signal lines must not become render events"
    );
    let compacted_events: Vec<_> = events
        .events
        .iter()
        .filter(|event| event.raw_type == "compacted")
        .collect();
    assert_eq!(compacted_events.len(), 1);
    assert_eq!(compacted_events[0].role, "system");
    assert_eq!(
        compacted_events[0].content_text.as_deref(),
        Some("Conversation compacted")
    );
    // The third turn was stopped before any model call reported tokens: it
    // closes with a duration and no usage, never the previous turn's counts
    // relabelled with this turn's model.
    let facts = parse_to_facts_snapshot(&base.join("rollout-turn_signals.jsonl"));
    let durations = facts
        .facts
        .iter()
        .filter(|f| f.kind == "turn.duration")
        .count();
    let usages = facts
        .facts
        .iter()
        .filter(|f| f.kind == "turn.usage")
        .count();
    assert_eq!((durations, usages), (3, 2));
    let stopped = facts
        .facts
        .iter()
        .find(|f| f.kind == "turn.duration" && f.payload["duration_ms"] == 4800)
        .expect("the stopped third turn has a duration");
    assert!(
        !facts
            .facts
            .iter()
            .any(|f| f.kind == "turn.usage" && f.source_offset == stopped.source_offset),
        "a turn without token_count yields no usage fact"
    );
}

/// The engine resumes mid-file. When the batch starts on the closing line,
/// the turn's `turn_context` and `token_count` lie before the offset; the
/// parser replays them from a bounded window so the usage fact is unchanged.
#[test]
fn golden_codex_facts_survive_incremental_resume() {
    let base = fixtures_dir().join("golden").join("codex");
    let path = base.join("rollout-turn_signals.jsonl");
    let bytes = std::fs::read(&path).expect("read fixture");
    let needle = b"\"type\":\"task_complete\"";
    let hit = bytes
        .windows(needle.len())
        .position(|window| window == needle)
        .expect("fixture has a task_complete line");
    let line_start = bytes[..hit]
        .iter()
        .rposition(|&b| b == b'\n')
        .map(|nl| nl + 1)
        .unwrap_or(0) as u64;
    let full = parse_to_facts_snapshot(&path);
    let resumed = parse_to_facts_snapshot_from(&path, line_start);
    let full_at_offset = full
        .facts
        .iter()
        .filter(|fact| fact.source_offset == line_start)
        .cloned()
        .collect::<Vec<_>>();
    let resumed_at_offset = resumed
        .facts
        .iter()
        .filter(|fact| fact.source_offset == line_start)
        .cloned()
        .collect::<Vec<_>>();
    assert_eq!(full_at_offset, resumed_at_offset);
    let usage = resumed_at_offset
        .iter()
        .find(|fact| fact.kind == "turn.usage")
        .expect("resume still yields the usage fact");
    assert_eq!(usage.payload["model"], "gpt-5.6-luna");
    assert_eq!(usage.payload["effort"], "xhigh");
    assert_eq!(usage.payload["context_tokens"], 25210);
}

/// Usage lands once per turn on the line that ends it, never on tool-use
/// hops; API retries and compaction accounting are facts beside the same
/// compaction system event the transcript already shows.
#[test]
fn golden_claude_usage_error_and_compaction_facts() {
    let base = fixtures_dir().join("golden").join("claude");
    run_golden_facts_test(
        &base.join("usage_errors_compaction.jsonl"),
        &base.join("usage_errors_compaction.facts.expected.json"),
    );
    let events = parse_to_snapshot(&base.join("usage_errors_compaction.jsonl"));
    assert!(
        events
            .events
            .iter()
            .any(|event| event.raw_type == "compact_boundary"),
        "the compaction boundary stays a visible system event"
    );
}

// ---------------------------------------------------------------------------
// Canonical item facts: message.phase, turn.thinking, turn.end, tool.intent
// ---------------------------------------------------------------------------

fn items_of(snapshot: &Snapshot) -> Vec<&Value> {
    snapshot
        .events
        .iter()
        .filter_map(|event| event.item.as_ref())
        .collect()
}

fn has_item(snapshot: &Snapshot, expected: Value) -> bool {
    items_of(snapshot).into_iter().any(|item| *item == expected)
}

/// Claude: the stop reason of the message carrying each text block splits
/// commentary from the answer; a signature-only thinking block is withheld
/// reasoning; `turn_duration` ends a turn and an interrupt marker ends one
/// interrupted; a tool call's `description` is its intent.
#[test]
fn golden_claude_items() {
    let base = fixtures_dir().join("golden").join("claude");
    run_golden_test(&base.join("items.jsonl"), &base.join("items.expected.json"));
    let snapshot = parse_to_snapshot(&base.join("items.jsonl"));
    for expected in [
        serde_json::json!({"phase": "commentary", "phase_source": "derived"}),
        serde_json::json!({"phase": "final", "phase_source": "derived"}),
        serde_json::json!({"kind": "reasoning", "visibility": "withheld"}),
        serde_json::json!({"kind": "reasoning", "visibility": "full"}),
        serde_json::json!({"kind": "turn_end", "end_reason": "completed"}),
        serde_json::json!({"kind": "turn_end", "end_reason": "interrupted"}),
        serde_json::json!({"intent": "Run the test suite"}),
    ] {
        assert!(has_item(&snapshot, expected.clone()), "missing {expected}");
    }
}

/// Codex labels the phase itself; reasoning is encrypted (withheld) unless a
/// summary is shared; `task_complete` and `turn_aborted` end turns. Codex
/// tool calls carry no intent.
#[test]
fn golden_codex_items() {
    let base = fixtures_dir().join("golden").join("codex");
    run_golden_test(
        &base.join("rollout-items.jsonl"),
        &base.join("rollout-items.expected.json"),
    );
    let snapshot = parse_to_snapshot(&base.join("rollout-items.jsonl"));
    for expected in [
        serde_json::json!({"phase": "commentary", "phase_source": "native"}),
        serde_json::json!({"phase": "final", "phase_source": "native"}),
        serde_json::json!({"kind": "reasoning", "visibility": "withheld"}),
        serde_json::json!({"kind": "reasoning", "visibility": "summary"}),
        serde_json::json!({"kind": "turn_end", "end_reason": "completed"}),
        serde_json::json!({"kind": "turn_end", "end_reason": "interrupted"}),
    ] {
        assert!(has_item(&snapshot, expected.clone()), "missing {expected}");
    }
}

/// Pi: `stopReason` splits commentary (`toolUse`) from the answer (`stop`) and
/// says why a turn ended. Pi tool calls carry no intent.
#[test]
fn golden_pi_items() {
    let base = fixtures_dir().join("golden").join("pi");
    run_golden_test(&base.join("items.jsonl"), &base.join("items.expected.json"));
    let snapshot = parse_to_snapshot(&base.join("items.jsonl"));
    for expected in [
        serde_json::json!({"phase": "commentary", "phase_source": "derived"}),
        serde_json::json!({"phase": "final", "phase_source": "derived"}),
        serde_json::json!({"kind": "reasoning", "visibility": "full"}),
        // A signature with no text: withheld, not dropped.
        serde_json::json!({"kind": "reasoning", "visibility": "withheld"}),
        serde_json::json!({"kind": "turn_end", "end_reason": "completed"}),
        serde_json::json!({"kind": "turn_end", "end_reason": "aborted"}),
    ] {
        assert!(has_item(&snapshot, expected.clone()), "missing {expected}");
    }
}

/// OMP as Pi, and each call states its purpose (`intent`, or `arguments.i`).
#[test]
fn golden_omp_items() {
    let base = fixtures_dir().join("golden").join("omp");
    run_golden_test(&base.join("items.jsonl"), &base.join("items.expected.json"));
    let snapshot = parse_to_snapshot(&base.join("items.jsonl"));
    for expected in [
        serde_json::json!({"phase": "commentary", "phase_source": "derived"}),
        serde_json::json!({"phase": "final", "phase_source": "derived"}),
        serde_json::json!({"kind": "reasoning", "visibility": "full"}),
        serde_json::json!({"kind": "reasoning", "visibility": "withheld"}),
        serde_json::json!({"kind": "turn_end", "end_reason": "completed"}),
        serde_json::json!({"intent": "Run the test suite"}),
        serde_json::json!({"intent": "Read the notes"}),
    ] {
        assert!(has_item(&snapshot, expected.clone()), "missing {expected}");
    }
}

/// Antigravity: a planner step that calls tools is commentary and one that
/// calls none is the answer (derived); `toolSummary` is the call's intent.
#[test]
fn golden_antigravity_items() {
    let base = fixtures_dir().join("golden").join("antigravity");
    run_golden_test(
        &base.join("native.jsonl"),
        &base.join("native.expected.json"),
    );
    let snapshot = parse_to_snapshot(&base.join("native.jsonl"));
    for expected in [
        serde_json::json!({"phase": "final", "phase_source": "derived"}),
        serde_json::json!({"kind": "reasoning", "visibility": "full"}),
        serde_json::json!({"intent": "Sleep 6 seconds"}),
    ] {
        assert!(has_item(&snapshot, expected.clone()), "missing {expected}");
    }
}

#[test]
fn golden_claude_fidelity_signals() {
    let base = fixtures_dir().join("golden").join("claude");
    run_golden_test(
        &base.join("claude_fidelity_signals.jsonl"),
        &base.join("claude_fidelity_signals.expected.json"),
    );
}

#[test]
fn golden_claude_fidelity_signals_facts() {
    let base = fixtures_dir().join("golden").join("claude");
    run_golden_facts_test(
        &base.join("claude_fidelity_signals.jsonl"),
        &base.join("claude_fidelity_signals.facts.expected.json"),
    );
}
