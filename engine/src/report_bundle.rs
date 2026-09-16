//! Fetch and materialize an owner-approved bug report before Console startup.

use anyhow::{bail, Context, Result};
use futures_util::future::try_join_all;
use reqwest::Client;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;
use uuid::Uuid;

const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024;
const MAX_TOTAL_BYTES: u64 = 8 * 1024 * 1024;
const MAX_FILES: usize = 6;
// The command reply has a ten-second server budget. Fetch the manifest and
// all files concurrently, with a bounded per-request timeout, so evidence
// cannot consume that budget through sequential downloads.
const FETCH_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Deserialize)]
struct ReportManifest {
    schema_version: u32,
    report_id: String,
    files: Vec<ReportFile>,
}

#[derive(Debug, Deserialize)]
struct ReportFile {
    name: String,
    mime_type: String,
    byte_size: u64,
    sha256: String,
    kind: String,
}

fn validate_name(name: &str) -> Result<()> {
    if name.is_empty()
        || name == "."
        || name == ".."
        || name.contains('/')
        || name.contains('\\')
        || name
            .bytes()
            .any(|byte| !(byte.is_ascii_alphanumeric() || b"._-".contains(&byte)))
    {
        bail!("report_stage_failed: unsafe report filename")
    }
    Ok(())
}

fn validate_mime_type(mime_type: &str) -> Result<()> {
    if !matches!(
        mime_type,
        "text/markdown"
            | "application/json"
            | "image/png"
            | "image/jpeg"
            | "image/webp"
            | "image/gif"
    ) {
        bail!("report_stage_failed: unsupported report mime type")
    }
    Ok(())
}

fn validate_manifest(manifest: &ReportManifest, report_id: &str) -> Result<()> {
    if manifest.schema_version != 1 || manifest.report_id != report_id {
        bail!("report_stage_failed: unsupported or mismatched report manifest")
    }

    if manifest.files.is_empty() || manifest.files.len() > MAX_FILES {
        bail!("report_stage_failed: invalid report file count")
    }
    let mut total = 0_u64;
    for file in &manifest.files {
        validate_name(&file.name)?;
        if file.byte_size == 0 || file.byte_size > MAX_FILE_BYTES {
            bail!(
                "report_stage_failed: invalid report file size for {}",
                file.name
            )
        }
        if file.sha256.len() != 64 || !file.sha256.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            bail!(
                "report_stage_failed: invalid report file hash for {}",
                file.name
            )
        }
        validate_mime_type(&file.mime_type)?;
        if file.mime_type.is_empty() || file.kind.is_empty() {
            bail!(
                "report_stage_failed: incomplete report file metadata for {}",
                file.name
            )
        }
        total = total.saturating_add(file.byte_size);
        if total > MAX_TOTAL_BYTES {
            bail!("report_stage_failed: report exceeds total byte limit")
        }
    }
    Ok(())
}

fn resolve_url(api_url: &str, report_id: &str, filename: Option<&str>) -> String {
    let base = api_url.trim_end_matches('/');
    match filename {
        Some(name) => format!("{base}/api/agents/reports/{report_id}/files/{name}"),
        None => format!("{base}/api/agents/reports/{report_id}/manifest"),
    }
}

fn write_private(path: &Path, bytes: &[u8]) -> Result<()> {
    let temporary = path.with_extension(format!("tmp-{}", Uuid::new_v4()));
    fs::write(&temporary, bytes).with_context(|| format!("writing {}", temporary.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))?;
    }
    fs::rename(&temporary, path).with_context(|| format!("publishing {}", path.display()))?;
    Ok(())
}

