//! Which local history the Machine Agent may import.
//!
//! A machine's provider folders can hold years of transcripts, some of them
//! about work that was never meant to leave it. The scope is the one durable
//! answer to "what may this agent send": a time bound plus optional project
//! folders, stored beside the device credentials in
//! `<longhouse home>/machine/import-scope.json`.
//!
//! - `since`: a session is in scope when it *started* at or after this
//!   instant. `None` means no lower bound: all local history.
//! - `projects`: folders whose sessions are in scope whatever their age. A
//!   session belongs to a folder when its recorded working directory is that
//!   folder or below it.
//!
//! Sessions that start after the scope was chosen are always in scope, so
//! choosing "from now on" never hides new work. The engine enforces the scope
//! at the seams where a source enters shipping (discovery, watcher events, the
//! OpenCode session walk, Claude presence hooks); the `longhouse` facade
//! writes it. This module is shared by both binaries and has no dependency on
//! either.
//!
//! An absent file means nobody ever chose. The engine resolves that once at
//! startup (`config::resolve_import_scope`): a machine whose shipper state
//! already holds history keeps shipping all of it, a machine connected to a
//! Runtime Host with no history starts "from now on", and an engine run by hand
//! with no machine state is unrestricted. Everything that runs before that
//! resolution sees an absent file as unrestricted, which is what every install
//! did before scopes existed.

#![allow(dead_code)] // The facade and the engine each use a different half.

use std::collections::HashMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};
use std::time::SystemTime;

use anyhow::{bail, Context, Result};
use chrono::{DateTime, Local, NaiveDate, TimeZone, Utc};
use serde::{Deserialize, Serialize};

pub const FILE_NAME: &str = "import-scope.json";
const SCHEMA_VERSION: u8 = 1;

/// Bytes read from the front of a transcript to find its working directory.
/// Provider transcripts state `cwd` in their first records; Codex puts a long
/// system prompt in the first line, which fits with room to spare.
const CWD_PEEK_BYTES: usize = 64 * 1024;
const CWD_PEEK_LINES: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ImportScope {
    pub schema_version: u8,
    /// Sessions that started at or after this instant are in scope; `None`
    /// means no lower bound.
    pub since: Option<DateTime<Utc>>,
    /// Folders whose sessions are in scope regardless of age. Canonical paths.
    #[serde(default)]
    pub projects: Vec<PathBuf>,
    pub chosen_at: DateTime<Utc>,
    /// Who chose: `cli`, `prompt`, `default` (the engine's first-run answer) or
    /// `legacy` (a machine that shipped everything before scopes existed).
    pub chosen_via: String,
}

