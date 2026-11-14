//! OMP-native session roots, identity, and pre-parse ownership fencing.
//!
//! OMP is Pi's sibling, not a Pi installation. Keep its archive layout and
//! opaque native-id rules here so discovery and shipping cannot reuse Pi's
//! provider semantics accidentally.

use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde_json::Value;
use uuid::Uuid;

/// Machine-Agent-only OMP discovery overrides. These are intentionally not
/// Pi's `PI_*` environment contract.
pub const OMP_DATA_DIR_ENV: &str = "LONGHOUSE_OMP_DATA_DIR";
pub const OMP_SESSION_DIR_ENV: &str = "LONGHOUSE_OMP_SESSION_DIR";
pub const OMP_SESSION_DIR_NAME: &str = "sessions";
const OMP_DATA_DIR_NAME: &str = "omp";
pub const OMP_CONFIG_DIR_ENV: &str = "LONGHOUSE_OMP_CONFIG_DIR";
const AUTO_RESUME_OVERLAY_FILE_NAME: &str = "omp-helm-config.yml";
const OMP_PROFILE_ENV: &str = "OMP_PROFILE";
const MAX_HEADER_SCAN_BYTES: u64 = 1024 * 1024;
const MAX_RECORD_BYTES: usize = 8 * 1024 * 1024;
/// Nested task archives are accepted only through this bounded, header-linked
/// artifact chain. It prevents a parked registry tree or an unrelated file
/// below the provider root from becoming a discovered session.
const MAX_NESTED_SESSION_DEPTH: usize = 4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OmpSessionHeader {
    pub native_id: String,
    pub cwd: String,
    pub timestamp: Option<String>,
    pub parent_session: Option<String>,
}

/// Ownership established before the parser is allowed to prepare an envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
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

/// Resolve OMP's version-18.1.18 archive roots. Named profiles are enumerated
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

    // OMP reads XDG_DATA_HOME itself and uses `$XDG_DATA_HOME/omp` only when the
    // variable is set and that directory exists (packages/utils/src/dirs.ts in
    // oh-my-pi). It never falls back to `~/.local/share`, so neither may this:
    // a `~/.local/share/omp` that something else created would otherwise name a
    // root OMP does not write to, and the launch would wait for a transcript
    // that never appears there.
    if let Some(xdg_data_home) = nonempty_env_path("XDG_DATA_HOME") {
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
    if let Some(header) = header_cache::lookup(path, &metadata) {
        return Ok(header);
    }
    let header = read_session_header_uncached(path)?;
    header_cache::store(path, &metadata, &header);
    Ok(header)
}

/// Session headers by file identity. Every transcript write event asks for
/// the header of the file it touched, and the open+read was ~6% of the
/// engine's busy samples with six live sessions in the steady-state soak. An
/// appended session file keeps its header; a file replaced by rename has a new
/// inode, and one rewritten in place shrinks first, so either reads afresh.
mod header_cache {
    use std::collections::HashMap;
    use std::path::{Path, PathBuf};
    use std::sync::Mutex;

    use super::OmpSessionHeader;

    const MAX_ENTRIES: usize = 4096;

    struct Entry {
        device: u64,
        inode: u64,
        len: u64,
        header: OmpSessionHeader,
    }

    static CACHE: Mutex<Option<HashMap<PathBuf, Entry>>> = Mutex::new(None);

    #[cfg(unix)]
    fn identity(metadata: &std::fs::Metadata) -> (u64, u64) {
        use std::os::unix::fs::MetadataExt;
        (metadata.dev(), metadata.ino())
    }

    #[cfg(not(unix))]
    fn identity(_metadata: &std::fs::Metadata) -> (u64, u64) {
        (0, 0)
    }

    pub(super) fn lookup(path: &Path, metadata: &std::fs::Metadata) -> Option<OmpSessionHeader> {
        if !cfg!(unix) {
            return None;
        }
        let (device, inode) = identity(metadata);
        let mut cache = CACHE.lock().ok()?;
        let entry = cache.as_mut()?.get_mut(path)?;
        if entry.device != device || entry.inode != inode || metadata.len() < entry.len {
            return None;
        }
        entry.len = metadata.len();
        Some(entry.header.clone())
    }

    pub(super) fn store(path: &Path, metadata: &std::fs::Metadata, header: &OmpSessionHeader) {
        if !cfg!(unix) {
            return;
        }
        let (device, inode) = identity(metadata);
        let Ok(mut cache) = CACHE.lock() else {
            return;
        };
        let cache = cache.get_or_insert_with(HashMap::new);
        if cache.len() >= MAX_ENTRIES && !cache.contains_key(path) {
            cache.clear();
        }
        cache.insert(
            path.to_path_buf(),
            Entry {
                device,
                inode,
                len: metadata.len(),
                header: header.clone(),
            },
        );
    }
}

/// Header failure that may resolve while OMP finishes flushing its first record.
#[derive(Debug)]
struct HeaderNotMaterialized(String);

impl std::fmt::Display for HeaderNotMaterialized {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for HeaderNotMaterialized {}

pub(crate) fn is_transient_header_error(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause.is::<HeaderNotMaterialized>()
            || cause
                .downcast_ref::<std::io::Error>()
                .is_some_and(|io_err| io_err.kind() == std::io::ErrorKind::NotFound)
    })
}

