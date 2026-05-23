//! Image attachment fetch + LocalImage payload builder for the codex bridge.
//!
//! The runtime host stores attachment blobs and stamps their machine URLs
//! into the IPC payload that the backend hands to `codex-bridge send`.
//! This module pulls the bytes back over `X-Agents-Token`, writes them
//! into a per-session tmpdir, verifies sha256 against the runtime-host
//! row, and returns paths the bridge can hand to Codex via
//! `UserInput::LocalImage`.
//!
//! Codex itself loads the file off disk — we never hand it base64.

use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

const ATTACHMENT_FETCH_TIMEOUT: Duration = Duration::from_secs(15);
const TMP_ROOT_NAME: &str = "lh-attach";
const MAX_BLOB_BYTES: usize = 4 * 1024 * 1024; // 2 MB server cap + ample headroom

/// Reference to a single blob the engine should fetch from the runtime
/// host before invoking turn/start or turn/steer.
///
/// Mirrors the JSON the backend stamps into the IPC payload:
/// `{"id": "...", "mime_type": "image/png", "sha256": "...", "blob_url": "https://..."}`.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct AttachmentRef {
    pub id: String,
    pub mime_type: String,
    pub sha256: String,
    pub blob_url: String,
}

/// Outcome of fetching one attachment to disk.
#[derive(Debug, Clone)]
pub struct FetchedAttachment {
    pub id: String,
    pub mime_type: String,
    pub path: PathBuf,
    pub bytes: usize,
}

/// Parse the optional `attachments` array from an IPC request.
///
/// Returns `Ok(vec![])` when the array is missing or empty so the no-image
/// path stays a free zero-overhead branch. Surfaces a clear error when the
/// shape is wrong rather than silently dropping fields.
pub fn parse_attachments(request: &Value) -> Result<Vec<AttachmentRef>> {
    let Some(value) = request.get("attachments") else {
        return Ok(Vec::new());
    };
    if value.is_null() {
        return Ok(Vec::new());
    }
    let array = value
        .as_array()
        .context("'attachments' must be a JSON array")?;
    let mut out = Vec::with_capacity(array.len());
    for (idx, item) in array.iter().enumerate() {
        let parsed: AttachmentRef = serde_json::from_value(item.clone())
            .with_context(|| format!("attachments[{idx}] is not a valid AttachmentRef"))?;
        if Uuid::parse_str(&parsed.id).is_err() {
            bail!("attachments[{idx}].id is not a valid UUID");
        }
        if parsed.sha256.len() != 64 || !parsed.sha256.chars().all(|c| c.is_ascii_hexdigit()) {
            bail!("attachments[{idx}].sha256 must be 64 hex chars");
        }
        validate_blob_url(&parsed.blob_url)
            .with_context(|| format!("attachments[{idx}].blob_url"))?;
        out.push(parsed);
    }
    Ok(out)
}

/// Reject anything that isn't a relative path under `/api/agents/`.
///
/// The engine forwards `X-Agents-Token` to whatever URL we resolve, so an
/// absolute URL — even with the right scheme — would let a leaked or
/// malformed payload exfiltrate the machine token. Keeping this strict
/// also means tests can only assert one canonical resolution path.
fn validate_blob_url(blob_url: &str) -> Result<()> {
    let trimmed = blob_url.trim();
    if trimmed.is_empty() {
        bail!("is empty");
    }
    if !trimmed.starts_with('/') {
        bail!("must be a relative path starting with '/'");
    }
    if !trimmed.starts_with("/api/agents/") {
        bail!("must point at /api/agents/");
    }
    if trimmed.contains("..") {
        bail!("must not contain '..'");
    }
    Ok(())
}

/// Per-session tmpdir for attachment blobs. Lives under `$TMPDIR/lh-attach`
/// so cleanup on engine restart is straightforward. Session id is verified
/// to be a UUID before reaching here so `..` / `/` can never escape.
pub fn session_tmpdir(session_id: &str) -> PathBuf {
    std::env::temp_dir().join(TMP_ROOT_NAME).join(session_id)
}

/// Root tmpdir for all sessions. Used by startup orphan cleanup.
pub fn tmp_root() -> PathBuf {
    std::env::temp_dir().join(TMP_ROOT_NAME)
}

