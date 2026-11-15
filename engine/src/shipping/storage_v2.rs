//! Typed Machine Agent wire contracts for storage-v2 negotiation and receipts.

use anyhow::{bail, Context, Result};
use base64::prelude::{Engine as _, BASE64_STANDARD};
use serde::{Deserialize, Serialize};

use crate::pipeline::items::ItemFacts;

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
    /// Envelope body encodings the Runtime Host accepts. Absent on older
    /// hosts, which accept identity only.
    #[serde(default)]
    pub envelope_content_encodings: Vec<String>,
    /// Raw record encodings the Runtime Host accepts. Absent on older hosts,
    /// which accept base64 only.
    #[serde(default)]
    pub envelope_record_encodings: Vec<String>,
    /// Where a machine with no local shipping state asks what the host already
    /// holds for its sources. Absent on hosts without the endpoint, which keep
    /// today's behavior: a fresh epoch from position zero.
    #[serde(default)]
    pub source_resume_path: Option<String>,
    /// The Runtime Host builds the render from raw and the capture witness
    /// (host-normalized-transcripts Phase 4), so a witnessed envelope need
    /// not carry the laptop's render. Absent on older hosts.
    #[serde(default)]
    pub host_render: bool,
    /// The host validates and serves canonical item facts on render records
    /// (host-normalized-transcripts Phase 5). An older host rejects the
    /// field, so the engine strips it there.
    #[serde(default)]
    pub render_item_facts: bool,
}

impl StorageV2Capabilities {
    pub fn validate(&self, expected_machine_id: &str) -> Result<()> {
        if self.protocol_version != 2 {
            bail!(
                "Runtime Host returned unsupported storage protocol {}",
                self.protocol_version
            );
        }
        if self.tenant_id.is_empty() {
            bail!("Runtime Host storage identity does not match this Machine Agent");
        }
        if self.machine_id != expected_machine_id {
            return Err(MachineIdentityMismatch {
                configured: expected_machine_id.to_string(),
                token_device: self.machine_id.clone(),
            }
            .into());
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

    /// Whether this host accepts raw records as UTF-8 text (`data`).
    pub fn accepts_text_records(&self) -> bool {
        self.envelope_record_encodings
            .iter()
            .any(|encoding| encoding == "utf8")
    }

    /// The encoding for envelope bodies on this host. The engine stores every
    /// envelope as zstd, so a host that accepts zstd receives the stored bytes
    /// as-is and the wire carries several times fewer bytes.
    pub fn envelope_body_encoding(&self) -> StorageV2BodyEncoding {
        if self
            .envelope_content_encodings
            .iter()
            .any(|encoding| encoding == "zstd")
        {
            StorageV2BodyEncoding::Zstd
        } else {
            StorageV2BodyEncoding::Identity
        }
    }
}

/// The Runtime Host binds a machine to the device its token was issued for,
/// so a Machine Agent configured under any other name can never ship. Retrying
/// cannot change that; only re-adopting the token's name can.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MachineIdentityMismatch {
    /// The name this Machine Agent is configured as (`machine/state.json`).
    pub configured: String,
    /// The device the stored token belongs to on the Runtime Host.
    pub token_device: String,
}

impl std::fmt::Display for MachineIdentityMismatch {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "Runtime Host storage identity does not match this Machine Agent: this machine is configured as \"{}\", but its device token belongs to \"{}\". Fix: {}",
            self.configured,
            self.token_device,
            crate::config::adopt_device_identity_command()
        )
    }
}

impl std::error::Error for MachineIdentityMismatch {}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StorageV2BodyEncoding {
    Identity,
    Zstd,
}

impl StorageV2BodyEncoding {
    pub fn header_value(self) -> &'static str {
        match self {
            Self::Identity => "identity",
            Self::Zstd => "zstd",
        }
    }
}

/// One source record with its exact bytes. Valid UTF-8 (every JSON-lines
/// record) travels as a string in `data`; anything else as base64 in
/// `data_b64`. Exactly one is set. Base64 inside the zstd envelope halved its
/// compression (5.2x to 2.6x on captured envelopes), so text is the default and
/// `wire_body` converts to base64 only for a host that predates text records.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct StorageV2Record {
    pub source_position: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data_b64: Option<String>,
}

