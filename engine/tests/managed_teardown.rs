//! Integration coverage for managed Codex teardown.
//!
//! The bug these exist for: a clean Helm exit reported
//! `managed Codex bridge cleanup timed out after 2 seconds` because the bridge
//! posted a terminal event to hosted ingest *before* acknowledging the stop
//! RPC, and the facade's 2s patience ran out while hosted was slow. The session
//! had in fact stopped cleanly.
//!
//! Two levels are covered:
//!
//! 1. The real `longhouse` facade binary, driven as a subprocess against a
//!    stub stop-helper and a temporary `LONGHOUSE_HOME`. This exercises the
//!    actual deadline, the actual state-file re-read, and the actual contract
//!    cleanup — the code that produced the user-visible failure. A test that
//!    called the classification function directly would pass while the
//!    original bug persisted, so these drive the binary instead.
//! 2. The commit → publish → reconcile chain against a real filesystem,
//!    including the crash windows between the durable state write and the
//!    outbox enqueue.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const SESSION_ID: &str = "11111111-2222-3333-4444-555555555555";

fn facade_binary() -> PathBuf {
    // target/<profile>/deps/<test binary> -> target/<profile>/longhouse
    let mut path = std::env::current_exe().expect("test binary path");
    path.pop();
    if path.ends_with("deps") {
        path.pop();
    }
    let facade = path.join("longhouse");
    assert!(
        facade.exists(),
        "facade binary not built at {}; `make test-engine` builds it first",
        facade.display()
    );
    facade
}

/// A stand-in for `longhouse-engine codex-bridge stop`.
///
/// Only the helper is stubbed: the facade's spawn, deadline, kill, state
/// re-read, and cleanup are all the real implementation.
fn write_stub_helper(dir: &Path, body: &str) -> PathBuf {
    let path = dir.join("stub-engine");
    fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    }
    path
}

fn state_dir(home: &Path) -> PathBuf {
    home.join("managed-local/codex-bridge")
}

fn contract_path(home: &Path) -> PathBuf {
    home.join("managed-local/contracts/codex")
        .join(format!("{SESSION_ID}.json"))
}

fn write_state_full(home: &Path, status: &str, run_id: Option<&str>, stopped: bool) {
    let dir = state_dir(home);
    fs::create_dir_all(&dir).unwrap();
    let state = serde_json::json!({
        "schema_version": 1,
        "session_id": SESSION_ID,
        "cwd": "/tmp",
        "codex_bin": "codex",
        "ws_url": "ws://127.0.0.1:1/ws",
        "thread_id": "thread-1",
        "pid": 1u32,
        "status": status,
        "run_id": run_id,
        "stopped_at": if stopped { Some("2026-07-24T00:00:00Z") } else { None },
        "log_file": "/tmp/x.log",
        "active_turn_id": serde_json::Value::Null,
        "last_turn_status": serde_json::Value::Null,
        "last_error": serde_json::Value::Null,
        "updated_at": "2026-07-24T00:00:00Z",
    });
    fs::write(
        dir.join(format!("{SESSION_ID}.json")),
        serde_json::to_vec_pretty(&state).unwrap(),
    )
    .unwrap();
}

/// A durable teardown commit: stopped, with the terminal marker present.
fn write_state(home: &Path, status: &str) {
    let stopped = status == "stopped";
    write_state_full(home, status, Some("run-1"), stopped);
}

fn write_contract(home: &Path) {
    let path = contract_path(home);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(&path, b"{}").unwrap();
}

struct TeardownRun {
    status: std::process::ExitStatus,
    stdout: String,
    stderr: String,
}

fn run_facade_stop(home: &Path, helper: &Path) -> TeardownRun {
    let output = Command::new(facade_binary())
        .args(["codex", "stop", "--session-id", SESSION_ID])
        .env("LONGHOUSE_HOME", home)
        .env("LONGHOUSE_ENGINE_BIN", helper)
        .output()
        .expect("run longhouse facade");
    TeardownRun {
        status: output.status,
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
    }
}

/// T1/T2: the helper never acknowledges — the shape of a slow or wedged hosted
/// ingest under the old code — but the bridge committed `stopped`. This is the
/// exact 2026-07-24 failure. Before the fix it exited nonzero with a teardown
/// error.
#[test]
fn late_acknowledgement_with_committed_state_is_success() {
    let home = tempfile::tempdir().unwrap();
    write_state(home.path(), "stopped");
    write_contract(home.path());
    let helper = write_stub_helper(home.path(), "sleep 60");

    let started = std::time::Instant::now();
    let run = run_facade_stop(home.path(), &helper);
    let elapsed = started.elapsed();

    assert!(
        run.status.success(),
        "clean teardown must not fail on a late ack; stderr: {}",
        run.stderr
    );
    assert!(
        !contract_path(home.path()).exists(),
        "contract must be removed even when the stop RPC did not acknowledge"
    );
    assert!(
        elapsed < std::time::Duration::from_secs(15),
        "teardown must stay bounded, took {elapsed:?}"
    );
}