fn read_session_header_uncached(path: &Path) -> Result<OmpSessionHeader> {
    let file =
        File::open(path).with_context(|| format!("opening OMP source: {}", path.display()))?;
    read_session_header_from(file, path)
}

/// Read the session header from transcript bytes. `label` names the source in
/// errors; it is never opened.
pub fn read_session_header_from(
    source: impl std::io::Read,
    label: &Path,
) -> Result<OmpSessionHeader> {
    let path = label;
    let mut reader = BufReader::new(source);
    let mut line = String::new();
    let mut scanned = 0_u64;
    loop {
        line.clear();
        let bytes = reader.read_line(&mut line)?;
        if bytes == 0 {
            return Err(HeaderNotMaterialized(format!(
                "OMP source has no session header: {}",
                path.display()
            ))
            .into());
        }
        scanned = scanned.saturating_add(bytes as u64);
        if scanned > MAX_HEADER_SCAN_BYTES {
            bail!("OMP session header exceeds scan limit: {}", path.display());
        }
        if line.trim().is_empty() {
            continue;
        }
        let Some(value) = parse_record(&line) else {
            if !line.ends_with('\n') {
                return Err(HeaderNotMaterialized("malformed OMP header".to_string()).into());
            }
            bail!("malformed OMP header");
        };
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
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string),
        });
    }
}

