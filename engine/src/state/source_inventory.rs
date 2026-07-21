//! Durable, privacy-safe inventory of locally discovered transcript sources.
//!
//! Paths never leave the discovery boundary. The persisted snapshot contains
//! only provider-level aggregates so it is safe to include in heartbeats.

use std::collections::BTreeMap;

use anyhow::Result;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::pending_source_envelope::StorageV2OutboxSnapshot;

const INVENTORY_SCHEMA_VERSION: u8 = 1;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderSourceInventory {
    pub provider: String,
    pub source_count: u64,
    pub source_bytes: u64,
    pub wal_bytes: u64,
    pub footprint_bytes: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub oldest_modified_at_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub newest_modified_at_ms: Option<i64>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SourceInventoryObservation {
    pub observed_at: String,
    pub scan_duration_ms: u64,
    pub scan_error_count: u64,
    pub source_count: u64,
    pub source_bytes: u64,
    pub wal_bytes: u64,
    pub footprint_bytes: u64,
    pub providers: Vec<ProviderSourceInventory>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceInventorySnapshot {
    pub schema_version: u8,
    pub generation: u64,
    pub content_sha256: String,
    pub observed_at: String,
    pub scan_duration_ms: u64,
    pub scan_error_count: u64,
    pub source_count: u64,
    pub source_bytes: u64,
    pub wal_bytes: u64,
    pub footprint_bytes: u64,
    pub providers: Vec<ProviderSourceInventory>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct ProviderHistoryProgress {
    pub provider: String,
    /// `bytes` for append-only files, `records` for SQLite-backed sources.
    pub unit: String,
    pub inventory_source_count: u64,
    pub inventory_source_bytes: u64,
    pub tracked_source_count: u64,
    pub complete_source_count: u64,
    pub observed_units: u64,
    pub acknowledged_units: u64,
    pub remaining_units: u64,
    /// True only when the reported observed units are a complete denominator.
    pub exact_total: bool,
    /// Whether every inventoried source has been reconciled into this cursor view.
    ///
    /// File providers can prove this from a clean inventory whose byte total is
    /// at least as fresh as the active epochs. SQLite providers require a later
    /// durable reconciliation checkpoint because inventory files and record
    /// epochs are not one-to-one.
    pub inventory_coverage_complete: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct HistoryImportProgress {
    pub acknowledged_source_bytes: u64,
    pub remaining_source_bytes: u64,
    pub acknowledged_records: u64,
    pub remaining_records: u64,
    pub pending_outbox_count: u64,
    pub pending_outbox_bytes: u64,
    pub blocked_source_count: u64,
    pub blocked_bytes: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latest_block_kind: Option<String>,
    pub providers: Vec<ProviderHistoryProgress>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HistoryImportSnapshot {
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inventory: Option<SourceInventorySnapshot>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub progress: Option<HistoryImportProgress>,
}

impl Default for HistoryImportSnapshot {
    fn default() -> Self {
        Self {
            state: "discovering".to_string(),
            inventory: None,
            progress: None,
        }
    }
}

impl HistoryImportSnapshot {
    pub fn load(conn: &Connection, outbox: &StorageV2OutboxSnapshot) -> Self {
        match load_inventory(conn) {
            Ok(Some(inventory)) => match load_progress(conn, &inventory, outbox) {
                Ok(progress) => Self {
                    state: if progress.blocked_source_count > 0 {
                        "blocked_source"
                    } else if progress_has_work(&progress) {
                        "importing"
                    } else {
                        "inventory_ready"
                    }
                    .to_string(),
                    inventory: Some(inventory),
                    progress: Some(progress),
                },
                Err(_) => Self::unavailable(),
            },
            Ok(None) => Self::default(),
            Err(_) => Self::unavailable(),
        }
    }

    fn unavailable() -> Self {
        Self {
            state: "unavailable".to_string(),
            inventory: None,
            progress: None,
        }
    }

    pub fn apply_runtime_state(
        &mut self,
        offline: bool,
        paused: bool,
        backpressured: bool,
        background_active: bool,
        archive_blocked: bool,
    ) {
        if self.inventory.is_none() || self.state == "unavailable" {
            return;
        }
        let progress = self.progress.as_ref();
        let blocked = archive_blocked || progress.is_some_and(|item| item.blocked_source_count > 0);
        let has_work = background_active || progress.is_some_and(progress_has_work);
        self.state = if blocked {
            "blocked_source"
        } else if offline && has_work {
            "offline"
        } else if paused && has_work {
            "paused"
        } else if backpressured && has_work {
            "backpressured"
        } else if has_work {
            "importing"
        } else {
            "inventory_ready"
        }
        .to_string();
    }
}

fn progress_has_work(progress: &HistoryImportProgress) -> bool {
    progress.pending_outbox_count > 0
        || progress.remaining_source_bytes > 0
        || progress.remaining_records > 0
        || progress
            .providers
            .iter()
            .any(|provider| !provider.inventory_coverage_complete)
}

#[derive(Default)]
struct EpochProgress {
    tracked_source_count: u64,
    complete_source_count: u64,
    observed_units: u64,
    acknowledged_units: u64,
    remaining_units: u64,
}

fn load_progress(
    conn: &Connection,
    inventory: &SourceInventorySnapshot,
    outbox: &StorageV2OutboxSnapshot,
) -> Result<HistoryImportProgress> {
    let mut stmt = conn.prepare(
        "SELECT epoch.provider,
                COUNT(*),
                COALESCE(SUM(CASE
                    WHEN COALESCE(lane.last_position, 0) >= epoch.max_observed_len THEN 1
                    ELSE 0 END), 0),
                COALESCE(SUM(epoch.max_observed_len), 0),
                COALESCE(SUM(CASE
                    WHEN COALESCE(lane.last_position, 0) < epoch.max_observed_len
                    THEN COALESCE(lane.last_position, 0)
                    ELSE epoch.max_observed_len END), 0),
                COALESCE(SUM(CASE
                    WHEN COALESCE(lane.last_position, 0) < epoch.max_observed_len
                    THEN epoch.max_observed_len - COALESCE(lane.last_position, 0)
                    ELSE 0 END), 0)
         FROM source_epoch_registry AS epoch
         LEFT JOIN source_epoch_lane_state AS lane
           ON lane.source_epoch = epoch.source_epoch AND lane.lane = 'durable'
         WHERE epoch.ended_at IS NULL
         GROUP BY epoch.provider",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            EpochProgress {
                tracked_source_count: row.get::<_, i64>(1)?.max(0) as u64,
                complete_source_count: row.get::<_, i64>(2)?.max(0) as u64,
                observed_units: row.get::<_, i64>(3)?.max(0) as u64,
                acknowledged_units: row.get::<_, i64>(4)?.max(0) as u64,
                remaining_units: row.get::<_, i64>(5)?.max(0) as u64,
            },
        ))
    })?;
    let mut epochs = BTreeMap::new();
    for row in rows {
        let (provider, progress) = row?;
        epochs.insert(provider, progress);
    }

    let mut providers = Vec::with_capacity(inventory.providers.len());
    let mut acknowledged_source_bytes = 0_u64;
    let mut remaining_source_bytes = 0_u64;
    let mut acknowledged_records = 0_u64;
    let mut remaining_records = 0_u64;
    for source in &inventory.providers {
        let epoch = epochs.remove(&source.provider).unwrap_or_default();
        let unit = provider_progress_unit(&source.provider);
        let (
            observed_units,
            acknowledged_units,
            remaining_units,
            exact_total,
            inventory_coverage_complete,
        ) = match unit {
            "bytes" => {
                // Inventory and epoch observation happen on independent ticks.
                // Never let an older inventory truncate newer durable work.
                let observed = source.source_bytes.max(epoch.observed_units);
                let acknowledged = epoch.acknowledged_units.min(observed);
                let remaining = observed.saturating_sub(acknowledged);
                let exact =
                    inventory.scan_error_count == 0 && source.source_bytes >= epoch.observed_units;
                acknowledged_source_bytes = acknowledged_source_bytes.saturating_add(acknowledged);
                remaining_source_bytes = remaining_source_bytes.saturating_add(remaining);
                (observed, acknowledged, remaining, exact, exact)
            }
            "records" => {
                acknowledged_records =
                    acknowledged_records.saturating_add(epoch.acknowledged_units);
                remaining_records = remaining_records.saturating_add(epoch.remaining_units);
                (
                    epoch.observed_units,
                    epoch.acknowledged_units,
                    epoch.remaining_units,
                    false,
                    false,
                )
            }
            _ => (
                epoch.observed_units,
                epoch.acknowledged_units,
                epoch.remaining_units,
                false,
                false,
            ),
        };
        providers.push(ProviderHistoryProgress {
            provider: source.provider.clone(),
            unit: unit.to_string(),
            inventory_source_count: source.source_count,
            inventory_source_bytes: source.source_bytes,
            tracked_source_count: epoch.tracked_source_count,
            complete_source_count: epoch.complete_source_count,
            observed_units,
            acknowledged_units,
            remaining_units,
            exact_total,
            inventory_coverage_complete,
        });
    }

    Ok(HistoryImportProgress {
        acknowledged_source_bytes,
        remaining_source_bytes,
        acknowledged_records,
        remaining_records,
        pending_outbox_count: outbox.pending_count,
        pending_outbox_bytes: outbox.pending_bytes,
        blocked_source_count: outbox.blocked_source_count,
        blocked_bytes: outbox.blocked_bytes,
        latest_block_kind: outbox.latest_block_kind.clone(),
        providers,
    })
}

fn provider_progress_unit(provider: &str) -> &'static str {
    match provider {
        "claude" | "codex" | "antigravity" | "cursor_acp" => "bytes",
        "cursor" | "opencode" => "records",
        _ => "unknown",
    }
}

