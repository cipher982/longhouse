//! Workspace recovery for Antigravity sessions.
//!
//! Antigravity's transcript at
//! `~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/
//! transcript.jsonl` carries no working-directory field, so a parser that only
//! reads the transcript can never attribute the session. The CLI does record
//! the workspace, in two places beside the transcript:
//!
//! - `conversations/<conversation-id>.db`, whose `trajectory_metadata_blob`
//!   holds a `file://` workspace URI and, when the workspace is a checkout, the
//!   git remote.
//! - `history.jsonl`, one JSON object per user turn carrying `workspace` and
//!   `conversationId`.
//!
//! Neither covers every session and their coverage differs, so both are
//! consulted: measured on a 96-session store, the conversation database
//! resolved 39, history resolved 37, and together they resolved 45.
//!
//! The blob is protobuf without a published schema. Rather than decode fields
//! by number — which would break on any upstream reordering — this scans for
//! the two self-describing shapes inside it: a `file://` URI and a git remote
//! ending in `.git`. A shape that is absent yields `None`, never a guess.

use std::path::Path;
use std::path::PathBuf;

use rusqlite::{Connection, OpenFlags};
use serde_json::Value;

/// Workspace facts recovered from Antigravity's own sidecars.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AntigravityWorkspace {
    pub cwd: String,
    /// Present only when the conversation database recorded one. `history.jsonl`
    /// carries no remote, so a session resolved through it has `None` here and
    /// falls back to whatever the working directory resolves to on disk.
    pub git_repo: Option<String>,
}

/// Root of the Antigravity CLI store, given a brain transcript path.
///
/// `<root>/brain/<id>/.system_generated/logs/transcript.jsonl` — walk back up to
/// the directory that holds `brain`, so the sidecars are found relative to the
/// transcript rather than by assuming `$HOME`. A relocated or test store then
/// resolves the same way a real one does.
fn store_root_from_transcript(path: &Path) -> Option<PathBuf> {
    let mut dir = path.parent();
    while let Some(candidate) = dir {
        if candidate.file_name().and_then(|name| name.to_str()) == Some("brain") {
            return candidate.parent().map(Path::to_path_buf);
        }
        dir = candidate.parent();
    }
    None
}

/// Extract the first `file://` path and any git remote from the metadata blob.
fn scan_metadata_blob(blob: &[u8]) -> Option<AntigravityWorkspace> {
    let cwd = scan_file_uri(blob)?;
    Some(AntigravityWorkspace {
        cwd,
        git_repo: scan_git_remote(blob),
    })
}

/// The workspace URI is stored as `file:///abs/path` in a length-delimited
/// field, so it ends at the first byte that cannot appear in a path.
fn scan_file_uri(blob: &[u8]) -> Option<String> {
    const PREFIX: &[u8] = b"file://";
    let start = blob
        .windows(PREFIX.len())
        .position(|window| window == PREFIX)?
        + PREFIX.len();
    let end = blob[start..]
        .iter()
        .position(|byte| !(0x20..0x7f).contains(byte))
        .map(|offset| start + offset)
        .unwrap_or(blob.len());
    let path = std::str::from_utf8(&blob[start..end]).ok()?.trim();
    if path.is_empty() || !path.starts_with('/') {
        return None;
    }
    Some(path.to_string())
}

fn scan_git_remote(blob: &[u8]) -> Option<String> {
    for prefix in [b"git@".as_slice(), b"https://".as_slice()] {
        let Some(start) = blob
            .windows(prefix.len())
            .position(|window| window == prefix)
        else {
            continue;
        };
        let end = blob[start..]
            .iter()
            .position(|byte| !(0x21..0x7f).contains(byte))
            .map(|offset| start + offset)
            .unwrap_or(blob.len());
        let Ok(candidate) = std::str::from_utf8(&blob[start..end]) else {
            continue;
        };
        // Only accept a remote that terminates in `.git`; a bare `https://`
        // hit is as likely to be a documentation link inside the blob.
        if let Some(cut) = candidate.find(".git") {
            return Some(candidate[..cut + 4].to_string());
        }
    }
    None
}

fn from_conversation_db(root: &Path, conversation_id: &str) -> Option<AntigravityWorkspace> {
    let db = root.join("conversations").join(format!("{conversation_id}.db"));
    if !db.is_file() {
        return None;
    }
    let conn = Connection::open_with_flags(&db, OpenFlags::SQLITE_OPEN_READ_ONLY).ok()?;
    let blob: Vec<u8> = conn
        .query_row("SELECT data FROM trajectory_metadata_blob", [], |row| {
            row.get(0)
        })
        .ok()?;
    scan_metadata_blob(&blob)
}

fn from_history(root: &Path, conversation_id: &str) -> Option<AntigravityWorkspace> {
    let history = std::fs::read_to_string(root.join("history.jsonl")).ok()?;
    // Later turns win: a conversation that moved workspace should report where
    // it ended up, matching the transcript the session actually wrote.
    let mut found = None;
    for line in history.lines() {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if value.get("conversationId").and_then(Value::as_str) != Some(conversation_id) {
            continue;
        }
        if let Some(workspace) = value.get("workspace").and_then(Value::as_str) {
            let trimmed = workspace.trim();
            if !trimmed.is_empty() {
                found = Some(trimmed.to_string());
            }
        }
    }
    found.map(|cwd| AntigravityWorkspace {
        cwd,
        git_repo: None,
    })
}

