//! Pi-native session roots and exact identity resolution.
//!
//! This module deliberately knows only Pi's session layout. Longhouse claims,
//! source bindings, and process ownership remain in their existing contracts;
//! callers pass an already-verified file here when those contracts provide one.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde_json::Value;
use uuid::Uuid;
use walkdir::WalkDir;

const PI_AGENT_DIR_ENV: &str = "PI_CODING_AGENT_DIR";
const PI_SESSION_DIR_ENV: &str = "PI_CODING_AGENT_SESSION_DIR";
const LEGACY_PI_ROOT: &str = ".longhouse/agent/pi-console";
const MAX_HEADER_SCAN_BYTES: u64 = 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PiSessionTarget {
    pub provider_thread_id: String,
    pub session_dir: PathBuf,
    pub session_file: Option<PathBuf>,
}

/// Ownership established before the parser is allowed to prepare an envelope.
#[derive(Debug, PartialEq, Eq)]
pub enum SourceOwnership {
    Managed(String),
    Pending,
    Unclaimed,
}

#[derive(Debug)]
struct NativeOwnershipEvidence {
    session_id: String,
    provider_thread_id: Option<String>,
    exact_source: bool,
    in_session_dir: bool,
    active: bool,
}

/// Resolve a fresh native target or an exact existing Pi session.
pub fn prepare_session(
    cwd: &Path,
    session_dir: Option<&Path>,
    resume_thread_id: Option<&str>,
    resume_session_file: Option<&Path>,
) -> Result<PiSessionTarget> {
    let resume_id = resume_thread_id
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if resume_session_file.is_some() && resume_id.is_none() {
        bail!("Pi resume requires a provider thread UUID");
    }

    if let Some(thread_id) = resume_id {
        Uuid::parse_str(thread_id)
            .with_context(|| format!("Pi provider thread id is not a UUID: {thread_id}"))?;
        let session_file = match resume_session_file {
            Some(path) => verify_exact_session_file(path, thread_id)?,
            None => find_exact_session_file(cwd, session_dir, thread_id)?,
        };
        let session_dir = session_file
            .parent()
            .context("verified Pi session file has no parent directory")?
            .to_path_buf();
        return Ok(PiSessionTarget {
            provider_thread_id: thread_id.to_string(),
            session_dir,
            session_file: Some(session_file),
        });
    }

    let session_dir = match session_dir {
        Some(path) => resolve_path(path, cwd)?,
        None => default_session_dir(cwd)?,
    };
    Ok(PiSessionTarget {
        provider_thread_id: Uuid::new_v4().to_string(),
        session_dir,
        session_file: None,
    })
}