impl ImportScope {
    /// All local history, present and future.
    pub fn all(via: &str) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            since: None,
            projects: Vec::new(),
            chosen_at: Utc::now(),
            chosen_via: via.to_string(),
        }
    }

    /// Only sessions that start from `now` on.
    pub fn starting(now: DateTime<Utc>, via: &str) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            since: Some(now),
            projects: Vec::new(),
            chosen_at: Utc::now(),
            chosen_via: via.to_string(),
        }
    }

    pub fn is_unrestricted(&self) -> bool {
        self.since.is_none()
    }

    /// Stable text for change detection and cache keys.
    pub fn signature(&self) -> String {
        let projects = self
            .projects
            .iter()
            .map(|path| path.display().to_string())
            .collect::<Vec<_>>()
            .join("\u{1f}");
        format!(
            "{}|{projects}",
            self.since
                .map_or_else(|| "all".to_string(), |t| t.to_rfc3339())
        )
    }

    /// One line a person can read back.
    pub fn describe(&self) -> String {
        let base = match self.since {
            None => "all local history".to_string(),
            Some(since) => format!(
                "sessions that start on or after {}",
                since.with_timezone(&Local).format("%Y-%m-%d %H:%M %Z")
            ),
        };
        if self.since.is_some() && !self.projects.is_empty() {
            let projects = self
                .projects
                .iter()
                .map(|path| path.display().to_string())
                .collect::<Vec<_>>()
                .join(", ");
            format!("{base}, plus the full history of: {projects}")
        } else {
            base
        }
    }

    /// Whether a source that started at `started` and ran in the folder `cwd`
    /// is in scope. `started == None` means the source does not exist yet, so
    /// it is new by definition. `cwd` is only asked for when age alone does not
    /// decide, because reading it costs a file read.
    pub fn admits(
        &self,
        started: Option<DateTime<Utc>>,
        cwd: impl FnOnce() -> Option<String>,
    ) -> bool {
        let Some(since) = self.since else {
            return true;
        };
        match started {
            None => return true,
            Some(started) if started >= since => return true,
            Some(_) => {}
        }
        if self.projects.is_empty() {
            return false;
        }
        cwd().is_some_and(|cwd| self.project_contains(&cwd))
    }

    /// Whether a transcript file is in scope. A file that cannot be read for
    /// its start time does not exist yet and counts as new.
    pub fn admits_file(&self, path: &Path) -> bool {
        if self.is_unrestricted() {
            return true;
        }
        self.admits(file_started_at(path), || file_cwd(path))
    }

    fn project_contains(&self, cwd: &str) -> bool {
        let cwd = Path::new(cwd);
        let physical = cwd.canonicalize().ok();
        self.projects.iter().any(|project| {
            cwd.starts_with(project)
                || physical
                    .as_deref()
                    .is_some_and(|physical| physical.starts_with(project))
        })
    }

    pub fn path(machine_dir: &Path) -> PathBuf {
        machine_dir.join(FILE_NAME)
    }

    /// `Ok(None)` when no scope was ever chosen. A file that exists but cannot
    /// be trusted is an error: guessing would either hide or leak history.
    pub fn load(machine_dir: &Path) -> Result<Option<Self>> {
        let path = Self::path(machine_dir);
        let raw = match std::fs::read(&path) {
            Ok(raw) => raw,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error).with_context(|| format!("read {}", path.display())),
        };
        let scope: Self = serde_json::from_slice(&raw)
            .with_context(|| format!("{} is not a valid import scope", path.display()))?;
        if scope.schema_version != SCHEMA_VERSION {
            bail!(
                "{} has schema version {}, this build understands {SCHEMA_VERSION}",
                path.display(),
                scope.schema_version
            );
        }
        Ok(Some(scope))
    }

    /// Replace the stored scope. The rename makes the change all-or-nothing for
    /// a running engine that reads the file at any moment.
    pub fn save(&self, machine_dir: &Path) -> Result<()> {
        let temporary = self.write_temporary(machine_dir)?;
        let target = Self::path(machine_dir);
        std::fs::rename(&temporary, &target).map_err(|error| {
            let _ = std::fs::remove_file(&temporary);
            anyhow::Error::from(error).context(format!("write {}", target.display()))
        })
    }

    /// Store this scope only if none exists. Returns whether it was stored, so
    /// the engine's first-run default can never overwrite a choice made a
    /// moment earlier by `longhouse machine scope`.
    pub fn save_if_absent(&self, machine_dir: &Path) -> Result<bool> {
        let temporary = self.write_temporary(machine_dir)?;
        let target = Self::path(machine_dir);
        let outcome = std::fs::hard_link(&temporary, &target);
        let _ = std::fs::remove_file(&temporary);
        match outcome {
            Ok(()) => Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
            Err(error) => Err(error).with_context(|| format!("write {}", target.display())),
        }
    }

    fn write_temporary(&self, machine_dir: &Path) -> Result<PathBuf> {
        std::fs::create_dir_all(machine_dir)
            .with_context(|| format!("create {}", machine_dir.display()))?;
        let temporary = machine_dir.join(format!(
            ".{FILE_NAME}.{}.tmp",
            uuid::Uuid::new_v4().simple()
        ));
        let body = format!("{}\n", serde_json::to_string_pretty(self)?);
        write_private(&temporary, body.as_bytes())
            .with_context(|| format!("write {}", temporary.display()))?;
        Ok(temporary)
    }
}

#[cfg(unix)]
fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(bytes)
}

#[cfg(not(unix))]
fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    std::fs::write(path, bytes)
}