/// Validate a provider-owned nested artifact by walking its bounded chain of
/// parent session files. `parentSession` is an authoritative archive edge,
/// but it is not a running-worker claim: plain forks remain ordinary child
/// archives and parked registry references are never scanned here.
fn nested_session_has_authoritative_parent(
    root: &Path,
    path: &Path,
    header: &OmpSessionHeader,
) -> bool {
    let mut current = path.to_path_buf();
    let mut child_header = header.clone();
    for _ in 0..MAX_NESTED_SESSION_DEPTH {
        let Some(parent_dir) = current.parent() else {
            return false;
        };
        if !parent_dir.starts_with(root) {
            return false;
        }
        let parent_path = parent_dir.with_extension("jsonl");
        if parent_path == current || !parent_path.starts_with(root) {
            return false;
        }
        let Ok(parent_header) = read_session_header(&parent_path) else {
            return false;
        };
        let parent_matches = child_header
            .parent_session
            .as_deref()
            .is_some_and(|declared| {
                declared == parent_header.native_id || Path::new(declared) == parent_path
            });
        if !parent_matches {
            return false;
        }
        // Parent edges terminate only at a native source in the configured
        // root, an encoded-cwd bucket, or a launch-scoped cwd bucket.
        if is_archive_root_source(root, &parent_path) {
            return native_filename_id(&parent_path)
                .is_none_or(|filename_id| filename_id == parent_header.native_id);
        }
        current = parent_path;
        child_header = parent_header;
    }
    false
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

fn is_cwd_bucket(dir: &Path) -> bool {
    native_filename_id(dir).is_none() && !dir.with_extension("jsonl").is_file()
}

fn is_launch_scope_dir(root: &Path, dir: &Path) -> bool {
    dir.parent() == Some(root)
        && dir
            .file_name()
            .and_then(|name| name.to_str())
            .and_then(|name| name.strip_prefix("longhouse-"))
            .is_some_and(|id| Uuid::parse_str(id).is_ok())
}

fn is_archive_root_source(root: &Path, path: &Path) -> bool {
    let parent = path.parent();
    let direct = parent == Some(root);
    let one_level_down = parent.and_then(Path::parent) == Some(root);
    let cwd_bucket = one_level_down && parent.is_some_and(is_cwd_bucket);
    let launch_cwd_bucket = parent
        .and_then(Path::parent)
        .is_some_and(|scope| is_launch_scope_dir(root, scope))
        && parent.is_some_and(is_cwd_bucket);
    direct || cwd_bucket || launch_cwd_bucket
}

/// Whether a file is in the version-bound OMP native session tree. The
/// timestamp/id suffix is authoritative only for that tree; arbitrary exact
/// resume paths are validated by `read_session_header` alone.
pub fn is_session_path(root: &Path, path: &Path) -> bool {
    let parent = path.parent();
    let direct_source = parent == Some(root);
    // One level down is either an encoded-cwd bucket, a launch scope, or an
    // archive artifact directory whose files must prove their parent edge.
    let one_level_down = parent.and_then(Path::parent) == Some(root);
    let bucket_source = one_level_down && parent.is_some_and(is_cwd_bucket);
    let launch_bucket_source = parent
        .and_then(Path::parent)
        .is_some_and(|scope| is_launch_scope_dir(root, scope))
        && parent.is_some_and(is_cwd_bucket);
    if path.extension().and_then(|value| value.to_str()) != Some("jsonl") || !path.starts_with(root)
    {
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
    if !direct_source
        && !bucket_source
        && !launch_bucket_source
        && !nested_session_has_authoritative_parent(root, path, &header)
    {
        return false;
    }
    // OMP accepts arbitrary explicit resume filenames. The timestamp/id suffix
    // is only an additional check for generated direct-child archive names;
    // nested task artifacts are accepted only through their header edge.
    let generated_name = direct_source || one_level_down || launch_bucket_source;
    !generated_name
        || native_filename_id(path).is_none_or(|filename_id| filename_id == header.native_id)
}
/// Whether a provider-reported JSONL path is scoped to this exact OMP
/// directory. It also accepts OMP's single cwd-bucket layer before the file
/// exists, so lazy session creation can be reserved without inventing a header.
pub fn session_file_path_in_session_dir(session_dir: &Path, source: &Path) -> bool {
    SessionSourceLocation::new(source).is_some_and(|location| location.is_in(session_dir))
}

/// A candidate OMP source path, canonicalized once so it can be tested
/// against many launch session directories (one per retained Helm state)
/// without re-resolving the same parent directories for each.
pub struct SessionSourceLocation {
    parent: PathBuf,
    /// The parent's parent, when the parent itself is a bucket directory OMP
    /// made inside the launch's session directory.
    bucket_root: Option<PathBuf>,
    /// Session directories already resolved by this location. Older OMP
    /// launches share one session directory (248 of 274 retained states on
    /// cinder), so one ownership check otherwise resolved the same path for
    /// each of them.
    resolved: std::cell::RefCell<std::collections::HashMap<PathBuf, PathBuf>>,
}

impl SessionSourceLocation {
    pub fn new(source: &Path) -> Option<Self> {
        if !source.is_absolute()
            || source.extension().and_then(|value| value.to_str()) != Some("jsonl")
        {
            return None;
        }
        let raw_parent = source.parent()?;
        let parent = crate::storage_v2_shipper::stable_source_path(raw_parent);
        let bucket_root = raw_parent
            .parent()
            .filter(|_| {
                native_filename_id(raw_parent).is_none()
                    && !raw_parent.with_extension("jsonl").is_file()
            })
            .map(crate::storage_v2_shipper::stable_source_path);
        Some(Self {
            parent,
            bucket_root,
            resolved: Default::default(),
        })
    }

    pub fn is_in(&self, session_dir: &Path) -> bool {
        if !session_dir.is_absolute() {
            return false;
        }
        // Resolving a path keeps its last component unless that component is
        // itself a symlink (or the path has `.`/`..` parts). A session
        // directory whose name matches neither candidate, and that is not a
        // symlink, cannot resolve to either: skip the realpath, which ran
        // for every retained Helm state on every OMP path job. Only a path
        // already in normal form qualifies: `Path::components` hides a
        // trailing `/` or `/.`, and lstat through either follows the link.
        let plain = session_dir.components().all(|component| {
            matches!(
                component,
                std::path::Component::RootDir | std::path::Component::Normal(_)
            )
        }) && session_dir.as_os_str()
            == session_dir.components().collect::<PathBuf>().as_os_str();
        if plain {
            // macOS volumes are usually case-insensitive and realpath reports
            // the stored case, so names compare ignoring ASCII case; anything
            // non-ASCII (normalization forms) always takes the full check.
            let same_name = |left: Option<&std::ffi::OsStr>, right: Option<&std::ffi::OsStr>| match (
                left.and_then(|v| v.to_str()),
                right.and_then(|v| v.to_str()),
            ) {
                (Some(left), Some(right)) if left.is_ascii() && right.is_ascii() => {
                    left.eq_ignore_ascii_case(right)
                }
                _ => true,
            };
            let name = session_dir.file_name();
            let could_match = same_name(name, self.parent.file_name())
                || self
                    .bucket_root
                    .as_deref()
                    .is_some_and(|root| same_name(name, root.file_name()));
            if !could_match
                && !std::fs::symlink_metadata(session_dir)
                    .is_ok_and(|metadata| metadata.file_type().is_symlink())
            {
                return false;
            }
        }
        let mut resolved = self.resolved.borrow_mut();
        let session_dir = resolved
            .entry(session_dir.to_path_buf())
            .or_insert_with(|| crate::storage_v2_shipper::stable_source_path(session_dir));
        &self.parent == session_dir || self.bucket_root.as_ref() == Some(session_dir)
    }
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

/// Whether a launch must pass OMP's `--session-dir` explicitly.
///
/// OMP files a session under its canonical-cwd bucket (`<root>/<encoded-cwd>/
/// <timestamp>_<id>.jsonl`) only while no session directory is supplied; an
/// explicit directory makes it write the transcript flat into that directory,
/// outside its own session picker's current-folder scope. Longhouse therefore
/// leaves the choice to OMP unless Longhouse itself redirects the archive root,
/// which only its own configuration knobs do.
pub fn session_dir_flag_required(configured_session_dir: Option<&Path>) -> bool {
    configured_session_dir.is_some()
        || nonempty_env_path(OMP_SESSION_DIR_ENV).is_some()
        || nonempty_env_path(OMP_DATA_DIR_ENV).is_some()
        || nonempty_env_path(OMP_CONFIG_DIR_ENV).is_some()
}

/// Both interactive Helm launches and headless Console (`--mode rpc`) runs omit
/// `--session-dir` so OMP files the transcript in its canonical-cwd bucket.
/// Without that flag or `--resume`, OMP's `autoResume` setting adopts the cwd's
/// newest transcript instead of creating one. Passing this overlay via
/// `--config` pins `autoResume: false` for the launched process only.
pub const AUTO_RESUME_OVERLAY: &str = "autoResume: false\n";

pub fn write_auto_resume_overlay(dir: &Path) -> Result<PathBuf> {
    fs::create_dir_all(dir)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?;
    }
    let path = dir.join(AUTO_RESUME_OVERLAY_FILE_NAME);
    fs::write(&path, AUTO_RESUME_OVERLAY)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(path)
}

/// Stock OMP relocates `$HOME` launches to the first accessible scratch root
/// (`~/tmp`, `/tmp`, `/var/tmp`, or `os.tmpdir()`) when neither `--allow-home`
/// nor `--cwd` is passed, so its session header records that scratch path while
/// Longhouse launched from `$HOME`.
fn omp_home_relocation_matches(actual_canonical: &Path, expected_canonical: &Path) -> bool {
    let Ok(home_canonical) = home_dir().canonicalize() else {
        return false;
    };
    if expected_canonical != home_canonical {
        return false;
    }
    let candidates = [
        home_dir().join("tmp"),
        PathBuf::from("/tmp"),
        PathBuf::from("/var/tmp"),
        std::env::temp_dir(),
    ];
    candidates
        .iter()
        .filter_map(|candidate| candidate.canonicalize().ok())
        .any(|candidate| candidate == actual_canonical)
}

/// Compare workspace identities after resolving harmless symlink aliases and
/// OMP's own `$HOME` startup relocation.
fn workspace_binding_matches(actual: &str, expected: &str) -> bool {
    if actual == expected {
        return true;
    }
    match (
        Path::new(actual).canonicalize(),
        Path::new(expected).canonicalize(),
    ) {
        (Ok(actual), Ok(expected)) => {
            actual == expected || omp_home_relocation_matches(&actual, &expected)
        }
        _ => false,
    }
}
/// Verify one materialized provider header without walking a live transcript.
/// Fresh OMP sessions report their path before this header is written.
pub fn verify_session_header(
    path: &Path,
    expected_native_id: &str,
    expected_cwd: Option<&str>,
) -> Result<OmpSessionHeader> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("reading OMP native source metadata: {}", path.display()))?;
    anyhow::ensure!(
        metadata.file_type().is_file(),
        "OMP native source is missing, empty, or not a regular file: {}",
        path.display()
    );
    if metadata.len() == 0 {
        return Err(HeaderNotMaterialized(format!(
            "OMP native source is missing, empty, or not a regular file: {}",
            path.display()
        ))
        .into());
    }
    let header = read_session_header(path)?;
    anyhow::ensure!(
        header.native_id == expected_native_id,
        "OMP native id does not match the exact source binding"
    );
    if let Some(expected_cwd) = expected_cwd {
        anyhow::ensure!(
            workspace_binding_matches(&header.cwd, expected_cwd),
            "OMP native workspace does not match the exact source binding"
        );
    }
    Ok(header)
}

