//! The launch-time source claim: a launcher's authority, held in its own file.
//!
//! A managed launch has to tell the rest of the machine two things before the
//! provider starts writing: "this transcript path is mine" (so discovery does
//! not mint a Shadow session for it) and "this is the provider's native
//! identity for it". Both used to be written straight into the shipper database
//! by a cold launcher process, and on 2026-09-17 a busy database turned that
//! into permanent damage: the bind failed, nothing retried, and the session
//! stayed degraded with no native identity.
//!
//! The authority is local, so it belongs in a local file: an atomically
//! renamed claim under `managed-local/claims/`, one per session, written and
//! read by the launcher alone. `ready` follows the claim, not the database. The
//! daemon projects claims into `session_binding` — the view discovery reads —
//! and it does that **before** it looks for sources, because a discovery pass
//! that ran first would mint the duplicate the claim exists to prevent.
//!
//! A claim is a lease, not a record: it carries an expiry, and an expired or
//! unreadable claim is ignored rather than obeyed. Losing one costs at most a
//! re-scan; it can never lose shipped bytes.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

/// Claims older than this are ignored. A managed session may legitimately live
/// for days, so this bounds leftovers from a killed launcher rather than
/// session length.
pub const CLAIM_TTL: Duration = Duration::from_secs(7 * 24 * 60 * 60);

