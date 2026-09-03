//! Salvage a structurally corrupt shipper state database.
//!
//! Corruption can destroy page 1 and make the whole file unreadable, or damage
//! deeper table/index pages while leaving `sqlite_master` readable. Both have
//! occurred on `cinder`: the former hid 204 MB of readable b-tree content
//! behind sixteen unreadable bytes; the latter passed a schema probe while
//! ordinary reads failed with `SQLITE_CORRUPT` and `SQLITE_IOERR_SHORT_READ`.
//!
//! Wiping and starting fresh is the tempting move and the wrong one. The
//! storage-v2 cursors live here, and the host rejects a rewound range with
//! `source_epoch_conflict` rather than deduplicating it — so a fresh database
//! does not quietly re-sync, it re-mints epochs for the whole corpus. What must
//! survive is `source_epoch_lane_state.last_position` and the registry rows that
//! give it meaning.
//!
//! # Why the recovered DDL, and not the current schema
//!
//! When the schema is gone, `.recover` emits rows as positional `c0..cN` into
//! `lost_and_found`. When the schema survives, it recreates ordinary tables.
//! In both cases, inserting into the tables `open_db` would create can silently
//! scramble every row. `ALTER TABLE` appended `file_identity` and
//! `acked_cursor_fingerprint` to the *end* of `file_state` on disk, while the
//! current `CREATE TABLE` lists them in the *middle*:
//!
//! | position | on disk                | current CREATE             |
//! |----------|------------------------|----------------------------|
//! | c4       | `session_id`           | `file_identity`            |
//! | c6       | `last_updated`         | `session_id`               |
//!
//! Session ids would land in `file_identity`, timestamps in `session_id`, and
//! `PRAGMA integrity_check` would report a perfectly healthy database.
//! `source_epoch_registry` carries the same hazard across three columns.
//!
//! So recovery recreates each table from the DDL `.recover` found *on disk*,
//! inserts by that DDL's own column names, and only then hands the file to
//! `open_db`, whose `ALTER ADD`-if-missing migrations land the current shape.
//!
//! # Why root-page attribution is not enough
//!
//! SQLite reuses page numbers over a database's life, so `lost_and_found`
//! groups records from several tables under one `rootpgno`. On the real file,
//! `file_state`'s root page also held `source_epoch_registry` rows: 8,161
//! records attributed, 6,741 genuine. Every row is therefore validated against
//! the shape its table actually requires before it is kept, and the counts of
//! what was rejected are reported rather than swallowed.

use anyhow::{bail, Context, Result};
use rusqlite::Connection;
use serde::Serialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// SQLite's file magic, including its terminating NUL.
const SQLITE_MAGIC: &[u8] = b"SQLite format 3\0";

/// Tables worth salvaging, in load order.
///
/// Deliberately short. `spool_queue` and `pending_source_envelope` hold
/// in-flight work the host re-accepts on its own, and the Cursor store holds
/// BLOBs that would turn a 204 MB salvage into a larger and less trustworthy
/// file. Everything here is either a shipping cursor or the identity a cursor
/// is meaningless without.
const RECOVERED_TABLES: &[&str] = &[
    "source_epoch_registry",
    "source_epoch_lane_state",
    "file_state",
    "session_binding",
];