pub fn persist_inventory(
    conn: &Connection,
    mut observation: SourceInventoryObservation,
) -> Result<SourceInventorySnapshot> {
    observation
        .providers
        .sort_by(|left, right| left.provider.cmp(&right.provider));
    let content_sha256 = content_digest(&observation)?;
    let previous: Option<(u64, String)> = conn
        .query_row(
            "SELECT generation, content_sha256 FROM source_inventory WHERE singleton_id = 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let generation = match previous {
        Some((generation, digest)) if digest == content_sha256 => generation,
        Some((generation, _)) => generation.saturating_add(1),
        None => 1,
    };
    let providers_json = serde_json::to_string(&observation.providers)?;
    conn.execute(
        "INSERT INTO source_inventory (
            singleton_id, schema_version, generation, content_sha256,
            observed_at, scan_duration_ms, scan_error_count, source_count,
            source_bytes, wal_bytes, footprint_bytes, providers_json
         ) VALUES (1, ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
         ON CONFLICT(singleton_id) DO UPDATE SET
            schema_version = excluded.schema_version,
            generation = excluded.generation,
            content_sha256 = excluded.content_sha256,
            observed_at = excluded.observed_at,
            scan_duration_ms = excluded.scan_duration_ms,
            scan_error_count = excluded.scan_error_count,
            source_count = excluded.source_count,
            source_bytes = excluded.source_bytes,
            wal_bytes = excluded.wal_bytes,
            footprint_bytes = excluded.footprint_bytes,
            providers_json = excluded.providers_json",
        params![
            INVENTORY_SCHEMA_VERSION,
            generation,
            content_sha256,
            observation.observed_at,
            observation.scan_duration_ms,
            observation.scan_error_count,
            observation.source_count,
            observation.source_bytes,
            observation.wal_bytes,
            observation.footprint_bytes,
            providers_json,
        ],
    )?;
    Ok(load_inventory(conn)?.expect("persisted source inventory must be readable"))
}

