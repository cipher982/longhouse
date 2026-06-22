//! Redact oversized inline image data URLs from source-line payloads.
//!
//! Provider logs sometimes store pasted screenshots as
//! `data:image/...;base64,...` strings. Those bytes are media evidence, but
//! they should not travel through the source-line ingest path as megabyte-scale
//! JSON strings. This module replaces large inline image URLs with a stable
//! content-addressed placeholder while preserving enough metadata to reconcile
//! the original bytes through the media lane.

use std::borrow::Cow;

use base64::{engine::general_purpose, Engine as _};
use sha2::{Digest, Sha256};

pub const INLINE_IMAGE_DATA_URL_REDACT_THRESHOLD_CHARS: usize = 512;

const DATA_IMAGE_PREFIX: &str = "data:image/";
const BASE64_MARKER: &str = ";base64,";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InlineImageRedaction {
    pub placeholder: String,
    pub mime_type: String,
    pub sha256: String,
    pub byte_size: usize,
    pub original_chars: usize,
}

pub fn redact_inline_image_data_url(value: &str) -> Option<InlineImageRedaction> {
    if value.len() <= INLINE_IMAGE_DATA_URL_REDACT_THRESHOLD_CHARS {
        return None;
    }
    if !value.starts_with(DATA_IMAGE_PREFIX) {
        return None;
    }
    let (mime_type, data) = value.split_once(BASE64_MARKER)?;
    let bytes = general_purpose::STANDARD.decode(data).ok()?;
    let sha256 = format!("{:x}", Sha256::digest(&bytes));
    let placeholder = format!(
        "longhouse_media_ref:sha256={sha256};mime={mime_type};bytes={};original_chars={}",
        bytes.len(),
        value.len()
    );
    Some(InlineImageRedaction {
        placeholder,
        mime_type: mime_type.to_string(),
        sha256,
        byte_size: bytes.len(),
        original_chars: value.len(),
    })
}

pub fn redact_inline_image_data_urls_in_json_line(raw: &str) -> Cow<'_, str> {
    if !raw.contains(DATA_IMAGE_PREFIX) {
        return Cow::Borrowed(raw);
    }

    let mut out = String::with_capacity(raw.len().min(4096));
    let mut cursor = 0usize;
    let mut changed = false;

    while let Some(rel_start) = raw[cursor..].find(DATA_IMAGE_PREFIX) {
        let start = cursor + rel_start;
        let Some(end) = find_json_string_end(raw, start) else {
            break;
        };
        let candidate = &raw[start..end];
        let Some(redaction) = redact_inline_image_data_url(candidate) else {
            cursor = end;
            continue;
        };

        out.push_str(&raw[cursor..start]);
        out.push_str(&redaction.placeholder);
        cursor = end;
        changed = true;
    }

    if !changed {
        return Cow::Borrowed(raw);
    }
    out.push_str(&raw[cursor..]);
    Cow::Owned(out)
}

fn find_json_string_end(raw: &str, start: usize) -> Option<usize> {
    let bytes = raw.as_bytes();
    let mut idx = start;
    let mut escaped = false;
    while idx < bytes.len() {
        let byte = bytes[idx];
        if escaped {
            escaped = false;
        } else if byte == b'\\' {
            escaped = true;
        } else if byte == b'"' {
            return Some(idx);
        }
        idx += 1;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_large_data_image_url_with_sha_placeholder() {
        let data = general_purpose::STANDARD.encode([7u8; 600]);
        let url = format!("data:image/png;base64,{data}");
        let redaction = redact_inline_image_data_url(&url).expect("redacts image url");

        assert_eq!(redaction.mime_type, "data:image/png");
        assert_eq!(redaction.byte_size, 600);
        assert_eq!(redaction.original_chars, url.len());
        assert!(redaction
            .placeholder
            .contains("longhouse_media_ref:sha256="));
        assert!(redaction.placeholder.contains(";mime=data:image/png;"));
        assert!(redaction.placeholder.contains(";bytes=600;"));
        assert!(redaction
            .placeholder
            .contains(&format!(";original_chars={}", url.len())));
        assert!(!redaction.placeholder.contains(&data));
    }

    #[test]
    fn leaves_small_data_image_url_alone() {
        assert!(redact_inline_image_data_url("data:image/png;base64,abc123").is_none());
    }

    #[test]
    fn redacts_data_url_inside_json_line_without_reordering_json() {
        let data = general_purpose::STANDARD.encode([3u8; 600]);
        let raw = format!(r#"{{"b":1,"image_url":"data:image/png;base64,{data}","a":2}}"#);
        let redacted = redact_inline_image_data_urls_in_json_line(&raw);

        assert!(matches!(redacted, Cow::Owned(_)));
        assert!(redacted.starts_with(r#"{"b":1,"image_url":"#));
        assert!(redacted.ends_with(r#"","a":2}"#));
        assert!(redacted.contains("longhouse_media_ref:sha256="));
        assert!(!redacted.contains(&data));
    }
}