impl StorageV2Record {
    pub fn from_bytes(source_position: u64, bytes: Vec<u8>) -> Self {
        match String::from_utf8(bytes) {
            Ok(text) => Self {
                source_position,
                data: Some(text),
                data_b64: None,
            },
            Err(error) => Self {
                source_position,
                data: None,
                data_b64: Some(BASE64_STANDARD.encode(error.into_bytes())),
            },
        }
    }

    /// The record's exact source bytes, whichever form it carries.
    pub fn bytes(&self) -> Result<Vec<u8>> {
        match (&self.data, &self.data_b64) {
            (Some(text), None) => Ok(text.as_bytes().to_vec()),
            (None, Some(encoded)) => BASE64_STANDARD
                .decode(encoded)
                .context("decoding storage-v2 record base64"),
            _ => bail!("storage-v2 record must carry exactly one of data or data_b64"),
        }
    }

    /// The same record as base64, for a host that accepts only `data_b64`.
    pub fn into_base64(self) -> Result<Self> {
        let bytes = self.bytes()?;
        Ok(Self {
            source_position: self.source_position,
            data: None,
            data_b64: Some(BASE64_STANDARD.encode(bytes)),
        })
    }
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
    /// The provider CLI's own release. Only Claude and Codex transcripts carry
    /// one; absent everywhere else, and absent on the wire when unknown.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_version: Option<String>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_uuid: Option<String>,
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
    /// Parser-owned semantic kind. Serialized only when a parser has an exact
    /// fact to assert, so ordinary records keep the legacy field set the host
    /// validates against.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interaction_kind: Option<String>,
    /// Canonical item facts; omitted when empty so a record without them
    /// keeps the field set older hosts validate.
    #[serde(default, skip_serializing_if = "ItemFacts::is_empty")]
    pub item: ItemFacts,
    pub raw_record_ordinal: usize,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct StorageV2Render {
    pub generation_id: String,
    pub parser_revision: String,
    pub ordering_revision: String,
    pub records: Vec<StorageV2RenderRecord>,
}

/// One provider-authored fact beside the transcript: a turn duration, a
/// recap, a title. Parsed from lines the render surface does not show and
/// shipped in the same envelope as the bytes that carry them, so ordering and
/// idempotency ride the raw commit sequence. Excluded from envelope identity.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct StorageV2ProviderFact {
    pub kind: String,
    /// Provider wall-clock time the fact describes, RFC 3339.
    pub at: String,
    pub source_position: u64,
    pub payload: serde_json::Value,
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
    #[serde(default)]
    pub facts: Vec<StorageV2ProviderFact>,
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

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct StorageV2ResumeQuery {
    pub provider: String,
    pub opaque_source_id: String,
    pub range_kind: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct StorageV2SourceResume {
    pub v: u8,
    pub sources: Vec<StorageV2ResumeEntry>,
    pub commit_seq: String,
    pub observed_at: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct StorageV2ResumeEntry {
    pub provider: String,
    pub opaque_source_id: String,
    pub range_kind: String,
    pub found: bool,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub source_epoch: Option<StorageV2SourceEpoch>,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub first_object: Option<StorageV2ResumeObject>,
    #[serde(default)]
    pub last_object: Option<StorageV2ResumeObject>,
}

/// Evidence for one live raw object of the host's open epoch: its byte range and
/// the digest of its record hashes, which a machine recomputes from its own file.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct StorageV2ResumeObject {
    pub range_start: String,
    pub range_end: String,
    pub record_count: u64,
    pub record_hashes_hash: String,
    pub envelope_id: String,
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

    #[test]
    fn hosts_without_envelope_encodings_negotiate_identity() {
        let older_host: StorageV2Capabilities = serde_json::from_value(serde_json::json!({
            "protocol_version": 2,
            "cutover": true,
            "tenant_id": "tenant-a",
            "machine_id": "cinder",
            "ingest_path": STORAGE_V2_ENVELOPES_PATH,
            "max_wire_body_bytes": 12 * 1024 * 1024,
            "max_raw_record_bytes": 4 * 1024 * 1024,
            "max_records": 10_000,
            "media_claim_path": "/api/agents/storage/v2/media/claims",
            "media_upload_path_template": "/api/agents/storage/v2/media/{sha256}",
            "max_media_bytes": 32 * 1024 * 1024,
            "max_media_claims": 512,
            "range_kinds": ["byte_offset", "record_ordinal"],
            "lanes": ["live", "repair"],
            "lane_header": STORAGE_V2_LANE_HEADER,
        }))
        .unwrap();
        older_host.validate("cinder").unwrap();
        assert_eq!(
            older_host.envelope_body_encoding(),
            StorageV2BodyEncoding::Identity
        );

        assert!(!older_host.accepts_text_records());

        let current_host = StorageV2Capabilities {
            envelope_content_encodings: vec!["zstd".to_string(), "identity".to_string()],
            envelope_record_encodings: vec!["utf8".to_string(), "base64".to_string()],
            source_resume_path: None,
            host_render: false,
            render_item_facts: false,
            ..older_host
        };
        assert_eq!(
            current_host.envelope_body_encoding(),
            StorageV2BodyEncoding::Zstd
        );
        assert!(current_host.accepts_text_records());
    }

    #[test]
    fn identity_mismatch_names_both_identities_and_the_fix() {
        let error = capabilities(true)
            .validate("Manageds-VM.local")
            .unwrap_err();
        let mismatch = error
            .downcast_ref::<MachineIdentityMismatch>()
            .expect("a typed identity mismatch");
        assert_eq!(mismatch.configured, "Manageds-VM.local");
        assert_eq!(mismatch.token_device, "cinder");
        let text = error.to_string();
        assert!(
            text.contains("configured as \"Manageds-VM.local\""),
            "{text}"
        );
        assert!(
            text.contains("device token belongs to \"cinder\""),
            "{text}"
        );
        assert!(
            text.contains("longhouse auth && longhouse machine repair --repair-service"),
            "{text}"
        );
        assert!(text.contains("machine/device-token"), "{text}");
    }

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
            envelope_content_encodings: Vec::new(),
            envelope_record_encodings: vec!["utf8".to_string(), "base64".to_string()],
            source_resume_path: None,
            host_render: false,
            render_item_facts: false,
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
            envelope_content_encodings: Vec::new(),
            envelope_record_encodings: vec!["utf8".to_string(), "base64".to_string()],
            source_resume_path: None,
            host_render: false,
            render_item_facts: false,
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

#[cfg(test)]
mod record_encoding_tests {
    use super::*;

    #[test]
    fn utf8_records_travel_as_text_and_round_trip_exactly() {
        let bytes = "{\"t\":\"caf\u{e9} \u{1f600} \\\" \\u0000 tab\t nul\u{0}\"}\n"
            .as_bytes()
            .to_vec();
        let record = StorageV2Record::from_bytes(7, bytes.clone());
        assert!(record.data.is_some() && record.data_b64.is_none());

        let wire = serde_json::to_vec(&record).unwrap();
        let parsed: StorageV2Record = serde_json::from_slice(&wire).unwrap();
        assert_eq!(parsed.bytes().unwrap(), bytes);
        assert_eq!(parsed.source_position, 7);
        assert!(!String::from_utf8(wire).unwrap().contains("data_b64"));
    }

    #[test]
    fn invalid_utf8_records_fall_back_to_base64() {
        let bytes = vec![0x7b, 0xff, 0xfe, 0x7d, b'\n'];
        let record = StorageV2Record::from_bytes(3, bytes.clone());
        assert!(record.data.is_none() && record.data_b64.is_some());
        assert_eq!(record.bytes().unwrap(), bytes);
    }

    #[test]
    fn envelopes_stored_before_text_records_still_read() {
        let stored = serde_json::json!({"source_position": 0, "data_b64": "aGVsbG8K"});
        let record: StorageV2Record = serde_json::from_value(stored).unwrap();
        assert_eq!(record.bytes().unwrap(), b"hello\n");
    }

    #[test]
    fn a_record_must_carry_exactly_one_form() {
        for value in [
            serde_json::json!({"source_position": 0}),
            serde_json::json!({"source_position": 0, "data": "x", "data_b64": "eA=="}),
        ] {
            let record: StorageV2Record = serde_json::from_value(value).unwrap();
            assert!(record.bytes().is_err());
        }
    }

    #[test]
    fn into_base64_keeps_the_exact_bytes() {
        let bytes = "line \u{4e2d}\n".as_bytes().to_vec();
        let record = StorageV2Record::from_bytes(1, bytes.clone())
            .into_base64()
            .unwrap();
        assert!(record.data.is_none());
        assert_eq!(record.bytes().unwrap(), bytes);
    }
}
