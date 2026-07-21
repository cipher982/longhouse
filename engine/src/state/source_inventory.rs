//! Durable, privacy-safe inventory of locally discovered transcript sources.
//!
//! Paths never leave the discovery boundary. The persisted snapshot contains
//! only provider-level aggregates so it is safe to include in heartbeats.

use anyhow::Result;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

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

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HistoryImportSnapshot {
    pub state: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inventory: Option<SourceInventorySnapshot>,
}

impl Default for HistoryImportSnapshot {
    fn default() -> Self {
        Self {
            state: "discovering",
            inventory: None,
        }
    }
}

impl HistoryImportSnapshot {
    pub fn load(conn: &Connection) -> Self {
        match load_inventory(conn) {
            Ok(Some(inventory)) => Self {
                state: "inventory_ready",
                inventory: Some(inventory),
            },
            Ok(None) => Self::default(),
            Err(_) => Self {
                state: "unavailable",
                inventory: None,
            },
        }
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
    let row: Option<(u8, u64, String, String, u64, u64, u64, u64, u64, u64, String)> = conn
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
        let loaded = HistoryImportSnapshot::load(&reopened);
        assert_eq!(loaded.state, "inventory_ready");
        assert_eq!(loaded.inventory.unwrap().wal_bytes, 500);
    }
}