pub fn load_inventory(conn: &Connection) -> Result<Option<SourceInventorySnapshot>> {
    let row: Option<(
        u8,
        u64,
        String,
        String,
        u64,
        u64,
        u64,
        u64,
        u64,
        u64,
        String,
    )> = conn
        .query_row(
            "SELECT schema_version, generation, content_sha256, observed_at,
                    scan_duration_ms, scan_error_count, source_count,
                    source_bytes, wal_bytes, footprint_bytes, providers_json
             FROM source_inventory WHERE singleton_id = 1",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                    row.get(7)?,
                    row.get(8)?,
                    row.get(9)?,
                    row.get(10)?,
                ))
            },
        )
        .optional()?;
    row.map(
        |(
            schema_version,
            generation,
            content_sha256,
            observed_at,
            scan_duration_ms,
            scan_error_count,
            source_count,
            source_bytes,
            wal_bytes,
            footprint_bytes,
            providers_json,
        )| {
            Ok(SourceInventorySnapshot {
                schema_version,
                generation,
                content_sha256,
                observed_at,
                scan_duration_ms,
                scan_error_count,
                source_count,
                source_bytes,
                wal_bytes,
                footprint_bytes,
                providers: serde_json::from_str(&providers_json)?,
            })
        },
    )
    .transpose()
}

