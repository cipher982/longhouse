//! Typed Machine Agent wire contracts for storage-v2 negotiation and receipts.

use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};

pub const STORAGE_V2_CAPABILITIES_PATH: &str = "/api/agents/storage/v2/capabilities";
pub const STORAGE_V2_ENVELOPES_PATH: &str = "/api/agents/storage/v2/envelopes";
pub const STORAGE_V2_LANE_HEADER: &str = "X-Longhouse-Storage-Lane";

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct StorageV2Capabilities {
    pub protocol_version: u8,
    pub cutover: bool,
    pub tenant_id: String,
    pub machine_id: String,
    pub ingest_path: String,
    pub max_wire_body_bytes: u64,
    pub max_raw_record_bytes: u64,
    pub max_records: u64,
    pub range_kinds: Vec<String>,
    pub lanes: Vec<String>,
    pub lane_header: String,
}

impl StorageV2Capabilities {
    pub fn validate(&self, expected_machine_id: &str) -> Result<()> {
        if self.protocol_version != 2 {
            bail!("Runtime Host returned unsupported storage protocol {}", self.protocol_version);
        }
        if self.tenant_id.is_empty() || self.machine_id != expected_machine_id {
            bail!("Runtime Host storage identity does not match this Machine Agent");
        }
        if self.ingest_path != STORAGE_V2_ENVELOPES_PATH
            || self.lane_header != STORAGE_V2_LANE_HEADER
            || self.max_wire_body_bytes < self.max_raw_record_bytes
            || self.max_raw_record_bytes == 0
            || self.max_raw_record_bytes > 4 * 1024 * 1024
            || self.max_records == 0
            || self.max_records > 10_000
            || self.range_kinds != ["byte_offset", "record_ordinal"]
            || self.lanes != ["live", "repair"]
        {
            bail!("Runtime Host returned an incompatible storage-v2 capability contract");
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct StorageV2Record {
    pub source_position: u64,
    pub data_b64: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct StorageV2SessionFacts {
    pub environment: String,
    pub project: Option<String>,
    pub cwd: Option<String>,
    pub git_repo: Option<String>,
    pub git_branch: Option<String>,
    pub started_at: String,
    pub last_activity_at: String,
    pub ended_at: Option<String>,
    pub origin_kind: Option<String>,
    pub hidden_from_default_timeline: bool,
    pub launch_actor: Option<String>,
    pub launch_surface: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct StorageV2RenderRecord {
    pub event_id: String,
    pub order_time_us: i64,
    pub source_position: u64,
    pub event_subordinal: u32,
    pub role: String,
    pub content_text: Option<String>,
    pub tool_name: Option<String>,
    pub tool_input_json: Option<serde_json::Value>,
    pub tool_output_text: Option<String>,
    pub tool_call_id: Option<String>,
    pub thread_id: Option<String>,
    pub branch_kind: Option<String>,
    pub raw_record_ordinal: usize,
}

#[derive(Clone, Debug, Serialize)]
pub struct StorageV2Render {
    pub generation_id: String,
    pub parser_revision: String,
    pub ordering_revision: String,
    pub records: Vec<StorageV2RenderRecord>,
}

#[derive(Clone, Debug, Serialize)]
pub struct StorageV2Envelope {
    pub protocol_version: u8,
    pub tenant_id: String,
    pub machine_id: String,
    pub session_id: String,
    pub provider: String,
    pub opaque_source_id: String,
    pub source_epoch: String,
    pub predecessor_source_epoch: Option<String>,
    pub epoch_opened_at: String,
    pub range_kind: String,
    pub range_start: u64,
    pub range_end: u64,
    pub render: Option<StorageV2Render>,
    pub session: StorageV2SessionFacts,
    pub records: Vec<StorageV2Record>,
    pub expected_envelope_id: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct StorageV2Receipt {
    pub v: u8,
    pub envelope_id: String,
    pub object_hash: String,
    pub commit_seq: String,
    pub raw_state: String,
    pub render_state: String,
    pub media_state: String,
    pub missing_media_hashes: Vec<String>,
}

impl StorageV2Receipt {
    pub fn validate(&self, expected_envelope_id: &str) -> Result<()> {
        if self.v != 2
            || self.envelope_id != expected_envelope_id
            || !is_lower_sha256(&self.envelope_id)
            || !is_lower_sha256(&self.object_hash)
            || self.commit_seq.parse::<u64>().is_err()
            || self.raw_state != "durable"
            || !matches!(self.render_state.as_str(), "ready" | "pending" | "failed")
            || !matches!(self.media_state.as_str(), "complete" | "pending" | "missing")
        {
            bail!("Runtime Host returned an invalid storage-v2 durable receipt");
        }
        if self.missing_media_hashes.windows(2).any(|pair| pair[0] >= pair[1])
            || self.missing_media_hashes.iter().any(|value| !is_lower_sha256(value))
            || (self.media_state == "complete" && !self.missing_media_hashes.is_empty())
            || (self.media_state == "missing" && self.missing_media_hashes.is_empty())
        {
            bail!("Runtime Host returned invalid storage-v2 media receipt state");
        }
        Ok(())
    }
}

fn is_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capability_validation_refuses_contract_drift() {
        let valid = StorageV2Capabilities {
            protocol_version: 2,
            cutover: false,
            tenant_id: "david010".to_string(),
            machine_id: "cinder".to_string(),
            ingest_path: STORAGE_V2_ENVELOPES_PATH.to_string(),
            max_wire_body_bytes: 6 * 1024 * 1024,
            max_raw_record_bytes: 4 * 1024 * 1024,
            max_records: 10_000,
            range_kinds: vec!["byte_offset".to_string(), "record_ordinal".to_string()],
            lanes: vec!["live".to_string(), "repair".to_string()],
            lane_header: STORAGE_V2_LANE_HEADER.to_string(),
        };
        valid.validate("cinder").unwrap();
        let mut drift = valid;
        drift.ingest_path = "/api/agents/ingest".to_string();
        assert!(drift.validate("cinder").is_err());
    }

    #[test]
    fn receipt_validation_requires_exact_identity_and_canonical_media() {
        let hash = "a".repeat(64);
        let receipt = StorageV2Receipt {
            v: 2,
            envelope_id: hash.clone(),
            object_hash: "b".repeat(64),
            commit_seq: "42".to_string(),
            raw_state: "durable".to_string(),
            render_state: "pending".to_string(),
            media_state: "complete".to_string(),
            missing_media_hashes: Vec::new(),
        };
        receipt.validate(&hash).unwrap();
        assert!(receipt.validate(&"c".repeat(64)).is_err());
    }
}