#[derive(Debug, Clone, Serialize)]
pub struct RecoveredTable {
    pub table: String,
    /// Rows `.recover` attributed to this table's root page.
    pub attributed: usize,
    /// Rows that survived validation and were written.
    pub recovered: usize,
    /// Rows rejected as belonging to another table (page reuse) or malformed.
    pub rejected: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct RecoveryReport {
    pub dry_run: bool,
    pub source_db: PathBuf,
    /// Where the unreadable original was moved. Retained for forensics
    /// and pruned by the daemon's daily retention lifecycle.
    pub quarantined_to: Option<PathBuf>,
    pub tables: Vec<RecoveredTable>,
    pub notes: Vec<String>,
}

impl RecoveryReport {
    pub fn total_recovered(&self) -> usize {
        self.tables.iter().map(|table| table.recovered).sum()
    }
}

/// True when SQLite reports structural corruption in the database.
///
/// A damaged leaf or index page can leave `sqlite_master` readable while normal
/// queries fail with `SQLITE_CORRUPT`. Check the full b-tree structure rather
/// than treating a readable schema as proof that the database is healthy.
/// Recovery still refuses locks, permissions errors, and other transient I/O.
pub fn needs_recovery(db_path: &Path) -> bool {
    fn is_corruption(error: &rusqlite::Error) -> bool {
        // Only SQLite saying "this is not a database" or "this database is
        // malformed" earns a destructive repair. A lock held by a live agent, a
        // permission problem, or a transient I/O error must never be answered by
        // rewriting the file — those are recoverable by doing nothing.
        matches!(
            error.sqlite_error_code(),
            Some(rusqlite::ErrorCode::NotADatabase) | Some(rusqlite::ErrorCode::DatabaseCorrupt)
        )
    }

    match Connection::open(db_path) {
        Ok(conn) => {
            match conn.query_row("PRAGMA quick_check(1)", [], |row| row.get::<_, String>(0)) {
                Ok(result) => result != "ok",
                Err(error) => is_corruption(&error),
            }
        }
        Err(error) => is_corruption(&error),
    }
}

/// Restore the file magic on a copy so SQLite will parse pages again.
///
/// Only the sixteen magic bytes are rewritten when the rest of the header still
/// looks plausible. Bytes 16..100 carry page size, text encoding and reserved
/// space; synthesizing those can make the recovery walk read the file at the
/// wrong page size and quietly produce garbage. A fully destroyed header is
/// reported rather than guessed at.
fn restore_magic(copy_path: &Path) -> Result<()> {
    use std::io::{Read, Seek, SeekFrom, Write};

    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(copy_path)
        .with_context(|| format!("opening recovery copy: {}", copy_path.display()))?;

    let mut header = [0u8; 100];
    file.read_exact(&mut header)
        .context("reading the first page header")?;

    let declared = u16::from_be_bytes([header[16], header[17]]);
    let declared_is_plausible =
        declared == 1 || (declared.is_power_of_two() && (512..=32768).contains(&declared));

    if declared_is_plausible {
        // The rest of the header survived; restore only the magic and leave
        // page size, encoding and reserved space exactly as written.
        file.seek(SeekFrom::Start(0))?;
        file.write_all(SQLITE_MAGIC)?;
        file.flush()?;
        return Ok(());
    }

    // The whole header is gone, so the page size has to be inferred from the
    // pages themselves rather than assumed. Synthesizing the wrong one makes
    // the recovery walk read the file at the wrong stride and produce coherent
    // looking garbage, which is the worst possible outcome here.
    let page_size = infer_page_size(copy_path)?;
    let donor = synthetic_header(page_size);
    file.seek(SeekFrom::Start(0))?;
    file.write_all(&donor)?;
    file.flush()?;
    Ok(())
}

/// Infer the page size by looking at what the pages actually are.
///
/// Every page starts with a b-tree page type byte, so the right stride lands on
/// one far more often than a wrong stride does. On the observed 204 MB file,
/// 4096 scored 0.67 against 0.08 for the next candidate — not a close call, and
/// the margin requirement below is what keeps it from becoming one.
fn infer_page_size(path: &Path) -> Result<u16> {
    use std::io::{Read, Seek, SeekFrom};

    const VALID_PAGE_TYPES: [u8; 4] = [0x02, 0x05, 0x0a, 0x0d];
    const SAMPLE_PAGES: u64 = 2_000;
    const MINIMUM_SCORE: f64 = 0.30;
    const MINIMUM_MARGIN: f64 = 3.0;

    let size = std::fs::metadata(path)?.len();
    let mut file = std::fs::File::open(path)?;
    let mut scores: Vec<(u16, f64)> = Vec::new();

    for candidate in [512u16, 1024, 2048, 4096, 8192, 16384, 32768] {
        let stride = u64::from(candidate);
        // A SQLite file is a whole number of pages.
        if size % stride != 0 {
            continue;
        }
        let pages = size / stride;
        let step = (pages / SAMPLE_PAGES).max(1);
        let mut hits = 0u64;
        let mut total = 0u64;
        let mut index = 1u64; // page 1 is the destroyed one
        while index < pages {
            if file.seek(SeekFrom::Start(index * stride)).is_err() {
                break;
            }
            let mut byte = [0u8; 1];
            if file.read_exact(&mut byte).is_err() {
                break;
            }
            total += 1;
            if VALID_PAGE_TYPES.contains(&byte[0]) {
                hits += 1;
            }
            index += step;
        }
        if total > 0 {
            scores.push((candidate, hits as f64 / total as f64));
        }
    }

    scores.sort_by(|left, right| right.1.total_cmp(&left.1));
    let Some(&(best, best_score)) = scores.first() else {
        bail!("cannot infer a page size: no candidate divides the file evenly");
    };
    let runner_up = scores.get(1).map(|entry| entry.1).unwrap_or(0.0);
    if best_score < MINIMUM_SCORE || best_score < runner_up * MINIMUM_MARGIN {
        bail!(
            "cannot infer a page size with confidence (best {best} scored {best_score:.3},              runner-up {runner_up:.3}); recovery will not guess"
        );
    }
    Ok(best)
}

/// A minimal, valid 100-byte header for a database of the given page size.
///
/// Only the fields SQLite needs to start walking pages are set; the recovery
/// walk rebuilds everything else from the pages themselves.
fn synthetic_header(page_size: u16) -> [u8; 100] {
    let mut header = [0u8; 100];
    header[..SQLITE_MAGIC.len()].copy_from_slice(SQLITE_MAGIC);
    header[16..18].copy_from_slice(&page_size.to_be_bytes());
    header[18] = 2; // write version: WAL
    header[19] = 2; // read version: WAL
    header[20] = 0; // reserved space per page
    header[21] = 64; // maximum embedded payload fraction
    header[22] = 32; // minimum embedded payload fraction
    header[23] = 32; // leaf payload fraction
    header[56..60].copy_from_slice(&1u32.to_be_bytes()); // text encoding: UTF-8
    header
}

/// One table's on-disk shape, as `.recover` found it.
struct RecoveredSchema {
    table: String,
    ddl: String,
    columns: Vec<String>,
    rows: RecoveredRows,
}

#[derive(Clone, Copy)]
enum RecoveredRows {
    /// The schema survived, so `.recover` recreated the table directly.
    Table,
    /// The schema was unreadable, so `.recover` attributed raw records by the
    /// original root page instead.
    LostAndFound { root_page: i64 },
}

/// Read the `sqlite_master` records the recovery walk salvaged.
///
/// These carry the on-disk column order, which is the whole point: the current
/// `CREATE TABLE` in `db.rs` is not it.
fn recovered_schemas(conn: &Connection) -> Result<HashMap<String, RecoveredSchema>> {
    let mut schemas = HashMap::new();

    // Partial corruption can leave page 1 and sqlite_master intact. In that
    // case `.recover` emits ordinary CREATE/INSERT statements and the loaded
    // salvage contains real tables. Prefer those rows: their original DDL
    // preserves the on-disk column order, just like the lost_and_found path.
    let mut direct = conn.prepare(
        "SELECT name, sql FROM sqlite_master
         WHERE type = 'table' AND sql IS NOT NULL",
    )?;
    let direct_rows = direct.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in direct_rows {
        let (table, ddl) = row?;
        if !RECOVERED_TABLES.contains(&table.as_str()) {
            continue;
        }
        let columns = parse_ddl_columns(&ddl);
        if columns.is_empty() {
            continue;
        }
        schemas.insert(
            table.clone(),
            RecoveredSchema {
                table,
                ddl,
                columns,
                rows: RecoveredRows::Table,
            },
        );
    }

    // A destroyed schema page has no direct tables. Fall back to the raw
    // records `.recover` places in lost_and_found, without replacing a direct
    // table when both forms are present.
    let has_lost_and_found: bool = conn.query_row(
        "SELECT EXISTS(
             SELECT 1 FROM sqlite_master
             WHERE type = 'table' AND name = 'lost_and_found'
         )",
        [],
        |row| row.get(0),
    )?;
    if !has_lost_and_found {
        return Ok(schemas);
    }
    let mut stmt = conn
        .prepare("SELECT c1, c3, c4 FROM lost_and_found WHERE c0 = 'table' AND c1 IS NOT NULL")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
        ))
    })?;

    for row in rows {
        let (table, root_page, ddl) = row?;
        if !RECOVERED_TABLES.contains(&table.as_str()) || schemas.contains_key(&table) {
            continue;
        }
        let columns = parse_ddl_columns(&ddl);
        if columns.is_empty() {
            continue;
        }
        schemas.insert(
            table.clone(),
            RecoveredSchema {
                table,
                ddl,
                columns,
                rows: RecoveredRows::LostAndFound { root_page },
            },
        );
    }
    Ok(schemas)
}