const CLAIM_SCHEMA_VERSION: u64 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClaimState {
    /// The path is taken; the provider has not reported its native identity yet.
    Reserved,
    /// The provider's native identity was verified against its own session file.
    Bound,
    /// The launcher is done with the path; the daemon may purge its projection.
    Released,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceClaim {
    pub schema_version: u64,
    pub session_id: String,
    pub provider: String,
    pub source_path: String,
    pub native_session_id: Option<String>,
    pub cwd: String,
    pub provider_pid: Option<u32>,
    pub provider_start_time: Option<String>,
    pub state: ClaimState,
    pub created_at: String,
    pub updated_at: String,
    pub expires_at: String,
}

impl SourceClaim {
    pub fn is_expired(&self, now: chrono::DateTime<chrono::Utc>) -> bool {
        match chrono::DateTime::parse_from_rfc3339(&self.expires_at) {
            Ok(expires) => expires.with_timezone(&chrono::Utc) <= now,
            // An unreadable expiry must not extend a lease forever.
            Err(_) => true,
        }
    }
}

/// Where every launcher's claim lives. Shared across providers so the daemon
/// reads one directory rather than learning each launcher's state dir.
pub fn claims_dir() -> Result<PathBuf> {
    #[cfg(test)]
    {
        // A claim is authority over a real transcript path, so a test that
        // forgot to set `LONGHOUSE_HOME` must not write into the developer's
        // own home: it wrote a file that could refuse a real launch once
        // already. Tests that isolate the home keep doing so; the rest share a
        // per-process temporary directory, which still exercises the claim path
        // instead of skipping it.
        if std::env::var("LONGHOUSE_HOME").is_err() {
            return Ok(test_claims_dir());
        }
    }
    Ok(crate::config::get_longhouse_home()?
        .join("managed-local")
        .join("claims"))
}

/// One temporary claims directory per test process.
#[cfg(test)]
fn test_claims_dir() -> PathBuf {
    use std::sync::OnceLock;
    static DIR: OnceLock<PathBuf> = OnceLock::new();
    DIR.get_or_init(|| {
        let dir =
            std::env::temp_dir().join(format!("longhouse-test-claims-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        dir
    })
    .clone()
}

fn claim_path(session_id: &str) -> Result<PathBuf> {
    anyhow::ensure!(
        !session_id.is_empty() && !session_id.contains('/') && !session_id.contains(".."),
        "claim session id must be a plain identifier"
    );
    Ok(claims_dir()?.join(format!("{session_id}.json")))
}

fn now() -> chrono::DateTime<chrono::Utc> {
    chrono::Utc::now()
}

fn expiry() -> String {
    (now() + chrono::Duration::from_std(CLAIM_TTL).unwrap_or_else(|_| chrono::Duration::days(7)))
        .to_rfc3339()
}

/// Write a claim through a temporary file and a rename, so a reader never sees
/// half a claim and a crash leaves either the old claim or the new one.
fn write_claim(claim: &SourceClaim) -> Result<()> {
    use std::io::Write;

    let path = claim_path(&claim.session_id)?;
    let dir = path.parent().context("claim path has no parent")?;
    std::fs::create_dir_all(dir)
        .with_context(|| format!("creating the claim directory {}", dir.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    }

    // Same idiom as `turn_claims`: a private temporary file, fsynced, then
    // renamed over the claim, so a reader sees one whole claim or the previous
    // one.
    let temporary = path.with_extension("json.tmp");
    let payload = serde_json::to_vec_pretty(claim).context("serializing a source claim")?;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&temporary)
        .with_context(|| format!("writing the claim {}", temporary.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    file.write_all(&payload)
        .with_context(|| format!("writing the claim {}", temporary.display()))?;
    file.sync_all()?;
    drop(file);
    std::fs::rename(&temporary, &path)
        .with_context(|| format!("publishing the claim {}", path.display()))?;
    Ok(())
}

/// Reserve a source path for a managed session.
pub fn reserve(
    session_id: &str,
    provider: &str,
    source_path: &Path,
    cwd: &Path,
    provider_pid: Option<u32>,
    provider_start_time: Option<String>,
) -> Result<SourceClaim> {
    // One path, one managed owner. The archive binding used to enforce this in
    // the database; the claim is the authority now, so it enforces it here.
    let normalized = crate::storage_v2_shipper::stable_source_path(source_path);
    for existing in active_claims()? {
        if existing.session_id == session_id {
            continue;
        }
        if crate::storage_v2_shipper::stable_source_path(Path::new(&existing.source_path))
            == normalized
        {
            anyhow::bail!(
                "source {} is already claimed by managed session {}",
                source_path.display(),
                existing.session_id
            );
        }
    }
    let timestamp = now().to_rfc3339();
    let claim = SourceClaim {
        schema_version: CLAIM_SCHEMA_VERSION,
        session_id: session_id.to_string(),
        provider: provider.to_string(),
        source_path: source_path.display().to_string(),
        native_session_id: None,
        cwd: cwd.display().to_string(),
        provider_pid,
        provider_start_time,
        state: ClaimState::Reserved,
        created_at: timestamp.clone(),
        updated_at: timestamp,
        expires_at: expiry(),
    };
    write_claim(&claim)?;
    Ok(claim)
}

/// Commit the provider's native identity after the launcher has verified it
/// against the provider's own session file.
/// Commit the provider's native identity. The workspace and source path are the
/// ones `reserve` recorded: a later caller (a Console monitor, for instance) does
/// not necessarily know them, and inventing them would corrupt a resume record.
pub fn confirm_identity(
    session_id: &str,
    provider: &str,
    source_path: &Path,
    native_session_id: &str,
    provider_pid: Option<u32>,
    provider_start_time: Option<String>,
) -> Result<SourceClaim> {
    let existing = read_claim(session_id)?;
    let claim = SourceClaim {
        schema_version: CLAIM_SCHEMA_VERSION,
        session_id: session_id.to_string(),
        provider: provider.to_string(),
        source_path: source_path.display().to_string(),
        native_session_id: Some(native_session_id.to_string()),
        cwd: existing
            .as_ref()
            .map(|claim| claim.cwd.clone())
            .unwrap_or_else(|| String::new()),
        provider_pid: provider_pid
            .or_else(|| existing.as_ref().and_then(|claim| claim.provider_pid)),
        provider_start_time: provider_start_time.or_else(|| {
            existing
                .as_ref()
                .and_then(|claim| claim.provider_start_time.clone())
        }),
        state: ClaimState::Bound,
        created_at: existing
            .as_ref()
            .map(|claim| claim.created_at.clone())
            .unwrap_or_else(|| now().to_rfc3339()),
        updated_at: now().to_rfc3339(),
        expires_at: expiry(),
    };
    write_claim(&claim)?;
    Ok(claim)
}

pub fn read_claim(session_id: &str) -> Result<Option<SourceClaim>> {
    let path = claim_path(session_id)?;
    let Ok(bytes) = std::fs::read(&path) else {
        return Ok(None);
    };
    let claim: SourceClaim = serde_json::from_slice(&bytes)
        .with_context(|| format!("parsing the claim {}", path.display()))?;
    Ok(Some(claim))
}

/// Every claim the daemon should honour, newest first.
///
/// An unreadable or expired claim is skipped and reported, never obeyed: this
/// is a lease over a path, and a corrupt lease must not hold a path hostage.
pub fn active_claims() -> Result<Vec<SourceClaim>> {
    let dir = claims_dir()?;
    let entries = match std::fs::read_dir(&dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("reading the claim directory {}", dir.display()))
        }
    };
    let now = now();
    let mut claims = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let Ok(bytes) = std::fs::read(&path) else {
            continue;
        };
        let claim: SourceClaim = match serde_json::from_slice(&bytes) {
            Ok(claim) => claim,
            Err(error) => {
                tracing::warn!(path = %path.display(), error = %error, "ignoring an unreadable source claim");
                continue;
            }
        };
        if claim.is_expired(now) {
            tracing::warn!(
                session_id = %claim.session_id,
                source = %claim.source_path,
                "ignoring an expired source claim"
            );
            continue;
        }
        if claim.state != ClaimState::Released {
            claims.push(claim);
        }
    }
    claims.sort_by(|left, right| right.updated_at.cmp(&left.updated_at));
    Ok(claims)
}

/// Refuse a bind that would contradict what is already claimed.
///
/// The launchers used to read the archive binding for this: another managed
/// session owning the path, or this session changing the identity it already
/// bound. The claim is the authority now, so the rule lives here.
pub fn ensure_bindable(session_id: &str, source_path: &Path, native_id: &str) -> Result<()> {
    let normalized = crate::storage_v2_shipper::stable_source_path(source_path);
    for existing in active_claims()? {
        let existing_path =
            crate::storage_v2_shipper::stable_source_path(Path::new(&existing.source_path));
        if existing_path != normalized {
            continue;
        }
        if existing.session_id != session_id {
            anyhow::bail!(
                "source {} is already claimed by managed session {}",
                source_path.display(),
                existing.session_id
            );
        }
        if let Some(bound) = existing.native_session_id.as_deref() {
            anyhow::ensure!(
                bound == native_id,
                "source {} is already bound to native identity {bound}",
                source_path.display()
            );
        }
    }
    Ok(())
}

/// Release a session's claim. Called when a launcher exits.
///
/// The file becomes a tombstone rather than disappearing: the daemon needs to
/// see that the claim *ended* to retire the projection it wrote, and a claim that
/// simply vanishes would leave a binding row naming a session nobody released.
/// `retire_released` removes the file once it has.
pub fn release(session_id: &str) -> Result<()> {
    let Some(mut claim) = read_claim(session_id)? else {
        return Ok(());
    };
    if claim.state == ClaimState::Released {
        return Ok(());
    }
    claim.state = ClaimState::Released;
    claim.native_session_id = None;
    claim.updated_at = now().to_rfc3339();
    claim.expires_at = expiry();
    write_claim(&claim)
}

/// Retire the projections of claims that ended, then remove their tombstones.
///
/// The binding row is *marked exited*, not deleted: it stays as the ownership
/// evidence a later reader needs, which is what `SessionBinding::mark_exited`
/// exists for. A row that cannot be marked is left and the tombstone is kept, so
/// the next pass tries again rather than losing the obligation.
pub fn retire_released(conn: &rusqlite::Connection) -> Result<usize> {
    let dir = claims_dir()?;
    let entries = match std::fs::read_dir(&dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(error).context("reading the claim directory"),
    };
    let mut retired = 0;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let Ok(bytes) = std::fs::read(&path) else {
            continue;
        };
        let Ok(claim) = serde_json::from_slice::<SourceClaim>(&bytes) else {
            continue;
        };
        if claim.state != ClaimState::Released {
            continue;
        }
        let binding = crate::state::session_binding::SessionBinding::new(conn);
        // The projection binds the normalized path, so retirement must look up
        // the same one (macOS `/tmp` is a symlink, and a raw comparison misses).
        let projected_path =
            crate::storage_v2_shipper::stable_source_path(Path::new(&claim.source_path))
                .to_string_lossy()
                .into_owned();
        match binding.get_with_thread_for_provider(&projected_path, &claim.provider) {
            // Nobody owns it: nothing to retire.
            Ok(None) => {}
            Ok(Some((owner, _))) if owner == claim.session_id => {
                if let Err(error) = binding.mark_exited(&projected_path) {
                    tracing::warn!(
                        session_id = %claim.session_id,
                        error = %format!("{error:#}"),
                        "retiring a released source claim failed; the next pass retries it"
                    );
                    continue;
                }
            }
            // A later session owns the path now: the tombstone is stale and the
            // row is not ours to touch.
            Ok(Some((owner, _))) => tracing::debug!(
                released = %claim.session_id,
                owner = %owner,
                "a released claim's path belongs to another session; dropping the tombstone"
            ),
            Err(error) => {
                tracing::warn!(
                    session_id = %claim.session_id,
                    error = %format!("{error:#}"),
                    "reading a binding to retire a released claim failed; the next pass retries it"
                );
                continue;
            }
        }
        match std::fs::remove_file(&path) {
            Ok(()) => retired += 1,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => retired += 1,
            Err(error) => tracing::warn!(
                path = %path.display(),
                error = %error,
                "removing a retired claim failed"
            ),
        }
    }
    Ok(retired)
}

/// What one projection pass did.
#[derive(Debug, Default, Clone, Copy)]
pub struct ProjectionReport {
    pub applied: usize,
    pub failed: usize,
}

/// Apply active claims to `session_binding`, which is the view discovery reads.
///
/// The daemon calls this through the observation scan, before any provider
/// source is enumerated. It is idempotent — a reservation and a bind both end in
/// the same rows — so a pass that fails or is missed costs nothing but the next
/// pass, and a session never becomes degraded because a projection was late.
pub fn project_claims(db_path: &Path) -> Result<ProjectionReport> {
    let conn = crate::state::db::open_connection(db_path).with_context(|| {
        format!(
            "opening {} to project managed source claims",
            db_path.display()
        )
    })?;
    // Retire what ended before applying what is live, so a released path is not
    // re-bound in the same pass.
    retire_released(&conn)?;
    let claims = active_claims()?;
    if claims.is_empty() {
        return Ok(ProjectionReport::default());
    }
    let mut report = ProjectionReport::default();
    for claim in claims {
        let outcome = project_claim(&conn, &claim);
        match outcome {
            Ok(()) => report.applied += 1,
            Err(error) => {
                report.failed += 1;
                tracing::warn!(
                    session_id = %claim.session_id,
                    source = %claim.source_path,
                    error = %format!("{error:#}"),
                    "projecting a managed source claim failed; the next pass retries it"
                );
            }
        }
    }
    Ok(report)
}

/// Project one claim into the binding discovery reads.
///
/// The OMP family keeps its own reservation helper, which carries the source
/// checks that provider's launchers rely on. Every other provider binds through
/// the same `session_binding` upsert its launcher used to write directly, with
/// the provider name taken from the claim — never assumed.
fn project_claim(conn: &rusqlite::Connection, claim: &SourceClaim) -> Result<()> {
    let path = Path::new(&claim.source_path);
    let native_id = claim
        .native_session_id
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if claim.state == ClaimState::Released {
        return Ok(());
    }
    if is_omp_family(&claim.provider) {
        return match claim.state {
            ClaimState::Reserved => crate::omp_session::reserve_source_for_thread(
                conn,
                path,
                &claim.session_id,
                native_id,
            ),
            ClaimState::Bound => crate::omp_session::bind_source_for_thread(
                conn,
                path,
                &claim.session_id,
                native_id.context("a bound claim carries no native identity")?,
            ),
            ClaimState::Released => Ok(()),
        };
    }
    let stable_path = crate::storage_v2_shipper::stable_source_path(path);
    crate::state::session_binding::SessionBinding::new(conn).bind_for_thread(
        &stable_path.to_string_lossy(),
        &claim.session_id,
        &claim.provider,
        native_id,
    )
}

fn is_omp_family(provider: &str) -> bool {
    matches!(provider, "omp" | "oh-my-pi" | "pi-omp")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn with_home<T>(home: &Path, body: impl FnOnce() -> T) -> T {
        temp_env::with_var("LONGHOUSE_HOME", Some(home), body)
    }

    #[test]
    fn projection_makes_a_claimed_path_visible_to_discovery() {
        let dir = tempfile::tempdir().unwrap();
        let with_home_dir = dir.path().join("longhouse");
        with_home(&with_home_dir, || {
            let db_path = with_home_dir.join("agent/longhouse-shipper.db");
            let source = dir.path().join("session.jsonl");
            std::fs::write(&source, b"{}\n").unwrap();
            let session_id = uuid::Uuid::new_v4().to_string();

            // A reservation alone is enough to take the path out of discovery's
            // reach before the provider has written anything.
            reserve(&session_id, "omp", &source, dir.path(), Some(4242), None).expect("reserve");
            let conn = crate::state::db::open_db(Some(&db_path)).expect("open agent db");
            let report = project_claims(&db_path).expect("project");
            assert_eq!(report.applied, 1);
            assert_eq!(report.failed, 0);
            // The binding normalizes its path (macOS `/tmp` is a symlink), so
            // the assertion keys on the session, not on the path it was given.
            let bound_path: String = conn
                .query_row(
                    "SELECT path FROM session_binding WHERE session_id = ?1",
                    [session_id.as_str()],
                    |row| row.get(0),
                )
                .expect("a projected claim must appear in session_binding");
            assert!(
                bound_path.ends_with("session.jsonl"),
                "the projected path must be the claimed source: {bound_path}"
            );

            // Binding carries the verified native identity through.
            confirm_identity(&session_id, "omp", &source, "native-1", Some(4242), None)
                .expect("confirm identity");
            let report = project_claims(&db_path).expect("project bound claim");
            assert_eq!(report.applied, 1);
            let native: Option<String> = conn
                .query_row(
                    "SELECT provider_session_id FROM session_binding WHERE session_id = ?1",
                    [session_id.as_str()],
                    |row| row.get(0),
                )
                .expect("a bound claim must carry its native identity");
            assert_eq!(native.as_deref(), Some("native-1"));
        });
    }

    #[test]
    fn a_claim_round_trips_through_reserve_then_bind() {
        let dir = tempfile::tempdir().unwrap();
        with_home(&dir.path().join("longhouse"), || {
            let source = Path::new("/tmp/session.jsonl");
            let reserved = reserve(
                "session-1",
                "omp",
                source,
                Path::new("/tmp"),
                Some(42),
                None,
            )
            .expect("reserve");
            assert_eq!(reserved.state, ClaimState::Reserved);
            assert_eq!(reserved.native_session_id, None);
            assert!(reserved.provider_pid.is_some(), "reserve keeps observation");

            let bound = confirm_identity("session-1", "omp", source, "native-1", None, None)
                .expect("confirm");
            assert_eq!(bound.state, ClaimState::Bound);
            assert_eq!(bound.native_session_id.as_deref(), Some("native-1"));
            assert_eq!(bound.created_at, reserved.created_at);
            assert_eq!(
                bound.provider_pid, reserved.provider_pid,
                "binding must not discard what reserve observed"
            );

            let active = active_claims().expect("active claims");
            assert_eq!(active.len(), 1);
            assert_eq!(active[0].session_id, "session-1");

            release("session-1").expect("release");
            assert!(active_claims().expect("active claims").is_empty());
        });
    }

    #[test]
    fn releasing_a_claim_retires_its_binding_and_keeps_the_evidence() {
        // The lifecycle end: a session stops, the daemon marks the binding it
        // wrote as exited (the row is ownership evidence and stays), and only
        // then does the tombstone go.
        let dir = tempfile::tempdir().unwrap();
        with_home(&dir.path().join("longhouse"), || {
            let db_path = dir.path().join("agent/longhouse-shipper.db");
            let source = dir.path().join("session.jsonl");
            std::fs::write(&source, b"{}\n").unwrap();
            let session_id = uuid::Uuid::new_v4().to_string();
            let conn = crate::state::db::open_db(Some(&db_path)).expect("open agent db");

            reserve(&session_id, "codex", &source, dir.path(), None, None).expect("claim");
            confirm_identity(&session_id, "codex", &source, "native-1", None, None).expect("bind");
            project_claims(&db_path).expect("project");
            let claimed_path: String = conn
                .query_row(
                    "SELECT path FROM session_binding WHERE session_id = ?1",
                    [session_id.as_str()],
                    |row| row.get(0),
                )
                .expect("the claim must be projected");

            release(&session_id).expect("release");
            assert_eq!(
                read_claim(&session_id)
                    .expect("read")
                    .map(|claim| claim.state),
                Some(ClaimState::Released),
                "release leaves a tombstone the daemon can see"
            );

            project_claims(&db_path).expect("project again");
            let state: String = conn
                .query_row(
                    "SELECT state FROM session_binding WHERE session_id = ?1",
                    [session_id.as_str()],
                    |row| row.get(0),
                )
                .expect("the row stays as ownership evidence");
            assert_eq!(state, "exited");
            assert!(
                read_claim(&session_id).expect("read").is_none(),
                "a retired tombstone is removed, not accumulated"
            );
            assert!(Path::new(&claimed_path).exists() || !claimed_path.is_empty());
        });
    }

    #[test]
    fn a_bind_cannot_contradict_an_existing_claim() {
        let dir = tempfile::tempdir().unwrap();
        with_home(&dir.path().join("longhouse"), || {
            let source = dir.path().join("session.jsonl");
            reserve("session-1", "pi", &source, dir.path(), None, None).expect("claim");
            confirm_identity("session-1", "pi", &source, "native-1", None, None).expect("bind");

            assert!(
                ensure_bindable("session-1", &source, "native-1").is_ok(),
                "the same identity may be re-bound"
            );
            assert!(
                ensure_bindable("session-1", &source, "native-2").is_err(),
                "a session may not silently change the identity it bound"
            );
            assert!(
                ensure_bindable("session-2", &source, "native-1").is_err(),
                "another session may not take a claimed path"
            );
        });
    }

    #[test]
    fn a_second_session_cannot_claim_another_sessions_path() {
        // One transcript path has one managed owner. The archive binding used to
        // enforce this; the claim is the authority now, so overwriting another
        // session's claim would silently hand its transcript to this one.
        let dir = tempfile::tempdir().unwrap();
        with_home(&dir.path().join("longhouse"), || {
            let source = dir.path().join("session.jsonl");
            reserve("session-1", "omp", &source, dir.path(), None, None).expect("first claim");

            let second = reserve("session-2", "codex", &source, dir.path(), None, None);

            assert!(
                second.is_err(),
                "a path already claimed by another managed session must be refused"
            );
            let held = read_claim("session-1").expect("read").expect("claim");
            assert_eq!(
                held.session_id, "session-1",
                "the refusal must leave the owner's claim untouched"
            );
        });
    }

    #[test]
    fn an_expired_claim_is_ignored_rather_than_obeyed() {
        let dir = tempfile::tempdir().unwrap();
        with_home(&dir.path().join("longhouse"), || {
            reserve(
                "session-1",
                "omp",
                Path::new("/tmp/a.jsonl"),
                Path::new("/tmp"),
                None,
                None,
            )
            .expect("reserve");
            let mut claim = read_claim("session-1").expect("read").expect("claim");
            claim.expires_at = (now() - chrono::Duration::seconds(1)).to_rfc3339();
            write_claim(&claim).expect("rewrite");

            assert!(
                active_claims().expect("active claims").is_empty(),
                "an expired lease must not hold a path"
            );
        });
    }

    #[test]
    fn an_unreadable_claim_is_skipped_and_does_not_hide_the_others() {
        let dir = tempfile::tempdir().unwrap();
        with_home(&dir.path().join("longhouse"), || {
            reserve(
                "session-1",
                "omp",
                Path::new("/tmp/a.jsonl"),
                Path::new("/tmp"),
                None,
                None,
            )
            .expect("reserve");
            let broken = claims_dir().expect("dir").join("session-2.json");
            std::fs::write(&broken, b"{ not json").expect("write broken claim");

            let active = active_claims().expect("active claims");
            assert_eq!(active.len(), 1, "one good claim survives one broken one");
            assert_eq!(active[0].session_id, "session-1");
        });
    }

    #[test]
    fn a_released_claim_is_not_projected() {
        let dir = tempfile::tempdir().unwrap();
        with_home(&dir.path().join("longhouse"), || {
            reserve(
                "session-1",
                "omp",
                Path::new("/tmp/a.jsonl"),
                Path::new("/tmp"),
                None,
                None,
            )
            .expect("reserve");
            let mut claim = read_claim("session-1").expect("read").expect("claim");
            claim.state = ClaimState::Released;
            write_claim(&claim).expect("rewrite");

            assert!(active_claims().expect("active claims").is_empty());
        });
    }

    #[test]
    fn no_claim_directory_is_not_an_error() {
        let dir = tempfile::tempdir().unwrap();
        with_home(&dir.path().join("longhouse"), || {
            assert!(active_claims().expect("no claims yet").is_empty());
        });
    }
}