/// Modification stamp of the stored scope, for cheap change detection.
pub fn fingerprint(machine_dir: &Path) -> Option<(SystemTime, u64)> {
    let meta = std::fs::metadata(ImportScope::path(machine_dir)).ok()?;
    Some((meta.modified().ok()?, meta.len()))
}

/// The engine's first-run answer, written once and never revisited.
///
/// `has_shipped_history` says whether this machine's shipper state already
/// tracks sources. Such a machine was imported in full before scopes existed and
/// keeps that: silently narrowing it would strand history half-shipped. A
/// machine with no history starts from now, so a stranger's old transcripts are
/// never imported until they say so.
pub fn resolve_at_startup(machine_dir: &Path, has_shipped_history: bool) -> Result<ImportScope> {
    if let Some(scope) = ImportScope::load(machine_dir)? {
        return Ok(scope);
    }
    let scope = if has_shipped_history {
        ImportScope::all("legacy")
    } else {
        ImportScope::starting(Utc::now(), "default")
    };
    if scope.save_if_absent(machine_dir)? {
        return Ok(scope);
    }
    ImportScope::load(machine_dir)?
        .context("import scope vanished while resolving the first-run default")
}

/// Parse `--since`: `now`, `all`, a local date, or an RFC 3339 instant.
/// `None` in the result means "all".
pub fn parse_since(value: &str, now: DateTime<Utc>) -> Result<Option<DateTime<Utc>>> {
    let value = value.trim();
    match value.to_ascii_lowercase().as_str() {
        "now" => return Ok(Some(now)),
        "all" => return Ok(None),
        _ => {}
    }
    if let Ok(instant) = DateTime::parse_from_rfc3339(value) {
        return Ok(Some(instant.with_timezone(&Utc)));
    }
    if let Ok(date) = NaiveDate::parse_from_str(value, "%Y-%m-%d") {
        let midnight = date.and_hms_opt(0, 0, 0).context("invalid date")?;
        let local = Local
            .from_local_datetime(&midnight)
            .earliest()
            .context("that local date does not exist")?;
        return Ok(Some(local.with_timezone(&Utc)));
    }
    bail!("expected `now`, `all`, a date like 2026-09-01, or an RFC 3339 time; got `{value}`")
}

// ---------------------------------------------------------------------------
// Facts about a source file
// ---------------------------------------------------------------------------

/// When the session behind a transcript file started.
///
/// The file's creation time where the file system records one, otherwise its
/// last modification. A Claude subagent transcript belongs to its parent
/// session and takes the parent's start, so resuming an old session never
/// makes its subagents look new. `None` when the file does not exist.
pub fn file_started_at(path: &Path) -> Option<DateTime<Utc>> {
    let anchor = session_anchor(path);
    let meta = std::fs::metadata(&anchor)
        .or_else(|_| std::fs::metadata(path))
        .ok()?;
    let stamp = meta.created().or_else(|_| meta.modified()).ok()?;
    Some(DateTime::<Utc>::from(stamp))
}

/// `<projects>/<dir>/<session>/subagents/**/x.jsonl` -> `<projects>/<dir>/<session>.jsonl`.
fn session_anchor(path: &Path) -> PathBuf {
    let mut ancestor = path.parent();
    while let Some(dir) = ancestor {
        if dir.file_name().is_some_and(|name| name == "subagents") {
            if let Some(session_dir) = dir.parent() {
                let parent = session_dir.with_extension("jsonl");
                if parent.is_file() {
                    return parent;
                }
            }
            break;
        }
        ancestor = dir.parent();
    }
    path.to_path_buf()
}

type CwdCache = Mutex<HashMap<PathBuf, (u64, Option<String>)>>;
static CWD_CACHE: LazyLock<CwdCache> = LazyLock::new(|| Mutex::new(HashMap::new()));

/// The working directory a transcript records, read from its first records.
/// Cached by path and size: a session's folder never changes, but a file that
/// had no records yet is looked at again once it grows.
pub fn file_cwd(path: &Path) -> Option<String> {
    let len = std::fs::metadata(path).ok()?.len();
    if let Ok(cache) = CWD_CACHE.lock() {
        if let Some((seen_len, cwd)) = cache.get(path) {
            if cwd.is_some() || *seen_len == len {
                return cwd.clone();
            }
        }
    }
    let cwd = peek_cwd(path);
    if let Ok(mut cache) = CWD_CACHE.lock() {
        cache.insert(path.to_path_buf(), (len, cwd.clone()));
    }
    cwd
}

