use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

use serde_json::{json, Value};

#[test]
fn registered_engine_command_advertises_coordination_tools() {
    let binary = env!("CARGO_BIN_EXE_longhouse-engine");
    let mut child = Command::new(binary)
        .args(["claude-channel", "serve"])
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
    let _: Value = serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
    let tools: Value = serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
    let names = tools["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|tool| tool["name"].as_str())
        .collect::<Vec<_>>();
    for expected in [
        "peers",
        "message_session",
        "check_messages",
        "ack_message",
        "check_wall",
        "session_tail",
    ] {
        assert!(names.contains(&expected), "missing {expected}");
    }
    assert!(child.wait().unwrap().success());
}