/// Column names from a `CREATE TABLE` body, in declaration order.
///
/// Bounded and deliberately simple: the shipper schema is plain columns with
/// type names and inline constraints, no table-level constraint clauses that
/// would need a real parser.
fn parse_ddl_columns(ddl: &str) -> Vec<String> {
    let Some(open) = ddl.find('(') else {
        return Vec::new();
    };
    let Some(close) = ddl.rfind(')') else {
        return Vec::new();
    };
    if close <= open {
        return Vec::new();
    }

    let mut columns = Vec::new();
    let mut depth = 0usize;
    let mut current = String::new();
    for character in ddl[open + 1..close].chars() {
        match character {
            '(' => {
                depth += 1;
                current.push(character);
            }
            ')' => {
                depth = depth.saturating_sub(1);
                current.push(character);
            }
            ',' if depth == 0 => {
                push_column_name(&mut columns, &current);
                current.clear();
            }
            _ => current.push(character),
        }
    }
    push_column_name(&mut columns, &current);
    columns
}

fn push_column_name(columns: &mut Vec<String>, clause: &str) {
    let trimmed = clause.trim();
    if trimmed.is_empty() {
        return;
    }
    let first = trimmed.split_whitespace().next().unwrap_or_default();
    let name = first.trim_matches(|c| c == '"' || c == '`' || c == '[' || c == ']');
    if name.is_empty() {
        return;
    }
    // Table-level constraints are not columns.
    let upper = name.to_ascii_uppercase();
    if matches!(
        upper.as_str(),
        "PRIMARY" | "UNIQUE" | "CHECK" | "FOREIGN" | "CONSTRAINT"
    ) {
        return;
    }
    columns.push(name.to_string());
}

/// Does this recovered row plausibly belong to this table?
///
/// Page reuse means `rootpgno` alone attributes foreign records to a table, so
/// every row is checked against what its table actually requires. The checks
/// are deliberately about *kind*, not value: a path is absolute, an offset is a
/// non-negative integer, an epoch id is uuid-shaped.
fn row_is_plausible(table: &str, values: &[rusqlite::types::Value]) -> bool {
    use rusqlite::types::Value;

    fn text(values: &[Value], index: usize) -> Option<&str> {
        match values.get(index) {
            Some(Value::Text(value)) => Some(value.as_str()),
            _ => None,
        }
    }
    fn non_negative_integer(values: &[Value], index: usize) -> bool {
        matches!(values.get(index), Some(Value::Integer(value)) if *value >= 0)
    }
    fn is_known_provider(provider: &str) -> bool {
        matches!(
            provider,
            "claude" | "codex" | "cursor" | "opencode" | "antigravity" | "gemini" | "pi"
        )
    }
    fn file_length(path: &str) -> Option<u64> {
        std::fs::metadata(path).ok().map(|meta| meta.len())
    }

    match table {
        "file_state" => {
            let Some(path) = text(values, 0) else {
                return false;
            };
            let (Some(Value::Integer(queued)), Some(Value::Integer(acked))) =
                (values.get(2), values.get(3))
            else {
                return false;
            };
            path.starts_with('/')
                && text(values, 1).is_some_and(is_known_provider)
                && *queued >= 0
                && *acked >= 0
                // A cursor claiming more acknowledged than queued, or more than
                // the file on disk holds, is not a cursor worth restoring — it
                // would tell the shipper to resume past bytes that do not exist.
                && *acked <= *queued
                && file_length(path).is_none_or(|length| *acked as u64 <= length)
        }
        "source_epoch_registry" => {
            text(values, 0).is_some_and(|epoch| epoch.len() == 36)
                && text(values, 1).is_some_and(|provider| !provider.is_empty())
                && text(values, 2).is_some_and(|source| !source.is_empty())
        }
        "source_epoch_lane_state" => {
            // `last_position` is the storage-v2 cursor itself. SQLite will store
            // text in an INTEGER column and `integrity_check` will still say ok,
            // so its type is checked here or nowhere.
            text(values, 0).is_some_and(|epoch| epoch.len() == 36)
                && text(values, 1).is_some_and(|lane| !lane.is_empty())
                && non_negative_integer(values, 2)
        }
        "session_binding" => text(values, 0).is_some_and(|key| !key.is_empty()),
        _ => false,
    }
}

