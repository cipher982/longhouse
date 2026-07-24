use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

use serde_json::{json, Value};

#[test]
fn registered_engine_command_advertises_coordination_tools() {
    let binary = env!("CARGO_BIN_EXE_longhouse-engine");
    let state_root = tempfile::tempdir().unwrap();
    let mut child = Command::new(binary)
        .args(["claude-channel", "serve", "--state-root"])
        .arg(state_root.path())
        .env("LONGHOUSE_COORDINATION_TOKEN", "test-coordination-token")
        .env(
            "LONGHOUSE_MANAGED_SESSION_ID",
            "11111111-1111-4111-8111-111111111111",
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("spawn registered MCP command");
    let mut stdin = child.stdin.take().expect("MCP stdin");
    let stdout = child.stdout.take().expect("MCP stdout");
    writeln!(stdin, "{}", json!({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}
    }))
    .unwrap();
    writeln!(
        stdin,
        "{}",
        json!({"jsonrpc":"2.0","id":2,"method":"tools/list"})
    )
    .unwrap();
    drop(stdin);

    let mut lines = BufReader::new(stdout).lines();
    let initialize: Value = serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
    assert_eq!(
        initialize["result"]["capabilities"]["tools"]["listChanged"],
        false
    );
    assert!(initialize["result"]["capabilities"]
        .get("experimental")
        .is_none());
    assert_eq!(
        initialize["result"]["serverInfo"]["name"],
        "longhouse-coordination"
    );
    let tools: Value = serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
    let names = tools["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|tool| tool["name"].as_str())
        .collect::<Vec<_>>();
    assert_eq!(names.len(), 5);
    for expected in ["peers", "tail", "send", "inbox", "reply"] {
        assert!(names.contains(&expected), "missing {expected}");
    }
    assert!(child.wait().unwrap().success());
    assert!(!state_root.path().join("sessions").exists());
}
