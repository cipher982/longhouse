//! OMP-native session roots, identity, and pre-parse ownership fencing.
//!
//! OMP is Pi's sibling, not a Pi installation. Keep its archive layout and
//! opaque native-id rules here so discovery and shipping cannot reuse Pi's
//! provider semantics accidentally.

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde_json::Value;
use sha2::Digest;
use uuid::Uuid;

/// Machine-Agent-only OMP discovery overrides. These are intentionally not
/// Pi's `PI_*` environment contract.
pub const OMP_DATA_DIR_ENV: &str = "LONGHOUSE_OMP_DATA_DIR";
pub const OMP_SESSION_DIR_ENV: &str = "LONGHOUSE_OMP_SESSION_DIR";
pub const OMP_SESSION_DIR_NAME: &str = "sessions";
const OMP_DATA_DIR_NAME: &str = "omp";
const OMP_CONFIG_DIR_ENV: &str = "LONGHOUSE_OMP_CONFIG_DIR";
const OMP_PROFILE_ENV: &str = "OMP_PROFILE";
const MAX_HEADER_SCAN_BYTES: u64 = 1024 * 1024;
const OMP_TITLE_SLOT_BYTES: usize = 256;
const MAX_RECORD_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OmpSessionHeader {
    pub native_id: String,
    pub cwd: String,
    pub timestamp: Option<String>,
    pub parent_session: Option<String>,
}

/// Ownership established before the parser is allowed to prepare an envelope.
#[derive(Debug, PartialEq, Eq)]
pub enum SourceOwnership {
    Managed(String),
    Pending,
    Unclaimed,
}

fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp"))
}

fn resolve_path(path: PathBuf, cwd: &Path) -> PathBuf {
    if path.is_absolute() {
        path
    } else {
        cwd.join(path)
    }
}

fn nonempty_env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

pub fn active_profile() -> Option<String> {
    let value = std::env::var_os(OMP_PROFILE_ENV)?;
    let value = value.to_string_lossy();
    safe_profile_name(value.trim()).map(str::to_string)
}

fn discovery_profile() -> Option<String> {
    let value = std::env::var_os(OMP_PROFILE_ENV)?;
    let value = value.to_string_lossy();
    safe_profile_name(value.trim()).map(str::to_string)
}

fn safe_profile_name(profile: &str) -> Option<&str> {
    let path = Path::new(profile);
    (path.components().count() == 1 && !profile.is_empty() && profile != "." && profile != "..")
        .then_some(profile)
}

fn provider_config_root() -> PathBuf {
    home_dir().join(".omp")
}
fn launch_config_root(cwd: &Path) -> PathBuf {
    nonempty_env_path(OMP_CONFIG_DIR_ENV)
        .map(|path| resolve_path(path, cwd))
        .unwrap_or_else(provider_config_root)
}

fn configured_config_roots(cwd: &Path) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(value) = nonempty_env_path(OMP_CONFIG_DIR_ENV) {
        roots.push(resolve_path(value, cwd));
    }
    roots.push(provider_config_root());
    roots.push(home_dir().join(".omp"));
    roots.sort();
    roots.dedup();
    roots
}

fn configured_data_roots(cwd: &Path) -> Vec<PathBuf> {
    let mut roots = Vec::new();

    if let Some(value) = nonempty_env_path(OMP_DATA_DIR_ENV) {
        roots.push(resolve_path(value, cwd));
    }

    // OMP's native data root follows XDG when supplied. Keep the explicit
    // override additive so discovery can see both a qualification sandbox and
    // the provider's ordinary profile roots on every supported host.
    let xdg_data = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|value| !value.as_os_str().is_empty())
        .unwrap_or_else(|| home_dir().join(".local").join("share"));
    let upstream_root = xdg_data.join(OMP_DATA_DIR_NAME);
    if upstream_root.is_dir() {
        roots.push(upstream_root);
    }

    roots.sort();
    roots.dedup();
    roots
}

fn profile_session_roots(data_root: &Path) -> Vec<PathBuf> {
    let mut roots = vec![data_root.join(OMP_SESSION_DIR_NAME)];
    let profiles = data_root.join("profiles");
    let Ok(entries) = fs::read_dir(profiles) else {
        return roots;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
            roots.push(path.join(OMP_SESSION_DIR_NAME));
        }
    }
    roots
}

fn legacy_profile_session_roots(config_root: &Path, profile: Option<&str>) -> Vec<PathBuf> {
    let mut roots = vec![config_root.join("agent").join(OMP_SESSION_DIR_NAME)];
    if let Some(profile) = profile.and_then(safe_profile_name) {
        roots.push(
            config_root
                .join("profiles")
                .join(profile)
                .join("agent")
                .join(OMP_SESSION_DIR_NAME),
        );
    }
    let profiles = config_root.join("profiles");
    if let Ok(entries) = fs::read_dir(profiles) {
        for entry in entries.flatten() {
            if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                let candidate = entry.path().join("agent").join(OMP_SESSION_DIR_NAME);
                if !roots.contains(&candidate) {
                    roots.push(candidate);
                }
            }
        }
    }
    roots
}