fn extension_for_mime(mime: &str) -> &'static str {
    match mime {
        "image/png" => "png",
        "image/jpeg" => "jpg",
        "image/webp" => "webp",
        "image/gif" => "gif",
        _ => "bin",
    }
}

/// Resolve `blob_url` against the engine's configured `api_url`.
/// `validate_blob_url` already enforced relative + `/api/agents/` prefix,
/// so we just join here. The backend never has to know what hostname the
/// engine actually reaches it at, and we never send the machine token to
/// an arbitrary origin.
fn resolve_blob_url(api_url: &str, blob_url: &str) -> String {
    let base = api_url.trim_end_matches('/');
    format!("{base}{blob_url}")
}

/// Fetch one attachment over HTTP, verify sha256, and write it under
/// `session_tmpdir(session_id)` with mode 0600.
pub async fn fetch_one(
    http: &reqwest::Client,
    api_url: &str,
    api_token: &str,
    session_id: &str,
    attachment: &AttachmentRef,
) -> Result<FetchedAttachment> {
    if Uuid::parse_str(session_id).is_err() {
        bail!("session_id {session_id:?} is not a valid UUID");
    }
    if Uuid::parse_str(&attachment.id).is_err() {
        bail!("attachment id {:?} is not a valid UUID", attachment.id);
    }
    validate_blob_url(&attachment.blob_url)?;
    let started = Instant::now();
    let resolved_url = resolve_blob_url(api_url, &attachment.blob_url);
    let response = http
        .get(&resolved_url)
        .header("X-Agents-Token", api_token)
        .timeout(ATTACHMENT_FETCH_TIMEOUT)
        .send()
        .await
        .with_context(|| format!("fetching attachment {} from {}", attachment.id, resolved_url))?;

    let status = response.status();
    if !status.is_success() {
        let body = response.text().await.unwrap_or_default();
        bail!(
            "attachment_fetch_failed: HTTP {} fetching {} ({})",
            status,
            attachment.id,
            body.chars().take(200).collect::<String>()
        );
    }

    let bytes = response
        .bytes()
        .await
        .with_context(|| format!("reading body for attachment {}", attachment.id))?;
    if bytes.len() > MAX_BLOB_BYTES {
        bail!(
            "attachment_fetch_failed: blob {} is {} bytes, exceeds engine cap of {}",
            attachment.id,
            bytes.len(),
            MAX_BLOB_BYTES
        );
    }

    let actual = format!("{:x}", Sha256::digest(&bytes));
    if !actual.eq_ignore_ascii_case(&attachment.sha256) {
        bail!(
            "attachment_fetch_failed: sha256 mismatch for {} (expected {}, got {})",
            attachment.id,
            attachment.sha256,
            actual
        );
    }

    let dir = session_tmpdir(session_id);
    create_dir_owner_only(&dir)
        .with_context(|| format!("creating attachment tmpdir {}", dir.display()))?;
    let path = dir.join(format!(
        "{}.{}",
        attachment.id,
        extension_for_mime(&attachment.mime_type)
    ));
    write_owner_only(&path, &bytes)
        .with_context(|| format!("writing attachment blob {}", path.display()))?;

    let elapsed_ms = started.elapsed().as_millis();
    eprintln!(
        "[codex-attach] fetched id={} bytes={} elapsed_ms={} path={}",
        attachment.id,
        bytes.len(),
        elapsed_ms,
        path.display()
    );

    Ok(FetchedAttachment {
        id: attachment.id.clone(),
        mime_type: attachment.mime_type.clone(),
        path,
        bytes: bytes.len(),
    })
}

#[cfg(unix)]
fn create_dir_owner_only(dir: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    if dir.exists() {
        return Ok(());
    }
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true).mode(0o700);
    builder.create(dir)?;
    Ok(())
}

#[cfg(not(unix))]
fn create_dir_owner_only(dir: &std::path::Path) -> Result<()> {
    fs::create_dir_all(dir)?;
    Ok(())
}

#[cfg(unix)]
fn write_owner_only(path: &std::path::Path, bytes: &[u8]) -> Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = fs::OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

#[cfg(not(unix))]
fn write_owner_only(path: &std::path::Path, bytes: &[u8]) -> Result<()> {
    fs::write(path, bytes)?;
    Ok(())
}

