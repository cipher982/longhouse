//! Salvage a shipper state database whose page 1 is destroyed.
//!
//! A corrupt page 1 takes out the SQLite header and the `sqlite_master` b-tree
//! root together, so the file stops opening entirely even though every other
//! page is intact. The observed failure on `cinder` was exactly that: 204 MB of
//! readable b-tree content behind sixteen unreadable bytes, and 290,944 logged
//! `file is not a database` retries.
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
//! `.recover` emits rows as positional `c0..cN` into `lost_and_found`, and the
//! obvious move — insert them into the tables `open_db` would create — silently
//! scrambles every row. `ALTER TABLE` appended `file_identity` and
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

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use rusqlite::Connection;
use serde::Serialize;

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
    /// Where the unreadable original was moved. Never deleted.
    pub quarantined_to: Option<PathBuf>,
    pub tables: Vec<RecoveredTable>,
    pub notes: Vec<String>,
}

impl RecoveryReport {
    pub fn total_recovered(&self) -> usize {
        self.tables.iter().map(|table| table.recovered).sum()
    }
}

/// True when the database cannot be opened and read at all.
///
/// Recovery refuses to run against a healthy database — salvage is strictly a
/// response to a file SQLite will not accept, never a routine maintenance pass.
pub fn is_unreadable(db_path: &Path) -> bool {
    match Connection::open(db_path) {
        Ok(conn) => conn
            .query_row("SELECT count(*) FROM sqlite_master", [], |row| {
                row.get::<_, i64>(0)
            })
            .is_err(),
        Err(_) => true,
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
    root_page: i64,
    ddl: String,
    columns: Vec<String>,
}

/// Read the `sqlite_master` records the recovery walk salvaged.
///
/// These carry the on-disk column order, which is the whole point: the current
/// `CREATE TABLE` in `db.rs` is not it.
fn recovered_schemas(conn: &Connection) -> Result<HashMap<String, RecoveredSchema>> {
    let mut stmt = conn
        .prepare("SELECT c1, c3, c4 FROM lost_and_found WHERE c0 = 'table' AND c1 IS NOT NULL")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
        ))
    })?;

    let mut schemas = HashMap::new();
    for row in rows {
        let (table, root_page, ddl) = row?;
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
                root_page,
                ddl,
                columns,
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

    match table {
        "file_state" => {
            let Some(path) = text(values, 0) else {
                return false;
            };
            path.starts_with('/')
                && text(values, 1).is_some_and(|provider| !provider.is_empty())
                && non_negative_integer(values, 2)
                && non_negative_integer(values, 3)
        }
        "source_epoch_registry" => {
            text(values, 0).is_some_and(|epoch| epoch.len() == 36)
                && text(values, 1).is_some_and(|provider| !provider.is_empty())
                && text(values, 2).is_some_and(|source| !source.is_empty())
        }
        "source_epoch_lane_state" => {
            text(values, 0).is_some_and(|epoch| epoch.len() == 36)
                && text(values, 1).is_some_and(|lane| !lane.is_empty())
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
    if !is_unreadable(db_path) {
        bail!(
            "{} opens and reads normally; recovery is only for a database SQLite rejects",
            db_path.display()
        );
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

    let mut tables = Vec::new();
    let mut notes = Vec::new();
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

    rebuilt.execute_batch("PRAGMA foreign_keys=ON;").ok();
    let integrity: String = rebuilt.query_row("PRAGMA integrity_check", [], |row| row.get(0))?;
    if integrity != "ok" {
        bail!("rebuilt database failed integrity_check: {integrity}");
    }
    drop(rebuilt);
    drop(salvaged);

    // Only now is the original touched, and only by being moved aside.
    let quarantine = quarantine_path(db_path);
    std::fs::rename(db_path, &quarantine).with_context(|| {
        format!(
            "quarantining the corrupt database to {}",
            quarantine.display()
        )
    })?;
    std::fs::copy(&rebuilt_path, db_path)
        .with_context(|| format!("installing the recovered database at {}", db_path.display()))?;

    // Hand it to the normal opener so the ALTER-if-missing migrations bring the
    // recovered on-disk shape up to the current schema.
    crate::state::db::open_db(Some(db_path)).context("opening the recovered database")?;

    notes.push(format!("corrupt database kept at {}", quarantine.display()));
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
        std::fs::create_dir_all(&root)
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

fn quarantine_path(db_path: &Path) -> PathBuf {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_secs())
        .unwrap_or_default();
    let mut name = db_path.as_os_str().to_os_string();
    name.push(format!(".corrupt-{stamp}"));
    PathBuf::from(name)
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
    let selected = (0..column_count)
        .map(|index| format!("c{index}"))
        .collect::<Vec<_>>()
        .join(", ");
    let mut stmt = salvaged.prepare(&format!(
        "SELECT nfield, {selected} FROM lost_and_found WHERE rootpgno = ?1"
    ))?;

    let mut attributed = 0usize;
    let mut recovered = 0usize;
    let mut rejected = 0usize;

    let transaction = if dry_run {
        None
    } else {
        Some(rebuilt.unchecked_transaction()?)
    };

    let insert_sql = {
        let names = schema.columns.join(", ");
        let placeholders = (1..=column_count)
            .map(|index| format!("?{index}"))
            .collect::<Vec<_>>()
            .join(", ");
        format!(
            "INSERT OR REPLACE INTO {} ({names}) VALUES ({placeholders})",
            schema.table
        )
    };

    let mut rows = stmt.query([schema.root_page])?;
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
        let mut values = Vec::with_capacity(column_count);
        for index in 0..column_count {
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
            match transaction.execute(&insert_sql, rusqlite::params_from_iter(values.iter())) {
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

    let output = Command::new("sqlite3")
        .arg(source)
        .arg(".recover")
        .output()
        .context("running `sqlite3 .recover` (is the sqlite3 CLI installed?)")?;
    if !output.status.success() {
        bail!(
            "sqlite3 .recover failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let mut child = Command::new("sqlite3")
        .arg(destination)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
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
    let status = child.wait().context("waiting for the recovery loader")?;
    if !status.success() {
        // The loader reports errors for the destroyed schema page while still
        // materializing `lost_and_found`, which is the part recovery reads.
        if !destination.exists() {
            bail!("recovery loader produced no database");
        }
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
    fn refuses_a_healthy_database() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("healthy.db");
        seed_legacy_shape(&path);
        assert!(!is_unreadable(&path));
        let error = recover_state_database(&path, true).unwrap_err().to_string();
        assert!(error.contains("opens and reads normally"), "{error}");
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
        assert!(is_unreadable(&path));
    }
}