/// Materialize one immutable report into the selected Console workspace.
pub async fn stage_bug_report(
    client: &Client,
    api_url: &str,
    api_token: &str,
    report_id: &str,
    cwd: &Path,
) -> Result<PathBuf> {
    let normalized_report_id = Uuid::parse_str(report_id)
        .map(|value| value.to_string())
        .context("report_stage_failed: invalid report id")?;
    let report_dir = cwd
        .join(".longhouse")
        .join("bug-reports")
        .join(&normalized_report_id);
    if report_dir.is_dir() {
        return Ok(report_dir);
    }

    let response = client
        .get(resolve_url(api_url, &normalized_report_id, None))
        .header("X-Agents-Token", api_token)
        .timeout(FETCH_TIMEOUT)
        .send()
        .await
        .context("report_stage_failed: manifest request")?
        .error_for_status()
        .context("report_stage_failed: manifest response")?;
    let manifest: ReportManifest = response
        .json()
        .await
        .context("report_stage_failed: invalid manifest JSON")?;
    validate_manifest(&manifest, &normalized_report_id)?;

    let temporary_dir = report_dir.with_extension(format!("staging-{}", Uuid::new_v4()));
    fs::create_dir_all(&temporary_dir)
        .with_context(|| format!("creating {}", temporary_dir.display()))?;
    let result = async {
        let manifest_bytes = serde_json::to_vec_pretty(&serde_json::json!({
            "schema_version": manifest.schema_version,
            "report_id": manifest.report_id,
            "files": manifest.files.iter().map(|file| serde_json::json!({
                "name": file.name,
                "mime_type": file.mime_type,
                "byte_size": file.byte_size,
                "sha256": file.sha256,
                "kind": file.kind,
            })).collect::<Vec<_>>(),
        }))?;
        write_private(&temporary_dir.join("manifest.json"), &manifest_bytes)?;

        let file_bytes = try_join_all(manifest.files.iter().map(|file| {
            let report_id = normalized_report_id.clone();
            async move {
                let bytes = client
                    .get(resolve_url(api_url, &report_id, Some(&file.name)))
                    .header("X-Agents-Token", api_token)
                    .timeout(FETCH_TIMEOUT)
                    .send()
                    .await
                    .with_context(|| format!("report_stage_failed: file request {}", file.name))?
                    .error_for_status()
                    .with_context(|| format!("report_stage_failed: file response {}", file.name))?
                    .bytes()
                    .await
                    .with_context(|| format!("report_stage_failed: file body {}", file.name))?;
                if bytes.len() as u64 != file.byte_size {
                    bail!("report_stage_failed: size mismatch for {}", file.name);
                }
                let actual = format!("{:x}", Sha256::digest(&bytes));
                if actual != file.sha256.to_ascii_lowercase() {
                    bail!("report_stage_failed: sha256 mismatch for {}", file.name);
                }
                Ok::<_, anyhow::Error>((file.name.clone(), bytes))
            }
        }))
        .await?;

        for (name, bytes) in file_bytes {
            write_private(&temporary_dir.join(&name), &bytes)?;
        }
        fs::rename(&temporary_dir, &report_dir)
            .with_context(|| format!("publishing {}", report_dir.display()))?;
        Ok::<_, anyhow::Error>(())
    }
    .await;
    if result.is_err() {
        let _ = fs::remove_dir_all(&temporary_dir);
    }
    result?;
    Ok(report_dir)
}

#[cfg(test)]
mod tests {
    use super::{validate_manifest, ReportFile, ReportManifest, MAX_FILE_BYTES};

    #[test]
    fn manifest_rejects_traversal_and_oversize_files() {
        let traversal = ReportManifest {
            schema_version: 1,
            report_id: "report-1".to_string(),
            files: vec![ReportFile {
                name: "../secret".to_string(),
                mime_type: "text/plain".to_string(),
                byte_size: 1,
                sha256: "0".repeat(64),
                kind: "context".to_string(),
            }],
        };
        assert!(validate_manifest(&traversal, "report-1").is_err());

        let oversized = ReportManifest {
            schema_version: 1,
            report_id: "report-1".to_string(),
            files: vec![ReportFile {
                name: "context.json".to_string(),
                mime_type: "application/json".to_string(),
                byte_size: MAX_FILE_BYTES + 1,
                sha256: "0".repeat(64),
                kind: "context".to_string(),
            }],
        };
        assert!(validate_manifest(&oversized, "report-1").is_err());
    }
}
