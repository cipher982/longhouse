//! Byte-exact identities for the storage-v2 durability boundary.
//!
//! This module deliberately has no shipping or serving integration. It freezes
//! the producer-side identity contract before the v2 ingest path consumes it.

#![allow(dead_code)] // Wired into ingest only after the frozen contract lands.

use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;
use uuid::Uuid;

const ENVELOPE_DOMAIN: &[u8] = b"longhouse-envelope-v2\0";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RangeKind {
    ByteOffset,
    RecordOrdinal,
}

impl RangeKind {
    fn tag(self) -> u8 {
        match self {
            Self::ByteOffset => 1,
            Self::RecordOrdinal => 2,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct EnvelopeIdentity {
    pub tenant_id: String,
    pub machine_id: String,
    pub provider: String,
    pub opaque_source_id: String,
    pub source_epoch: Uuid,
    pub range_kind: RangeKind,
    pub range_start: u64,
    pub range_end: u64,
    pub record_hashes: Vec<[u8; 32]>,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub(crate) enum EnvelopeIdentityError {
    #[error("{field} must already be NFC-normalized")]
    NonCanonicalUnicode { field: &'static str },
    #[error("provider must be canonical lowercase ASCII")]
    InvalidProvider,
    #[error("range must be an unsigned [start, end) interval")]
    InvalidRange,
    #[error("an empty range cannot contain records")]
    RecordsInEmptyRange,
    #[error("a non-empty range must contain records")]
    EmptyNonEmptyRange,
    #[error("{field} exceeds {maximum} bytes")]
    FieldTooLong { field: &'static str, maximum: usize },
    #[error("record count exceeds u32")]
    TooManyRecords,
}

pub(crate) fn hash_record(record: &[u8]) -> [u8; 32] {
    Sha256::digest(record).into()
}

pub(crate) fn hash_records(records: &[Vec<u8>]) -> Vec<[u8; 32]> {
    records.iter().map(|record| hash_record(record)).collect()
}

pub(crate) fn encode_envelope_preimage(
    identity: &EnvelopeIdentity,
) -> Result<Vec<u8>, EnvelopeIdentityError> {
    validate_provider(&identity.provider)?;
    if identity.range_start > identity.range_end {
        return Err(EnvelopeIdentityError::InvalidRange);
    }
    if identity.range_start == identity.range_end && !identity.record_hashes.is_empty() {
        return Err(EnvelopeIdentityError::RecordsInEmptyRange);
    }
    if identity.range_start < identity.range_end && identity.record_hashes.is_empty() {
        return Err(EnvelopeIdentityError::EmptyNonEmptyRange);
    }
    let record_count = u32::try_from(identity.record_hashes.len())
        .map_err(|_| EnvelopeIdentityError::TooManyRecords)?;

    let mut preimage = Vec::with_capacity(
        ENVELOPE_DOMAIN.len()
            + 4 * 4
            + identity.tenant_id.len()
            + identity.machine_id.len()
            + identity.provider.len()
            + identity.opaque_source_id.len()
            + 16
            + 1
            + 8
            + 8
            + 4
            + identity.record_hashes.len() * 32,
    );
    preimage.extend_from_slice(ENVELOPE_DOMAIN);
    push_u32_string(&mut preimage, &identity.tenant_id, "tenant_id")?;
    push_u32_string(&mut preimage, &identity.machine_id, "machine_id")?;
    push_u32_bytes(&mut preimage, identity.provider.as_bytes(), "provider")?;
    push_u32_string(
        &mut preimage,
        &identity.opaque_source_id,
        "opaque_source_id",
    )?;
    preimage.extend_from_slice(identity.source_epoch.as_bytes());
    preimage.push(identity.range_kind.tag());
    preimage.extend_from_slice(&identity.range_start.to_be_bytes());
    preimage.extend_from_slice(&identity.range_end.to_be_bytes());
    preimage.extend_from_slice(&record_count.to_be_bytes());
    for record_hash in &identity.record_hashes {
        preimage.extend_from_slice(record_hash);
    }
    Ok(preimage)
}

pub(crate) fn envelope_id(identity: &EnvelopeIdentity) -> Result<[u8; 32], EnvelopeIdentityError> {
    Ok(Sha256::digest(encode_envelope_preimage(identity)?).into())
}

fn validate_provider(provider: &str) -> Result<(), EnvelopeIdentityError> {
    let bytes = provider.as_bytes();
    let first_is_alphanumeric = bytes
        .first()
        .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit());
    let all_are_canonical = bytes.iter().all(|byte| {
        byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
    });
    if !(first_is_alphanumeric && all_are_canonical && bytes.len() <= 32) {
        return Err(EnvelopeIdentityError::InvalidProvider);
    }
    Ok(())
}

fn push_u32_string(
    target: &mut Vec<u8>,
    value: &str,
    field: &'static str,
) -> Result<(), EnvelopeIdentityError> {
    if value.nfc().ne(value.chars()) {
        return Err(EnvelopeIdentityError::NonCanonicalUnicode { field });
    }
    push_u32_bytes(target, value.as_bytes(), field)
}

fn push_u32_bytes(
    target: &mut Vec<u8>,
    value: &[u8],
    field: &'static str,
) -> Result<(), EnvelopeIdentityError> {
    let length = u32::try_from(value.len()).map_err(|_| EnvelopeIdentityError::FieldTooLong {
        field,
        maximum: u32::MAX as usize,
    })?;
    target.extend_from_slice(&length.to_be_bytes());
    target.extend_from_slice(value);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    const CONTRACT_VECTORS: &str = include_str!("../../schemas/storage-v2-contract-vectors.json");

    #[derive(Deserialize)]
    struct Fixture {
        envelope_identity: EnvelopeFixture,
    }

    #[derive(Deserialize)]
    struct EnvelopeFixture {
        domain_hex: String,
        vectors: Vec<EnvelopeVector>,
    }

    #[derive(Deserialize)]
    struct EnvelopeVector {
        name: String,
        tenant_id: String,
        machine_id: String,
        provider: String,
        opaque_source_id: String,
        source_epoch: String,
        range_kind: String,
        range_start: u64,
        range_end: u64,
        record_bytes_hex: Vec<String>,
        record_hashes: Vec<String>,
        preimage_hex: String,
        envelope_id: String,
    }

    #[test]
    fn shared_fixture_matches_record_hashes_preimages_and_envelope_ids() {
        let fixture: Fixture = serde_json::from_str(CONTRACT_VECTORS).unwrap();
        assert_eq!(
            decode_hex(&fixture.envelope_identity.domain_hex),
            ENVELOPE_DOMAIN
        );

        for vector in fixture.envelope_identity.vectors {
            let records: Vec<Vec<u8>> = vector
                .record_bytes_hex
                .iter()
                .map(|value| decode_hex(value))
                .collect();
            let record_hashes = hash_records(&records);
            let expected_hashes: Vec<[u8; 32]> = vector
                .record_hashes
                .iter()
                .map(|value| decode_array(value))
                .collect();
            assert_eq!(
                record_hashes, expected_hashes,
                "{} record hashes",
                vector.name
            );

            let identity = EnvelopeIdentity {
                tenant_id: vector.tenant_id,
                machine_id: vector.machine_id,
                provider: vector.provider,
                opaque_source_id: vector.opaque_source_id,
                source_epoch: Uuid::parse_str(&vector.source_epoch).unwrap(),
                range_kind: match vector.range_kind.as_str() {
                    "byte_offset" => RangeKind::ByteOffset,
                    "record_ordinal" => RangeKind::RecordOrdinal,
                    other => panic!("unsupported fixture range kind {other}"),
                },
                range_start: vector.range_start,
                range_end: vector.range_end,
                record_hashes,
            };

            let preimage = encode_envelope_preimage(&identity).unwrap();
            assert_eq!(
                preimage,
                decode_hex(&vector.preimage_hex),
                "{} preimage",
                vector.name
            );
            assert_eq!(
                envelope_id(&identity).unwrap(),
                decode_array(&vector.envelope_id),
                "{} envelope id",
                vector.name
            );
        }
    }

    #[test]
    fn identity_rejects_noncanonical_inputs() {
        let base = EnvelopeIdentity {
            tenant_id: "tenant".to_string(),
            machine_id: "machine".to_string(),
            provider: "codex".to_string(),
            opaque_source_id: "history.jsonl".to_string(),
            source_epoch: Uuid::nil(),
            range_kind: RangeKind::RecordOrdinal,
            range_start: 0,
            range_end: 0,
            record_hashes: Vec::new(),
        };

        let mut invalid_provider = base.clone();
        invalid_provider.provider = "Codex".to_string();
        assert_eq!(
            encode_envelope_preimage(&invalid_provider),
            Err(EnvelopeIdentityError::InvalidProvider)
        );
        invalid_provider.provider = "_codex".to_string();
        assert_eq!(
            encode_envelope_preimage(&invalid_provider),
            Err(EnvelopeIdentityError::InvalidProvider)
        );
        invalid_provider.provider = "a".repeat(33);
        assert_eq!(
            encode_envelope_preimage(&invalid_provider),
            Err(EnvelopeIdentityError::InvalidProvider)
        );

        let mut non_nfc = base.clone();
        non_nfc.machine_id = "e\u{301}".to_string();
        assert_eq!(
            encode_envelope_preimage(&non_nfc),
            Err(EnvelopeIdentityError::NonCanonicalUnicode {
                field: "machine_id"
            })
        );

        let mut empty_with_records = base;
        empty_with_records.record_hashes.push([0; 32]);
        assert_eq!(
            encode_envelope_preimage(&empty_with_records),
            Err(EnvelopeIdentityError::RecordsInEmptyRange)
        );

        let mut nonempty_without_records = empty_with_records;
        nonempty_without_records.range_end = 1;
        nonempty_without_records.record_hashes.clear();
        assert_eq!(
            encode_envelope_preimage(&nonempty_without_records),
            Err(EnvelopeIdentityError::EmptyNonEmptyRange)
        );
    }

    fn decode_array<const N: usize>(value: &str) -> [u8; N] {
        decode_hex(value).try_into().unwrap()
    }

    fn decode_hex(value: &str) -> Vec<u8> {
        assert_eq!(value.len() % 2, 0, "hex fixture has odd length");
        value
            .as_bytes()
            .chunks_exact(2)
            .map(|pair| (hex_nibble(pair[0]) << 4) | hex_nibble(pair[1]))
            .collect()
    }

    fn hex_nibble(byte: u8) -> u8 {
        match byte {
            b'0'..=b'9' => byte - b'0',
            b'a'..=b'f' => byte - b'a' + 10,
            b'A'..=b'F' => byte - b'A' + 10,
            _ => panic!("invalid hex fixture byte"),
        }
    }
}