/// Fetch all attachments concurrently. On any failure, deletes the
/// per-session tmpdir so the bridge never hands Codex a partial set.
pub async fn fetch_all(
    http: &reqwest::Client,
    api_url: &str,
    api_token: &str,
    session_id: &str,
    attachments: &[AttachmentRef],
) -> Result<Vec<FetchedAttachment>> {
    if attachments.is_empty() {
        return Ok(Vec::new());
    }
    let started = Instant::now();
    let mut handles = Vec::with_capacity(attachments.len());
    for attachment in attachments {
        let http = http.clone();
        let api_url = api_url.to_string();
        let token = api_token.to_string();
        let session = session_id.to_string();
        let attachment = attachment.clone();
        handles.push(tokio::spawn(async move {
            fetch_one(&http, &api_url, &token, &session, &attachment).await
        }));
    }
    let mut fetched = Vec::with_capacity(attachments.len());
    let mut first_err: Option<anyhow::Error> = None;
    for handle in handles {
        match handle.await {
            Ok(Ok(item)) => fetched.push(item),
            Ok(Err(err)) => {
                if first_err.is_none() {
                    first_err = Some(err);
                }
            }
            Err(join_err) => {
                if first_err.is_none() {
                    first_err = Some(anyhow!("attachment fetch task panicked: {join_err}"));
                }
            }
        }
    }
    if let Some(err) = first_err {
        cleanup_session_tmpdir(session_id);
        eprintln!(
            "[codex-attach] fetch_all failed session={} count={} elapsed_ms={} error={}",
            session_id,
            attachments.len(),
            started.elapsed().as_millis(),
            err
        );
        return Err(err);
    }
    let total_bytes: usize = fetched.iter().map(|f| f.bytes).sum();
    eprintln!(
        "[codex-attach] fetch_all ok session={} count={} total_bytes={} elapsed_ms={}",
        session_id,
        fetched.len(),
        total_bytes,
        started.elapsed().as_millis()
    );
    Ok(fetched)
}

/// Best-effort wipe of a session's attachment tmpdir. Called on Stop and
/// when fetch_all bails partway through.
pub fn cleanup_session_tmpdir(session_id: &str) {
    let dir = session_tmpdir(session_id);
    if dir.exists() {
        if let Err(err) = fs::remove_dir_all(&dir) {
            eprintln!(
                "[codex-attach] cleanup tmpdir {} failed: {}",
                dir.display(),
                err
            );
        }
    }
}

/// Wipe every per-session attachment tmpdir under `$TMPDIR/lh-attach`.
///
/// Called once at engine startup. A previous engine process may have
/// crashed mid-turn and left blobs on disk; we'd rather drop them than
/// leak them into a fresh session by accident.
pub fn cleanup_orphan_tmpdirs() {
    let root = tmp_root();
    if !root.exists() {
        return;
    }
    match fs::remove_dir_all(&root) {
        Ok(_) => {
            eprintln!("[codex-attach] cleared orphan tmpdir {}", root.display());
        }
        Err(err) => {
            eprintln!(
                "[codex-attach] startup cleanup of {} failed: {}",
                root.display(),
                err
            );
        }
    }
}

