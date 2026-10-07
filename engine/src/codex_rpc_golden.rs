//! Golden capture of the JSON-RPC each Codex app-server client writes and the
//! outcomes it reports: the Helm bridge, the Console worker pool and the
//! release canary. The fixtures under `tests/fixtures/codex_rpc/` were captured
//! before the three clients shared `codex_app_server_rpc`, so they pin each
//! client's wire output exactly. `LONGHOUSE_UPDATE_GOLDEN=1` rewrites them.

use std::path::{Path, PathBuf};

use serde_json::Value;

/// Replace the crate version and scratch paths so captures compare across
/// builds and runs.
pub(crate) fn normalize_text(text: &str, scratch: &[&Path]) -> String {
    let mut text = text.replace(env!("CARGO_PKG_VERSION"), "<version>");
    for path in scratch {
        text = text.replace(&path.display().to_string(), "<scratch>");
    }
    text
}

/// A stdio fake app-server: `body` handles one parsed `msg` per stdin line, and
/// every raw line Longhouse wrote is appended to the returned log first.
pub(crate) fn write_recording_fake(dir: &Path, body: &str) -> (PathBuf, PathBuf) {
    use std::os::unix::fs::PermissionsExt;

    let bin = dir.join("codex");
    let raw_log = dir.join("client-lines.raw");
    let script = format!(
        r#"#!/usr/bin/env python3
import json, sys
RAW = open({raw}, "a", encoding="utf-8")

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    RAW.write(line)
    RAW.flush()
    msg = json.loads(line)
    method = msg.get("method")
{body}
"#,
        raw = serde_json::to_string(&raw_log.display().to_string()).unwrap(),
        body = body
            .lines()
            .map(|line| format!("    {line}"))
            .collect::<Vec<_>>()
            .join("\n"),
    );
    std::fs::write(&bin, script).unwrap();
    let mut permissions = std::fs::metadata(&bin).unwrap().permissions();
    permissions.set_mode(0o755);
    std::fs::set_permissions(&bin, permissions).unwrap();
    (bin, raw_log)
}

/// The raw lines a recording fake logged, newline-stripped and normalized.
pub(crate) fn recorded_lines(raw_log: &Path, scratch: &[&Path]) -> Vec<Value> {
    std::fs::read_to_string(raw_log)
        .unwrap_or_default()
        .lines()
        .map(|line| Value::String(normalize_text(line, scratch)))
        .collect()
}

/// `Ok(result)` / `Err(message)` as a comparable value.
pub(crate) fn outcome<E: std::fmt::Display>(result: &Result<Value, E>, scratch: &[&Path]) -> Value {
    match result {
        Ok(value) => serde_json::json!({ "ok": value }),
        Err(error) => serde_json::json!({ "err": normalize_text(&format!("{error:#}"), scratch) }),
    }
}

pub(crate) fn assert_golden(name: &str, actual: &Value) {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/codex_rpc")
        .join(format!("{name}.json"));
    if std::env::var_os("LONGHOUSE_UPDATE_GOLDEN").is_some() {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let mut text = serde_json::to_string_pretty(actual).unwrap();
        text.push('\n');
        std::fs::write(&path, text).unwrap();
        return;
    }
    let expected: Value = serde_json::from_slice(
        &std::fs::read(&path).unwrap_or_else(|_| panic!("missing golden {}", path.display())),
    )
    .unwrap();
    pretty_assertions::assert_eq!(&expected, actual, "golden {name} drifted");
}
