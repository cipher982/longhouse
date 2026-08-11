use std::fs;
use std::io::{Read, Write};
use std::os::unix::net::UnixListener;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};
use tempfile::tempdir;

fn engine() -> &'static str {
    env!("CARGO_BIN_EXE_longhouse-engine")
}

fn run_hook(home: &std::path::Path, command: &str, event: &str, input: &str) -> Value {
    run_hook_with_env(home, command, event, input, &[])
}

fn run_hook_with_env(
    home: &std::path::Path,
    command_name: &str,
    event: &str,
    input: &str,
    extra_env: &[(&str, &str)],
) -> Value {
    let mut command = Command::new(engine());
    command
        .args([command_name, event])
        .env("LONGHOUSE_HOME", home)
        .env("LONGHOUSE_SESSION_ID", "managed-session")
        .env("LONGHOUSE_CURSOR_LAUNCH_ID", "launch-id")
        .env("LONGHOUSE_PERMISSION_HOOK_ENABLED", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped());
    for (name, value) in extra_env {
        command.env(name, value);
    }
    let mut child = command.spawn().unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

fn permission_server(decision: &'static str) -> (String, thread::JoinHandle<()>) {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let handle = thread::spawn(move || {
        for index in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut raw = [0u8; 8192];
            let count = stream.read(&mut raw).unwrap();
            let request = String::from_utf8_lossy(&raw[..count]);
            assert!(request
                .to_ascii_lowercase()
                .contains("x-agents-token: session-token"));
            let body = if index == 0 {
                r#"{"pause_request_id":"pause-1"}"#.to_string()
            } else {
                format!(r#"{{"resolved":true,"decision":"{decision}"}}"#)
            };
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    (url, handle)
}

fn captured_permission_server(
    invocations: usize,
) -> (String, Arc<Mutex<Vec<String>>>, thread::JoinHandle<()>) {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let request_ids = Arc::new(Mutex::new(Vec::new()));
    let captured = request_ids.clone();
    let handle = thread::spawn(move || {
        for index in 0..(invocations * 2) {
            let (mut stream, _) = listener.accept().unwrap();
            let mut raw = [0u8; 8192];
            let count = stream.read(&mut raw).unwrap();
            let request = String::from_utf8_lossy(&raw[..count]);
            let body = if index % 2 == 0 {
                let json = request.split("\r\n\r\n").nth(1).unwrap();
                let value: Value = serde_json::from_str(json).unwrap();
                captured
                    .lock()
                    .unwrap()
                    .push(value["tool_use_id"].as_str().unwrap().to_owned());
                r#"{"pause_request_id":"pause-1"}"#
            } else {
                r#"{"resolved":true,"decision":"allow"}"#
            };
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
        }
    });
    (url, request_ids, handle)
}

fn unresolved_permission_server() -> (String, thread::JoinHandle<()>) {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let handle = thread::spawn(move || loop {
        let (mut stream, _) = listener.accept().unwrap();
        let mut raw = [0u8; 8192];
        let count = stream.read(&mut raw).unwrap();
        let request = String::from_utf8_lossy(&raw[..count]);
        let (status, body, done) = if request.starts_with("POST /api/agents/permission-requests/") {
            ("200 OK", r#"{}"#, true)
        } else if request.starts_with("POST /api/agents/permission-requests ") {
            ("200 OK", r#"{"pause_request_id":"pause-timeout"}"#, false)
        } else {
            ("200 OK", r#"{"resolved":false}"#, false)
        };
        write!(
            stream,
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
        if done {
            break;
        }
    });
    (url, handle)
}

fn pending_claim(home: &std::path::Path) -> std::path::PathBuf {
    let claim = home.join("managed-local/cursor-helm/binding-probes/managed-session.json");
    fs::create_dir_all(claim.parent().unwrap()).unwrap();
    fs::write(
        &claim,
        serde_json::to_vec(&json!({
            "schema_version": 2,
            "provider": "cursor",
            "status": "pending",
            "session_id": "managed-session",
            "conversation_uuid": "cursor-id",
            "launch_id": "launch-id",
            "permission_policy": "remote_human"
        }))
        .unwrap(),
    )
    .unwrap();
    claim
}

#[test]
fn native_engine_owns_idempotent_cursor_hook_configuration() {
    let root = tempdir().unwrap();
    let cursor = root.path().join(".cursor");
    fs::create_dir_all(&cursor).unwrap();
    fs::write(
        cursor.join("hooks.json"),
        serde_json::to_vec(&json!({
            "version": 1,
            "hooks": {"beforeShellExecution": [
                {"command":"./user-hook","timeout":3},
                {"command":"/tmp/longhouse-cursor-hook.py beforeShellExecution","timeout":5}
            ]}
        }))
        .unwrap(),
    )
    .unwrap();
    for _ in 0..2 {
        let status = Command::new(engine())
            .args(["cursor-helm", "configure-hooks", "--cursor-dir"])
            .arg(&cursor)
            .status()
            .unwrap();
        assert!(status.success());
    }
    let config: Value =
        serde_json::from_slice(&fs::read(cursor.join("hooks.json")).unwrap()).unwrap();
    let shell = config["hooks"]["beforeShellExecution"].as_array().unwrap();
    assert_eq!(
        shell
            .iter()
            .filter(|entry| entry["command"] == "./user-hook")
            .count(),
        1
    );
    assert_eq!(
        shell
            .iter()
            .filter(|entry| entry["command"]
                .as_str()
                .is_some_and(|value| value.contains("cursor-lifecycle-hook")))
            .count(),
        1
    );
    assert_eq!(
        shell
            .iter()
            .filter(|entry| entry["command"]
                .as_str()
                .is_some_and(|value| value.contains("cursor-permission-hook")))
            .count(),
        1
    );
    assert!(shell.iter().all(|entry| !entry["command"]
        .as_str()
        .is_some_and(|value| value.contains("longhouse-cursor-hook.py"))));
}

#[test]
fn enabled_permission_hook_denies_malformed_input_and_missing_claim() {
    let root = tempdir().unwrap();
    let malformed = run_hook(
        root.path(),
        "cursor-permission-hook",
        "beforeShellExecution",
        "not-json",
    );
    assert_eq!(malformed["permission"], "deny");

    let missing = run_hook(
        root.path(),
        "cursor-permission-hook",
        "beforeShellExecution",
        r#"{"conversation_id":"cursor-id","command":"pwd"}"#,
    );
    assert_eq!(missing["permission"], "deny");
}

#[test]
fn permission_hook_returns_remote_allow_deny_and_transport_denial() {
    for decision in ["allow", "deny"] {
        let root = tempdir().unwrap();
        pending_claim(root.path());
        let (url, server) = permission_server(decision);
        let output = run_hook_with_env(
            root.path(),
            "cursor-permission-hook",
            "beforeShellExecution",
            r#"{"conversation_id":"cursor-id","generation_id":"turn-1","command":"pwd"}"#,
            &[
                ("LONGHOUSE_HOOK_URL", &url),
                ("LONGHOUSE_HOOK_TOKEN", "session-token"),
            ],
        );
        server.join().unwrap();
        assert_eq!(output["permission"], decision);
    }

    let root = tempdir().unwrap();
    pending_claim(root.path());
    let output = run_hook_with_env(
        root.path(),
        "cursor-permission-hook",
        "beforeShellExecution",
        r#"{"conversation_id":"cursor-id","generation_id":"turn-1","command":"pwd"}"#,
        &[
            ("LONGHOUSE_HOOK_URL", "http://127.0.0.1:1"),
            ("LONGHOUSE_HOOK_TOKEN", "session-token"),
        ],
    );
    assert_eq!(output["permission"], "deny");
}

#[test]
fn permission_hook_denies_mismatch_invalid_timeout_poll_failure_and_timeout() {
    let root = tempdir().unwrap();
    let claim = pending_claim(root.path());
    let mut mismatched: Value = serde_json::from_slice(&fs::read(&claim).unwrap()).unwrap();
    mismatched["launch_id"] = json!("other-launch");
    fs::write(&claim, serde_json::to_vec(&mismatched).unwrap()).unwrap();
    let denied = run_hook(
        root.path(),
        "cursor-permission-hook",
        "beforeShellExecution",
        r#"{"conversation_id":"cursor-id","command":"pwd"}"#,
    );
    assert_eq!(denied["permission"], "deny");

    mismatched
        .as_object_mut()
        .unwrap()
        .remove("permission_policy");
    fs::write(&claim, serde_json::to_vec(&mismatched).unwrap()).unwrap();
    let inert = run_hook(
        root.path(),
        "cursor-permission-hook",
        "beforeShellExecution",
        r#"{"conversation_id":"cursor-id","command":"pwd"}"#,
    );
    assert_eq!(inert, json!({}));

    pending_claim(root.path());
    let invalid = run_hook_with_env(
        root.path(),
        "cursor-permission-hook",
        "beforeShellExecution",
        r#"{"conversation_id":"cursor-id","command":"pwd"}"#,
        &[
            ("LONGHOUSE_HOOK_URL", "http://127.0.0.1:1"),
            ("LONGHOUSE_HOOK_TOKEN", "token"),
            ("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S", "not-a-number"),
        ],
    );
    assert_eq!(invalid["permission"], "deny");

    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let server = thread::spawn(move || {
        for index in 0..2 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut raw = [0u8; 8192];
            let _ = stream.read(&mut raw).unwrap();
            let (status, body) = if index == 0 {
                ("200 OK", r#"{"pause_request_id":"pause-1"}"#)
            } else {
                ("500 Internal Server Error", r#"{}"#)
            };
            write!(stream, "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", body.len(), body).unwrap();
        }
    });
    let poll_failure = run_hook_with_env(
        root.path(),
        "cursor-permission-hook",
        "beforeShellExecution",
        r#"{"conversation_id":"cursor-id","command":"pwd"}"#,
        &[
            ("LONGHOUSE_HOOK_URL", &url),
            ("LONGHOUSE_HOOK_TOKEN", "token"),
        ],
    );
    server.join().unwrap();
    assert_eq!(poll_failure["permission"], "deny");

    let (url, server) = unresolved_permission_server();
    let timeout = run_hook_with_env(
        root.path(),
        "cursor-permission-hook",
        "beforeShellExecution",
        r#"{"conversation_id":"cursor-id","command":"pwd"}"#,
        &[
            ("LONGHOUSE_HOOK_URL", &url),
            ("LONGHOUSE_HOOK_TOKEN", "token"),
            ("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S", "1"),
        ],
    );
    server.join().unwrap();
    assert_eq!(timeout["permission"], "deny");
}

#[test]
fn identical_calls_without_cursor_ids_receive_distinct_request_ids() {
    let root = tempdir().unwrap();
    pending_claim(root.path());
    let (url, request_ids, server) = captured_permission_server(2);
    for _ in 0..2 {
        let result = run_hook_with_env(
            root.path(),
            "cursor-permission-hook",
            "beforeShellExecution",
            r#"{"conversation_id":"cursor-id","generation_id":"turn-1","command":"pwd"}"#,
            &[
                ("LONGHOUSE_HOOK_URL", &url),
                ("LONGHOUSE_HOOK_TOKEN", "token"),
            ],
        );
        assert_eq!(result["permission"], "allow");
    }
    server.join().unwrap();
    let request_ids = request_ids.lock().unwrap();
    assert_eq!(request_ids.len(), 2);
    assert_ne!(request_ids[0], request_ids[1]);
}

#[test]
fn lifecycle_promotes_only_registered_claim_and_emits_presence() {
    let root = tempdir().unwrap();
    let claim = pending_claim(root.path());
    let payload = r#"{"conversation_id":"cursor-id","generation_id":"turn-1"}"#;

    let first = run_hook(
        root.path(),
        "cursor-lifecycle-hook",
        "beforeSubmitPrompt",
        payload,
    );
    assert_eq!(first, json!({}));
    assert_eq!(
        serde_json::from_slice::<Value>(&fs::read(&claim).unwrap()).unwrap()["status"],
        "pending"
    );

    fs::write(
        root.path()
            .join("managed-local/cursor-helm/managed-session.json"),
        br#"{"registration":"registered"}"#,
    )
    .unwrap();
    run_hook(
        root.path(),
        "cursor-lifecycle-hook",
        "beforeSubmitPrompt",
        payload,
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&fs::read(&claim).unwrap()).unwrap()["status"],
        "observed"
    );
    assert_eq!(
        fs::read_dir(root.path().join("agent/outbox"))
            .unwrap()
            .count(),
        2
    );
}

#[test]
fn concurrent_lifecycle_hooks_append_complete_ndjson_records() {
    let root = tempdir().unwrap();
    let claim = pending_claim(root.path());
    let mut value: Value = serde_json::from_slice(&fs::read(&claim).unwrap()).unwrap();
    value["status"] = json!("observed");
    fs::write(&claim, serde_json::to_vec(&value).unwrap()).unwrap();

    let mut children = Vec::new();
    for index in 0..32 {
        let event = if index % 2 == 0 {
            "stop"
        } else {
            "afterAgentResponse"
        };
        let payload = serde_json::to_vec(&json!({
            "conversation_id": "cursor-id",
            "generation_id": format!("turn-{index}"),
            "text": "x".repeat(16 * 1024),
        }))
        .unwrap();
        let mut child = Command::new(engine())
            .args(["cursor-lifecycle-hook", event])
            .env("LONGHOUSE_HOME", root.path())
            .env("CURSOR_HOME", root.path().join("cursor"))
            .env("LONGHOUSE_SESSION_ID", "managed-session")
            .env("LONGHOUSE_CURSOR_LAUNCH_ID", "launch-id")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        child.stdin.take().unwrap().write_all(&payload).unwrap();
        children.push(child);
    }
    for mut child in children {
        assert!(child.wait().unwrap().success());
    }

    let events = fs::read_to_string(
        root.path()
            .join("managed-local/cursor-helm/hook-events/managed-session.ndjson"),
    )
    .unwrap();
    let rows = events
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(rows.len(), 32);
    let generation_ids = rows
        .iter()
        .map(|row| row["payload"]["generation_id"].as_str().unwrap())
        .collect::<std::collections::HashSet<_>>();
    assert_eq!(generation_ids.len(), 32);
    assert!(rows.iter().all(|row| row["launch_id"] == "launch-id"));
}

#[test]
fn terminal_hook_wakes_the_exact_cursor_store() {
    let root = tempdir().unwrap();
    let cursor = root.path().join("cursor");
    let store = cursor.join("chats/workspace/cursor-id/store.db");
    fs::create_dir_all(store.parent().unwrap()).unwrap();
    fs::write(&store, b"cursor-store").unwrap();
    let claim = pending_claim(root.path());
    let mut value: Value = serde_json::from_slice(&fs::read(&claim).unwrap()).unwrap();
    value["status"] = json!("observed");
    fs::write(&claim, serde_json::to_vec(&value).unwrap()).unwrap();

    let socket = root.path().join("agent/transcript-wake.sock");
    fs::create_dir_all(socket.parent().unwrap()).unwrap();
    let listener = UnixListener::bind(&socket).unwrap();
    listener.set_nonblocking(false).unwrap();
    let receiver = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut raw = Vec::new();
        stream.read_to_end(&mut raw).unwrap();
        serde_json::from_slice::<Value>(&raw).unwrap()
    });

    let mut child = Command::new(engine())
        .args(["cursor-lifecycle-hook", "stop"])
        .env("LONGHOUSE_HOME", root.path())
        .env("CURSOR_HOME", &cursor)
        .env("LONGHOUSE_SESSION_ID", "managed-session")
        .env("LONGHOUSE_CURSOR_LAUNCH_ID", "launch-id")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(br#"{"conversation_id":"cursor-id","generation_id":"turn-1"}"#)
        .unwrap();
    assert!(child.wait().unwrap().success());
    let wake = receiver.join().unwrap();
    assert_eq!(wake["path"], store.display().to_string());
    assert_eq!(wake["session_id"], "managed-session");
    assert_eq!(wake["turn_id"], "turn-1");
    assert_eq!(wake["file_len_hint"], 12);
}