fn peek_cwd(path: &Path) -> Option<String> {
    let mut head = Vec::with_capacity(CWD_PEEK_BYTES);
    std::fs::File::open(path)
        .ok()?
        .take(CWD_PEEK_BYTES as u64)
        .read_to_end(&mut head)
        .ok()?;
    // The last line may be cut off by the byte limit; only whole lines count.
    let complete = match head.iter().rposition(|byte| *byte == b'\n') {
        Some(end) => &head[..end],
        None if head.len() < CWD_PEEK_BYTES => &head[..],
        None => return None,
    };
    complete
        .split(|byte| *byte == b'\n')
        .take(CWD_PEEK_LINES)
        .filter_map(|line| serde_json::from_slice::<serde_json::Value>(line).ok())
        .find_map(|record| record_cwd(&record))
}

/// Claude and Pi/OMP write `cwd` on the record, Codex inside `payload`.
fn record_cwd(record: &serde_json::Value) -> Option<String> {
    let text = |value: &serde_json::Value| {
        value
            .as_str()
            .map(str::trim)
            .filter(|cwd| !cwd.is_empty())
            .map(str::to_string)
    };
    record
        .get("cwd")
        .and_then(text)
        .or_else(|| record.get("payload")?.get("cwd").and_then(text))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::Duration;

    fn at(value: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(value)
            .unwrap()
            .with_timezone(&Utc)
    }

    fn scope_since(since: &str, projects: &[&str]) -> ImportScope {
        ImportScope {
            schema_version: SCHEMA_VERSION,
            since: Some(at(since)),
            projects: projects.iter().map(PathBuf::from).collect(),
            chosen_at: at("2026-09-30T00:00:00Z"),
            chosen_via: "cli".to_string(),
        }
    }

    #[test]
    fn unrestricted_scope_admits_everything_without_reading_anything() {
        let scope = ImportScope::all("cli");
        assert!(scope.admits(Some(at("2001-01-01T00:00:00Z")), || panic!(
            "cwd not needed"
        )));
        assert!(scope.admits_file(Path::new("/nonexistent/anything.jsonl")));
    }

    #[test]
    fn sessions_started_before_the_bound_are_out_and_after_are_in() {
        let scope = scope_since("2026-09-30T12:00:00Z", &[]);
        assert!(!scope.admits(Some(at("2026-09-30T11:59:59Z")), || None));
        assert!(scope.admits(Some(at("2026-09-30T12:00:00Z")), || None));
        assert!(scope.admits(Some(at("2026-10-01T00:00:00Z")), || None));
    }

    #[test]
    fn a_source_that_does_not_exist_yet_is_new() {
        let scope = scope_since("2026-09-30T12:00:00Z", &[]);
        assert!(scope.admits(None, || None));
    }

    #[test]
    fn age_alone_decides_new_sessions_without_reading_the_folder() {
        let scope = scope_since("2026-09-30T12:00:00Z", &["/work/a"]);
        assert!(scope.admits(Some(at("2026-10-01T00:00:00Z")), || panic!(
            "cwd not needed"
        )));
    }

    #[test]
    fn old_sessions_need_a_listed_project_and_a_recorded_folder() {
        let scope = scope_since("2026-09-30T12:00:00Z", &["/work/a"]);
        let old = Some(at("2026-01-01T00:00:00Z"));
        assert!(scope.admits(old, || Some("/work/a".into())));
        assert!(scope.admits(old, || Some("/work/a/sub/deeper".into())));
        assert!(
            !scope.admits(old, || Some("/work/ab".into())),
            "component match, not prefix"
        );
        assert!(!scope.admits(old, || Some("/work/b".into())));
        assert!(
            !scope.admits(old, || None),
            "no recorded folder is never opted in"
        );
    }

    #[test]
    fn projects_are_matched_through_symlinks() {
        let dir = tempfile::tempdir().unwrap();
        let real = dir.path().join("real");
        fs::create_dir_all(real.join("sub")).unwrap();
        let link = dir.path().join("link");
        #[cfg(unix)]
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let scope = ImportScope {
            projects: vec![real.canonicalize().unwrap()],
            ..scope_since("2026-09-30T12:00:00Z", &[])
        };
        let old = Some(at("2026-01-01T00:00:00Z"));
        assert!(scope.admits(old, || Some(link.join("sub").display().to_string())));
    }

    #[test]
    fn parse_since_accepts_now_all_dates_and_instants() {
        let now = at("2026-09-30T12:00:00Z");
        assert_eq!(parse_since("now", now).unwrap(), Some(now));
        assert_eq!(parse_since(" ALL ", now).unwrap(), None);
        assert_eq!(
            parse_since("2026-10-01T05:00:00+02:00", now).unwrap(),
            Some(at("2026-10-01T03:00:00Z"))
        );
        let date = parse_since("2026-09-01", now).unwrap().unwrap();
        assert!(date >= at("2026-08-31T00:00:00Z") && date <= at("2026-09-02T00:00:00Z"));
        assert!(parse_since("yesterday", now).is_err());
    }

    #[test]
    fn scope_round_trips_and_a_missing_file_is_not_an_error() {
        let dir = tempfile::tempdir().unwrap();
        assert!(ImportScope::load(dir.path()).unwrap().is_none());
        let scope = scope_since("2026-09-30T12:00:00Z", &["/work/a"]);
        scope.save(dir.path()).unwrap();
        assert_eq!(ImportScope::load(dir.path()).unwrap(), Some(scope));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(ImportScope::path(dir.path()))
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o077, 0, "scope is private to the user");
        }
    }

    #[test]
    fn a_corrupt_or_future_scope_file_is_refused_not_guessed() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(ImportScope::path(dir.path()), "{not json").unwrap();
        assert!(ImportScope::load(dir.path()).is_err());
        let mut future = ImportScope::all("cli");
        future.schema_version = 99;
        fs::write(
            ImportScope::path(dir.path()),
            serde_json::to_vec(&future).unwrap(),
        )
        .unwrap();
        assert!(ImportScope::load(dir.path()).is_err());
    }

    #[test]
    fn first_run_default_is_from_now_and_history_keeps_everything() {
        let fresh = tempfile::tempdir().unwrap();
        let scope = resolve_at_startup(fresh.path(), false).unwrap();
        assert_eq!(scope.chosen_via, "default");
        assert!(scope.since.is_some());
        assert!(scope.projects.is_empty());
        assert_eq!(ImportScope::load(fresh.path()).unwrap(), Some(scope));

        let existing = tempfile::tempdir().unwrap();
        let scope = resolve_at_startup(existing.path(), true).unwrap();
        assert_eq!(scope.chosen_via, "legacy");
        assert!(scope.is_unrestricted());
    }

    #[test]
    fn the_default_never_overwrites_a_choice_already_made() {
        let dir = tempfile::tempdir().unwrap();
        ImportScope::all("cli").save(dir.path()).unwrap();
        // Even a machine with no history keeps the explicit choice.
        let scope = resolve_at_startup(dir.path(), false).unwrap();
        assert_eq!(scope.chosen_via, "cli");
        assert!(scope.is_unrestricted());
        // And the race itself: a default written after a choice does not land.
        assert!(!ImportScope::starting(Utc::now(), "default")
            .save_if_absent(dir.path())
            .unwrap());
        assert_eq!(
            ImportScope::load(dir.path()).unwrap().unwrap().chosen_via,
            "cli"
        );
    }

    #[test]
    fn cwd_is_read_from_claude_pi_and_codex_records() {
        let dir = tempfile::tempdir().unwrap();
        let claude = dir.path().join("claude.jsonl");
        fs::write(
            &claude,
            "{\"type\":\"file-history-snapshot\"}\n{\"cwd\":\"/work/a\",\"type\":\"user\"}\n",
        )
        .unwrap();
        assert_eq!(file_cwd(&claude).as_deref(), Some("/work/a"));

        let codex = dir.path().join("codex.jsonl");
        fs::write(
            &codex,
            "{\"type\":\"session_meta\",\"payload\":{\"id\":\"x\",\"cwd\":\"/work/b\"}}\n",
        )
        .unwrap();
        assert_eq!(file_cwd(&codex).as_deref(), Some("/work/b"));

        let none = dir.path().join("none.jsonl");
        fs::write(&none, "{\"type\":\"user\"}\nnot json\n").unwrap();
        assert_eq!(file_cwd(&none), None);
    }

    #[test]
    fn a_file_with_no_records_yet_is_read_again_once_it_grows() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("late.jsonl");
        fs::write(&path, "{\"type\":\"summary\"}\n").unwrap();
        assert_eq!(file_cwd(&path), None);
        fs::write(&path, "{\"type\":\"summary\"}\n{\"cwd\":\"/work/late\"}\n").unwrap();
        assert_eq!(file_cwd(&path).as_deref(), Some("/work/late"));
    }

    #[test]
    fn a_line_cut_off_by_the_peek_limit_is_not_parsed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("huge.jsonl");
        let padding = "x".repeat(CWD_PEEK_BYTES + 10);
        fs::write(
            &path,
            format!("{{\"pad\":\"{padding}\",\"cwd\":\"/work/hidden\"}}\n"),
        )
        .unwrap();
        assert_eq!(file_cwd(&path), None);
    }

    #[test]
    fn file_scope_follows_creation_order_and_project_opt_in() {
        let dir = tempfile::tempdir().unwrap();
        let old = dir.path().join("old.jsonl");
        fs::write(&old, "{\"cwd\":\"/work/a\"}\n").unwrap();
        let other = dir.path().join("other.jsonl");
        fs::write(&other, "{\"cwd\":\"/work/z\"}\n").unwrap();
        std::thread::sleep(Duration::from_millis(60));
        let since = Utc::now();
        std::thread::sleep(Duration::from_millis(60));
        let new = dir.path().join("new.jsonl");
        fs::write(&new, "{\"cwd\":\"/work/z\"}\n").unwrap();

        let scope = ImportScope::starting(since, "cli");
        assert!(!scope.admits_file(&old));
        assert!(!scope.admits_file(&other));
        assert!(scope.admits_file(&new));
        assert!(scope.admits_file(&dir.path().join("not-created-yet.jsonl")));

        let with_project = ImportScope {
            projects: vec![PathBuf::from("/work/a")],
            ..scope.clone()
        };
        assert!(
            with_project.admits_file(&old),
            "old but in an opted-in project"
        );
        assert!(!with_project.admits_file(&other));
        assert!(with_project.admits_file(&new));
    }

    #[test]
    fn subagent_transcripts_take_their_parent_sessions_start() {
        let dir = tempfile::tempdir().unwrap();
        let project = dir.path().join("projects").join("-work-a");
        let session = "11111111-2222-3333-4444-555555555555";
        fs::create_dir_all(project.join(session).join("subagents")).unwrap();
        let parent = project.join(format!("{session}.jsonl"));
        fs::write(&parent, "{\"cwd\":\"/work/a\"}\n").unwrap();
        std::thread::sleep(Duration::from_millis(60));
        let since = Utc::now();
        std::thread::sleep(Duration::from_millis(60));
        // The subagent file is created after `since`, as when an old session resumes.
        let child = project
            .join(session)
            .join("subagents")
            .join("agent-1.jsonl");
        fs::write(&child, "{\"cwd\":\"/work/a\"}\n").unwrap();

        let scope = ImportScope::starting(since, "cli");
        assert!(!scope.admits_file(&parent));
        assert!(
            !scope.admits_file(&child),
            "an old session's subagent is old"
        );
    }

    #[test]
    fn signature_changes_with_every_dimension() {
        let base = scope_since("2026-09-30T12:00:00Z", &[]);
        let later = scope_since("2026-10-01T12:00:00Z", &[]);
        let with_project = scope_since("2026-09-30T12:00:00Z", &["/work/a"]);
        let all = ImportScope::all("cli");
        let signatures = [
            base.signature(),
            later.signature(),
            with_project.signature(),
            all.signature(),
        ];
        for (index, left) in signatures.iter().enumerate() {
            for right in &signatures[index + 1..] {
                assert_ne!(left, right);
            }
        }
    }
}