/// Recover the workspace for the Antigravity conversation whose transcript is
/// at `transcript_path`. Returns `None` when neither sidecar knows it, which is
/// the honest answer for roughly half the store.
pub fn antigravity_workspace(
    transcript_path: &Path,
    conversation_id: &str,
) -> Option<AntigravityWorkspace> {
    let root = store_root_from_transcript(transcript_path)?;
    from_conversation_db(&root, conversation_id).or_else(|| from_history(&root, conversation_id))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store(temp: &Path, conversation_id: &str) -> PathBuf {
        let transcript = temp
            .join("brain")
            .join(conversation_id)
            .join(".system_generated")
            .join("logs")
            .join("transcript.jsonl");
        std::fs::create_dir_all(transcript.parent().unwrap()).unwrap();
        std::fs::write(&transcript, "{}\n").unwrap();
        transcript
    }

    #[test]
    fn store_root_is_found_relative_to_the_transcript() {
        let temp = tempfile::tempdir().unwrap();
        let transcript = store(temp.path(), "5f62a636-1412-4afe-9cfd-a5079e0a0366");
        assert_eq!(
            store_root_from_transcript(&transcript).as_deref(),
            Some(temp.path())
        );
        assert_eq!(store_root_from_transcript(Path::new("/tmp/elsewhere.jsonl")), None);
    }

    #[test]
    fn metadata_blob_yields_workspace_and_remote() {
        // Byte shape taken from a real trajectory_metadata_blob: length-delimited
        // protobuf fields with the URI and remote embedded as plain text.
        let mut blob = vec![0x0a, 0x7b, 0x0a, 0x1f];
        blob.extend_from_slice(b"file:///Users/davidrose/git/g55");
        blob.extend_from_slice(&[0x1a, 0x31, 0x0a, 0x0d]);
        blob.extend_from_slice(b"cipher982/g55");
        blob.extend_from_slice(&[0x12, 0x20]);
        blob.extend_from_slice(b"git@github.com:cipher982/g55.git");
        blob.extend_from_slice(&[0x22, 0x04]);
        blob.extend_from_slice(b"main");

        let facts = scan_metadata_blob(&blob).unwrap();
        assert_eq!(facts.cwd, "/Users/davidrose/git/g55");
        assert_eq!(facts.git_repo.as_deref(), Some("git@github.com:cipher982/g55.git"));
    }

    #[test]
    fn a_blob_without_a_workspace_uri_yields_nothing() {
        assert_eq!(scan_metadata_blob(b"\x0a\x04none"), None);
        // A relative or malformed URI is refused rather than half-accepted.
        assert_eq!(scan_file_uri(b"file://relative/path"), None);
        assert_eq!(scan_file_uri(b"file://"), None);
    }

    #[test]
    fn a_bare_link_is_not_mistaken_for_a_remote() {
        assert_eq!(scan_git_remote(b"see https://antigravity.google/docs"), None);
        assert_eq!(
            scan_git_remote(b"https://github.com/cipher982/g55.git and more").as_deref(),
            Some("https://github.com/cipher982/g55.git")
        );
    }

    #[test]
    fn history_resolves_a_conversation_and_prefers_its_last_turn() {
        let temp = tempfile::tempdir().unwrap();
        let id = "5f62a636-1412-4afe-9cfd-a5079e0a0366";
        let transcript = store(temp.path(), id);
        std::fs::write(
            temp.path().join("history.jsonl"),
            format!(
                "{}\n{}\n{}\n",
                r#"{"display":"x","workspace":"/Users/me/git/other","conversationId":"11111111-1111-4111-8111-111111111111"}"#,
                format_args!(r#"{{"display":"a","workspace":"/Users/me/git/first","conversationId":"{id}"}}"#),
                format_args!(r#"{{"display":"b","workspace":"/Users/me/git/last","conversationId":"{id}"}}"#),
            ),
        )
        .unwrap();

        let facts = antigravity_workspace(&transcript, id).unwrap();
        assert_eq!(facts.cwd, "/Users/me/git/last");
        assert_eq!(facts.git_repo, None);
    }

    #[test]
    fn the_conversation_database_wins_over_history() {
        let temp = tempfile::tempdir().unwrap();
        let id = "5f62a636-1412-4afe-9cfd-a5079e0a0366";
        let transcript = store(temp.path(), id);
        std::fs::create_dir_all(temp.path().join("conversations")).unwrap();
        let conn =
            Connection::open(temp.path().join("conversations").join(format!("{id}.db"))).unwrap();
        conn.execute(
            "CREATE TABLE trajectory_metadata_blob (id text DEFAULT \"main\", data blob, PRIMARY KEY (id))",
            [],
        )
        .unwrap();
        let mut blob = vec![0x0a, 0x1f];
        blob.extend_from_slice(b"file:///Users/me/git/authoritative");
        conn.execute(
            "INSERT INTO trajectory_metadata_blob (id, data) VALUES ('main', ?1)",
            [&blob],
        )
        .unwrap();
        drop(conn);

        std::fs::write(
            temp.path().join("history.jsonl"),
            format!(r#"{{"workspace":"/Users/me/git/stale","conversationId":"{id}"}}"#),
        )
        .unwrap();

        assert_eq!(
            antigravity_workspace(&transcript, id).unwrap().cwd,
            "/Users/me/git/authoritative"
        );
    }

    #[test]
    fn an_unknown_conversation_stays_unresolved() {
        let temp = tempfile::tempdir().unwrap();
        let id = "5f62a636-1412-4afe-9cfd-a5079e0a0366";
        let transcript = store(temp.path(), id);
        assert_eq!(antigravity_workspace(&transcript, id), None);
    }
}