fn content_digest(observation: &SourceInventoryObservation) -> Result<String> {
    #[derive(Serialize)]
    struct Content<'a> {
        scan_error_count: u64,
        source_count: u64,
        source_bytes: u64,
        providers: Vec<StableProvider<'a>>,
    }

    #[derive(Serialize)]
    struct StableProvider<'a> {
        provider: &'a str,
        source_count: u64,
        source_bytes: u64,
    }

    let bytes = serde_json::to_vec(&Content {
        scan_error_count: observation.scan_error_count,
        source_count: observation.source_count,
        source_bytes: observation.source_bytes,
        providers: observation
            .providers
            .iter()
            .map(|provider| StableProvider {
                provider: &provider.provider,
                source_count: provider.source_count,
                source_bytes: provider.source_bytes,
            })
            .collect(),
    })?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db;

    fn observation(source_count: u64, observed_at: &str) -> SourceInventoryObservation {
        SourceInventoryObservation {
            observed_at: observed_at.to_string(),
            scan_duration_ms: 12,
            scan_error_count: 0,
            source_count,
            source_bytes: source_count * 100,
            wal_bytes: 0,
            footprint_bytes: source_count * 100,
            providers: vec![ProviderSourceInventory {
                provider: "claude".to_string(),
                source_count,
                source_bytes: source_count * 100,
                wal_bytes: 0,
                footprint_bytes: source_count * 100,
                oldest_modified_at_ms: Some(1),
                newest_modified_at_ms: Some(2),
            }],
        }
    }

    #[test]
    fn unchanged_content_keeps_generation_but_refreshes_observation() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = db::open_db(Some(tmp.path())).unwrap();
        let first = persist_inventory(&conn, observation(2, "2026-07-20T10:00:00Z")).unwrap();
        let second = persist_inventory(&conn, observation(2, "2026-07-20T10:05:00Z")).unwrap();
        assert_eq!(first.generation, 1);
        assert_eq!(second.generation, 1);
        assert_eq!(second.observed_at, "2026-07-20T10:05:00Z");
    }

    #[test]
    fn changed_content_advances_generation() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = db::open_db(Some(tmp.path())).unwrap();
        persist_inventory(&conn, observation(2, "2026-07-20T10:00:00Z")).unwrap();
        let changed = persist_inventory(&conn, observation(3, "2026-07-20T10:01:00Z")).unwrap();
        assert_eq!(changed.generation, 2);
    }

    #[test]
    fn volatile_wal_bytes_do_not_advance_generation_and_survive_restart() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = db::open_db(Some(tmp.path())).unwrap();
        let mut first = observation(2, "2026-07-20T10:00:00Z");
        first.wal_bytes = 50;
        first.footprint_bytes += 50;
        first.providers[0].wal_bytes = 50;
        first.providers[0].footprint_bytes += 50;
        persist_inventory(&conn, first).unwrap();

        let mut second = observation(2, "2026-07-20T10:05:00Z");
        second.wal_bytes = 500;
        second.footprint_bytes += 500;
        second.providers[0].wal_bytes = 500;
        second.providers[0].footprint_bytes += 500;
        let updated = persist_inventory(&conn, second).unwrap();
        assert_eq!(updated.generation, 1);
        assert_eq!(updated.wal_bytes, 500);
        drop(conn);

        let reopened = db::open_connection(tmp.path()).unwrap();
        let loaded = HistoryImportSnapshot::load(&reopened, &StorageV2OutboxSnapshot::default());
        assert_eq!(loaded.state, "importing");
        assert_eq!(loaded.inventory.unwrap().wal_bytes, 500);
    }

    #[test]
    fn progress_uses_bytes_for_files_and_records_for_sqlite_without_fake_percent() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = db::open_db(Some(tmp.path())).unwrap();
        persist_inventory(
            &conn,
            SourceInventoryObservation {
                observed_at: "2026-07-20T10:00:00Z".to_string(),
                scan_duration_ms: 1,
                scan_error_count: 0,
                source_count: 2,
                source_bytes: 6_000,
                wal_bytes: 0,
                footprint_bytes: 6_000,
                providers: vec![
                    ProviderSourceInventory {
                        provider: "codex".to_string(),
                        source_count: 1,
                        source_bytes: 1_000,
                        wal_bytes: 0,
                        footprint_bytes: 1_000,
                        oldest_modified_at_ms: None,
                        newest_modified_at_ms: None,
                    },
                    ProviderSourceInventory {
                        provider: "opencode".to_string(),
                        source_count: 1,
                        source_bytes: 5_000,
                        wal_bytes: 0,
                        footprint_bytes: 5_000,
                        oldest_modified_at_ms: None,
                        newest_modified_at_ms: None,
                    },
                ],
            },
        )
        .unwrap();
        for (epoch, provider, max_len, position) in [
            ("epoch-codex", "codex", 800_i64, 600_i64),
            ("epoch-opencode-a", "opencode", 10, 7),
            ("epoch-opencode-b", "opencode", 20, 20),
        ] {
            conn.execute(
                "INSERT INTO source_epoch_registry (
                    source_epoch, provider, opaque_source_id, file_incarnation,
                    predecessor_epoch, start_reason, max_observed_len, source_revision,
                    bound_session_id, created_at, updated_at, ended_at, end_reason
                 ) VALUES (?1, ?2, ?3, 'incarnation', NULL, 'initial', ?4, NULL,
                           NULL, '2026-07-20T10:00:00Z', '2026-07-20T10:00:00Z', NULL, NULL)",
                params![epoch, provider, format!("opaque-{epoch}"), max_len],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO source_epoch_lane_state (source_epoch, lane, last_position, updated_at)
                 VALUES (?1, 'durable', ?2, '2026-07-20T10:00:00Z')",
                params![epoch, position],
            )
            .unwrap();
        }

        let snapshot = HistoryImportSnapshot::load(&conn, &StorageV2OutboxSnapshot::default());
        assert_eq!(snapshot.state, "importing");
        let progress = snapshot.progress.unwrap();
        assert_eq!(progress.acknowledged_source_bytes, 600);
        assert_eq!(progress.remaining_source_bytes, 400);
        assert_eq!(progress.acknowledged_records, 27);
        assert_eq!(progress.remaining_records, 3);
        assert_eq!(progress.providers[0].unit, "bytes");
        assert!(progress.providers[0].exact_total);
        assert!(progress.providers[0].inventory_coverage_complete);
        assert_eq!(progress.providers[0].observed_units, 1_000);
        assert_eq!(progress.providers[1].unit, "records");
        assert!(!progress.providers[1].exact_total);
        assert!(!progress.providers[1].inventory_coverage_complete);
        assert_eq!(progress.providers[1].tracked_source_count, 2);
        assert_eq!(progress.providers[1].complete_source_count, 1);

        let encoded = serde_json::to_string(&progress).unwrap();
        assert!(!encoded.contains("opaque-"));
        assert!(!encoded.contains("path"));
        assert!(!encoded.contains("percent"));
    }

    #[test]
    fn sqlite_inventory_without_epochs_remains_importing_without_claiming_zero_is_complete() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = db::open_db(Some(tmp.path())).unwrap();
        persist_inventory(
            &conn,
            SourceInventoryObservation {
                observed_at: "2026-07-20T10:00:00Z".to_string(),
                scan_duration_ms: 1,
                scan_error_count: 0,
                source_count: 1,
                source_bytes: 5_000,
                wal_bytes: 100,
                footprint_bytes: 5_100,
                providers: vec![ProviderSourceInventory {
                    provider: "cursor".to_string(),
                    source_count: 1,
                    source_bytes: 5_000,
                    wal_bytes: 100,
                    footprint_bytes: 5_100,
                    oldest_modified_at_ms: None,
                    newest_modified_at_ms: None,
                }],
            },
        )
        .unwrap();

        let snapshot = HistoryImportSnapshot::load(&conn, &StorageV2OutboxSnapshot::default());
        assert_eq!(snapshot.state, "importing");
        let provider = &snapshot.progress.unwrap().providers[0];
        assert_eq!(provider.observed_units, 0);
        assert_eq!(provider.remaining_units, 0);
        assert_eq!(provider.tracked_source_count, 0);
        assert!(!provider.exact_total);
        assert!(!provider.inventory_coverage_complete);
    }

    #[test]
    fn newer_epoch_than_inventory_never_reports_an_exact_zero_remainder() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = db::open_db(Some(tmp.path())).unwrap();
        persist_inventory(&conn, observation(1, "2026-07-20T10:00:00Z")).unwrap();
        conn.execute(
            "INSERT INTO source_epoch_registry (
                source_epoch, provider, opaque_source_id, file_incarnation,
                predecessor_epoch, start_reason, max_observed_len, source_revision,
                bound_session_id, created_at, updated_at, ended_at, end_reason
             ) VALUES ('epoch-newer', 'claude', 'opaque-newer', 'incarnation', NULL,
                       'initial', 150, NULL, NULL, '2026-07-20T10:01:00Z',
                       '2026-07-20T10:01:00Z', NULL, NULL)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO source_epoch_lane_state (source_epoch, lane, last_position, updated_at)
             VALUES ('epoch-newer', 'durable', 100, '2026-07-20T10:01:00Z')",
            [],
        )
        .unwrap();

        let snapshot = HistoryImportSnapshot::load(&conn, &StorageV2OutboxSnapshot::default());
        assert_eq!(snapshot.state, "importing");
        let provider = &snapshot.progress.unwrap().providers[0];
        assert_eq!(provider.inventory_source_bytes, 100);
        assert_eq!(provider.observed_units, 150);
        assert_eq!(provider.acknowledged_units, 100);
        assert_eq!(provider.remaining_units, 50);
        assert!(!provider.exact_total);
        assert!(!provider.inventory_coverage_complete);
    }

    #[test]
    fn runtime_state_preserves_block_pause_and_offline_precedence() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = db::open_db(Some(tmp.path())).unwrap();
        persist_inventory(&conn, observation(1, "2026-07-20T10:00:00Z")).unwrap();
        let mut snapshot = HistoryImportSnapshot::load(
            &conn,
            &StorageV2OutboxSnapshot {
                blocked_source_count: 1,
                blocked_bytes: 10,
                latest_block_kind: Some("source_epoch_conflict".to_string()),
                ..StorageV2OutboxSnapshot::default()
            },
        );
        snapshot.apply_runtime_state(true, true, true, true, false);
        assert_eq!(snapshot.state, "blocked_source");

        let mut snapshot = HistoryImportSnapshot::load(&conn, &StorageV2OutboxSnapshot::default());
        snapshot.apply_runtime_state(false, true, false, true, false);
        assert_eq!(snapshot.state, "paused");
        snapshot.apply_runtime_state(true, false, false, true, false);
        assert_eq!(snapshot.state, "offline");
    }
}