/// Salvage `db_path` into a readable database.
///
/// The original is never modified: work happens on a copy, and the corrupt file
/// is moved aside — kept, not deleted — only once a validated replacement
/// exists. `dry_run` reports what each table would yield and writes nothing.
pub fn recover_state_database(db_path: &Path, dry_run: bool) -> Result<RecoveryReport> {
    if !db_path.exists() {
        bail!("no state database at {}", db_path.display());
    }
    if !needs_recovery(db_path) {
        bail!(
            "{} passes SQLite quick_check; recovery is only for a structurally corrupt database",
            db_path.display()
        );
    }

    // A `-wal` holds committed frames that live only there, and a `-shm` is
    // evidence of a connection that may still hold a writable fd — renaming the
    // path does not take that fd away. Recovery copies the main database only,
    // so proceeding past either would silently drop committed work or race a
    // writer. Both are the agent's to clear by stopping.
    for sidecar in ["-wal", "-shm"] {
        let mut path = db_path.as_os_str().to_os_string();
        path.push(sidecar);
        let path = PathBuf::from(path);
        if path.exists() {
            bail!(
                "{} exists: stop the Machine Agent before recovering, so no connection holds \
                 the database and no committed frames are left behind",
                path.display()
            );
        }
    }

    // Beside the database, not in the system temp dir: the final install is a
    // rename, which fails across filesystems, and a 204 MB copy has no business
    // crossing one.
    let workspace = RecoveryWorkspace::beside(db_path)?;
    let copy_path = workspace.path().join("source.db");
    std::fs::copy(db_path, &copy_path)
        .with_context(|| format!("copying {} for recovery", db_path.display()))?;
    restore_magic(&copy_path)?;

    let salvage_path = workspace.path().join("salvaged.db");
    run_recovery_walk(&copy_path, &salvage_path)?;

    let salvaged = Connection::open(&salvage_path).context("opening the salvage database")?;
    let schemas = recovered_schemas(&salvaged)?;

    let rebuilt_path = workspace.path().join("rebuilt.db");
    let mut rebuilt = Connection::open(&rebuilt_path).context("creating the rebuilt database")?;
    // Registry rows must land before the lanes that reference them, and the
    // recovered set is partial by design, so constraints stay off during load.
    rebuilt.execute_batch("PRAGMA foreign_keys=OFF;")?;

    let mut tables: Vec<RecoveredTable> = Vec::new();
    let mut notes: Vec<String> = Vec::new();
    for table_name in RECOVERED_TABLES {
        let Some(schema) = schemas.get(*table_name) else {
            notes.push(format!(
                "{table_name}: no schema record recovered; table will be recreated empty"
            ));
            tables.push(RecoveredTable {
                table: (*table_name).to_string(),
                attributed: 0,
                recovered: 0,
                rejected: 0,
            });
            continue;
        };
        let summary = recover_table(&salvaged, &mut rebuilt, schema, dry_run)?;
        tables.push(summary);
    }

    if dry_run {
        return Ok(RecoveryReport {
            dry_run: true,
            source_db: db_path.to_path_buf(),
            quarantined_to: None,
            tables,
            notes,
        });
    }

    // Install gate. A salvage that produced almost nothing is a worse database
    // than the corrupt one it would replace, because the corrupt one can be
    // recovered again and an installed empty one silently rewinds every cursor.
    // Nothing is allowed near the real path until every check below passes.
    let integrity: String = rebuilt.query_row("PRAGMA integrity_check", [], |row| row.get(0))?;
    if integrity != "ok" {
        bail!("rebuilt database failed integrity_check: {integrity}");
    }
    // Recovery is partial by nature: a lane whose registry row failed validation
    // references an epoch that is not here. Such a lane is unusable — the
    // shipper cannot interpret a position without the source identity it
    // belongs to — so drop it rather than refuse the other 27,000 that are
    // fine. Counted as rejected, because that is what happened to it.
    let orphaned_lanes = rebuilt.execute(
        "DELETE FROM source_epoch_lane_state
         WHERE source_epoch NOT IN (SELECT source_epoch FROM source_epoch_registry)",
        [],
    )?;
    if orphaned_lanes > 0 {
        if let Some(entry) = tables
            .iter_mut()
            .find(|table| table.table == "source_epoch_lane_state")
        {
            entry.recovered = entry.recovered.saturating_sub(orphaned_lanes);
            entry.rejected += orphaned_lanes;
        }
        notes.push(format!(
            "{orphaned_lanes} lane cursor(s) dropped: their source epoch did not survive validation"
        ));
    }

    rebuilt.execute_batch("PRAGMA foreign_keys=ON;").ok();
    let foreign_key_violations: i64 =
        rebuilt.query_row("SELECT count(*) FROM pragma_foreign_key_check", [], |row| {
            row.get(0)
        })?;
    if foreign_key_violations > 0 {
        bail!("rebuilt database still has {foreign_key_violations} foreign key violation(s) after pruning orphans");
    }
    let cursors_recovered = tables
        .iter()
        .find(|table| table.table == "source_epoch_lane_state")
        .map(|table| table.recovered)
        .unwrap_or(0);
    if cursors_recovered == 0 {
        bail!(
            "refusing to install a recovery with no shipping cursors: \
             source_epoch_lane_state came back empty"
        );
    }
    drop(rebuilt);
    drop(salvaged);

    // Stage the replacement beside its destination and flush it, so the final
    // step is a rename rather than a copy. A crash mid-copy would otherwise
    // leave a truncated database at the canonical path with the original
    // already moved away.
    let staged = staged_path(db_path);
    std::fs::copy(&rebuilt_path, &staged)
        .with_context(|| format!("staging the recovered database at {}", staged.display()))?;
    std::fs::File::open(&staged)?.sync_all().ok();

    let quarantine = quarantine_path(db_path);
    std::fs::rename(db_path, &quarantine).with_context(|| {
        format!(
            "quarantining the corrupt database to {}",
            quarantine.display()
        )
    })?;
    // Atomic within the directory: either the old path or the new one is there,
    // never a half-written file.
    if let Err(error) = std::fs::rename(&staged, db_path) {
        // Put the original back rather than leaving no database at all.
        let _ = std::fs::rename(&quarantine, db_path);
        return Err(error).with_context(|| {
            format!(
                "installing the recovered database at {}; the original was restored",
                db_path.display()
            )
        });
    }

    // Hand it to the normal opener so the ALTER-if-missing migrations bring the
    // recovered on-disk shape up to the current schema.
    crate::state::db::open_db(Some(db_path)).context("opening the recovered database")?;

    notes.push(format!(
        "corrupt database quarantined to {}",
        quarantine.display()
    ));
    Ok(RecoveryReport {
        dry_run: false,
        source_db: db_path.to_path_buf(),
        quarantined_to: Some(quarantine),
        tables,
        notes,
    })
}

/// A scratch directory beside the database, removed when recovery ends.
struct RecoveryWorkspace {
    root: PathBuf,
}

impl RecoveryWorkspace {
    fn beside(db_path: &Path) -> Result<Self> {
        let parent = db_path
            .parent()
            .context("state database has no parent directory")?;
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or_default();
        let root = parent.join(format!(
            ".longhouse-recovery-{}-{stamp}",
            std::process::id()
        ));
        // `create_dir` rather than `create_dir_all`: this must be a directory
        // this run owns. Adopting a pre-existing one would mean the Drop below
        // recursively deletes files that belong to somebody else.
        std::fs::create_dir(&root)
            .with_context(|| format!("creating recovery workspace {}", root.display()))?;
        Ok(Self { root })
    }

    fn path(&self) -> &Path {
        &self.root
    }
}

