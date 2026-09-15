//! Claim and upload parsed archive media blobs before transcript ingest.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use image::codecs::jpeg::JpegEncoder;
use image::ImageEncoder;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Longest edge of a derived preview. The timeline renders media in a grid cell
/// a little over a hundred points wide, so this is generous even at 3x density.
const PREVIEW_MAX_EDGE: u32 = 512;
/// Below this the original is already cheap to fetch and is served as-is.
const PREVIEW_SKIP_BYTES: usize = 64 * 1024;
const PREVIEW_MIME_TYPE: &str = "image/jpeg";
const PREVIEW_JPEG_QUALITY: u8 = 75;

/// A small JPEG standing in for an oversized image.
///
/// Both clients show a preview in the row and link to the original, so the bytes
/// fetched by default should be a fraction of a retina screenshot rather than
/// the screenshot. Decoding is CPU work over a possibly multi-megabyte buffer,
/// so it runs on the blocking pool, and anything already small enough to serve
/// directly is left alone.
async fn derive_preview(bytes: Vec<u8>) -> Option<Vec<u8>> {
    if bytes.len() <= PREVIEW_SKIP_BYTES {
        return None;
    }
    tokio::task::spawn_blocking(move || {
        let decoded = image::load_from_memory(&bytes).ok()?;
        if decoded.width().max(decoded.height()) <= PREVIEW_MAX_EDGE {
            return None;
        }
        let preview = decoded.thumbnail(PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE);
        let rgb = preview.to_rgb8();
        let mut out = Vec::new();
        JpegEncoder::new_with_quality(&mut out, PREVIEW_JPEG_QUALITY)
            .write_image(
                rgb.as_raw(),
                rgb.width(),
                rgb.height(),
                image::ExtendedColorType::Rgb8,
            )
            .ok()?;
        Some(out)
    })
    .await
    .ok()
    .flatten()
}

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
        let lane_headers = vec![(STORAGE_V2_LANE_HEADER.to_string(), lane.to_string())];
        // The preview is uploaded first: the object that points at it is only
        // accepted with a link the store can already resolve.
        let preview_hash =
            upload_preview(client, capabilities, media, &lane_headers, request_timeout).await;
        let mut path = capabilities
            .media_upload_path_template
            .replace("{sha256}", sha256);
        if let Some(preview_hash) = preview_hash {
            path = format!("{path}?thumb_sha256={preview_hash}");
        }
        client
            .put_bytes_with_timeout(
                &path,
                &media.mime_type,
                lane_headers,
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

/// Upload this object's preview, if it is worth having one, and report its hash.
///
/// A preview is an optimization: when it cannot be produced or stored, the
/// original is still uploaded and served, so nothing about fidelity depends on
/// this succeeding.
async fn upload_preview(
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    media: &ParsedMediaObject,
    lane_headers: &[(String, String)],
    request_timeout: Option<Duration>,
) -> Option<String> {
    if !media.mime_type.starts_with("image/") {
        return None;
    }
    let preview = derive_preview(media.bytes.clone()).await?;
    let preview_hash = format!("{:x}", Sha256::digest(&preview));
    let preview_path = capabilities
        .media_upload_path_template
        .replace("{sha256}", &preview_hash);
    match client
        .put_bytes_with_timeout(
            &preview_path,
            PREVIEW_MIME_TYPE,
            lane_headers.to_vec(),
            preview,
            request_timeout,
        )
        .await
    {
        Ok(()) => Some(preview_hash),
        Err(error) => {
            tracing::debug!(media = %media.sha256, error = %error, "storage-v2 preview upload failed; serving the original");
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Deterministic, incompressible pixels so the encoded size tracks the
    /// dimensions instead of collapsing to a few kilobytes.
    fn noisy_png(width: u32, height: u32) -> Vec<u8> {
        let mut state = 0x2545_f491_4f6c_dd1du64;
        let mut pixels = Vec::with_capacity((width * height * 3) as usize);
        for _ in 0..(width * height) {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            pixels.push((state & 0xff) as u8);
            pixels.push(((state >> 8) & 0xff) as u8);
            pixels.push(((state >> 16) & 0xff) as u8);
        }
        let mut out = Vec::new();
        image::codecs::png::PngEncoder::new(&mut out)
            .write_image(&pixels, width, height, image::ExtendedColorType::Rgb8)
            .unwrap();
        out
    }

    #[tokio::test]
    async fn derives_a_bounded_preview_for_an_oversized_screenshot() {
        let original = noisy_png(1200, 900);
        assert!(
            original.len() > PREVIEW_SKIP_BYTES,
            "the fixture must be big enough to be worth previewing"
        );

        let preview = derive_preview(original.clone()).await.expect("a preview");
        assert!(
            preview.len() < original.len() / 4,
            "a preview must be a fraction of the original: {} vs {}",
            preview.len(),
            original.len()
        );
        let decoded = image::load_from_memory(&preview).expect("a decodable preview");
        assert_eq!(decoded.width().max(decoded.height()), PREVIEW_MAX_EDGE);
        assert_eq!(decoded.width(), PREVIEW_MAX_EDGE);
        assert_eq!(decoded.height(), 384);
    }

    /// A smooth image: large in pixels, a few hundred bytes encoded, which is
    /// exactly the case that needs no preview.
    fn flat_png(width: u32, height: u32) -> Vec<u8> {
        let pixels = vec![200u8; (width * height * 3) as usize];
        let mut out = Vec::new();
        image::codecs::png::PngEncoder::new(&mut out)
            .write_image(&pixels, width, height, image::ExtendedColorType::Rgb8)
            .unwrap();
        out
    }

    #[tokio::test]
    async fn leaves_an_image_that_is_already_small_enough_alone() {
        let small = flat_png(2000, 1500);
        assert!(small.len() <= PREVIEW_SKIP_BYTES, "fixture must be small");
        assert!(derive_preview(small).await.is_none());
    }

    #[tokio::test]
    async fn does_not_preview_an_image_already_within_the_preview_edge() {
        let compact = noisy_png(400, 400);
        assert!(
            compact.len() > PREVIEW_SKIP_BYTES,
            "fixture must exceed the byte floor to exercise the dimension rule"
        );
        assert!(derive_preview(compact).await.is_none());
    }

    #[tokio::test]
    async fn refuses_bytes_it_cannot_decode() {
        assert!(derive_preview(vec![0u8; 200_000]).await.is_none());
    }
}