/// Resolve OMP's version-18.1.14 archive roots. Named profiles are enumerated
/// from disk and never inferred from Pi's environment contract.
pub fn configured_session_roots(cwd: &Path) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(value) = std::env::var_os(OMP_SESSION_DIR_ENV).filter(|value| !value.is_empty()) {
        roots.push(resolve_path(PathBuf::from(value), cwd));
    }
    let profile = discovery_profile();
    for data_root in configured_data_roots(cwd) {
        roots.extend(profile_session_roots(&data_root));
    }
    for config_root in configured_config_roots(cwd) {
        roots.extend(legacy_profile_session_roots(
            &config_root,
            profile.as_deref(),
        ));
    }
    roots.sort();
    roots.dedup();
    roots
}
pub fn session_dir_for_launch(cwd: &Path, profile: Option<&str>) -> Result<PathBuf> {
    if let Some(value) = nonempty_env_path(OMP_SESSION_DIR_ENV) {
        return Ok(resolve_path(value, cwd));
    }
    let profile = profile
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .or_else(active_profile);

    let explicit_data = nonempty_env_path(OMP_DATA_DIR_ENV).map(|path| resolve_path(path, cwd));
    if let Some(data_root) = explicit_data {
        let candidate = match profile.as_deref().and_then(safe_profile_name) {
            Some(profile) => data_root
                .join("profiles")
                .join(profile)
                .join(OMP_SESSION_DIR_NAME),
            None => data_root.join(OMP_SESSION_DIR_NAME),
        };
        return Ok(candidate);
    }

    let xdg_data_home = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|value| !value.as_os_str().is_empty())
        .unwrap_or_else(|| home_dir().join(".local").join("share"));
    let upstream_root = xdg_data_home.join(OMP_DATA_DIR_NAME);
    let candidate = match profile.as_deref().and_then(safe_profile_name) {
        Some(profile) => upstream_root
            .join("profiles")
            .join(profile)
            .join(OMP_SESSION_DIR_NAME),
        None => upstream_root.join(OMP_SESSION_DIR_NAME),
    };
    if upstream_root.is_dir() {
        return Ok(candidate);
    }

    let config_root = launch_config_root(cwd);
    if let Some(profile) = profile.as_deref().and_then(safe_profile_name) {
        return Ok(config_root
            .join("profiles")
            .join(profile)
            .join("agent")
            .join(OMP_SESSION_DIR_NAME));
    }
    Ok(config_root.join("agent").join(OMP_SESSION_DIR_NAME))
}
fn normalized_overlap_path(path: &Path, cwd: &Path) -> PathBuf {
    let absolute = resolve_path(path.to_path_buf(), cwd);
    let mut existing = absolute.clone();
    let mut missing = Vec::new();
    while !existing.exists() {
        let Some(name) = existing.file_name() else {
            break;
        };
        missing.push(name.to_os_string());
        if !existing.pop() {
            break;
        }
    }
    let mut normalized = existing.canonicalize().unwrap_or(existing);
    for name in missing.iter().rev() {
        normalized.push(name);
    }
    normalized
}

fn paths_overlap(left: &Path, right: &Path) -> bool {
    left == right || left.starts_with(right) || right.starts_with(left)
}

/// Refuse OMP storage that could also be crawled as Pi storage.
///
/// OMP accepts Pi-compatible environment variables in its child process, but
/// Longhouse must never let the two providers claim one archive implicitly.
pub fn ensure_session_dir_is_disjoint_from_pi(cwd: &Path, session_dir: &Path) -> Result<()> {
    let selected = normalized_overlap_path(session_dir, cwd);
    let mut pi_roots = crate::pi_session::configured_native_session_roots(cwd)?;
    pi_roots.push(home_dir().join(".longhouse/agent/pi-console"));
    pi_roots.sort();
    pi_roots.dedup();
    for pi_root in pi_roots {
        let normalized_pi_root = normalized_overlap_path(&pi_root, cwd);
        if paths_overlap(&selected, &normalized_pi_root) {
            bail!(
                "OMP session directory {} overlaps Pi storage {}; choose a non-overlapping --session-dir or LONGHOUSE_OMP_SESSION_DIR",
                selected.display(),
                normalized_pi_root.display()
            );
        }
    }
    Ok(())
}

fn parse_record(line: &str) -> Option<Value> {
    serde_json::from_str(line.trim()).ok()
}