/// Validate a retained exact continuation before OMP is allowed to resume it.
pub fn verify_exact_session_file(
    path: &Path,
    expected_native_id: &str,
    expected_cwd: Option<&str>,
) -> Result<OmpSessionHeader> {
    let header = verify_session_header(path, expected_native_id, expected_cwd)?;
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
                object
                    .get("cwd")
                    .and_then(Value::as_str)
                    .is_some_and(|actual| workspace_binding_matches(actual, expected_cwd)),
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

fn bind_discovered_source_with_state(
    conn: &rusqlite::Connection,
    path: &Path,
    claims: &[impl std::borrow::Borrow<crate::turn_claims::TurnClaim>],
    cached_state_ownership: Option<SourceOwnership>,
) -> Result<(SourceOwnership, Option<SourceOwnership>)> {
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
        return Ok((SourceOwnership::Pending, None));
    };
    if let Some((_, _, Some(existing_native_id))) = existing.as_ref() {
        anyhow::ensure!(
            existing_native_id == &header.native_id,
            "OMP source binding native id changed"
        );
    }
    let state_ownership = match cached_state_ownership {
        Some(ownership) => ownership,
        None => crate::managed_omp_helm_scan::source_ownership(&path)?,
    };
    let cached_state_ownership = state_ownership.clone();
    match state_ownership {
        SourceOwnership::Managed(session_id) => {
            if let Some((existing_owner, _, _)) = existing.as_ref() {
                anyhow::ensure!(
                    existing_owner == &session_id,
                    "OMP native source has conflicting managed owners"
                );
            }
            bind_source_for_thread(conn, &path, &session_id, &header.native_id)?;
            return Ok((
                SourceOwnership::Managed(session_id),
                Some(cached_state_ownership),
            ));
        }
        SourceOwnership::Pending if existing.is_none() => {
            return Ok((SourceOwnership::Pending, Some(cached_state_ownership)));
        }
        SourceOwnership::Pending | SourceOwnership::Unclaimed => {}
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
        .map(std::borrow::Borrow::borrow)
        .filter(|claim: &&crate::turn_claims::TurnClaim| claim.provider.eq_ignore_ascii_case("omp"))
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
        let recorded_source = claim
            .source_path
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty());
        let exact_source = recorded_source
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
        } else if recorded_source.is_none() && native_id.is_none() && in_session_dir && active {
            pending = true;
        }
    }

    if pending || (provider_ids.len() > 1 && existing.is_none()) {
        return Ok((SourceOwnership::Pending, Some(cached_state_ownership)));
    }
    if let Some(session_id) = owner {
        bind_source_for_thread(conn, &path, &session_id, &header.native_id)?;
        return Ok((
            SourceOwnership::Managed(session_id),
            Some(cached_state_ownership),
        ));
    }
    Ok((SourceOwnership::Unclaimed, Some(cached_state_ownership)))
}
/// Resolve source and launch ownership once per source identity. Watcher events
/// for managed claims and state invalidate both snapshots without changing the
/// binding checks performed for each envelope.
pub(crate) fn bind_discovered_source(
    conn: &rusqlite::Connection,
    path: &Path,
) -> Result<SourceOwnership> {
    let metadata = fs::symlink_metadata(path)
        .ok()
        .filter(|metadata| metadata.file_type().is_file());
    let generation = discovered_source_cache::generation();
    if let Some(metadata) = metadata.as_ref() {
        if let Some((state_ownership, claims)) =
            discovered_source_cache::lookup(path, metadata, generation)
        {
            if discovered_source_cache::generation() == generation {
                let cached =
                    bind_discovered_source_with_state(conn, path, &claims, Some(state_ownership));
                if discovered_source_cache::generation() == generation {
                    return cached.map(|(ownership, _)| ownership);
                }
            }
        }
    }

    let claims = crate::turn_claims::default_registry()?.list_all_shared()?;
    let (ownership, state_ownership) =
        bind_discovered_source_with_state(conn, path, &claims, None)?;
    if let (Some(metadata), Some(state_ownership)) = (metadata.as_ref(), state_ownership.as_ref()) {
        discovered_source_cache::store(path, metadata, generation, state_ownership, &claims);
    }
    Ok(ownership)
}

