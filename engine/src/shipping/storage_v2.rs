//! Typed Machine Agent wire contracts for storage-v2 negotiation and receipts.

use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};

pub const STORAGE_V2_CAPABILITIES_PATH: &str = "/api/agents/storage/v2/capabilities";
pub const STORAGE_V2_ENVELOPES_PATH: &str = "/api/agents/storage/v2/envelopes";
pub const STORAGE_V2_SOURCE_EPOCHS_PATH: &str = "/api/agents/storage/v2/source-epochs";
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
    pub media_claim_path: String,
    pub media_upload_path_template: String,
    pub max_media_bytes: u64,
    pub max_media_claims: u64,
    pub range_kinds: Vec<String>,
    pub lanes: Vec<String>,
    pub lane_header: String,
}

impl StorageV2Capabilities {
    pub fn validate(&self, expected_machine_id: &str) -> Result<()> {
        if self.protocol_version != 2 {
            bail!(
                "Runtime Host returned unsupported storage protocol {}",
                self.protocol_version
            );
        }
        if self.tenant_id.is_empty() || self.machine_id != expected_machine_id {
            bail!("Runtime Host storage identity does not match this Machine Agent");
        }
        if self.ingest_path != STORAGE_V2_ENVELOPES_PATH
            || self.lane_header != STORAGE_V2_LANE_HEADER
            || self.max_wire_body_bytes < self.max_raw_record_bytes
            || self.max_raw_record_bytes == 0
            || self.max_raw_record_bytes > 32 * 1024 * 1024
            || self.max_records == 0
            || self.max_records > 10_000
            || self.media_claim_path != "/api/agents/storage/v2/media/claims"
            || self.media_upload_path_template != "/api/agents/storage/v2/media/{sha256}"
            || self.max_media_bytes == 0
            || self.max_media_bytes > 32 * 1024 * 1024
            || self.max_media_claims == 0
            || self.max_media_claims > 512
            || self.range_kinds != ["byte_offset", "record_ordinal"]
            || self.lanes != ["live", "repair"]
        {
            bail!("Runtime Host returned an incompatible storage-v2 capability contract");
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct StorageV2Record {
    pub source_position: u64,
    pub data_b64: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct StorageV2MediaRef {
    pub sha256: String,
    pub source_position: u64,
    pub ref_key: String,
    pub availability: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct StorageV2SessionFacts {
    /// Provider-native conversation identity observed in this source. This is
    /// routing evidence, not the Longhouse session identity.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_session_id: Option<String>,
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
    /// True when this transcript is an in-harness subagent rather than a
    /// session a human started. The Runtime Host classifies on this; without
    /// it, worker transcripts land in the timeline as first-class sessions.
    #[serde(default)]
    pub is_subagent: bool,
    /// Parent identity as the *provider* states it. This is not a Longhouse
    /// session id and must not be treated as one: a shipped session id can come
    /// from a managed binding override, so only the host can resolve provider
    /// identity to its own row, through the alias table it already maintains.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_provider_session_id: Option<String>,
    /// The parent tool call that spawned this subagent, when the provider says
    /// so. Also the idempotency key for the lineage edge across replays.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_tool_call_id: Option<String>,
    /// Fan-out run this subagent belongs to, for providers that group workers
    /// under one parent tool call.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workflow_run_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
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

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct StorageV2Render {
    pub generation_id: String,
    pub parser_revision: String,
    pub ordering_revision: String,
    pub records: Vec<StorageV2RenderRecord>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
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
    pub media: Vec<StorageV2MediaRef>,
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

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct StorageV2SourceManifest {
    pub v: u8,
    pub source_epoch: StorageV2SourceEpoch,
    pub objects: Vec<StorageV2SourceObject>,
    pub commit_seq: String,
    pub observed_at: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct StorageV2SourceEpoch {
    pub source_epoch: String,
    pub tenant_id: String,
    pub machine_id: String,
    pub provider: String,
    pub opaque_source_id: String,
    pub range_kind: String,
    pub state: String,
    pub predecessor_source_epoch: Option<String>,
    pub replaced_by_source_epoch: Option<String>,
    pub accepted_through: String,
    #[serde(default)]
    pub opened_at: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct StorageV2SourceObject {
    pub envelope_id: String,
    pub tenant_id: String,
    #[serde(default)]
    pub session_id: String,
    pub machine_id: String,
    pub provider: String,
    pub opaque_source_id: String,
    pub source_epoch: String,
    pub range_kind: String,
    pub range_start: String,
    pub range_end: String,
    pub retired_at: Option<String>,
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
            || !matches!(
                self.media_state.as_str(),
                "complete" | "pending" | "missing"
            )
        {
            bail!("Runtime Host returned an invalid storage-v2 durable receipt");
        }
        if self
            .missing_media_hashes
            .windows(2)
            .any(|pair| pair[0] >= pair[1])
            || self
                .missing_media_hashes
                .iter()
                .any(|value| !is_lower_sha256(value))
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

/// First Runtime Host release that serves storage-v2 with the cutover enabled.
/// `server/zerg/storage_v2/cutover.py` landed in b193fb439 and first shipped in
/// this tag; every host at or above it advertises `cutover: true`.
/// Not the first host that advertises cutover — that is v0.1.29, and steering a
/// user there is worse than saying nothing. This engine sends
/// `StorageV2SessionFacts.is_subagent`, which v0.1.29 through v0.1.36 reject as
/// an unknown field: the capability probe passes, the daemon starts, and then
/// every envelope 422s and quarantines its source. v0.1.37 (67aaca9a5) is the
/// first tag that accepts the envelope this engine actually sends.
pub const STORAGE_V2_MINIMUM_RUNTIME_HOST: &str = "v0.1.37";

/// Why a Runtime Host cannot accept transcripts from this Machine Agent.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StorageV2Unavailable {
    /// The host answered the capability probe but has not cut over.
    CutoverDisabled,
    /// The host has no `/api/agents/storage/v2/capabilities` route at all.
    NotAdvertised,
}

impl StorageV2Unavailable {
    fn observed(self) -> &'static str {
        match self {
            Self::CutoverDisabled => {
                "it advertises storage-v2 but answers the capability probe with cutover=false"
            }
            Self::NotAdvertised => {
                "it does not serve GET /api/agents/storage/v2/capabilities at all"
            }
        }
    }
}

/// The one message a user gets when the Runtime Host cannot take transcripts.
/// It has to be self-contained: it is what a launchd log line, a terminal exit,
/// and a support paste all carry, with no surrounding context.
pub fn runtime_host_too_old_message(api_url: &str, reason: StorageV2Unavailable) -> String {
    format!(
        "Runtime Host at {api_url} is too old for this Machine Agent: {observed}. \
         longhouse-engine {engine} ships transcripts only over storage-v2 — the legacy \
         /api/agents/ingest lane has been deleted from both the engine and the Runtime Host, \
         so there is nothing to fall back to and no transcript can be shipped to this host. \
         Nothing has been lost: your session files are untouched on disk, and the engine will \
         ship them from where it left off once the host can accept them. The Machine Agent \
         does not start at all against such a host, so remote control and machine presence \
         stop with it. \
         Fix: upgrade the Runtime Host to {minimum} or newer, then start the Machine Agent again.",
        observed = reason.observed(),
        engine = env!("CARGO_PKG_VERSION"),
        minimum = STORAGE_V2_MINIMUM_RUNTIME_HOST,
    )
}

/// Resolve a capability probe into the only transcript lane this Machine Agent
/// has. There is deliberately no second arm: a host without the storage-v2
/// cutover gets a loud refusal, never a shipping loop that silently drops
/// Source-tier history.
pub fn require_storage_v2_cutover(
    capabilities: Option<StorageV2Capabilities>,
    api_url: &str,
) -> Result<StorageV2Capabilities> {
    match capabilities {
        Some(capabilities) if capabilities.cutover => Ok(capabilities),
        Some(_) => bail!(
            "{}",
            runtime_host_too_old_message(api_url, StorageV2Unavailable::CutoverDisabled)
        ),
        None => bail!(
            "{}",
            runtime_host_too_old_message(api_url, StorageV2Unavailable::NotAdvertised)
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn capabilities(cutover: bool) -> StorageV2Capabilities {
        StorageV2Capabilities {
            protocol_version: 2,
            cutover,
            tenant_id: "tenant".to_string(),
            machine_id: "cinder".to_string(),
            ingest_path: STORAGE_V2_ENVELOPES_PATH.to_string(),
            max_wire_body_bytes: 8 * 1024 * 1024,
            max_raw_record_bytes: 4 * 1024 * 1024,
            max_records: 1000,
            media_claim_path: "/api/agents/storage/v2/media/claims".to_string(),
            media_upload_path_template: "/api/agents/storage/v2/media/{sha256}".to_string(),
            max_media_bytes: 32 * 1024 * 1024,
            max_media_claims: 512,
            range_kinds: vec!["byte_offset".to_string(), "record_ordinal".to_string()],
            lanes: vec!["live".to_string(), "repair".to_string()],
            lane_header: STORAGE_V2_LANE_HEADER.to_string(),
        }
    }

    #[test]
    fn a_cut_over_host_is_the_only_accepted_lane() {
        let resolved =
            require_storage_v2_cutover(Some(capabilities(true)), "https://longhouse.test").unwrap();
        assert!(resolved.cutover);
        assert_eq!(resolved.tenant_id, "tenant");
    }

    /// Both refusal arms have to be self-contained. This is the only thing a
    /// user sees when the daemon exits or `ship` fails, so it must name the
    /// host, say that nothing was lost, and give the fix — without the reader
    /// having to know that a v1 lane ever existed.
    #[test]
    fn every_refusal_names_the_host_the_safety_and_the_fix() {
        for capabilities in [None, Some(capabilities(false))] {
            let error = require_storage_v2_cutover(capabilities, "https://longhouse.test")
                .expect_err("a host without the storage-v2 cutover must be refused");
            let message = error.to_string();
            assert!(message.contains("https://longhouse.test"), "{message}");
            assert!(
                message.contains("too old for this Machine Agent"),
                "{message}"
            );
            assert!(message.contains("untouched on disk"), "{message}");
            // Against the constant, not a literal: a test that restates the
            // version can disagree with the code, and this one did.
            assert!(
                message.contains(&format!(
                    "upgrade the Runtime Host to {STORAGE_V2_MINIMUM_RUNTIME_HOST}"
                )),
                "{message}"
            );
        }
    }

    /// The two arms are different observations with the same fix, and the
    /// message has to say which one happened: "no such route" and "the route
    /// says cutover=false" are diagnosed differently even though both mean the
    /// host is behind.
    #[test]
    fn the_two_refusal_arms_are_distinguishable() {
        let absent = require_storage_v2_cutover(None, "https://longhouse.test")
            .unwrap_err()
            .to_string();
        let disabled =
            require_storage_v2_cutover(Some(capabilities(false)), "https://longhouse.test")
                .unwrap_err()
                .to_string();
        assert!(
            absent.contains("does not serve GET /api/agents/storage/v2/capabilities"),
            "{absent}"
        );
        assert!(disabled.contains("cutover=false"), "{disabled}");
        assert_ne!(absent, disabled);
    }

    #[test]
    fn capability_validation_refuses_contract_drift() {
        let valid = StorageV2Capabilities {
            protocol_version: 2,
            cutover: false,
            tenant_id: "david010".to_string(),
            machine_id: "cinder".to_string(),
            ingest_path: STORAGE_V2_ENVELOPES_PATH.to_string(),
            max_wire_body_bytes: 48 * 1024 * 1024,
            max_raw_record_bytes: 32 * 1024 * 1024,
            max_records: 10_000,
            media_claim_path: "/api/agents/storage/v2/media/claims".to_string(),
            media_upload_path_template: "/api/agents/storage/v2/media/{sha256}".to_string(),
            max_media_bytes: 32 * 1024 * 1024,
            max_media_claims: 512,
            range_kinds: vec!["byte_offset".to_string(), "record_ordinal".to_string()],
            lanes: vec!["live".to_string(), "repair".to_string()],
            lane_header: STORAGE_V2_LANE_HEADER.to_string(),
        };
        valid.validate("cinder").unwrap();
        let mut drift = valid;
        drift.max_raw_record_bytes += 1;
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