/// Read and validate the native session header UUID from an existing file.
pub fn read_session_header_id(path: &Path) -> Result<String> {
    let path = require_absolute(path, "Pi session file")?;
    let metadata = std::fs::metadata(&path)
        .with_context(|| format!("reading Pi session file metadata: {}", path.display()))?;
    if !metadata.is_file() {
        bail!("Pi session path is not a regular file: {}", path.display());
    }

    let file = File::open(&path)
        .with_context(|| format!("opening Pi session file: {}", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut line = String::new();
    let mut scanned = 0_u64;
    loop {
        line.clear();
        let bytes = reader.read_line(&mut line)?;
        if bytes == 0 {
            bail!("Pi session has no session header: {}", path.display());
        }
        scanned = scanned.saturating_add(bytes as u64);
        if scanned > MAX_HEADER_SCAN_BYTES {
            bail!("Pi session header exceeds scan limit: {}", path.display());
        }
        if line.trim().is_empty() {
            continue;
        }
        let value: Value = serde_json::from_str(line.trim())
            .with_context(|| format!("parsing Pi session header: {}", path.display()))?;
        if value.get("type").and_then(Value::as_str) != Some("session") {
            bail!(
                "Pi session does not start with a session header: {}",
                path.display()
            );
        }
        let id = value
            .get("id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .context("Pi session header has no id")?;
        Uuid::parse_str(id).with_context(|| format!("Pi session header id is not a UUID: {id}"))?;
        return Ok(id.to_string());
    }
}

fn default_session_dir(cwd: &Path) -> Result<PathBuf> {
    if let Some(path) = nonempty_env_path(PI_SESSION_DIR_ENV) {
        return resolve_path(&path, cwd);
    }
    let agent_dir = agent_dir(cwd)?;
    let global = read_settings(&agent_dir.join("settings.json"))?;
    let project = read_settings(&absolute_path(cwd)?.join(".pi/settings.json"))?;
    if let Some(path) = project
        .get("sessionDir")
        .or_else(|| global.get("sessionDir"))
        .and_then(Value::as_str)
        .filter(|path| !path.is_empty())
    {
        return resolve_path(Path::new(path), cwd);
    }
    Ok(agent_dir.join("sessions").join(encoded_cwd(cwd)))
}

/// Roots Pi may use without recursively searching arbitrary user directories.
/// Explicit custom launch paths are admitted through exact bindings and wakes,
/// not by adding another filesystem crawl root.
pub(crate) fn configured_native_session_roots(cwd: &Path) -> Result<Vec<PathBuf>> {
    let mut roots = Vec::new();
    roots.push(default_session_dir(cwd)?);
    roots.push(agent_dir(cwd)?.join("sessions"));
    if let Some(path) = nonempty_env_path(PI_SESSION_DIR_ENV) {
        roots.push(resolve_path(&path, cwd)?);
    }
    roots.sort_by_key(|path| path.components().count());
    roots.dedup();
    let mut non_overlapping = Vec::new();
    for root in roots {
        if !non_overlapping
            .iter()
            .any(|parent: &PathBuf| root.starts_with(parent))
        {
            non_overlapping.push(root);
        }
    }
    Ok(non_overlapping)
}

fn read_settings(path: &Path) -> Result<Value> {
    match std::fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .with_context(|| format!("reading Pi session directory settings: {}", path.display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Value::Null),
        Err(error) => {
            Err(error).with_context(|| format!("reading Pi settings: {}", path.display()))
        }
    }
}

fn nonempty_env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

fn agent_dir(cwd: &Path) -> Result<PathBuf> {
    match nonempty_env_path(PI_AGENT_DIR_ENV) {
        Some(path) => resolve_path(&path, cwd),
        None => Ok(home_dir()?.join(".pi/agent")),
    }
}

fn encoded_cwd(cwd: &Path) -> String {
    let resolved = absolute_path(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    let text = resolved.to_string_lossy();
    let without_root = text.trim_start_matches(|character| character == '/' || character == '\\');
    format!(
        "--{}--",
        without_root
            .chars()
            .map(|character| match character {
                '/' | '\\' | ':' => '-',
                other => other,
            })
            .collect::<String>()
    )
}

fn find_exact_session_file(
    cwd: &Path,
    explicit_session_dir: Option<&Path>,
    thread_id: &str,
) -> Result<PathBuf> {
    let roots = supported_roots(cwd, explicit_session_dir)?;
    let mut matches = BTreeMap::new();
    for root in roots {
        if !root.is_dir() {
            continue;
        }
        for entry in WalkDir::new(&root)
            .follow_links(false)
            .max_depth(6)
            .into_iter()
            .filter_map(Result::ok)
        {
            let path = entry.path();
            if !entry.file_type().is_file()
                || path.extension().and_then(|value| value.to_str()) != Some("jsonl")
            {
                continue;
            }
            let Ok(canonical) = path.canonicalize() else {
                continue;
            };
            if read_session_header_id(&canonical).ok().as_deref() == Some(thread_id) {
                matches.insert(canonical, ());
            }
        }
    }
    match matches.len() {
        0 => bail!("Pi session history not found for provider thread {thread_id}"),
        1 => Ok(matches.into_keys().next().expect("one Pi session match")),
        count => bail!("Pi provider thread {thread_id} has {count} matching session files"),
    }
}

fn supported_roots(cwd: &Path, explicit_session_dir: Option<&Path>) -> Result<Vec<PathBuf>> {
    let mut roots = Vec::new();
    if let Some(path) = explicit_session_dir {
        roots.push(resolve_path(path, cwd)?);
    }
    roots.extend(configured_native_session_roots(cwd)?);
    roots.push(home_dir()?.join(LEGACY_PI_ROOT));
    roots.sort();
    roots.dedup();
    Ok(roots)
}

/// Bind an exact Pi source to a managed Longhouse session before source data is
/// parsed. The path is normalized even when the native file has not been
/// created yet, and an existing owner for the path can never be replaced by a
/// different managed session.
pub fn bind_source_for_thread(
    conn: &rusqlite::Connection,
    path: &Path,
    session_id: &str,
    provider_thread_id: &str,
) -> Result<()> {
    let path = normalized_source_path(path)?;
    anyhow::ensure!(
        path.file_name().is_some(),
        "Pi source path must name a transcript file"
    );
    let session_id = Uuid::parse_str(session_id)
        .with_context(|| format!("Pi managed session id must be a UUID: {session_id}"))?
        .to_string();
    let provider_thread_id = Uuid::parse_str(provider_thread_id)
        .with_context(|| format!("Pi provider thread id must be a UUID: {provider_thread_id}"))?
        .to_string();
    let path_text = path.to_string_lossy().into_owned();
    if let Some((existing_session_id, provider, _)) = existing_binding(conn, &path_text)? {
        anyhow::ensure!(
            provider.eq_ignore_ascii_case("pi"),
            "Pi source is already owned by provider {provider}"
        );
        let existing_session_id = normalized_uuid(&existing_session_id)
            .context("Pi source has an invalid existing managed owner")?;
        anyhow::ensure!(
            existing_session_id == session_id,
            "Pi source already has another managed owner"
        );
    }
    crate::state::session_binding::SessionBinding::new(conn).bind_for_thread(
        &path_text,
        &session_id,
        "pi",
        Some(&provider_thread_id),
    )
}

/// Resolve exact Pi ownership from durable path bindings, Console turn claims,
/// retained Pi Helm launch state, and the native session header. A source with
/// no complete native identity is held pending so the parser cannot mint a
/// Shadow session during the launch race.
pub fn bind_discovered_source(
    conn: &rusqlite::Connection,
    path: &Path,
    claims: &[crate::turn_claims::TurnClaim],
) -> Result<SourceOwnership> {
    let path = normalized_source_path(path)?;
    let path_text = path.to_string_lossy().into_owned();
    let existing = existing_binding(conn, &path_text)?;
    if let Some((_, provider, _)) = existing.as_ref() {
        anyhow::ensure!(
            provider.eq_ignore_ascii_case("pi"),
            "Pi source is already owned by provider {provider}"
        );
    }
    let header_id = read_session_header_id(&path).ok();
    let mut owner = existing
        .as_ref()
        .map(|(session_id, _, _)| {
            normalized_uuid(session_id).context("Pi source has an invalid existing managed owner")
        })
        .transpose()?;
    let mut provider_ids = Vec::new();
    let mut pending = false;

    for claim in claims
        .iter()
        .filter(|claim| claim.provider.eq_ignore_ascii_case("pi"))
    {
        let Some(session_id) = normalized_uuid(&claim.session_id) else {
            continue;
        };
        let provider_thread_id = claim
            .provider_identity_confirmed
            .then(|| claim.provider_thread_id.as_deref())
            .flatten()
            .and_then(normalized_uuid);
        let exact_source = claim
            .source_path
            .as_deref()
            .and_then(|source| normalized_source_path(Path::new(source)).ok())
            .is_some_and(|source| source == path);
        let in_session_dir = claim
            .result
            .as_ref()
            .and_then(|result| result.get("session_dir"))
            .and_then(serde_json::Value::as_str)
            .and_then(|session_dir| normalized_source_path(Path::new(session_dir)).ok())
            .is_some_and(|session_dir| path.starts_with(session_dir));
        let active = !matches!(claim.state.as_str(), "terminal" | "failed");
        let identity_matches = provider_thread_id
            .as_deref()
            .zip(header_id.as_deref())
            .is_some_and(|(left, right)| left == right);

        if identity_matches {
            record_owner(
                &mut owner,
                &mut provider_ids,
                &session_id,
                provider_thread_id.as_deref(),
            )?;
        } else if exact_source {
            match provider_thread_id.as_deref() {
                Some(provider_thread_id)
                    if header_id
                        .as_deref()
                        .is_none_or(|id| id == provider_thread_id) =>
                {
                    record_owner(
                        &mut owner,
                        &mut provider_ids,
                        &session_id,
                        Some(provider_thread_id),
                    )?;
                }
                Some(_) if active => pending = true,
                None if active => pending = true,
                _ => {}
            }
        } else if header_id.is_none() && in_session_dir && active {
            pending = true;
        }
    }

    for evidence in read_pi_helm_evidence(&path)? {
        let identity_matches = evidence
            .provider_thread_id
            .as_deref()
            .zip(header_id.as_deref())
            .is_some_and(|(left, right)| left == right);
        if identity_matches {
            record_owner(
                &mut owner,
                &mut provider_ids,
                &evidence.session_id,
                evidence.provider_thread_id.as_deref(),
            )?;
        } else if evidence.exact_source {
            match evidence.provider_thread_id.as_deref() {
                Some(provider_thread_id)
                    if header_id
                        .as_deref()
                        .is_none_or(|id| id == provider_thread_id) =>
                {
                    record_owner(
                        &mut owner,
                        &mut provider_ids,
                        &evidence.session_id,
                        Some(provider_thread_id),
                    )?;
                }
                Some(_) if evidence.active => pending = true,
                None if evidence.active => pending = true,
                _ => {}
            }
        } else if header_id.is_none() && evidence.in_session_dir && evidence.active {
            pending = true;
        }
    }

    if let Some(session_id) = owner {
        if header_id.is_none() && provider_ids.len() > 1 && existing.is_none() {
            return Ok(SourceOwnership::Pending);
        }
        if let Some(provider_thread_id) = header_id
            .as_deref()
            .or_else(|| (provider_ids.len() == 1).then(|| provider_ids[0].as_str()))
        {
            bind_source_for_thread(conn, &path, &session_id, provider_thread_id)?;
        }
        return Ok(SourceOwnership::Managed(session_id));
    }
    if pending || header_id.is_none() {
        return Ok(SourceOwnership::Pending);
    }
    Ok(SourceOwnership::Unclaimed)
}

fn normalized_source_path(path: &Path) -> Result<PathBuf> {
    let normalized = crate::storage_v2_shipper::stable_source_path(path);
    anyhow::ensure!(
        normalized.is_absolute(),
        "Pi source path must resolve absolutely"
    );
    Ok(normalized)
}

fn normalized_uuid(value: &str) -> Option<String> {
    Uuid::parse_str(value.trim())
        .ok()
        .map(|value| value.to_string())
}

fn existing_binding(
    conn: &rusqlite::Connection,
    path: &str,
) -> Result<Option<(String, String, Option<String>)>> {
    let result = conn.query_row(
        "SELECT session_id, provider, provider_session_id FROM session_binding WHERE path = ?1",
        [path],
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        },
    );
    match result {
        Ok(binding) => Ok(Some(binding)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn record_owner(
    owner: &mut Option<String>,
    provider_ids: &mut Vec<String>,
    session_id: &str,
    provider_thread_id: Option<&str>,
) -> Result<()> {
    if let Some(existing) = owner.as_deref() {
        anyhow::ensure!(
            existing == session_id,
            "Pi native source has conflicting managed owners"
        );
    } else {
        *owner = Some(session_id.to_string());
    }
    if let Some(provider_thread_id) = provider_thread_id {
        if !provider_ids.iter().any(|value| value == provider_thread_id) {
            provider_ids.push(provider_thread_id.to_string());
        }
    }
    Ok(())
}

fn read_pi_helm_evidence(source_path: &Path) -> Result<Vec<NativeOwnershipEvidence>> {
    let source_path = normalized_source_path(source_path)?;
    let root = crate::config::get_longhouse_home()?.join("managed-local/pi-helm");
    let entries = match std::fs::read_dir(&root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(error).with_context(|| format!("reading Pi Helm state: {}", root.display()))
        }
    };
    let mut evidence = Vec::new();
    for entry in entries {
        let path = entry?.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let bytes = match std::fs::read(&path) {
            Ok(bytes) => bytes,
            Err(error) => {
                tracing::warn!(path = %path.display(), %error, "Skipping unreadable Pi Helm state");
                continue;
            }
        };
        let value: serde_json::Value = match serde_json::from_slice(&bytes) {
            Ok(value) => value,
            Err(error) => {
                tracing::warn!(path = %path.display(), %error, "Skipping invalid Pi Helm state");
                continue;
            }
        };
        if value.get("provider").and_then(serde_json::Value::as_str) != Some("pi") {
            continue;
        }
        let Some(session_id) = value
            .get("session_id")
            .and_then(serde_json::Value::as_str)
            .and_then(normalized_uuid)
        else {
            continue;
        };
        let provider_thread_id = value
            .get("provider_session_id")
            .and_then(serde_json::Value::as_str)
            .and_then(normalized_uuid);
        let session_file = value
            .get("session_file")
            .and_then(serde_json::Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .and_then(|value| normalized_source_path(Path::new(value)).ok());
        let session_dir = value
            .get("session_dir")
            .and_then(serde_json::Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .and_then(|value| normalized_source_path(Path::new(value)).ok());
        let active = value.get("status").and_then(serde_json::Value::as_str) != Some("stopped");
        evidence.push(NativeOwnershipEvidence {
            session_id,
            provider_thread_id,
            exact_source: session_file
                .as_ref()
                .is_some_and(|source| source == &source_path),
            in_session_dir: session_dir
                .as_ref()
                .is_some_and(|session_dir| source_path.starts_with(session_dir)),
            active,
        });
    }
    Ok(evidence)
}

fn verify_exact_session_file(path: &Path, expected_id: &str) -> Result<PathBuf> {
    let path = require_absolute(path, "Pi resume session file")?;
    let canonical = path
        .canonicalize()
        .with_context(|| format!("resolving Pi resume session file: {}", path.display()))?;
    if !canonical.is_file() {
        bail!(
            "Pi resume session is not a regular file: {}",
            canonical.display()
        );
    }
    let actual_id = read_session_header_id(&canonical)?;
    if actual_id != expected_id {
        bail!(
            "Pi resume session header id {actual_id} does not match requested thread {expected_id}"
        );
    }
    Ok(canonical)
}

fn require_absolute(path: &Path, label: &str) -> Result<PathBuf> {
    if !path.is_absolute() {
        bail!("{label} must be absolute: {}", path.display());
    }
    Ok(path.to_path_buf())
}

fn absolute_path(path: &Path) -> Result<PathBuf> {
    resolve_path(path, &std::env::current_dir()?)
}

fn resolve_path(path: &Path, cwd: &Path) -> Result<PathBuf> {
    let expanded = if path == Path::new("~") {
        home_dir()?
    } else if let Ok(rest) = path.strip_prefix("~/") {
        home_dir()?.join(rest)
    } else {
        path.to_path_buf()
    };
    let absolute = if expanded.is_absolute() {
        expanded
    } else if cwd.is_absolute() {
        cwd.join(expanded)
    } else {
        std::env::current_dir()?.join(cwd).join(expanded)
    };
    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            std::path::Component::ParentDir => {
                normalized.pop();
            }
            std::path::Component::CurDir => {}
            other => normalized.push(other.as_os_str()),
        }
    }
    Ok(normalized)
}

fn home_dir() -> Result<PathBuf> {
    nonempty_env_path("HOME").context("HOME is required to resolve Pi native session storage")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn header(id: &str) -> String {
        format!(
            "{{\"type\":\"session\",\"version\":3,\"id\":\"{id}\",\"timestamp\":\"2026-09-07T00:00:00.000Z\",\"cwd\":\"/tmp/project\"}}\n"
        )
    }

    #[test]
    fn fresh_target_uses_upstream_cwd_encoding_and_preallocates_identity() {
        let temp = tempfile::tempdir().unwrap();
        let target =
            prepare_session(Path::new("/tmp/project"), Some(temp.path()), None, None).unwrap();
        assert!(target.session_file.is_none());
        assert_eq!(target.session_dir, temp.path());
        assert_eq!(encoded_cwd(Path::new("/tmp/project")), "--tmp-project--");
        assert!(Uuid::parse_str(&target.provider_thread_id).is_ok());
    }

    #[test]
    fn explicit_resume_rejects_wrong_header_without_fallback() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("session.jsonl");
        fs::write(&path, header("11111111-1111-4111-8111-111111111111")).unwrap();
        let absolute = path.canonicalize().unwrap();
        let result = prepare_session(
            Path::new("/tmp/project"),
            None,
            Some("22222222-2222-4222-8222-222222222222"),
            Some(&absolute),
        );
        assert!(result.is_err());
    }

    #[test]
    fn lookup_rejects_ambiguous_duplicate_thread_identity() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        fs::create_dir_all(&root).unwrap();
        let id = "33333333-3333-4333-8333-333333333333";
        fs::write(root.join("one.jsonl"), header(id)).unwrap();
        fs::write(root.join("two.jsonl"), header(id)).unwrap();
        let result = prepare_session(Path::new("/tmp/project"), Some(&root), Some(id), None);
        assert!(result.is_err());
    }

    #[test]
    fn relative_session_override_is_resolved_in_the_provider_cwd() {
        let root = tempfile::tempdir().unwrap();
        let target = prepare_session(
            root.path(),
            Some(Path::new("history/../sessions")),
            None,
            None,
        )
        .unwrap();
        assert_eq!(target.session_dir, root.path().join("sessions"));
    }
}
