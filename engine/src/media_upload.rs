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
/// A transfer floor for sizing a body's deadline. The shipping timeout is sized
/// for a small JSON envelope; a multi-megabyte screenshot on a slow link would
/// expire mid-upload and retry the whole body.
const MIN_UPLOAD_BYTES_PER_SECOND: u64 = 256 * 1024;

/// The deadline a body of this size can actually be sent in.
fn upload_timeout(base: Option<Duration>, byte_size: usize) -> Option<Duration> {
    let base = base?;
    let transfer_seconds = (byte_size as u64)
        .div_ceil(MIN_UPLOAD_BYTES_PER_SECOND)
        .max(1);
    Some(base + Duration::from_secs(transfer_seconds))
}

/// A preview plus the source's pixel size, which the timeline needs to reserve
/// layout before the bytes arrive.
#[derive(Debug, Clone)]
pub struct DerivedPreview {
    pub bytes: Vec<u8>,
    pub width: u32,
    pub height: u32,
}

/// A decode ceiling. An image bomb is small on disk and gigabytes in memory, so
/// the bound has to be on pixels and allocation, not on the encoded size.
const MAX_DECODE_EDGE: u32 = 20_000;
const MAX_DECODE_ALLOC_BYTES: u64 = 256 * 1024 * 1024;

fn decode_limits() -> image::Limits {
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(MAX_DECODE_EDGE);
    limits.max_image_height = Some(MAX_DECODE_EDGE);
    limits.max_alloc = Some(MAX_DECODE_ALLOC_BYTES);
    limits
}

/// Composite onto white before dropping alpha.
///
/// Most of what users paste is a screenshot with transparency, and `to_rgb8`
/// discards alpha by ignoring it, which renders those as black smears.
fn flatten_onto_white(image: &image::DynamicImage) -> image::RgbImage {
    let rgba = image.to_rgba8();
    let mut rgb = image::RgbImage::new(rgba.width(), rgba.height());
    for (x, y, pixel) in rgba.enumerate_pixels() {
        let [red, green, blue, alpha] = pixel.0;
        let alpha = u32::from(alpha);
        let blend = |channel: u8| ((u32::from(channel) * alpha + 255 * (255 - alpha)) / 255) as u8;
        rgb.put_pixel(x, y, image::Rgb([blend(red), blend(green), blend(blue)]));
    }
    rgb
}

/// The pixel size of an image, read from its header alone.
fn image_dimensions(bytes: &[u8]) -> Option<(u32, u32)> {
    image::ImageReader::new(std::io::Cursor::new(bytes))
        .with_guessed_format()
        .ok()?
        .into_dimensions()
        .ok()
}

/// A small JPEG standing in for an oversized image.
///
/// Both clients show a preview in the row and link to the original, so the bytes
/// fetched by default should be a fraction of a retina screenshot rather than
/// the screenshot. The decision needs the image's pixel size, which the header
/// carries: judging by encoded size alone would skip a heavily compressed
/// 8000-pixel-wide image that very much needs one. Decoding is CPU work over a
/// possibly multi-megabyte buffer, so it runs on the blocking pool under a
/// pixel and allocation bound.
async fn derive_preview(bytes: Vec<u8>) -> Option<DerivedPreview> {
    tokio::task::spawn_blocking(move || derive_preview_blocking(&bytes))
        .await
        .ok()
        .flatten()
}