/// Read the native header without applying filename conventions. Explicit
/// `--resume <path>` accepts arbitrary filenames; the native layout predicate
/// applies its timestamp/id suffix check separately during discovery.
pub fn read_session_header(path: &Path) -> Result<OmpSessionHeader> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("reading OMP source metadata: {}", path.display()))?;
    if !metadata.file_type().is_file() {
        bail!("OMP source is not a regular file: {}", path.display());
    }
    let file =
        File::open(path).with_context(|| format!("opening OMP source: {}", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut line = String::new();
    let mut scanned = 0_u64;
    loop {
        line.clear();
        let bytes = reader.read_line(&mut line)?;
        if bytes == 0 {
            bail!("OMP source has no session header: {}", path.display());
        }
        scanned = scanned.saturating_add(bytes as u64);
        if scanned > MAX_HEADER_SCAN_BYTES {
            bail!("OMP session header exceeds scan limit: {}", path.display());
        }
        if line.trim().is_empty() {
            continue;
        }
        let value = parse_record(&line).ok_or_else(|| anyhow::anyhow!("malformed OMP header"))?;
        let kind = value
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if kind == "title" {
            continue;
        }
        if kind != "session" {
            bail!("OMP source does not begin with a session header");
        }
        if let Some(provider) = value
            .get("provider")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|provider| !provider.is_empty())
        {
            anyhow::ensure!(
                matches!(
                    provider.to_ascii_lowercase().as_str(),
                    "omp" | "oh-my-pi" | "@oh-my-pi/pi-coding-agent"
                ),
                "OMP session header contains foreign provider metadata: {provider}"
            );
        }
        let native_id = value
            .get("id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| anyhow::anyhow!("OMP session header has no opaque id"))?
            .to_string();
        let cwd = value
            .get("cwd")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| anyhow::anyhow!("OMP session header has no cwd"))?
            .to_string();
        return Ok(OmpSessionHeader {
            native_id,
            cwd,
            timestamp: value
                .get("timestamp")
                .and_then(Value::as_str)
                .map(str::to_string),
            parent_session: value
                .get("parentSession")
                .and_then(Value::as_str)
                .map(str::to_string),
        });
    }
}

fn native_filename_id(path: &Path) -> Option<&str> {
    let stem = path.file_stem()?.to_str()?;
    let (prefix, id) = stem.rsplit_once('_')?;
    let bytes = prefix.as_bytes();
    let date_shape = bytes.len() >= 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes[..4].iter().all(u8::is_ascii_digit)
        && bytes[5..7].iter().all(u8::is_ascii_digit)
        && bytes[8..10].iter().all(u8::is_ascii_digit);
    date_shape.then_some(id).filter(|id| !id.is_empty())
}

/// Whether a file is in the version-bound OMP native session tree. The
/// timestamp/id suffix is authoritative only for that tree; arbitrary exact
/// resume paths are validated by `read_session_header` alone.
pub fn is_session_path(root: &Path, path: &Path) -> bool {
    let parent = path.parent();
    let in_native_tree = parent == Some(root) || parent.and_then(Path::parent) == Some(root);
    if path.extension().and_then(|value| value.to_str()) != Some("jsonl") || !in_native_tree {
        return false;
    }
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    if !metadata.file_type().is_file() {
        return false;
    }
    let Ok(header) = read_session_header(path) else {
        return false;
    };
    // OMP accepts arbitrary explicit resume filenames. The timestamp/id suffix
    // is only an additional check for its generated archive names, not a
    // requirement for a valid exact source.
    let generated_name = parent.and_then(Path::parent) == Some(root);
    !generated_name
        || native_filename_id(path).is_none_or(|filename_id| filename_id == header.native_id)
}

/// Longhouse identity is derived from OMP's opaque native id only. A native
/// file move therefore keeps the same Longhouse session while its exact source
/// path and source epoch remain independent facts.
pub fn deterministic_session_id(native_id: &str) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("longhouse:omp:{native_id}").as_bytes(),
    )
    .to_string()
}