/// Build the `input` array for `turn/start` / `turn/steer`. LocalImage
/// items come first, then a Text item — matches Codex's CLI drag-drop
/// ordering. When `text` is empty and there are attachments, we still
/// emit an empty Text element so app-server always has an anchor.
pub fn build_user_input_items(text: &str, fetched: &[FetchedAttachment]) -> Vec<Value> {
    let mut items: Vec<Value> = fetched
        .iter()
        .map(|item| {
            json!({
                "type": "localImage",
                "path": item.path.to_string_lossy(),
            })
        })
        .collect();
    if !text.is_empty() || items.is_empty() {
        items.push(json!({ "type": "text", "text": text }));
    } else {
        items.push(json!({ "type": "text", "text": "" }));
    }
    items
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_attachments_handles_missing_field() {
        let req = json!({"text": "hi"});
        assert!(parse_attachments(&req).unwrap().is_empty());
    }

    #[test]
    fn parse_attachments_handles_null() {
        let req = json!({"attachments": null});
        assert!(parse_attachments(&req).unwrap().is_empty());
    }

    fn sample_id() -> String {
        Uuid::new_v4().to_string()
    }

    fn sample_blob_url(id: &str) -> String {
        format!(
            "/api/agents/sessions/{}/inputs/1/attachments/{}/blob",
            sample_id(),
            id,
        )
    }

    #[test]
    fn parse_attachments_round_trips_one() {
        let id = sample_id();
        let sha = "a".repeat(64);
        let req = json!({
            "attachments": [{
                "id": id,
                "mime_type": "image/png",
                "sha256": sha,
                "blob_url": sample_blob_url(&id),
            }]
        });
        let parsed = parse_attachments(&req).unwrap();
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].id, id);
        assert_eq!(parsed[0].mime_type, "image/png");
    }

    #[test]
    fn parse_attachments_rejects_short_sha() {
        let id = sample_id();
        let req = json!({
            "attachments": [{
                "id": id,
                "mime_type": "image/png",
                "sha256": "short",
                "blob_url": sample_blob_url(&id),
            }]
        });
        assert!(parse_attachments(&req).is_err());
    }

    #[test]
    fn parse_attachments_rejects_non_uuid_id() {
        let sha = "a".repeat(64);
        let req = json!({
            "attachments": [{
                "id": "../etc/passwd",
                "mime_type": "image/png",
                "sha256": sha,
                "blob_url": "/api/agents/sessions/x/inputs/1/attachments/y/blob",
            }]
        });
        assert!(parse_attachments(&req).is_err());
    }

    #[test]
    fn parse_attachments_rejects_absolute_blob_url() {
        let id = sample_id();
        let sha = "a".repeat(64);
        let req = json!({
            "attachments": [{
                "id": id,
                "mime_type": "image/png",
                "sha256": sha,
                "blob_url": "https://attacker.example/api/agents/sessions/x/blob",
            }]
        });
        assert!(parse_attachments(&req).is_err());
    }

    #[test]
    fn parse_attachments_rejects_path_traversal() {
        let id = sample_id();
        let sha = "a".repeat(64);
        let req = json!({
            "attachments": [{
                "id": id,
                "mime_type": "image/png",
                "sha256": sha,
                "blob_url": "/api/agents/../private",
            }]
        });
        assert!(parse_attachments(&req).is_err());
    }

    #[test]
    fn build_user_input_orders_images_before_text() {
        let fetched = vec![
            FetchedAttachment {
                id: "1".into(),
                mime_type: "image/png".into(),
                path: PathBuf::from("/tmp/a.png"),
                bytes: 10,
            },
            FetchedAttachment {
                id: "2".into(),
                mime_type: "image/jpeg".into(),
                path: PathBuf::from("/tmp/b.jpg"),
                bytes: 20,
            },
        ];
        let items = build_user_input_items("look", &fetched);
        assert_eq!(items.len(), 3);
        assert_eq!(items[0]["type"], "localImage");
        assert_eq!(items[0]["path"], "/tmp/a.png");
        assert_eq!(items[1]["type"], "localImage");
        assert_eq!(items[2]["type"], "text");
        assert_eq!(items[2]["text"], "look");
    }

    #[test]
    fn build_user_input_keeps_text_when_no_attachments() {
        let items = build_user_input_items("hello", &[]);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0]["type"], "text");
        assert_eq!(items[0]["text"], "hello");
    }

    #[test]
    fn build_user_input_emits_empty_text_when_only_images() {
        let fetched = vec![FetchedAttachment {
            id: "1".into(),
            mime_type: "image/png".into(),
            path: PathBuf::from("/tmp/a.png"),
            bytes: 10,
        }];
        let items = build_user_input_items("", &fetched);
        assert_eq!(items.len(), 2);
        assert_eq!(items[0]["type"], "localImage");
        assert_eq!(items[1]["type"], "text");
        assert_eq!(items[1]["text"], "");
    }

    #[test]
    fn resolve_blob_url_joins_relative_path() {
        assert_eq!(
            resolve_blob_url("https://api.example", "/api/agents/sessions/s/inputs/1/blob"),
            "https://api.example/api/agents/sessions/s/inputs/1/blob"
        );
        assert_eq!(
            resolve_blob_url("https://api.example/", "/api/agents/x"),
            "https://api.example/api/agents/x"
        );
    }

    #[test]
    fn extension_for_mime_maps_jpeg_to_jpg() {
        assert_eq!(extension_for_mime("image/jpeg"), "jpg");
        assert_eq!(extension_for_mime("image/png"), "png");
        assert_eq!(extension_for_mime("image/webp"), "webp");
        assert_eq!(extension_for_mime("image/gif"), "gif");
        assert_eq!(extension_for_mime("application/octet-stream"), "bin");
    }
}