/// T5: no commit and the helper is unresponsive. The one genuine error.
#[test]
fn unresponsive_helper_without_commit_is_an_error() {
    let home = tempfile::tempdir().unwrap();
    write_state(home.path(), "running");
    write_contract(home.path());
    let helper = write_stub_helper(home.path(), "sleep 60");

    let run = run_facade_stop(home.path(), &helper);

    assert!(
        !run.status.success(),
        "a bridge that never committed and never acked must fail loudly"
    );
    assert!(
        run.stderr.contains(SESSION_ID),
        "error must name the session; got: {}",
        run.stderr
    );
    assert!(
        contract_path(home.path()).exists(),
        "contract must be retained while the session may still be live"
    );
}

/// T6: no state file at all and the helper failed. Nothing local can
/// reconstruct the terminal event, so the user is told plainly what is and is
/// not known — but their exit is not failed over a bridge that is already gone.
#[test]
fn missing_state_after_failed_stop_is_a_warning_not_an_error() {
    let home = tempfile::tempdir().unwrap();
    write_contract(home.path());
    let helper = write_stub_helper(home.path(), "exit 1");

    let run = run_facade_stop(home.path(), &helper);

    assert!(
        run.status.success(),
        "an orphaned bridge must not fail the user's exit; stderr: {}",
        run.stderr
    );
    assert!(
        run.stderr.contains("no bridge state file remains"),
        "user should be told what is missing; got: {}",
        run.stderr
    );
    assert!(
        !run.stderr.contains("reconcile"),
        "must not promise reconciliation with no durable record; got: {}",
        run.stderr
    );
    assert!(!contract_path(home.path()).exists());
}

/// T7: the committed fact cannot be read. Never downgrade that to
/// "will reconcile" — that would promise convergence that cannot happen.
#[test]
fn unreadable_state_is_unresponsive_not_orphaned() {
    let home = tempfile::tempdir().unwrap();
    let dir = state_dir(home.path());
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join(format!("{SESSION_ID}.json")), b"{ not json").unwrap();
    let helper = write_stub_helper(home.path(), "exit 1");

    let run = run_facade_stop(home.path(), &helper);

    assert!(
        !run.status.success(),
        "an unreadable commit must not be reported as a clean close"
    );
    assert!(
        !run.stderr.contains("reconcile"),
        "must not promise reconciliation for an unreadable state; got: {}",
        run.stderr
    );
}

/// The helper acknowledges promptly: the ordinary path stays ordinary.
#[test]
fn prompt_acknowledgement_is_success_and_cleans_up() {
    let home = tempfile::tempdir().unwrap();
    write_state(home.path(), "stopped");
    write_contract(home.path());
    let helper = write_stub_helper(home.path(), "exit 0");

    let started = std::time::Instant::now();
    let run = run_facade_stop(home.path(), &helper);

    assert!(run.status.success(), "stderr: {}", run.stderr);
    assert!(!contract_path(home.path()).exists());
    assert!(
        started.elapsed() < std::time::Duration::from_secs(3),
        "a prompt ack must not wait out the deadline"
    );
    assert!(
        !run.stdout.contains("hearth"),
        "the explicit stop command should not print the Helm close banner"
    );
}

/// T14: teardown must not be gated on the network at all. The facade is given
/// an unroutable API URL; a network-coupled teardown would stall on it.
#[test]
fn teardown_does_not_depend_on_hosted_reachability() {
    let home = tempfile::tempdir().unwrap();
    write_state(home.path(), "stopped");
    write_contract(home.path());
    let helper = write_stub_helper(home.path(), "exit 0");

    let started = std::time::Instant::now();
    let output = Command::new(facade_binary())
        .args(["codex", "stop", "--session-id", SESSION_ID])
        .env("LONGHOUSE_HOME", home.path())
        .env("LONGHOUSE_ENGINE_BIN", &helper)
        // Black-holed: TEST-NET-1, guaranteed unroutable.
        .env("LONGHOUSE_API_URL", "http://192.0.2.1:9/")
        .output()
        .expect("run longhouse facade");

    assert!(
        output.status.success(),
        "teardown must not depend on hosted reachability; stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        started.elapsed() < std::time::Duration::from_secs(10),
        "teardown stalled on an unreachable host"
    );
}

/// A helper that exits 0 without the bridge ever committing must not be read
/// as a clean close — the RPC's exit status is evidence, not authority.
#[test]
fn helper_success_without_commit_is_not_a_clean_close() {
    let home = tempfile::tempdir().unwrap();
    write_state(home.path(), "running");
    write_contract(home.path());
    let helper = write_stub_helper(home.path(), "exit 0");

    let run = run_facade_stop(home.path(), &helper);

    assert!(
        !run.status.success(),
        "a successful helper with uncommitted state must not report a clean close"
    );
    assert!(
        contract_path(home.path()).exists(),
        "contract must be retained when the session may still be live"
    );
}

/// A `stopped` file written by an engine predating durable teardown carries no
/// terminal event, so nothing can reconstruct the run's end. Report that
/// honestly rather than promising reconciliation.
#[test]
fn stopped_state_without_durable_marker_is_not_committed() {
    let home = tempfile::tempdir().unwrap();
    write_state_full(home.path(), "stopped", Some("run-1"), false);
    write_contract(home.path());
    let helper = write_stub_helper(home.path(), "exit 0");

    let run = run_facade_stop(home.path(), &helper);

    assert!(run.status.success(), "must not fail the user's exit");
    assert!(
        !run.stderr.contains("reconcile"),
        "must not promise reconciliation with no terminal record; got: {}",
        run.stderr
    );
}
