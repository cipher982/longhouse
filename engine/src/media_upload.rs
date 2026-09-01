//! Claim and upload parsed archive media blobs before transcript ingest.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::pipeline::parser::ParsedMediaObject;
use crate::shipping::client::ShipperClient;
use crate::shipping::storage_v2::{StorageV2Capabilities, STORAGE_V2_LANE_HEADER};

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MediaUploadSummary {
    pub claimed: usize,
    pub already_present: usize,
    pub uploaded: usize,
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