/// Reserve an exact native path before a managed OMP process is spawned. OMP
/// owns creation of the header; the empty reservation is not a valid resume.
pub fn reserve_session_path(session_dir: &Path) -> Result<PathBuf> {
    if !session_dir.is_absolute() {
        bail!("OMP session directory must be absolute");
    }
    fs::create_dir_all(session_dir)
        .with_context(|| format!("creating OMP session directory: {}", session_dir.display()))?;
    for _ in 0..8 {
        let path = session_dir.join(format!("longhouse-{}.jsonl", Uuid::new_v4()));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => {
                file.sync_all()?;
                return Ok(path);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    bail!("unable to reserve a unique OMP session path")
}

/// Validate an exact continuation before OMP is allowed to apply its
/// create-if-missing `--resume` behavior.
pub fn verify_exact_session_file(
    path: &Path,
    expected_native_id: &str,
    expected_cwd: Option<&str>,
) -> Result<OmpSessionHeader> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("reading OMP resume metadata: {}", path.display()))?;
    if !metadata.file_type().is_file() || metadata.len() == 0 {
        bail!("OMP resume source is missing or empty: {}", path.display());
    }
    let header = read_session_header(path)?;
    anyhow::ensure!(
        header.native_id == expected_native_id,
        "OMP resume native id does not match the exact binding"
    );
    if let Some(expected_cwd) = expected_cwd {
        anyhow::ensure!(
            header.cwd == expected_cwd,
            "OMP resume workspace does not match the exact binding"
        );
    }
    let file = File::open(path)
        .with_context(|| format!("opening OMP resume history: {}", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut line = String::new();
    let mut session_headers = 0_u32;
    loop {
        line.clear();
        let bytes = reader.read_line(&mut line)?;
        if bytes == 0 {
            break;
        }
        anyhow::ensure!(
            line.len() <= MAX_RECORD_BYTES,
            "OMP resume record exceeds the validation limit"
        );
        if line.trim().is_empty() {
            bail!("OMP resume history contains a blank record");
        }
        let value: Value = serde_json::from_str(line.trim()).with_context(|| {
            format!(
                "OMP resume history contains malformed JSON: {}",
                path.display()
            )
        })?;
        let object = value
            .as_object()
            .context("OMP resume history contains a non-object record")?;
        if object.get("type").and_then(Value::as_str) != Some("session") {
            continue;
        }
        session_headers += 1;
        anyhow::ensure!(
            object.get("id").and_then(Value::as_str) == Some(expected_native_id),
            "OMP resume history contains a different native session identity"
        );
        if let Some(expected_cwd) = expected_cwd {
            anyhow::ensure!(
                object.get("cwd").and_then(Value::as_str) == Some(expected_cwd),
                "OMP resume history contains a different workspace identity"
            );
        }
    }
    anyhow::ensure!(
        session_headers == 1,
        "OMP resume history must contain exactly one native session header"
    );
    Ok(header)
}

fn normalized_source_path(path: &Path) -> Result<PathBuf> {
    let normalized = crate::storage_v2_shipper::stable_source_path(path);
    anyhow::ensure!(
        normalized.is_absolute(),
        "OMP source path must resolve absolutely"
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

/// Bind an exact OMP source to a managed Longhouse session. The native id is
/// opaque and is deliberately not parsed or normalized.
pub fn bind_source_for_thread(
    conn: &rusqlite::Connection,
    path: &Path,
    session_id: &str,
    native_id: &str,
) -> Result<()> {
    let path = normalized_source_path(path)?;
    anyhow::ensure!(
        path.file_name().is_some(),
        "OMP source path must name a transcript file"
    );
    let session_id = Uuid::parse_str(session_id)
        .with_context(|| format!("OMP managed session id must be a UUID: {session_id}"))?
        .to_string();
    let native_id = native_id.trim();
    anyhow::ensure!(!native_id.is_empty(), "OMP native id must not be empty");
    let path_text = path.to_string_lossy().into_owned();
    if let Some((existing_session_id, provider, existing_native_id)) =
        existing_binding(conn, &path_text)?
    {
        anyhow::ensure!(
            provider.eq_ignore_ascii_case("omp"),
            "OMP source is already owned by provider {provider}"
        );
        anyhow::ensure!(
            normalized_uuid(&existing_session_id).as_deref() == Some(session_id.as_str()),
            "OMP source already has another managed owner"
        );
        anyhow::ensure!(
            existing_native_id
                .as_deref()
                .is_none_or(|value| value == native_id),
            "OMP source binding native id changed"
        );
    }
    crate::state::session_binding::SessionBinding::new(conn).bind_for_thread(
        &path_text,
        &session_id,
        "omp",
        Some(native_id),
    )
}
/// Reserve an exact OMP source for a managed Helm owner before the provider
/// has materialized its native header. A reservation is not transcript
/// evidence; it only prevents discovery from minting a Shadow session during
/// the launch gap.
pub fn reserve_source_for_thread(
    conn: &rusqlite::Connection,
    path: &Path,
    session_id: &str,
    native_id: Option<&str>,
) -> Result<()> {
    let path = normalized_source_path(path)?;
    anyhow::ensure!(
        path.file_name().is_some(),
        "OMP source path must name a transcript file"
    );
    let session_id = Uuid::parse_str(session_id)
        .with_context(|| format!("OMP managed session id must be a UUID: {session_id}"))?
        .to_string();
    let path_text = path.to_string_lossy().into_owned();
    let effective_native_id = match existing_binding(conn, &path_text)? {
        Some((existing_session_id, provider, existing_native_id)) => {
            anyhow::ensure!(
                provider.eq_ignore_ascii_case("omp"),
                "OMP source is already owned by provider {provider}"
            );
            anyhow::ensure!(
                normalized_uuid(&existing_session_id).as_deref() == Some(session_id.as_str()),
                "OMP source already has another managed owner"
            );
            if let (Some(existing_native_id), Some(native_id)) =
                (existing_native_id.as_deref(), native_id)
            {
                anyhow::ensure!(
                    existing_native_id == native_id.trim(),
                    "OMP source binding native id changed"
                );
            }
            native_id
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .or(existing_native_id.as_deref())
                .map(str::to_owned)
        }
        None => native_id
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_owned),
    };
    crate::state::session_binding::SessionBinding::new(conn).bind_for_thread(
        &path_text,
        &session_id,
        "omp",
        effective_native_id.as_deref(),
    )
}

/// Resolve OMP ownership before parsing. A missing/partial native header is
/// always pending, including when a managed claim has already written early
/// state, so it cannot mint a provisional Shadow identity.
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
            provider.eq_ignore_ascii_case("omp"),
            "OMP source is already owned by provider {provider}"
        );
    }
    let Some(header) = read_session_header(&path).ok() else {
        return Ok(SourceOwnership::Pending);
    };
    if let Some((_, _, Some(existing_native_id))) = existing.as_ref() {
        anyhow::ensure!(
            existing_native_id == &header.native_id,
            "OMP source binding native id changed"
        );
    }

    let mut owner = existing
        .as_ref()
        .map(|(session_id, _, _)| {
            normalized_uuid(session_id).context("OMP source has an invalid managed owner")
        })
        .transpose()?;
    let mut provider_ids = Vec::new();
    let mut pending = false;

    for claim in claims
        .iter()
        .filter(|claim| claim.provider.eq_ignore_ascii_case("omp"))
    {
        let Some(session_id) = normalized_uuid(&claim.session_id) else {
            continue;
        };
        let native_id = claim
            .provider_identity_confirmed
            .then(|| claim.provider_thread_id.as_deref())
            .flatten()
            .map(str::trim)
            .filter(|value| !value.is_empty());
        let exact_source = claim
            .source_path
            .as_deref()
            .and_then(|source| normalized_source_path(Path::new(source)).ok())
            .is_some_and(|source| source == path);
        let in_session_dir = claim
            .result
            .as_ref()
            .and_then(|result| result.get("session_dir"))
            .and_then(Value::as_str)
            .and_then(|session_dir| normalized_source_path(Path::new(session_dir)).ok())
            .is_some_and(|session_dir| path.starts_with(session_dir));
        let active = !matches!(claim.state.as_str(), "terminal" | "failed");
        let identity_matches = native_id.is_some_and(|value| value == header.native_id);

        if identity_matches {
            record_owner(&mut owner, &mut provider_ids, &session_id, native_id)?;
        } else if exact_source {
            match native_id {
                Some(value) if value == header.native_id => {
                    record_owner(&mut owner, &mut provider_ids, &session_id, Some(value))?;
                }
                Some(_) if active => pending = true,
                None if active => pending = true,
                _ => {}
            }
        } else if native_id.is_none() && in_session_dir && active {
            pending = true;
        }
    }

    if let Some(session_id) = owner {
        if provider_ids.len() > 1 && existing.is_none() {
            return Ok(SourceOwnership::Pending);
        }
        bind_source_for_thread(conn, &path, &session_id, &header.native_id)?;
        return Ok(SourceOwnership::Managed(session_id));
    }
    if pending {
        return Ok(SourceOwnership::Pending);
    }
    Ok(SourceOwnership::Unclaimed)
}