/// Invalidate cached ownership for managed-state watcher events that can
/// change it: any claim write, and a state file appearing or disappearing.
/// A live Helm launcher rewrites its existing state file about every 2 s for
/// its heartbeat (cinder, 2026-10-13: 60 rewrites in 30 s with 5 sessions),
/// which invalidated every entry on every heartbeat and left the cache
/// missing at idle. A content change inside a known state file (a new run,
/// a terminal state) is picked up by the cache's 5 s `MAX_AGE`.
pub(crate) fn note_managed_state_changes(paths: &[PathBuf]) {
    static KNOWN_STATE_FILES: std::sync::Mutex<Option<std::collections::HashSet<PathBuf>>> =
        std::sync::Mutex::new(None);
    let claim_dirs = crate::config::get_longhouse_home().ok().map(|home| {
        [
            home.join("managed-local").join("claims"),
            home.join("agent").join("turn-claims"),
        ]
    });
    let Ok(mut known) = KNOWN_STATE_FILES.lock() else {
        discovered_source_cache::invalidate();
        return;
    };
    let known = known.get_or_insert_with(Default::default);
    let mut relevant = false;
    for path in paths {
        if claim_dirs
            .as_ref()
            .is_none_or(|dirs| dirs.iter().any(|dir| path.starts_with(dir)))
        {
            relevant = true;
        } else if path.exists() {
            relevant |= known.insert(path.clone());
        } else {
            relevant |= known.remove(path);
        }
    }
    if relevant {
        discovered_source_cache::invalidate();
    }
}

