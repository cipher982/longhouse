//! Cheap source-line reconciliation before replaying archive transcript bytes.

use std::time::Duration;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::shipper::SourceLineRef;
use crate::shipping::client::ShipperClient;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SourceLineClaimsSummary {
    pub claimed: usize,
    pub present: usize,
    pub missing: usize,
}

#[derive(Serialize)]
struct SourceLineClaimsRequest<'a> {
    items: Vec<SourceLineClaimItem<'a>>,
}

#[derive(Serialize)]
struct SourceLineClaimItem<'a> {
    session_id: &'a str,
    source_path: &'a str,
    source_offset: u64,
    line_hash: &'a str,
}

#[derive(Deserialize)]
struct SourceLineClaimsResponse {
    present: Vec<SourceLineClaimResponseItem>,
    missing: Vec<SourceLineClaimResponseItem>,
    rejected: Vec<SourceLineRejectedItem>,
}

#[derive(Deserialize)]
struct SourceLineClaimResponseItem {
    source_path: String,
    source_offset: u64,
    line_hash: String,
}

#[derive(Deserialize)]
struct SourceLineRejectedItem {
    source_path: Option<String>,
    source_offset: Option<u64>,
    line_hash: Option<String>,
    reason: String,
}

pub async fn claim_source_lines_present(
    client: &ShipperClient,
    session_id: &str,
    source_path: &str,
    source_lines: &[SourceLineRef],
    request_timeout: Option<Duration>,
) -> Result<SourceLineClaimsSummary> {
    if source_lines.is_empty() {
        return Ok(SourceLineClaimsSummary::default());
    }
    validate_session_uuid(session_id)?;

    let request = SourceLineClaimsRequest {
        items: source_lines
            .iter()
            .map(|line| SourceLineClaimItem {
                session_id,
                source_path,
                source_offset: line.source_offset,
                line_hash: &line.line_hash,
            })
            .collect(),
    };
    let claimed = request.items.len();
    let body = serde_json::to_vec(&request).context("serializing source-line claims")?;
    let response: SourceLineClaimsResponse = client
        .post_json_decode_with_timeout("/api/agents/source-lines/claims", body, request_timeout)
        .await
        .context("claiming source lines")?;

    if !response.rejected.is_empty() {
        let reasons = response
            .rejected
            .iter()
            .map(|item| {
                format!(
                    "{}:{}:{}:{}",
                    item.source_path.as_deref().unwrap_or(""),
                    item.source_offset
                        .map(|value| value.to_string())
                        .unwrap_or_default(),
                    item.line_hash.as_deref().unwrap_or(""),
                    item.reason
                )
            })
            .collect::<Vec<_>>()
            .join(", ");
        bail!("source-line claim rejected: {reasons}");
    }

    let present = response.present.len();
    let missing = response.missing.len();
    let accounted = present + missing;
    if accounted != claimed {
        bail!("source-line claim response accounted for {accounted} of {claimed} item(s)");
    }
    for item in response.present.iter().chain(response.missing.iter()) {
        if item.source_path != source_path {
            bail!(
                "source-line claim returned unexpected source path {}",
                item.source_path
            );
        }
        if !source_lines.iter().any(|line| {
            line.source_offset == item.source_offset && line.line_hash == item.line_hash
        }) {
            bail!(
                "source-line claim returned unexpected line {}:{}",
                item.source_offset,
                item.line_hash
            );
        }
    }

    Ok(SourceLineClaimsSummary {
        claimed,
        present,
        missing,
    })
}

fn validate_session_uuid(session_id: &str) -> Result<()> {
    Uuid::parse_str(session_id)
        .with_context(|| format!("source-line claim session_id is not a UUID: {session_id}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn claim_request_keeps_source_line_identity() {
        let source_lines = [
            SourceLineRef {
                source_offset: 10,
                line_hash: "a".repeat(64),
            },
            SourceLineRef {
                source_offset: 20,
                line_hash: "b".repeat(64),
            },
        ];
        let request = SourceLineClaimsRequest {
            items: source_lines
                .iter()
                .map(|line| SourceLineClaimItem {
                    session_id: "019c638d-0000-0000-0000-000000000001",
                    source_path: "/tmp/session.jsonl",
                    source_offset: line.source_offset,
                    line_hash: &line.line_hash,
                })
                .collect(),
        };
        let value = serde_json::to_value(&request).unwrap();
        let items = value["items"].as_array().unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0]["source_offset"], 10);
        assert_eq!(items[0]["line_hash"], "a".repeat(64));
        assert_eq!(items[1]["source_offset"], 20);
        assert_eq!(items[1]["line_hash"], "b".repeat(64));
    }
}