fn record_owner(
    owner: &mut Option<String>,
    provider_ids: &mut Vec<String>,
    session_id: &str,
    native_id: Option<&str>,
) -> Result<()> {
    if let Some(existing) = owner.as_deref() {
        anyhow::ensure!(
            existing == session_id,
            "OMP native source has conflicting managed owners"
        );
    } else {
        *owner = Some(session_id.to_string());
    }
    if let Some(native_id) = native_id {
        if !provider_ids.iter().any(|value| value == native_id) {
            provider_ids.push(native_id.to_string());
        }
    }
    Ok(())
}

/// Read only the fixed title slot used for OMP's in-place title rewrite. This
/// keeps source-epoch detection bounded and never scans the transcript body.
pub fn title_slot_revision(path: &Path) -> Result<Option<String>> {
    let mut file =
        File::open(path).with_context(|| format!("reading OMP title slot: {}", path.display()))?;
    let mut bytes = vec![0_u8; OMP_TITLE_SLOT_BYTES];
    let read = file.read(&mut bytes)?;
    if read < OMP_TITLE_SLOT_BYTES || bytes[OMP_TITLE_SLOT_BYTES - 1] != b'\n' {
        return Ok(None);
    }
    let Some(newline) = bytes.iter().position(|byte| *byte == b'\n') else {
        return Ok(None);
    };
    let Ok(slot) = serde_json::from_slice::<Value>(&bytes[..newline]) else {
        return Ok(None);
    };
    if slot.get("type").and_then(Value::as_str) != Some("title") {
        return Ok(None);
    }
    Ok(Some(format!("{:x}", sha2::Sha256::digest(&bytes))))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session_line(id: &str) -> String {
        format!(
            r#"{{"type":"session","version":3,"id":"{id}","timestamp":"2026-09-09T00:00:00Z","cwd":"/workspace"}}"#
        )
    }

    #[test]
    fn omp_xdg_roots_include_existing_named_profiles_and_config_roots() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        let xdg_data = home.path().join("xdg-data");
        let omp_config = home.path().join("omp-config");
        fs::create_dir_all(xdg_data.join("omp/sessions")).unwrap();
        fs::create_dir_all(xdg_data.join("omp/profiles/work/sessions")).unwrap();
        fs::create_dir_all(xdg_data.join("omp/profiles/archive/sessions")).unwrap();
        fs::create_dir_all(&cwd).unwrap();

        let roots = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("XDG_DATA_HOME", Some(xdg_data.to_str().unwrap()), || {
                temp_env::with_var(
                    OMP_CONFIG_DIR_ENV,
                    Some(omp_config.to_str().unwrap()),
                    || {
                        temp_env::with_var("PI_CONFIG_DIR", Some("/pi/config"), || {
                            temp_env::with_var("PI_CODING_AGENT_DIR", Some("/pi/agent"), || {
                                temp_env::with_var("PI_PROFILE", Some("pi"), || {
                                    temp_env::with_var("OMP_PROFILE", Some("work"), || {
                                        temp_env::with_var(OMP_DATA_DIR_ENV, None::<&str>, || {
                                            temp_env::with_var(
                                                OMP_SESSION_DIR_ENV,
                                                None::<&str>,
                                                || configured_session_roots(&cwd),
                                            )
                                        })
                                    })
                                })
                            })
                        })
                    },
                )
            })
        });

        assert!(roots.contains(&xdg_data.join("omp/sessions")));
        assert!(roots.contains(&xdg_data.join("omp/profiles/work/sessions")));
        assert!(roots.contains(&xdg_data.join("omp/profiles/archive/sessions")));
        assert!(roots.contains(&home.path().join(".omp/agent/sessions")));
        assert!(roots.contains(&omp_config.join("profiles/work/agent/sessions")));
        assert!(roots.contains(&home.path().join(".omp/profiles/work/agent/sessions")));
        assert!(!roots.iter().any(|root| root.starts_with("/pi")));
    }

    #[test]
    fn legacy_profile_roots_are_discovered_without_an_active_profile() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        let omp_config = home.path().join("omp-config");
        fs::create_dir_all(&cwd).unwrap();
        fs::create_dir_all(omp_config.join("profiles/archive/agent/sessions")).unwrap();
        fs::create_dir_all(home.path().join(".omp/profiles/default/agent/sessions")).unwrap();

        let roots = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("XDG_DATA_HOME", None::<&str>, || {
                temp_env::with_var(
                    OMP_CONFIG_DIR_ENV,
                    Some(omp_config.to_str().unwrap()),
                    || {
                        temp_env::with_var("OMP_PROFILE", None::<&str>, || {
                            configured_session_roots(&cwd)
                        })
                    },
                )
            })
        });

        assert!(roots.contains(&omp_config.join("profiles/archive/agent/sessions")));
        assert!(roots.contains(&home.path().join(".omp/profiles/default/agent/sessions")));
    }

    #[test]
    fn opaque_identity_survives_native_file_move_and_duplicate_ids_are_not_merged() {
        let dir = tempfile::tempdir().unwrap();
        let first = dir.path().join("first.jsonl");
        let second = dir.path().join("second.jsonl");
        fs::write(&first, format!("{}\n", session_line("same-native-id"))).unwrap();
        fs::rename(&first, &second).unwrap();
        assert_eq!(
            deterministic_session_id("same-native-id"),
            deterministic_session_id("same-native-id")
        );
        assert_ne!(first, second);
        assert_eq!(
            read_session_header(&second).unwrap().native_id,
            "same-native-id"
        );
    }

    #[test]
    fn omp_header_rejects_explicit_foreign_provider_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("foreign.jsonl");
        fs::write(
            &path,
            "{\"type\":\"session\",\"version\":3,\"id\":\"pi-id\",\"cwd\":\"/tmp/pi\",\"provider\":\"pi\"}\n",
        )
        .unwrap();
        let error = read_session_header(&path).unwrap_err().to_string();
        assert!(error.contains("foreign provider metadata"));

        fs::write(
            &path,
            "{\"type\":\"session\",\"version\":3,\"id\":\"omp-id\",\"cwd\":\"/tmp/omp\",\"provider\":\"omp\"}\n",
        )
        .unwrap();
        assert_eq!(read_session_header(&path).unwrap().native_id, "omp-id");
    }

    #[test]
    fn reserved_path_is_not_a_valid_resume_until_omp_writes_a_header() {
        let dir = tempfile::tempdir().unwrap();
        let path = reserve_session_path(dir.path()).unwrap();
        assert!(verify_exact_session_file(&path, "native-id", None).is_err());
        fs::write(&path, format!("{}\n", session_line("native-id"))).unwrap();
        assert_eq!(
            verify_exact_session_file(&path, "native-id", Some("/workspace"))
                .unwrap()
                .native_id,
            "native-id"
        );
        assert!(
            verify_exact_session_file(&dir.path().join("missing.jsonl"), "native-id", None)
                .is_err()
        );
        fs::write(dir.path().join("corrupt.jsonl"), b"not-json\n").unwrap();
        assert!(
            verify_exact_session_file(&dir.path().join("corrupt.jsonl"), "native-id", None)
                .is_err()
        );
    }

    #[test]
    fn exact_resume_rejects_malformed_or_conflicting_history_after_header() {
        let dir = tempfile::tempdir().unwrap();
        let malformed = dir.path().join("malformed.jsonl");
        fs::write(
            &malformed,
            format!(
                "{}\n{{\"type\":\"message\",\"message\":",
                session_line("native-id")
            ),
        )
        .unwrap();
        assert!(verify_exact_session_file(&malformed, "native-id", Some("/workspace")).is_err());

        let conflicting = dir.path().join("conflicting.jsonl");
        fs::write(
            &conflicting,
            format!(
                "{}\n{}\n",
                session_line("native-id"),
                session_line("other-native-id")
            ),
        )
        .unwrap();
        assert!(verify_exact_session_file(&conflicting, "native-id", Some("/workspace")).is_err());
    }

    #[test]
    fn omp_config_roots_ignore_pi_environment_aliases() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        let omp_config = home.path().join("omp-config");
        fs::create_dir_all(&cwd).unwrap();
        fs::create_dir_all(omp_config.join("profiles/work/agent/sessions")).unwrap();

        let roots = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("XDG_DATA_HOME", None::<&str>, || {
                temp_env::with_var(
                    OMP_CONFIG_DIR_ENV,
                    Some(omp_config.to_str().unwrap()),
                    || {
                        temp_env::with_var("PI_CONFIG_DIR", Some("/pi/config"), || {
                            temp_env::with_var("PI_CODING_AGENT_DIR", Some("pi-agent"), || {
                                temp_env::with_var(
                                    "PI_CODING_AGENT_SESSION_DIR",
                                    Some("pi-sessions"),
                                    || {
                                        temp_env::with_var("OMP_PROFILE", Some("work"), || {
                                            temp_env::with_var("PI_PROFILE", Some("work"), || {
                                                configured_session_roots(&cwd)
                                            })
                                        })
                                    },
                                )
                            })
                        })
                    },
                )
            })
        });

        assert!(roots.contains(&omp_config.join("profiles/work/agent/sessions")));
        assert!(!roots.iter().any(|root| root.starts_with("/pi")));
        assert!(!roots.iter().any(|root| root.ends_with("pi-agent/sessions")));
        assert!(!roots.iter().any(|root| root.ends_with("pi-sessions")));
        assert_eq!(
            roots.len(),
            roots
                .iter()
                .collect::<std::collections::BTreeSet<_>>()
                .len()
        );
    }

    #[test]
    fn omp_fresh_launch_prefers_active_xdg_or_legacy_root_without_pi_aliases() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        let xdg_data = home.path().join("xdg-data");
        fs::create_dir_all(&cwd).unwrap();

        let legacy = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("XDG_DATA_HOME", Some(xdg_data.to_str().unwrap()), || {
                session_dir_for_launch(&cwd, None).unwrap()
            })
        });
        assert_eq!(legacy, home.path().join(".omp/agent/sessions"));

        fs::create_dir_all(xdg_data.join("omp")).unwrap();
        let xdg = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("XDG_DATA_HOME", Some(xdg_data.to_str().unwrap()), || {
                session_dir_for_launch(&cwd, None).unwrap()
            })
        });
        assert_eq!(xdg, xdg_data.join("omp/sessions"));
    }

    #[test]
    fn named_profile_uses_omp_config_root_and_empty_profile_uses_default() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        let xdg_data = home.path().join("xdg-data");
        fs::create_dir_all(&cwd).unwrap();
        let profile = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var(OMP_CONFIG_DIR_ENV, Some("omp-config"), || {
                temp_env::with_var("OMP_PROFILE", Some("work"), || {
                    session_dir_for_launch(&cwd, None).unwrap()
                })
            })
        });
        assert_eq!(profile, cwd.join("omp-config/profiles/work/agent/sessions"));

        let default_profile =
            temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
                temp_env::with_var(OMP_CONFIG_DIR_ENV, Some("omp-config"), || {
                    temp_env::with_var("OMP_PROFILE", Some(""), || {
                        session_dir_for_launch(&cwd, None).unwrap()
                    })
                })
            });
        assert_eq!(default_profile, cwd.join("omp-config/agent/sessions"));

        fs::create_dir_all(xdg_data.join("omp")).unwrap();
        let xdg_profile = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("XDG_DATA_HOME", Some(xdg_data.to_str().unwrap()), || {
                temp_env::with_var(OMP_CONFIG_DIR_ENV, Some("omp-config"), || {
                    temp_env::with_var("OMP_PROFILE", Some("work"), || {
                        session_dir_for_launch(&cwd, None).unwrap()
                    })
                })
            })
        });
        assert_eq!(xdg_profile, xdg_data.join("omp/profiles/work/sessions"));
    }

    #[test]
    fn session_dir_override_ignores_profile() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        fs::create_dir_all(&cwd).unwrap();

        let selected = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var(OMP_SESSION_DIR_ENV, Some("omp-agent"), || {
                temp_env::with_var("OMP_PROFILE", Some("work"), || {
                    session_dir_for_launch(&cwd, None).unwrap()
                })
            })
        });
        assert_eq!(selected, cwd.join("omp-agent"));
    }

    #[test]
    fn empty_omp_profile_does_not_inherit_pi_profile() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        fs::create_dir_all(&cwd).unwrap();
        let selected = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("PI_PROFILE", Some("work"), || {
                temp_env::with_var("OMP_PROFILE", Some(""), || {
                    session_dir_for_launch(&cwd, None).unwrap()
                })
            })
        });
        assert_eq!(selected, home.path().join(".omp/agent/sessions"));
    }

    #[test]
    fn omp_launch_roots_ignore_all_pi_storage_aliases() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        fs::create_dir_all(&cwd).unwrap();

        let selected = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("PI_CONFIG_DIR", Some("pi-config"), || {
                temp_env::with_var("PI_CODING_AGENT_DIR", Some("pi-agent"), || {
                    temp_env::with_var("PI_CODING_AGENT_SESSION_DIR", Some("pi-sessions"), || {
                        temp_env::with_var("PI_PROFILE", Some("pi"), || {
                            session_dir_for_launch(&cwd, None).unwrap()
                        })
                    })
                })
            })
        });

        assert_eq!(selected, home.path().join(".omp/agent/sessions"));
    }

    #[test]
    fn omp_session_storage_rejects_pi_overlap_including_symlink_aliases() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        fs::create_dir_all(&cwd).unwrap();
        let pi_agent = cwd.join("pi-agent");
        let pi_sessions = pi_agent.join("sessions");
        fs::create_dir_all(&pi_sessions).unwrap();
        let alias = cwd.join("pi-alias");
        std::os::unix::fs::symlink(&pi_agent, &alias).unwrap();

        let results = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("PI_CODING_AGENT_DIR", Some("pi-agent"), || {
                (
                    ensure_session_dir_is_disjoint_from_pi(&cwd, &pi_sessions),
                    ensure_session_dir_is_disjoint_from_pi(&cwd, &pi_sessions.join("nested")),
                    ensure_session_dir_is_disjoint_from_pi(&cwd, &cwd.join("omp-agent/sessions")),
                    ensure_session_dir_is_disjoint_from_pi(&cwd, &alias.join("sessions")),
                )
            })
        });

        assert!(results.0.is_err());
        assert!(results.1.is_err());
        assert!(results.2.is_ok());
        assert!(results.3.is_err());
    }

    #[test]
    fn omp_discovery_keeps_overlapping_roots_for_shared_ambiguity_policy() {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        fs::create_dir_all(&cwd).unwrap();
        let pi_sessions = cwd.join("pi-agent/sessions");

        let roots = temp_env::with_var("HOME", Some(home.path().to_str().unwrap()), || {
            temp_env::with_var("PI_CODING_AGENT_DIR", Some("pi-agent"), || {
                temp_env::with_var(OMP_SESSION_DIR_ENV, Some("pi-agent/sessions"), || {
                    configured_session_roots(&cwd)
                })
            })
        });

        assert!(roots.iter().any(|root| root == &pi_sessions));
    }

    #[test]
    fn title_slot_revision_reads_only_the_fixed_native_slot() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        let mut title = br#"{"type":"title","v":1,"title":"before"}"#.to_vec();
        title.resize(OMP_TITLE_SLOT_BYTES - 1, b' ');
        title.push(b'\n');
        title.extend_from_slice(b"body that must not affect the title revision\n");
        fs::write(&path, &title).unwrap();
        let before = title_slot_revision(&path).unwrap().unwrap();
        title[35] = b'a';
        fs::write(&path, &title).unwrap();
        assert_ne!(before, title_slot_revision(&path).unwrap().unwrap());
    }
}