impl Drop for RecoveryWorkspace {
    fn drop(&mut self) {
        // Best effort: leaving scratch behind is untidy, failing recovery over
        // it would be worse.
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn staged_path(db_path: &Path) -> PathBuf {
    let mut name = db_path.as_os_str().to_os_string();
    name.push(format!(".recovered-{}", std::process::id()));
    PathBuf::from(name)
}

/// A quarantine name that cannot collide with an earlier one.
///
/// Second resolution alone would let a second recovery in the same second
/// overwrite the first quarantine — destroying the only copy of the evidence
/// this whole routine exists to preserve.
fn quarantine_path(db_path: &Path) -> PathBuf {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or_default();
    let mut candidate = {
        let mut name = db_path.as_os_str().to_os_string();
        name.push(format!(".corrupt-{stamp}"));
        PathBuf::from(name)
    };
    let mut attempt = 1u32;
    while candidate.exists() {
        let mut name = db_path.as_os_str().to_os_string();
        name.push(format!(".corrupt-{stamp}-{attempt}"));
        candidate = PathBuf::from(name);
        attempt += 1;
    }
    candidate
}
pub const MAX_QUARANTINE_AGE_DAYS: u64 = 7;
pub const MIN_PRESERVED_QUARANTINES: usize = 2;
pub const MAX_QUARANTINE_TOTAL_BYTES: u64 = 2 * 1024 * 1024 * 1024; // 2 GB

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct QuarantinePruneReport {
    pub deleted_files: usize,
    pub reclaimed_bytes: u64,
    pub retained_files: usize,
    pub retained_bytes: u64,
}

/// Identifies whether a directory entry is a quarantine/snapshot artifact for the target database.
pub fn is_quarantine_artifact_for(db_name: &str, file_name: &str) -> bool {
    if file_name == db_name
        || file_name == format!("{db_name}-wal")
        || file_name == format!("{db_name}-shm")
    {
        return false;
    }
    if !file_name.starts_with(db_name) {
        return false;
    }
    let suffix = &file_name[db_name.len()..];
    suffix.starts_with(".corrupt-")
        || suffix.starts_with(".pre-")
        || suffix.starts_with(".recovered-")
        || suffix.contains(".stale-")
}

pub fn prune_stale_quarantines(db_path: &Path) -> Result<QuarantinePruneReport> {
    let parent = match db_path.parent() {
        Some(dir) if dir.as_os_str().is_empty() => Path::new("."),
        Some(dir) => dir,
        None => Path::new("."),
    };
    let Some(db_name) = db_path.file_name().and_then(|name| name.to_str()) else {
        return Ok(QuarantinePruneReport::default());
    };

    let entries = match std::fs::read_dir(parent) {
        Ok(entries) => entries,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Ok(QuarantinePruneReport::default());
        }
        Err(err) => return Err(err).context("scanning directory for stale quarantines"),
    };

    let now = std::time::SystemTime::now();
    let max_age = Duration::from_secs(MAX_QUARANTINE_AGE_DAYS * 86400);

    struct Candidate {
        path: PathBuf,
        size: u64,
        mtime: std::time::SystemTime,
        age_eligible: bool,
    }

    let mut candidates = Vec::new();
    for entry in entries.flatten() {
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if !file_type.is_file() {
            continue;
        }
        let file_name = entry.file_name();
        let Some(name_str) = file_name.to_str() else {
            continue;
        };
        if !is_quarantine_artifact_for(db_name, name_str) {
            continue;
        }

        let Ok(meta) = entry.metadata() else {
            continue;
        };
        let size = meta.len();
        let mtime = meta.modified().unwrap_or(now);
        let age_eligible = match now.duration_since(mtime) {
            Ok(age) => age > max_age,
            Err(_) => false,
        };

        candidates.push(Candidate {
            path: entry.path(),
            size,
            mtime,
            age_eligible,
        });
    }

    candidates.sort_by(|a, b| b.mtime.cmp(&a.mtime));

    let mut to_delete = Vec::new();
    let mut retained = Vec::new();
    let mut total_retained_bytes = 0u64;

    for (index, candidate) in candidates.into_iter().enumerate() {
        if index < MIN_PRESERVED_QUARANTINES {
            total_retained_bytes = total_retained_bytes.saturating_add(candidate.size);
            retained.push(candidate);
        } else if candidate.age_eligible {
            to_delete.push(candidate);
        } else {
            total_retained_bytes = total_retained_bytes.saturating_add(candidate.size);
            retained.push(candidate);
        }
    }

    while total_retained_bytes > MAX_QUARANTINE_TOTAL_BYTES
        && retained.len() > MIN_PRESERVED_QUARANTINES
    {
        if let Some(oldest) = retained.pop() {
            total_retained_bytes = total_retained_bytes.saturating_sub(oldest.size);
            to_delete.push(oldest);
        } else {
            break;
        }
    }

    let mut report = QuarantinePruneReport {
        deleted_files: 0,
        reclaimed_bytes: 0,
        retained_files: retained.len(),
        retained_bytes: total_retained_bytes,
    };

    for item in to_delete {
        match std::fs::remove_file(&item.path) {
            Ok(()) => {
                report.deleted_files += 1;
                report.reclaimed_bytes = report.reclaimed_bytes.saturating_add(item.size);
            }
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => {
                tracing::warn!(
                    path = %item.path.display(),
                    error = %err,
                    "failed to delete stale quarantine file"
                );
                report.retained_files += 1;
                report.retained_bytes = report.retained_bytes.saturating_add(item.size);
            }
        }
    }

    Ok(report)
}

pub const VACUUM_FREELIST_THRESHOLD_BYTES: u64 = 50 * 1024 * 1024; // 50 MB

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CompactionReport {
    pub freelist_bytes_before: u64,
    pub freelist_bytes_after: u64,
    pub wal_checkpoint_busy: bool,
}

pub fn maybe_compact_database(db_path: &Path) -> Result<Option<CompactionReport>> {
    maybe_compact_database_with_threshold(db_path, VACUUM_FREELIST_THRESHOLD_BYTES)
}

pub fn maybe_compact_database_with_threshold(
    db_path: &Path,
    threshold_bytes: u64,
) -> Result<Option<CompactionReport>> {
    let conn = match crate::state::db::open_client_connection(db_path, Duration::from_millis(50)) {
        Ok(conn) => conn,
        Err(err) => {
            return Err(err).context("opening maintenance connection for vacuum");
        }
    };

    let freelist_pages: i64 = conn
        .query_row("PRAGMA freelist_count;", [], |row| row.get(0))
        .context("querying freelist_count")?;
    let page_size: i64 = conn
        .query_row("PRAGMA page_size;", [], |row| row.get(0))
        .context("querying page_size")?;

    let freelist_bytes = (freelist_pages.max(0) as u64).saturating_mul(page_size.max(0) as u64);
    if freelist_bytes < threshold_bytes {
        return Ok(None);
    }
    conn.execute("VACUUM;", []).context("executing VACUUM")?;

    let wal_checkpoint_busy = conn
        .query_row("PRAGMA wal_checkpoint(TRUNCATE);", [], |row| {
            let busy: i32 = row.get(0)?;
            Ok(busy != 0)
        })
        .unwrap_or(false);

    let freelist_pages_after: i64 = conn
        .query_row("PRAGMA freelist_count;", [], |row| row.get(0))
        .unwrap_or(0);
    let freelist_bytes_after =
        (freelist_pages_after.max(0) as u64).saturating_mul(page_size.max(0) as u64);

    Ok(Some(CompactionReport {
        freelist_bytes_before: freelist_bytes,
        freelist_bytes_after,
        wal_checkpoint_busy,
    }))
}

pub fn run_daily_storage_maintenance(db_path: &Path) {
    match prune_stale_quarantines(db_path) {
        Ok(report) if report.deleted_files > 0 => {
            tracing::info!(
                deleted_files = report.deleted_files,
                reclaimed_bytes = report.reclaimed_bytes,
                retained_files = report.retained_files,
                retained_bytes = report.retained_bytes,
                "Daily maintenance: pruned stale database quarantine snapshots"
            );
        }
        Ok(_) => {}
        Err(err) => {
            tracing::warn!(error = %err, "Daily maintenance: quarantine prune error");
        }
    }

    match maybe_compact_database(db_path) {
        Ok(Some(report)) => {
            let reclaimed = report
                .freelist_bytes_before
                .saturating_sub(report.freelist_bytes_after);
            tracing::info!(
                reclaimed_bytes = reclaimed,
                wal_checkpoint_busy = report.wal_checkpoint_busy,
                "Daily maintenance: compacted shipper database"
            );
        }
        Ok(None) => {}
        Err(err) => {
            tracing::warn!(error = %err, "Daily maintenance: database compaction deferred");
        }
    }
}

/// Recreate one table from its on-disk DDL and load its validated rows.
fn recover_table(
    salvaged: &Connection,
    rebuilt: &mut Connection,
    schema: &RecoveredSchema,
    dry_run: bool,
) -> Result<RecoveredTable> {
    if !dry_run {
        rebuilt
            .execute_batch(&schema.ddl)
            .with_context(|| format!("recreating {} from its recovered DDL", schema.table))?;
    }

    let column_count = schema.columns.len();
    let query = match schema.rows {
        RecoveredRows::Table => format!(
            "SELECT {column_count}, {} FROM {}",
            schema.columns.join(", "),
            schema.table
        ),
        RecoveredRows::LostAndFound { .. } => {
            let selected = (0..column_count)
                .map(|index| format!("c{index}"))
                .collect::<Vec<_>>()
                .join(", ");
            format!(
                "SELECT nfield, {selected} FROM lost_and_found \
                 WHERE rootpgno = ?1 ORDER BY pgno, id"
            )
        }
    };
    let mut stmt = salvaged.prepare(&query)?;

    let mut attributed = 0usize;
    let mut recovered = 0usize;
    let mut rejected = 0usize;

    let transaction = if dry_run {
        None
    } else {
        Some(rebuilt.unchecked_transaction()?)
    };

    // One statement per arity, so a pre-ALTER row inserts only the columns it
    // has. `OR IGNORE` rather than `OR REPLACE`: with duplicate keys the first
    // row wins deterministically instead of the last one silently deleting it.
    let insert_sql = |arity: usize| {
        let names = schema.columns[..arity].join(", ");
        let placeholders = (1..=arity)
            .map(|index| format!("?{index}"))
            .collect::<Vec<_>>()
            .join(", ");
        format!(
            "INSERT OR IGNORE INTO {} ({names}) VALUES ({placeholders})",
            schema.table
        )
    };

    let mut rows = match schema.rows {
        RecoveredRows::Table => stmt.query([])?,
        RecoveredRows::LostAndFound { root_page } => stmt.query([root_page])?,
    };
    let mut seen_short_rows = false;
    while let Some(row) = rows.next()? {
        attributed += 1;
        let nfield: i64 = row.get(0)?;
        // A row with more fields than this table has columns came from another
        // table that reused the page. One with fewer predates an ALTER and is
        // still genuine — its trailing columns stay NULL.
        if nfield > column_count as i64 {
            rejected += 1;
            continue;
        }
        // Bind only the fields the row actually carried. Binding the trailing
        // ones as NULL is not the same as omitting them: a column added later
        // with a NOT NULL default rejects an explicit NULL and takes the whole
        // row down with it, where an omitted column simply gets its default.
        let present = (nfield.max(0) as usize).min(column_count);
        if present < column_count {
            seen_short_rows = true;
        }
        let mut values = Vec::with_capacity(present);
        for index in 0..present {
            values.push(row.get::<_, rusqlite::types::Value>(index + 1)?);
        }
        if !row_is_plausible(&schema.table, &values) {
            rejected += 1;
            continue;
        }
        if let Some(transaction) = transaction.as_ref() {
            // Best effort per row. A salvage that aborts on one truncated
            // record out of 27,757 recovers nothing, so a row the recovered
            // schema rejects — a NOT NULL column that did not survive, a type
            // the column will not take — is counted and skipped rather than
            // failing the whole table.
            match transaction.execute(
                &insert_sql(values.len()),
                rusqlite::params_from_iter(values.iter()),
            ) {
                // `OR IGNORE` reports zero changed rows for a duplicate key
                // rather than erroring, and a row that changed nothing was not
                // recovered.
                Ok(0) => rejected += 1,
                Ok(_) => recovered += 1,
                Err(_) => rejected += 1,
            }
            continue;
        }
        recovered += 1;
    }

    if let Some(transaction) = transaction {
        transaction.commit()?;
    }

    if seen_short_rows {
        // Not an error: these predate a column being added, and their trailing
        // columns take defaults. Worth reporting so a thin recovery is not read
        // as a clean one.
        tracing::info!(
            table = %schema.table,
            "recovered rows from an older on-disk shape; trailing columns took their defaults"
        );
    }

    Ok(RecoveredTable {
        table: schema.table.clone(),
        attributed,
        recovered,
        rejected,
    })
}

/// Run SQLite's recovery walk, writing the salvage into a fresh database.
///
/// Shells the `sqlite3` CLI because `.recover` is a shell command implemented in
/// the CLI, not a library API rusqlite exposes.
fn run_recovery_walk(source: &Path, destination: &Path) -> Result<()> {
    use std::process::Command;

    // `--ignore-freelist` keeps deleted rows out of the salvage. Without it a
    // historical `file_state` row — an old, smaller offset for a path that has
    // since advanced — is indistinguishable from the live one and can win the
    // insert, rewinding a cursor to a position the host has already accepted.
    let output = Command::new("sqlite3")
        .arg(source)
        .arg(".recover --ignore-freelist")
        .output()
        .context("running `sqlite3 .recover` (is the sqlite3 CLI installed?)")?;
    if !output.status.success() {
        bail!(
            "sqlite3 .recover failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    if output.stdout.is_empty() {
        bail!("sqlite3 .recover produced no output");
    }

    // `output()` rather than a hand-managed pipe: an undrained stderr pipe
    // deadlocks `write_all` once the loader emits enough errors, and the
    // destroyed schema page guarantees it emits some.
    let mut child = Command::new("sqlite3")
        .arg(destination)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .context("loading the recovery walk output")?;
    {
        use std::io::Write;
        let stdin = child
            .stdin
            .as_mut()
            .context("recovery loader stdin unavailable")?;
        stdin.write_all(&output.stdout)?;
    }
    child.wait().context("waiting for the recovery loader")?;

    // The loader can report errors for corrupt pages, so its exit status alone
    // says nothing. What matters is whether either the raw recovery table or a
    // directly recreated target table came out the other side.
    let salvaged = Connection::open(destination).context("opening the salvage database")?;
    let raw_rows = salvaged
        .query_row("SELECT count(*) FROM lost_and_found", [], |row| {
            row.get::<_, i64>(0)
        })
        .unwrap_or(0);
    let direct_tables: i64 = salvaged.query_row(
        "SELECT count(*) FROM sqlite_master
         WHERE type = 'table'
           AND name IN ('source_epoch_registry', 'source_epoch_lane_state',
                        'file_state', 'session_binding')",
        [],
        |row| row.get(0),
    )?;
    if raw_rows == 0 && direct_tables == 0 {
        bail!("the recovery walk recovered no rows");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a database whose on-disk column order differs from the current
    /// schema, exactly as ALTER-appended columns do on the real file.
    fn seed_legacy_shape(path: &Path) {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE file_state (
                path TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                queued_offset INTEGER NOT NULL DEFAULT 0,
                acked_offset INTEGER NOT NULL DEFAULT 0,
                session_id TEXT,
                provider_session_id TEXT,
                last_updated TEXT NOT NULL
            , file_identity TEXT, acked_cursor_fingerprint TEXT);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_state VALUES ('/tmp/a.jsonl','claude',10,10,'sess-1','prov-1','2026-08-25T00:00:00Z','ident-1','fp-1')",
            [],
        )
        .unwrap();
    }

    #[test]
    fn parses_columns_in_on_disk_order_including_altered_tail() {
        let ddl = "CREATE TABLE file_state (\n path TEXT PRIMARY KEY,\n provider TEXT NOT NULL,\n \
                   queued_offset INTEGER NOT NULL DEFAULT 0,\n acked_offset INTEGER NOT NULL DEFAULT 0,\n \
                   session_id TEXT,\n provider_session_id TEXT,\n last_updated TEXT NOT NULL\n \
                   , file_identity TEXT, acked_cursor_fingerprint TEXT)";
        assert_eq!(
            parse_ddl_columns(ddl),
            vec![
                "path",
                "provider",
                "queued_offset",
                "acked_offset",
                "session_id",
                "provider_session_id",
                "last_updated",
                "file_identity",
                "acked_cursor_fingerprint",
            ]
        );
    }

    /// The bug this module exists to avoid: the ALTERed columns sit at the end
    /// on disk and in the middle in the current schema, so a positional load
    /// would put session ids in `file_identity`.
    #[test]
    fn on_disk_order_differs_from_the_current_schema() {
        let on_disk = parse_ddl_columns(
            "CREATE TABLE file_state (path TEXT, provider TEXT, queued_offset INTEGER, \
             acked_offset INTEGER, session_id TEXT, provider_session_id TEXT, last_updated TEXT, \
             file_identity TEXT, acked_cursor_fingerprint TEXT)",
        );
        let current = parse_ddl_columns(
            "CREATE TABLE file_state (path TEXT, provider TEXT, queued_offset INTEGER, \
             acked_offset INTEGER, file_identity TEXT, acked_cursor_fingerprint TEXT, \
             session_id TEXT, provider_session_id TEXT, last_updated TEXT)",
        );
        assert_ne!(on_disk, current);
        assert_eq!(on_disk[4], "session_id");
        assert_eq!(current[4], "file_identity");
    }

    #[test]
    fn rejects_rows_from_another_table_sharing_a_root_page() {
        use rusqlite::types::Value;
        // A genuine file_state row.
        assert!(row_is_plausible(
            "file_state",
            &[
                Value::Text("/Users/d/.claude/x.jsonl".into()),
                Value::Text("claude".into()),
                Value::Integer(84444),
                Value::Integer(84444),
            ]
        ));
        // A source_epoch_registry row that page reuse attributed to file_state.
        assert!(!row_is_plausible(
            "file_state",
            &[
                Value::Text("e3993a95-c688-462f-9899-322b89893b96".into()),
                Value::Text("codex".into()),
                Value::Text("path-sha256:b3cf27".into()),
                Value::Text("macos-file-v1:334554535".into()),
            ]
        ));
    }

    #[test]
    fn recovers_direct_table_rows_when_the_schema_survives() {
        let dir = tempfile::tempdir().unwrap();
        let salvage_path = dir.path().join("salvage.db");
        seed_legacy_shape(&salvage_path);
        let salvage = Connection::open(&salvage_path).unwrap();
        let schemas = recovered_schemas(&salvage).unwrap();
        let schema = schemas.get("file_state").unwrap();
        assert!(matches!(schema.rows, RecoveredRows::Table));

        let rebuilt_path = dir.path().join("rebuilt.db");
        let mut rebuilt = Connection::open(rebuilt_path).unwrap();
        let report = recover_table(&salvage, &mut rebuilt, schema, false).unwrap();
        assert_eq!(report.attributed, 1);
        assert_eq!(report.recovered, 1);
        assert_eq!(report.rejected, 0);
        let recovered_session: String = rebuilt
            .query_row(
                "SELECT session_id FROM file_state WHERE path = '/tmp/a.jsonl'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(recovered_session, "sess-1");
    }

    #[test]
    fn refuses_a_healthy_database() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("healthy.db");
        seed_legacy_shape(&path);
        assert!(!needs_recovery(&path));
        let error = recover_state_database(&path, true).unwrap_err().to_string();
        assert!(error.contains("passes SQLite quick_check"), "{error}");
    }

    #[test]
    fn detects_a_destroyed_header() {
        use std::io::{Seek, SeekFrom, Write};
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("corrupt.db");
        seed_legacy_shape(&path);
        let mut file = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
        file.seek(SeekFrom::Start(0)).unwrap();
        file.write_all(&[0u8; 16]).unwrap();
        file.flush().unwrap();
        drop(file);
        assert!(needs_recovery(&path));
    }

    #[test]
    fn detects_corruption_beyond_the_readable_schema() {
        use std::io::{Seek, SeekFrom, Write};

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("partially-corrupt.db");
        let root_page = {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                "PRAGMA journal_mode=DELETE;
                 CREATE TABLE damaged (value TEXT);
                 INSERT INTO damaged VALUES ('still has a readable schema');",
            )
            .unwrap();
            conn.query_row(
                "SELECT rootpage FROM sqlite_master WHERE name = 'damaged'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap()
        };

        let mut file = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
        file.seek(SeekFrom::Start((root_page as u64 - 1) * 4096))
            .unwrap();
        file.write_all(&[0xff]).unwrap();
        file.flush().unwrap();

        assert!(needs_recovery(&path));
    }

    #[test]
    fn quarantine_artifact_matcher_identifies_matching_files() {
        let db = "longhouse-shipper.db";
        assert!(!is_quarantine_artifact_for(db, "longhouse-shipper.db"));
        assert!(!is_quarantine_artifact_for(db, "longhouse-shipper.db-wal"));
        assert!(!is_quarantine_artifact_for(db, "longhouse-shipper.db-shm"));
        assert!(!is_quarantine_artifact_for(db, "other.db.corrupt-123"));
        assert!(!is_quarantine_artifact_for(db, "random-file.txt"));

        assert!(is_quarantine_artifact_for(
            db,
            "longhouse-shipper.db.corrupt-1787804634"
        ));
        assert!(is_quarantine_artifact_for(
            db,
            "longhouse-shipper.db.corrupt-1788242433141423000"
        ));
        assert!(is_quarantine_artifact_for(
            db,
            "longhouse-shipper.db.pre-epoch-rebind-20260831T2043Z"
        ));
        assert!(is_quarantine_artifact_for(
            db,
            "longhouse-shipper.db.pre-final-reconcile-20260831T2256Z"
        ));
        assert!(is_quarantine_artifact_for(
            db,
            "longhouse-shipper.db-shm.stale-20260831T2047Z"
        ));
        assert!(is_quarantine_artifact_for(
            db,
            "longhouse-shipper.db-wal.stale-20260831T2047Z"
        ));
    }

    #[test]
    fn prune_stale_quarantines_preserves_newest_and_deletes_aged_files() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("state.db");
        std::fs::write(&db_path, b"active database").unwrap();

        let now = std::time::SystemTime::now();
        let old_time = now - Duration::from_secs(10 * 86400); // 10 days old
        let recent_time = now - Duration::from_secs(1 * 86400); // 1 day old

        let old_1 = dir.path().join("state.db.corrupt-old1");
        std::fs::write(&old_1, &[1u8; 1000]).unwrap();
        let times = std::fs::FileTimes::new().set_modified(old_time);
        std::fs::File::options()
            .write(true)
            .open(&old_1)
            .unwrap()
            .set_times(times)
            .unwrap();

        let old_2 = dir.path().join("state.db.corrupt-old2");
        std::fs::write(&old_2, &[2u8; 2000]).unwrap();
        let times = std::fs::FileTimes::new().set_modified(old_time);
        std::fs::File::options()
            .write(true)
            .open(&old_2)
            .unwrap()
            .set_times(times)
            .unwrap();

        let old_3 = dir.path().join("state.db.corrupt-old3");
        std::fs::write(&old_3, &[3u8; 3000]).unwrap();
        let times = std::fs::FileTimes::new().set_modified(old_time);
        std::fs::File::options()
            .write(true)
            .open(&old_3)
            .unwrap()
            .set_times(times)
            .unwrap();

        let recent = dir.path().join("state.db.corrupt-recent");
        std::fs::write(&recent, &[4u8; 4000]).unwrap();
        let times = std::fs::FileTimes::new().set_modified(recent_time);
        std::fs::File::options()
            .write(true)
            .open(&recent)
            .unwrap()
            .set_times(times)
            .unwrap();

        let report = prune_stale_quarantines(&db_path).unwrap();

        // The 2 newest files are preserved (recent and one of the old ones)
        // The remaining 2 old files (> 7 days) are deleted.
        assert_eq!(report.deleted_files, 2);
        assert_eq!(report.retained_files, 2);
        assert!(db_path.is_file(), "active database must never be deleted");
        assert!(recent.is_file(), "recent file must be preserved");
    }

    #[test]
    fn maybe_compact_database_reports_none_below_threshold() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("clean.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("CREATE TABLE t (x TEXT); INSERT INTO t VALUES ('hello');")
            .unwrap();
        drop(conn);

        let report = maybe_compact_database(&db_path).unwrap();
        assert!(
            report.is_none(),
            "clean database with small freelist should not trigger vacuum"
        );
    }
    #[test]
    fn maybe_compact_database_vacuums_when_above_threshold() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("bloated.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             CREATE TABLE t (x TEXT);",
        )
        .unwrap();

        for i in 0..500 {
            conn.execute(
                "INSERT INTO t VALUES (?1)",
                [format!("row-{i}-padding-{}", "x".repeat(200))],
            )
            .unwrap();
        }
        conn.execute("DELETE FROM t WHERE rowid > 5", []).unwrap();
        drop(conn);

        let report = maybe_compact_database_with_threshold(&db_path, 1000).unwrap();
        assert!(
            report.is_some(),
            "expected compaction to trigger above threshold"
        );
        let report = report.unwrap();
        assert!(report.freelist_bytes_before > 0);
        assert!(report.freelist_bytes_after <= report.freelist_bytes_before);
    }
}
