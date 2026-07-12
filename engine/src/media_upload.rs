//! Claim and upload parsed archive media blobs before transcript ingest.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::pipeline::parser::ParsedMediaObject;
use crate::shipping::client::ShipperClient;
use crate::shipping::storage_v2::{StorageV2Capabilities, STORAGE_V2_LANE_HEADER};

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MediaUploadSummary {
    pub claimed: usize,
    pub already_present: usize,
    pub uploaded: usize,
}

#[derive(Serialize)]
struct MediaClaimsRequest<'a> {
    items: Vec<MediaClaimItem<'a>>,
}

#[derive(Serialize)]
struct MediaClaimItem<'a> {
    sha256: &'a str,
    mime_type: &'a str,
    byte_size: usize,
    session_id: &'a str,
    source_path: &'a str,
    source_offset: u64,
    source_line_hash: &'a str,
    provider: &'a str,
    original_kind: &'static str,
}

#[derive(Deserialize)]
struct MediaClaimsResponse {
    needed: Vec<String>,
    present: Vec<String>,
    rejected: Vec<MediaClaimRejected>,
}

#[derive(Deserialize)]
struct MediaClaimRejected {
    sha256: String,
    reason: String,
}

#[derive(Serialize)]
struct StorageV2MediaClaimsRequest<'a> {
    items: Vec<StorageV2MediaClaimItem<'a>>,
}

#[derive(Serialize)]
struct StorageV2MediaClaimItem<'a> {
    sha256: &'a str,
    mime_type: &'a str,
    byte_size: usize,
}

pub async fn ensure_storage_v2_media_uploaded(
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    media_objects: &[ParsedMediaObject],
    lane: &str,
    request_timeout: Option<Duration>,
) -> Result<MediaUploadSummary> {
    if media_objects.is_empty() {
        return Ok(MediaUploadSummary::default());
    }
    if lane != "live" && lane != "repair" {
        bail!("storage-v2 media lane must be live or repair");
    }
    let mut by_sha: BTreeMap<&str, &ParsedMediaObject> = BTreeMap::new();
    for media in media_objects {
        if media.byte_size != media.bytes.len()
            || media.byte_size == 0
            || media.byte_size as u64 > capabilities.max_media_bytes
        {
            bail!(
                "storage-v2 media {} violates the negotiated byte bound",
                media.sha256
            );
        }
        if let Some(prior) = by_sha.insert(media.sha256.as_str(), media) {
            if prior.bytes != media.bytes {
                bail!(
                    "storage-v2 media hash {} has conflicting bytes",
                    media.sha256
                );
            }
        }
    }
    if by_sha.len() as u64 > capabilities.max_media_claims {
        bail!("storage-v2 envelope references too many distinct media objects");
    }
    let request = StorageV2MediaClaimsRequest {
        items: by_sha
            .values()
            .map(|media| StorageV2MediaClaimItem {
                sha256: &media.sha256,
                mime_type: &media.mime_type,
                byte_size: media.byte_size,
            })
            .collect(),
    };
    let body = serde_json::to_vec(&request).context("serializing storage-v2 media claims")?;
    let response: MediaClaimsResponse = client
        .post_json_decode_with_timeout(&capabilities.media_claim_path, body, request_timeout)
        .await
        .context("claiming storage-v2 media")?;
    if !response.rejected.is_empty() {
        let reasons = response
            .rejected
            .iter()
            .map(|item| format!("{}:{}", item.sha256, item.reason))
            .collect::<Vec<_>>()
            .join(", ");
        bail!("storage-v2 media claim rejected: {reasons}");
    }
    let needed: BTreeSet<String> = response.needed.into_iter().collect();
    let present: BTreeSet<String> = response.present.into_iter().collect();
    let expected: BTreeSet<String> = by_sha.keys().map(|value| (*value).to_string()).collect();
    if !needed.is_disjoint(&present)
        || needed.union(&present).cloned().collect::<BTreeSet<_>>() != expected
    {
        bail!("storage-v2 media claim response is not an exact partition");
    }
    for sha256 in &needed {
        let media = by_sha
            .get(sha256.as_str())
            .with_context(|| format!("media claim requested unknown sha256 {sha256}"))?;
        let path = capabilities
            .media_upload_path_template
            .replace("{sha256}", sha256);
        client
            .put_bytes_with_timeout(
                &path,
                &media.mime_type,
                vec![(STORAGE_V2_LANE_HEADER.to_string(), lane.to_string())],
                media.bytes.clone(),
                request_timeout,
            )
            .await
            .with_context(|| format!("uploading storage-v2 media {sha256}"))?;
    }
    Ok(MediaUploadSummary {
        claimed: by_sha.len(),
        already_present: present.len(),
        uploaded: needed.len(),
    })
}