fn derive_preview_blocking(bytes: &[u8]) -> Option<DerivedPreview> {
    let reader = image::ImageReader::new(std::io::Cursor::new(bytes))
        .with_guessed_format()
        .ok()?;
    let (width, height) = reader.into_dimensions().ok()?;
    if width.max(height) <= PREVIEW_MAX_EDGE && bytes.len() <= PREVIEW_SKIP_BYTES {
        return None;
    }
    let mut reader = image::ImageReader::new(std::io::Cursor::new(bytes))
        .with_guessed_format()
        .ok()?;
    reader.limits(decode_limits());
    let decoded = reader.decode().ok()?;
    let preview = decoded.thumbnail(PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE);
    let flat = flatten_onto_white(&preview);
    let mut out = Vec::new();
    JpegEncoder::new_with_quality(&mut out, PREVIEW_JPEG_QUALITY)
        .write_image(
            flat.as_raw(),
            flat.width(),
            flat.height(),
            image::ExtendedColorType::Rgb8,
        )
        .ok()?;
    Some(DerivedPreview {
        bytes: out,
        width,
        height,
    })
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
        let path = capabilities
            .media_upload_path_template
            .replace("{sha256}", sha256);
        let mut query: Vec<String> = Vec::new();
        if let Some(preview_hash) = preview_hash {
            query.push(format!("thumb_sha256={preview_hash}"));
        }
        if media.mime_type.starts_with("image/") {
            // The pixel size lets the timeline reserve the row's layout before
            // the bytes arrive; the header carries it, so this costs a read of
            // a few dozen bytes rather than a decode.
            if let Some((width, height)) = image_dimensions(&media.bytes) {
                query.push(format!("width={width}"));
                query.push(format!("height={height}"));
            }
        }
        let path = if query.is_empty() {
            path
        } else {
            format!("{path}?{}", query.join("&"))
        };
        client
            .put_bytes_with_timeout(
                &path,
                &media.mime_type,
                lane_headers,
                media.bytes.clone(),
                upload_timeout(request_timeout, media.byte_size),
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
    let preview_hash = format!("{:x}", Sha256::digest(&preview.bytes));
    let preview_path = capabilities
        .media_upload_path_template
        .replace("{sha256}", &preview_hash);
    let preview_timeout = upload_timeout(request_timeout, preview.bytes.len());
    match client
        .put_bytes_with_timeout(
            // The preview names the image it came from; the read requires the
            // parent and the preview to agree about that.
            &format!("{preview_path}?derived_from={}", media.sha256),
            PREVIEW_MIME_TYPE,
            lane_headers.to_vec(),
            preview.bytes,
            preview_timeout,
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
        encode_png(&pixels, width, height)
    }

    /// A flat image: large in pixels, a few hundred bytes encoded.
    fn flat_png(width: u32, height: u32, color: [u8; 3]) -> Vec<u8> {
        let mut pixels = Vec::with_capacity((width * height * 3) as usize);
        for _ in 0..(width * height) {
            pixels.extend_from_slice(&color);
        }
        encode_png(&pixels, width, height)
    }

    fn encode_png(pixels: &[u8], width: u32, height: u32) -> Vec<u8> {
        let mut out = Vec::new();
        image::codecs::png::PngEncoder::new(&mut out)
            .write_image(pixels, width, height, image::ExtendedColorType::Rgb8)
            .unwrap();
        out
    }

    #[test]
    fn a_deadline_grows_with_the_body_it_has_to_carry() {
        let base = Some(Duration::from_secs(15));
        assert_eq!(upload_timeout(None, 19_000_000), None);
        // A body too small to matter still gets the base plus a second.
        assert_eq!(upload_timeout(base, 0), Some(Duration::from_secs(16)));
        // A 19 MB screenshot at the floor rate needs far more than the base.
        assert_eq!(
            upload_timeout(base, 19 * 1024 * 1024),
            Some(Duration::from_secs(15 + 76))
        );
    }

    #[tokio::test]
    async fn derives_a_bounded_preview_for_an_oversized_screenshot() {
        let original = noisy_png(1200, 900);
        assert!(
            original.len() > PREVIEW_SKIP_BYTES,
            "fixture must be worth previewing"
        );

        let preview = derive_preview(original.clone()).await.expect("a preview");
        assert!(
            preview.bytes.len() < original.len() / 4,
            "a preview must be a fraction of the original: {} vs {}",
            preview.bytes.len(),
            original.len()
        );
        assert_eq!((preview.width, preview.height), (1200, 900));
        let decoded = image::load_from_memory(&preview.bytes).expect("a decodable preview");
        assert_eq!(decoded.width(), PREVIEW_MAX_EDGE);
        assert_eq!(decoded.height(), 384);
    }

    #[tokio::test]
    async fn previews_a_small_file_that_is_large_in_pixels() {
        // The case an encoded-size shortcut gets wrong: 3 KB on disk, 2000
        // pixels wide, and exactly the image a timeline most needs scaled down.
        let compact = flat_png(2000, 1500, [200, 200, 200]);
        assert!(
            compact.len() <= PREVIEW_SKIP_BYTES,
            "fixture must be small on disk"
        );
        let preview = derive_preview(compact).await.expect("a preview");
        assert_eq!((preview.width, preview.height), (2000, 1500));
        let decoded = image::load_from_memory(&preview.bytes).expect("a decodable preview");
        assert_eq!(decoded.width().max(decoded.height()), PREVIEW_MAX_EDGE);
    }

    #[tokio::test]
    async fn leaves_an_image_that_is_small_in_every_sense_alone() {
        let small = flat_png(200, 150, [10, 20, 30]);
        assert!(small.len() <= PREVIEW_SKIP_BYTES, "fixture must be small");
        assert!(derive_preview(small).await.is_none());
    }

    #[tokio::test]
    async fn composites_transparency_onto_white_not_black() {
        // A screenshot with a transparent background must not preview as a
        // black smear, which is what dropping alpha silently does. Larger than
        // the preview edge, so the normal rule already produces a preview.
        let width = 600u32;
        let height = 600u32;
        let mut rgba = Vec::with_capacity((width * height * 4) as usize);
        for _ in 0..(width * height) {
            rgba.extend_from_slice(&[255, 255, 255, 0]);
        }
        let mut png = Vec::new();
        image::codecs::png::PngEncoder::new(&mut png)
            .write_image(&rgba, width, height, image::ExtendedColorType::Rgba8)
            .unwrap();

        let preview = derive_preview(png).await.expect("a preview");
        let decoded = image::load_from_memory(&preview.bytes).expect("decodable");
        let rgb = decoded.to_rgb8();
        for pixel in rgb.pixels() {
            assert!(
                pixel.0[0] > 200 && pixel.0[1] > 200 && pixel.0[2] > 200,
                "transparent pixels must flatten to white, got {:?}",
                pixel.0
            );
        }
    }

    #[tokio::test]
    async fn refuses_bytes_it_cannot_decode() {
        assert!(derive_preview(vec![0u8; 200_000]).await.is_none());
    }
}