mod discovered_source_cache {
    use std::collections::HashMap;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Arc, Mutex};

    use super::SourceOwnership;
    use crate::turn_claims::TurnClaim;

    const MAX_ENTRIES: usize = 4096;
    /// Watcher events invalidate the cache, but an event can be dropped
    /// (FSEvents coalesces and drops under load). Binding authority must not
    /// rest on that: an entry is re-derived at least this often, the managed
    /// observation cadence, so a missed claim change costs at most one scan.
    const MAX_AGE: std::time::Duration = std::time::Duration::from_secs(5);

    struct Entry {
        generation: u64,
        stored_at: std::time::Instant,
        device: u64,
        inode: u64,
        len: u64,
        source_ownership: SourceOwnership,
        claims: Arc<Vec<Arc<TurnClaim>>>,
    }

    static GENERATION: AtomicU64 = AtomicU64::new(0);
    static CACHE: Mutex<Option<HashMap<PathBuf, Entry>>> = Mutex::new(None);

    #[cfg(unix)]
    fn identity(metadata: &std::fs::Metadata) -> (u64, u64) {
        use std::os::unix::fs::MetadataExt;
        (metadata.dev(), metadata.ino())
    }

    #[cfg(not(unix))]
    fn identity(_metadata: &std::fs::Metadata) -> (u64, u64) {
        (0, 0)
    }

    pub(super) fn generation() -> u64 {
        GENERATION.load(Ordering::Acquire)
    }

    pub(super) fn invalidate() {
        GENERATION.fetch_add(1, Ordering::AcqRel);
    }

    pub(super) fn lookup(
        path: &Path,
        metadata: &std::fs::Metadata,
        generation: u64,
    ) -> Option<(SourceOwnership, Arc<Vec<Arc<TurnClaim>>>)> {
        if !cfg!(unix) {
            return None;
        }
        let (device, inode) = identity(metadata);
        let mut cache = CACHE.lock().ok()?;
        let entry = cache.as_mut()?.get_mut(path)?;
        if entry.generation != generation
            || entry.stored_at.elapsed() > MAX_AGE
            || entry.device != device
            || entry.inode != inode
            || metadata.len() < entry.len
        {
            return None;
        }
        entry.len = metadata.len();
        Some((entry.source_ownership.clone(), Arc::clone(&entry.claims)))
    }

    pub(super) fn store(
        path: &Path,
        metadata: &std::fs::Metadata,
        generation: u64,
        source_ownership: &SourceOwnership,
        claims: &Arc<Vec<Arc<TurnClaim>>>,
    ) {
        if !cfg!(unix) || generation != GENERATION.load(Ordering::Acquire) {
            return;
        }
        let (device, inode) = identity(metadata);
        let Ok(mut cache) = CACHE.lock() else {
            return;
        };
        if generation != GENERATION.load(Ordering::Acquire) {
            return;
        }
        let cache = cache.get_or_insert_with(HashMap::new);
        if cache.len() >= MAX_ENTRIES && !cache.contains_key(path) {
            cache.clear();
        }
        cache.insert(
            path.to_path_buf(),
            Entry {
                generation,
                stored_at: std::time::Instant::now(),
                device,
                inode,
                len: metadata.len(),
                source_ownership: source_ownership.clone(),
                claims: Arc::clone(claims),
            },
        );
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn managed_state_change_invalidates_cached_ownership_for_same_source() {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("session.jsonl");
            std::fs::write(&path, b"unchanged source").unwrap();
            let metadata = std::fs::symlink_metadata(&path).unwrap();
            let generation_before = generation();
            let ownership =
                SourceOwnership::Managed("11111111-2222-3333-4444-555555555555".to_string());
            let claims = Arc::new(Vec::new());

            store(&path, &metadata, generation_before, &ownership, &claims);
            assert_eq!(
                lookup(&path, &metadata, generation_before).map(|(value, _)| value),
                Some(ownership)
            );

            invalidate();
            assert!(
                lookup(&path, &metadata, generation()).is_none(),
                "cache invalidated"
            );
        }
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    fn session_line(id: &str) -> String {
        format!(
            r#"{{"type":"session","version":3,"id":"{id}","timestamp":"2026-09-09T00:00:00Z","cwd":"/workspace"}}"#
        )
    }

    #[test]
    fn cached_header_follows_replacement_and_in_place_rewrite() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        std::fs::write(&path, format!("{}\n", session_line("first"))).unwrap();
        assert_eq!(read_session_header(&path).unwrap().native_id, "first");

        // Appends keep the header.
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        std::io::Write::write_all(&mut file, b"{\"type\":\"message\"}\n").unwrap();
        drop(file);
        assert_eq!(read_session_header(&path).unwrap().native_id, "first");

        // Replaced by rename: a new inode, so the new header is read.
        let replacement = dir.path().join("replacement.jsonl");
        std::fs::write(&replacement, format!("{}\n", session_line("second"))).unwrap();
        std::fs::rename(&replacement, &path).unwrap();
        assert_eq!(read_session_header(&path).unwrap().native_id, "second");

        // Rewritten in place, shorter: same inode, but it shrank.
        std::fs::write(&path, format!("{}\n", session_line("t"))).unwrap();
        assert_eq!(read_session_header(&path).unwrap().native_id, "t");
    }

    #[test]
    fn session_dir_match_survives_the_name_shortcut() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path().canonicalize().unwrap();
        let real = root.join("longhouse-real");
        std::fs::create_dir_all(&real).unwrap();
        let source = real.join("2026-10-12_native.jsonl");
        let location = SessionSourceLocation::new(&source).unwrap();

        // Same directory under another name through a symlink: must match.
        let alias = root.join("alias-with-another-name");
        std::os::unix::fs::symlink(&real, &alias).unwrap();
        assert!(location.is_in(&alias));
        assert!(location.is_in(&real));
        // A different directory, plain and differently named: no match.
        let other = root.join("longhouse-other");
        std::fs::create_dir_all(&other).unwrap();
        assert!(!location.is_in(&other));
        // Agreement with the unshortcut definition across all of them,
        // including spellings `Path::components` normalizes away.
        let slash = PathBuf::from(format!("{}/", alias.display()));
        let dot = PathBuf::from(format!("{}/.", alias.display()));
        let doubled = PathBuf::from(format!("{}//{}", root.display(), "alias-with-another-name"));
        assert!(location.is_in(&slash));
        assert!(location.is_in(&dot));
        for dir in [&alias, &real, &other, &slash, &dot, &doubled] {
            assert_eq!(
                location.is_in(dir),
                crate::storage_v2_shipper::stable_source_path(dir)
                    == crate::storage_v2_shipper::stable_source_path(&real),
                "{}",
                dir.display()
            );
        }
    }

    #[test]
    fn omp_xdg_roots_include_existing_named_profiles_and_config_roots() {
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
    fn session_dir_flag_is_only_required_for_a_redirected_root() {
        let _guard = crate::console_adapter::agent_state_guard();
        temp_env::with_vars(
            [
                (OMP_SESSION_DIR_ENV, None::<&str>),
                (OMP_DATA_DIR_ENV, None::<&str>),
                (OMP_CONFIG_DIR_ENV, None::<&str>),
            ],
            || {
                assert!(!session_dir_flag_required(None));
                assert!(session_dir_flag_required(Some(Path::new(
                    "/tmp/omp-sessions"
                ))));
            },
        );
    }

    #[test]
    fn native_source_scope_follows_the_session_root() {
        let root = tempfile::tempdir().unwrap();
        let bucket = root.path().join("cwd-bucket");
        fs::create_dir(&bucket).unwrap();
        let source = bucket.join("2026-10-06T10-00-00-000Z_native-id.jsonl");
        fs::write(&source, format!("{}\n", session_line("native-id"))).unwrap();
        let header = verify_session_header(&source, "native-id", Some("/workspace")).unwrap();
        assert_eq!(header.native_id, "native-id");
        assert!(verify_session_header(&source, "other-id", Some("/workspace")).is_err());
        assert!(verify_session_header(&source, "native-id", Some("/elsewhere")).is_err());
        assert!(is_session_path(root.path(), &source));
        assert!(session_file_path_in_session_dir(root.path(), &source));
    }

    #[test]
    fn exact_resume_accepts_symlink_equivalent_workspace() {
        let dir = tempfile::tempdir().unwrap();
        let real_workspace = dir.path().join("real-workspace");
        let alias_workspace = dir.path().join("workspace-alias");
        fs::create_dir(&real_workspace).unwrap();
        std::os::unix::fs::symlink(&real_workspace, &alias_workspace).unwrap();
        let path = dir.path().join("session.jsonl");
        fs::write(
            &path,
            format!(
                "{{\"type\":\"session\",\"version\":3,\"id\":\"native-id\",\"cwd\":\"{}\"}}\n",
                alias_workspace.display()
            ),
        )
        .unwrap();

        assert!(verify_exact_session_file(
            &path,
            "native-id",
            Some(real_workspace.to_str().unwrap())
        )
        .is_ok());
    }

    #[test]
    fn verify_session_header_accepts_omp_home_directory_relocation_only_from_home() {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let fake_home = dir.path().join("home");
        let non_home = dir.path().join("project");
        fs::create_dir(&fake_home).unwrap();
        fs::create_dir(&non_home).unwrap();
        let path = dir.path().join("session.jsonl");
        fs::write(
            &path,
            "{\"type\":\"session\",\"version\":3,\"id\":\"native-id\",\"cwd\":\"/tmp\"}\n",
        )
        .unwrap();

        temp_env::with_var("HOME", Some(&fake_home), || {
            assert!(
                verify_exact_session_file(&path, "native-id", Some(fake_home.to_str().unwrap()))
                    .is_ok(),
                "launching from $HOME must accept OMP's /tmp relocation"
            );
            let err = verify_session_header(&path, "native-id", Some(non_home.to_str().unwrap()))
                .unwrap_err();
            assert!(
                !is_transient_header_error(&err),
                "a workspace mismatch on a complete header is permanent, not transient"
            );
        });
    }

    #[test]
    fn exact_resume_rejects_malformed_or_conflicting_history_after_header() {
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        let xdg_data = home.path().join("xdg-data");
        fs::create_dir_all(&cwd).unwrap();

        let legacy = temp_env::with_vars(
            [
                ("HOME", Some(home.path().to_str().unwrap())),
                (OMP_CONFIG_DIR_ENV, None),
                (OMP_PROFILE_ENV, None),
                (OMP_SESSION_DIR_ENV, None),
            ],
            || {
                temp_env::with_var("XDG_DATA_HOME", Some(xdg_data.to_str().unwrap()), || {
                    session_dir_for_launch(&cwd, None).unwrap()
                })
            },
        );
        assert_eq!(legacy, home.path().join(".omp/agent/sessions"));

        fs::create_dir_all(xdg_data.join("omp")).unwrap();
        let xdg = temp_env::with_vars(
            [
                ("HOME", Some(home.path().to_str().unwrap())),
                (OMP_CONFIG_DIR_ENV, None),
                (OMP_PROFILE_ENV, None),
                (OMP_SESSION_DIR_ENV, None),
            ],
            || {
                temp_env::with_var("XDG_DATA_HOME", Some(xdg_data.to_str().unwrap()), || {
                    session_dir_for_launch(&cwd, None).unwrap()
                })
            },
        );
        assert_eq!(xdg, xdg_data.join("omp/sessions"));
    }

    #[test]
    fn omp_fresh_launch_ignores_a_default_xdg_directory_omp_would_not_use() {
        // Mutates process-global environment: hold the shared agent-state lock.
        let _guard = crate::console_adapter::agent_state_guard();
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        fs::create_dir_all(&cwd).unwrap();
        // Something other than OMP (a provisioning script, another tool) made it.
        fs::create_dir_all(home.path().join(".local/share/omp/sessions")).unwrap();
        let selected = temp_env::with_vars(
            [
                ("HOME", Some(home.path().to_str().unwrap())),
                ("XDG_DATA_HOME", None),
                (OMP_CONFIG_DIR_ENV, None),
                (OMP_DATA_DIR_ENV, None),
                (OMP_PROFILE_ENV, None),
                (OMP_SESSION_DIR_ENV, None),
            ],
            || session_dir_for_launch(&cwd, None).unwrap(),
        );
        assert_eq!(selected, home.path().join(".omp/agent/sessions"));
    }

    #[test]
    fn named_profile_uses_omp_config_root_and_empty_profile_uses_default() {
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        fs::create_dir_all(&cwd).unwrap();
        let selected = temp_env::with_vars(
            [
                ("HOME", Some(home.path().to_str().unwrap())),
                (OMP_CONFIG_DIR_ENV, None),
                (OMP_SESSION_DIR_ENV, None),
                ("XDG_DATA_HOME", None),
            ],
            || {
                temp_env::with_var("PI_PROFILE", Some("work"), || {
                    temp_env::with_var("OMP_PROFILE", Some(""), || {
                        session_dir_for_launch(&cwd, None).unwrap()
                    })
                })
            },
        );
        assert_eq!(selected, home.path().join(".omp/agent/sessions"));
    }

    #[test]
    fn omp_launch_roots_ignore_all_pi_storage_aliases() {
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("workspace");
        fs::create_dir_all(&cwd).unwrap();

        let selected = temp_env::with_vars(
            [
                ("HOME", Some(home.path().to_str().unwrap())),
                (OMP_CONFIG_DIR_ENV, None),
                (OMP_PROFILE_ENV, None),
                (OMP_SESSION_DIR_ENV, None),
                ("XDG_DATA_HOME", None),
            ],
            || {
                temp_env::with_var("PI_CONFIG_DIR", Some("pi-config"), || {
                    temp_env::with_var("PI_CODING_AGENT_DIR", Some("pi-agent"), || {
                        temp_env::with_var(
                            "PI_CODING_AGENT_SESSION_DIR",
                            Some("pi-sessions"),
                            || {
                                temp_env::with_var("PI_PROFILE", Some("pi"), || {
                                    session_dir_for_launch(&cwd, None).unwrap()
                                })
                            },
                        )
                    })
                })
            },
        );

        assert_eq!(selected, home.path().join(".omp/agent/sessions"));
    }

    #[test]
    fn omp_session_storage_rejects_pi_overlap_including_symlink_aliases() {
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
    fn omp_nested_task_sources_require_an_authoritative_parent_header() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let parent_name = "2026-09-30T00-00-00-000Z_native-parent.jsonl";
        let parent = root.join(parent_name);
        let artifact_dir = root.join(parent_name.trim_end_matches(".jsonl"));
        fs::create_dir_all(&artifact_dir).unwrap();
        fs::write(
            &parent,
            include_str!("../tests/fixtures/golden/omp/task-parent.jsonl"),
        )
        .unwrap();
        let child = artifact_dir.join("task-child.jsonl");
        fs::write(
            &child,
            include_str!("../tests/fixtures/golden/omp/task-child.jsonl"),
        )
        .unwrap();
        let fork = artifact_dir.join("fork-child.jsonl");
        fs::write(
            &fork,
            include_str!("../tests/fixtures/golden/omp/fork-child.jsonl"),
        )
        .unwrap();

        assert!(is_session_path(&root, &parent));
        assert!(is_session_path(&root, &child));
        // `parentSession` is an archive edge for both task and plain fork
        // sources. Discovery retains the exact fork transcript but does not
        // claim either source as a running worker.
        assert!(is_session_path(&root, &fork));
        assert_eq!(
            read_session_header(&fork)
                .unwrap()
                .parent_session
                .as_deref(),
            Some("native-parent")
        );
        // Stock OMP stores sessions under an encoded-cwd bucket; the parent
        // file and its artifact directory must retain the same binding rules.
        let bucket = root.join("-tmp-omp-task");
        let bucket_parent = bucket.join(parent_name);
        let bucket_artifact_dir = bucket.join(parent_name.trim_end_matches(".jsonl"));
        fs::create_dir_all(&bucket_artifact_dir).unwrap();
        fs::write(
            &bucket_parent,
            include_str!("../tests/fixtures/golden/omp/task-parent.jsonl"),
        )
        .unwrap();
        let bucket_child = bucket_artifact_dir.join("task-child.jsonl");
        fs::write(
            &bucket_child,
            include_str!("../tests/fixtures/golden/omp/task-child.jsonl"),
        )
        .unwrap();
        assert!(is_session_path(&root, &bucket_parent));
        assert!(is_session_path(&root, &bucket_child));

        let no_edge = artifact_dir.join("no-edge.jsonl");
        fs::write(
            &no_edge,
            "{\"type\":\"session\",\"version\":3,\"id\":\"native-no-edge\",\"cwd\":\"/tmp/omp-task\"}\n",
        )
        .unwrap();
        assert!(!is_session_path(&root, &no_edge));

        let mismatched_dir = root.join("2026-09-30T00-00-01-000Z_other-parent");
        fs::create_dir_all(&mismatched_dir).unwrap();
        fs::write(
            mismatched_dir.with_extension("jsonl"),
            "{\"type\":\"session\",\"version\":3,\"id\":\"native-other-parent\",\"cwd\":\"/tmp/omp-task\"}\n",
        )
        .unwrap();
        let mismatched = mismatched_dir.join("task-child.jsonl");
        fs::write(
            &mismatched,
            include_str!("../tests/fixtures/golden/omp/task-child.jsonl"),
        )
        .unwrap();
        assert!(!is_session_path(&root, &mismatched));
    }

    #[test]
    fn omp_explicitly_named_archives_keep_the_parent_edge_requirement_for_their_artifacts() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let artifact_dir = root.join("explicit-parent");
        fs::create_dir_all(&artifact_dir).unwrap();
        fs::write(
            root.join("explicit-parent.jsonl"),
            include_str!("../tests/fixtures/golden/omp/task-parent.jsonl"),
        )
        .unwrap();
        let child = artifact_dir.join("task-child.jsonl");
        fs::write(
            &child,
            include_str!("../tests/fixtures/golden/omp/task-child.jsonl"),
        )
        .unwrap();
        let no_edge = artifact_dir.join("no-edge.jsonl");
        fs::write(
            &no_edge,
            "{\"type\":\"session\",\"version\":3,\"id\":\"native-no-edge\",\"cwd\":\"/tmp/omp-task\"}\n",
        )
        .unwrap();

        assert!(is_session_path(&root, &root.join("explicit-parent.jsonl")));
        assert!(is_session_path(&root, &child));
        assert!(!is_session_path(&root, &no_edge));
    }
}