pub async fn ensure_media_uploaded(
    client: &ShipperClient,
    session_id: &str,
    provider: &str,
    source_path: &str,
    media_objects: &[ParsedMediaObject],
    request_timeout: Option<Duration>,
) -> Result<MediaUploadSummary> {
    if media_objects.is_empty() {
        return Ok(MediaUploadSummary::default());
    }
    validate_session_uuid(session_id)?;

    let request = MediaClaimsRequest {
        items: media_objects
            .iter()
            .map(|media| MediaClaimItem {
                sha256: &media.sha256,
                mime_type: &media.mime_type,
                byte_size: media.byte_size,
                session_id,
                source_path,
                source_offset: media.source_offset,
                source_line_hash: &media.original_line_sha256,
                provider,
                original_kind: "inline_data_url",
            })
            .collect(),
    };
    let claimed = request.items.len();
    let body = serde_json::to_vec(&request).context("serializing media claims")?;
    let response: MediaClaimsResponse = client
        .post_json_decode_with_timeout("/api/agents/media/claims", body, request_timeout)
        .await
        .context("claiming archive media")?;

    if !response.rejected.is_empty() {
        let reasons = response
            .rejected
            .iter()
            .map(|item| format!("{}:{}", item.sha256, item.reason))
            .collect::<Vec<_>>()
            .join(", ");
        bail!("media claim rejected: {reasons}");
    }

    let by_sha: BTreeMap<&str, &ParsedMediaObject> = media_objects
        .iter()
        .map(|media| (media.sha256.as_str(), media))
        .collect();
    let needed: BTreeSet<String> = response.needed.into_iter().collect();
    for sha256 in &needed {
        let Some(media) = by_sha.get(sha256.as_str()) else {
            bail!("media claim requested unknown sha256 {sha256}");
        };
        client
            .put_bytes_with_timeout(
                &format!("/api/agents/media/{sha256}"),
                &media.mime_type,
                vec![("X-Longhouse-Session-Id".to_string(), session_id.to_string())],
                media.bytes.clone(),
                request_timeout,
            )
            .await
            .with_context(|| format!("uploading archive media {sha256}"))?;
    }

    Ok(MediaUploadSummary {
        claimed,
        already_present: response.present.len(),
        uploaded: needed.len(),
    })
}

fn validate_session_uuid(session_id: &str) -> Result<()> {
    Uuid::parse_str(session_id)
        .with_context(|| format!("media upload session_id is not a UUID: {session_id}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pipeline::parser::ParsedMediaObject;

    fn media(sha256: &str, source_offset: u64) -> ParsedMediaObject {
        ParsedMediaObject {
            source_offset,
            sha256: sha256.to_string(),
            mime_type: "image/png".to_string(),
            byte_size: 3,
            original_chars: 30,
            original_line_sha256: format!("{sha256}line"),
            bytes: vec![1, 2, 3],
        }
    }

    #[test]
    fn claim_request_keeps_one_item_per_source_ref() {
        let objects = vec![
            media(
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                10,
            ),
            media(
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                20,
            ),
        ];
        let request = MediaClaimsRequest {
            items: objects
                .iter()
                .map(|item| MediaClaimItem {
                    sha256: &item.sha256,
                    mime_type: &item.mime_type,
                    byte_size: item.byte_size,
                    session_id: "session-1",
                    source_path: "/tmp/session.jsonl",
                    source_offset: item.source_offset,
                    source_line_hash: &item.original_line_sha256,
                    provider: "codex",
                    original_kind: "inline_data_url",
                })
                .collect(),
        };
        let value = serde_json::to_value(&request).unwrap();
        let items = value["items"].as_array().unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0]["source_offset"], 10);
        assert_eq!(items[1]["source_offset"], 20);
        assert_eq!(items[0]["original_kind"], "inline_data_url");
    }

    #[test]
    fn validate_session_uuid_rejects_non_uuid() {
        assert!(validate_session_uuid("session-1").is_err());
        assert!(validate_session_uuid("019c638d-0000-0000-0000-000000000555").is_ok());
    }
}
