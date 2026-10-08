//! The Cursor store source: prompt history, turn alignment and render records.

use super::*;

/// Cursor writes the prompts a person actually typed to `prompt_history.json`
/// beside `store.db`. That file is the provider's own record of human
/// authorship, which neither the store nor the hook stream gives us: Cursor
/// wraps its own auto-continuations ("Briefly inform the user about the task
/// result…") in the same `<user_query>` envelope as a real prompt, and it
/// injects harness preambles as `role="user"` records. Both then render as
/// messages the user appears to have written.
///
/// Absence of a hook receipt would be the wrong test — Shadow sessions have no
/// hooks, hook files can be truncated, and Cursor does emit receipts for some
/// internal prompts. A positive record of what a human typed has none of those
/// failure modes, and it fails open: no file, an unreadable file, or a file
/// that matches nothing in this conversation leaves every turn a user turn.
#[derive(Debug, Default)]
pub(super) struct CursorPromptHistory {
    entries: std::collections::HashSet<String>,
}

impl CursorPromptHistory {
    pub(super) fn load(db_path: &Path) -> Self {
        let Some(dir) = db_path.parent() else {
            return Self::default();
        };
        let Ok(bytes) = std::fs::read(dir.join("prompt_history.json")) else {
            return Self::default();
        };
        let Ok(Value::Array(items)) = serde_json::from_slice::<Value>(&bytes) else {
            return Self::default();
        };
        // Cursor elides pasted blocks in the history as "[Pasted text #1 +83
        // lines]" while the store holds them expanded. Without the sidecar a
        // real prompt containing a paste would look unmatched.
        let pastes = Self::pasted_text(dir);
        Self {
            entries: items
                .iter()
                .filter_map(Value::as_str)
                .map(|entry| Self::expand_pastes(entry, &pastes).trim().to_string())
                .filter(|entry| !entry.is_empty())
                .collect(),
        }
    }

    pub(super) fn pasted_text(dir: &Path) -> HashMap<String, String> {
        let Ok(bytes) = std::fs::read(dir.join("pasted_text.json")) else {
            return HashMap::new();
        };
        let Ok(value) = serde_json::from_slice::<Value>(&bytes) else {
            return HashMap::new();
        };
        value
            .get("entries")
            .and_then(Value::as_object)
            .map(|entries| {
                entries
                    .iter()
                    .filter_map(|(key, value)| {
                        value.as_str().map(|text| (key.clone(), text.to_string()))
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Replace `[Pasted text #<id> ...]` with the sidecar's content. A pasted
    /// block can itself quote a placeholder, so expansion repeats until it
    /// reaches a fixed point, bounded so a self-referential sidecar cannot spin.
    pub(super) fn expand_pastes(entry: &str, pastes: &HashMap<String, String>) -> String {
        let mut expanded = Self::expand_pastes_once(entry, pastes);
        for _ in 0..4 {
            let next = Self::expand_pastes_once(&expanded, pastes);
            if next == expanded {
                break;
            }
            expanded = next;
        }
        expanded
    }

    pub(super) fn expand_pastes_once(entry: &str, pastes: &HashMap<String, String>) -> String {
        const OPEN: &str = "[Pasted text #";
        let mut out = String::with_capacity(entry.len());
        let mut rest = entry;
        while let Some(start) = rest.find(OPEN) {
            let (before, tail) = rest.split_at(start);
            out.push_str(before);
            let body = &tail[OPEN.len()..];
            let Some(end) = body.find(']') else {
                out.push_str(tail);
                return out;
            };
            let token = &body[..end];
            let id = token
                .split(|c: char| !c.is_ascii_digit())
                .next()
                .unwrap_or_default();
            match pastes.get(id) {
                Some(text) => out.push_str(text),
                None => {
                    out.push_str(OPEN);
                    out.push_str(token);
                    out.push(']');
                }
            }
            rest = &body[end + 1..];
        }
        out.push_str(rest);
        out
    }

    pub(super) fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub(super) fn contains(&self, prompt: &str) -> bool {
        self.entries.contains(prompt.trim())
    }
}

/// The history file is only trustworthy for a conversation it demonstrably
/// covers. Requiring one confirmed match keeps a rotated or capped history from
/// demoting every real prompt in a long session.
pub(super) fn cursor_human_prompt_witness(
    snapshot: &cursor_store::CursorStoreSnapshot,
    db_path: &Path,
) -> Option<CursorPromptHistory> {
    let history = CursorPromptHistory::load(db_path);
    if history.is_empty() {
        return None;
    }
    let cursor_store::RootMessageBlobIds::Parsed(root_ids) = &snapshot.root_message_blob_ids else {
        return None;
    };
    let blobs = snapshot
        .blob_rows
        .iter()
        .map(|row| (row.id.as_str(), row.data_bytes.as_slice()))
        .collect::<HashMap<_, _>>();
    let corroborated = root_ids.iter().any(|blob_id| {
        let Some(bytes) = blobs.get(blob_id.as_str()) else {
            return false;
        };
        let Ok(message) = serde_json::from_slice::<Value>(bytes) else {
            return false;
        };
        if message.get("role").and_then(Value::as_str) != Some("user") {
            return false;
        }
        cursor_message_blocks(&message)
            .iter()
            .filter_map(|block| block.get("text").and_then(Value::as_str))
            .any(|text| {
                let (role, body) = classify_cursor_text("user", text);
                role == "user" && history.contains(&body)
            })
    });
    corroborated.then_some(history)
}

#[derive(Debug, Default)]
pub(super) struct CursorTextProjection {
    suppressed: HashSet<(String, usize)>,
    abandoned: HashSet<(String, usize)>,
}

#[derive(Debug)]
pub(super) struct CursorStoreTurn {
    prompt: String,
    suppressible_blocks: Vec<(String, usize)>,
    text_blocks: Vec<(String, usize, String)>,
}

pub(super) fn cursor_message_blocks(message: &Value) -> Vec<Value> {
    match message.get("content") {
        Some(Value::Array(values)) => values.clone(),
        Some(Value::String(text)) => vec![serde_json::json!({"type":"text","text":text})],
        _ => Vec::new(),
    }
}

/// Turn alignment deliberately does not consult the prompt-history witness.
/// A store turn is whatever Cursor sent the model, and dropping one shifts every
/// later receipt: the turn's assistant blocks would attach to the previous turn,
/// `unique_cursor_receipt_path` would fail to reconstruct that receipt, and a
/// committed reply would be suppressed. The witness decides a row's *label*, and
/// a label must never be able to hide an answer.
pub(super) fn cursor_text_projection(
    snapshot: &cursor_store::CursorStoreSnapshot,
    evidence: Option<&crate::cursor_visibility::CursorVisibilityEvidence>,
) -> CursorTextProjection {
    let Some(evidence) = evidence else {
        return CursorTextProjection::default();
    };
    let cursor_store::RootMessageBlobIds::Parsed(root_ids) = &snapshot.root_message_blob_ids else {
        return CursorTextProjection::default();
    };
    let blobs = snapshot
        .blob_rows
        .iter()
        .map(|row| (row.id.as_str(), row.data_bytes.as_slice()))
        .collect::<HashMap<_, _>>();
    let mut store_turns = Vec::<CursorStoreTurn>::new();
    let mut current_turn: Option<CursorStoreTurn> = None;
    for blob_id in root_ids {
        let Some(bytes) = blobs.get(blob_id.as_str()) else {
            continue;
        };
        let Ok(message) = serde_json::from_slice::<Value>(bytes) else {
            continue;
        };
        let role = message
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("assistant");
        let blocks = cursor_message_blocks(&message);
        if role == "user" {
            let prompt = blocks
                .iter()
                .filter_map(|block| block.get("text").and_then(Value::as_str))
                .find_map(|text| {
                    let (effective_role, effective_text) = classify_cursor_text(role, text);
                    (effective_role == "user").then_some(effective_text)
                });
            // Cursor stores injected context as a user-role message, but it
            // is not a provider turn and has no matching hook receipt. Do
            // not let those context records shift the receipt/store-turn
            // alignment for the next real prompt.
            let Some(prompt) = prompt else {
                continue;
            };
            if let Some(turn) = current_turn.take() {
                store_turns.push(turn);
            }
            current_turn = Some(CursorStoreTurn {
                prompt,
                suppressible_blocks: Vec::new(),
                text_blocks: Vec::new(),
            });
            continue;
        }
        let Some(turn) = current_turn.as_mut() else {
            continue;
        };
        for (subordinal, block) in blocks.iter().enumerate() {
            let kind = block.get("type").and_then(Value::as_str);
            if matches!(kind, Some("text" | "reasoning")) {
                turn.suppressible_blocks.push((blob_id.clone(), subordinal));
            }
            if kind == Some("text") && block.get("text").and_then(Value::as_str).is_some() {
                turn.text_blocks.push((
                    blob_id.clone(),
                    subordinal,
                    block
                        .get("text")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                ));
            }
        }
    }
    if let Some(turn) = current_turn {
        store_turns.push(turn);
    }

    let mut projection = CursorTextProjection::default();
    for store_turn in &store_turns {
        projection.suppressed.extend(
            store_turn
                .suppressible_blocks
                .iter()
                .map(|(blob_id, subordinal)| (blob_id.clone(), *subordinal)),
        );
    }
    if evidence.ambiguous {
        tracing::warn!(
            reason = "conflicting_provider_receipt",
            store_turn_count = store_turns.len(),
            receipt_turn_count = evidence.turns.len(),
            "Suppressing managed Cursor assistant text"
        );
        return projection;
    }
    let Some(alignment) = unique_cursor_turn_alignment(&store_turns, &evidence.turns) else {
        tracing::warn!(
            reason = "ambiguous_turn_alignment",
            store_turn_count = store_turns.len(),
            receipt_turn_count = evidence.turns.len(),
            "Suppressing managed Cursor assistant text"
        );
        return projection;
    };
    for (store_index, evidence_index) in alignment {
        let store_turn = &store_turns[store_index];
        let receipt_turn = &evidence.turns[evidence_index];
        if let Some(response_text) = receipt_turn.response_text.as_ref() {
            if let Some(indices) =
                unique_cursor_receipt_path(&store_turn.text_blocks, response_text)
            {
                for index in indices {
                    let (blob_id, subordinal, _) = &store_turn.text_blocks[index];
                    projection
                        .suppressed
                        .remove(&(blob_id.clone(), *subordinal));
                }
            } else {
                tracing::warn!(
                    reason = "ambiguous_receipt_binding",
                    generation_id = receipt_turn.generation_id,
                    text_block_count = store_turn.text_blocks.len(),
                    "Suppressing managed Cursor assistant text"
                );
            }
        } else if matches!(
            receipt_turn.stop_status.as_deref(),
            Some("error" | "aborted")
        ) {
            // Native output survives a failed turn, but no successful response
            // receipt authorizes it as a head reply. Keep reasoning raw-only.
            for (blob_id, subordinal, _) in &store_turn.text_blocks {
                let key = (blob_id.clone(), *subordinal);
                projection.suppressed.remove(&key);
                projection.abandoned.insert(key);
            }
        }
    }
    projection
}

pub(super) fn unique_cursor_turn_alignment(
    store_turns: &[CursorStoreTurn],
    receipt_turns: &[crate::cursor_visibility::CursorProviderTurn],
) -> Option<Vec<(usize, usize)>> {
    // Cursor can emit lifecycle receipts for provider-internal prompts that
    // are not persisted as user messages in store.db.  Those receipts cannot
    // authorize or suppress any stored assistant text, so exclude them from
    // the alignment sequence.  Receipts whose prompt does match a stored
    // turn remain subject to the same unique-path check; repeated prompts are
    // still rejected as ambiguous rather than guessed.
    let relevant_receipts = receipt_turns
        .iter()
        .enumerate()
        .filter(|(_, receipt_turn)| {
            store_turns
                .iter()
                .any(|store_turn| store_turn.prompt.trim() == receipt_turn.prompt.trim())
        })
        .collect::<Vec<_>>();
    if relevant_receipts.is_empty() {
        return Some(Vec::new());
    }
    let mut states = HashMap::<usize, (u8, Vec<usize>)>::new();
    for (store_index, store_turn) in store_turns.iter().enumerate() {
        if store_turn.prompt.trim() == relevant_receipts[0].1.prompt.trim() {
            states.insert(store_index, (1, vec![store_index]));
        }
    }
    for (_, receipt_turn) in relevant_receipts.iter().skip(1) {
        let mut next = HashMap::<usize, (u8, Vec<usize>)>::new();
        for (previous_index, (path_count, path)) in &states {
            for (store_index, store_turn) in
                store_turns.iter().enumerate().skip(*previous_index + 1)
            {
                if store_turn.prompt.trim() != receipt_turn.prompt.trim() {
                    continue;
                }
                let entry = next.entry(store_index).or_insert_with(|| {
                    let mut candidate = path.clone();
                    candidate.push(store_index);
                    (0, candidate)
                });
                entry.0 = entry.0.saturating_add(*path_count).min(2);
            }
        }
        states = next;
    }
    let path_count = states
        .values()
        .fold(0u8, |total, (count, _)| total.saturating_add(*count).min(2));
    if path_count != 1 {
        return None;
    }
    let (_, path) = states.into_values().find(|(count, _)| *count == 1)?;
    Some(
        path.into_iter()
            .enumerate()
            .map(|(relevant_index, store_index)| (store_index, relevant_receipts[relevant_index].0))
            .collect(),
    )
}

pub(super) fn unique_cursor_receipt_path(
    text_blocks: &[(String, usize, String)],
    response_text: &str,
) -> Option<Vec<usize>> {
    let mut paths = HashMap::<usize, Vec<Vec<usize>>>::from([(0, vec![Vec::new()])]);
    for (index, (_, _, text)) in text_blocks.iter().enumerate() {
        if text.is_empty() {
            continue;
        }
        let previous = paths.clone();
        for (offset, candidates) in previous {
            let Some(suffix) = response_text.get(offset..) else {
                continue;
            };
            if !suffix.starts_with(text) {
                continue;
            }
            let next_offset = offset + text.len();
            let next_paths = paths.entry(next_offset).or_default();
            for candidate in candidates {
                if next_paths.len() >= 2 {
                    break;
                }
                let mut next = candidate;
                next.push(index);
                if !next_paths.contains(&next) {
                    next_paths.push(next);
                }
            }
        }
    }
    let paths = paths.remove(&response_text.len())?;
    (paths.len() == 1).then(|| paths.into_iter().next().expect("one receipt path exists"))
}

pub(super) fn cursor_render_records(
    snapshot: &cursor_store::CursorStoreSnapshot,
    selected: &[cursor_store_records::CursorRawRecord],
    started_at_us: i64,
    visibility_evidence: Option<&crate::cursor_visibility::CursorVisibilityEvidence>,
    witness: Option<&CursorPromptHistory>,
    provider_error: Option<&str>,
) -> Result<Vec<StorageV2RenderRecord>> {
    let cursor_store::RootMessageBlobIds::Parsed(root_ids) = &snapshot.root_message_blob_ids else {
        return Ok(Vec::new());
    };
    let mut selected_blobs: HashMap<String, (u64, usize, Vec<u8>)> = HashMap::new();
    for (raw_record_ordinal, record) in selected.iter().enumerate() {
        let Ok(wrapper) = serde_json::from_slice::<Value>(&record.bytes) else {
            continue;
        };
        if !matches!(
            wrapper.get("kind").and_then(Value::as_str),
            Some("blob" | "root_reference")
        ) {
            continue;
        }
        let Some(blob_id) = wrapper.get("blob_id").and_then(Value::as_str) else {
            continue;
        };
        let Some(encoded) = wrapper.get("blob_bytes_b64").and_then(Value::as_str) else {
            continue;
        };
        let Ok(bytes) = BASE64_STANDARD.decode(encoded) else {
            continue;
        };
        selected_blobs.insert(
            blob_id.to_string(),
            (record.source_position, raw_record_ordinal, bytes),
        );
    }
    let projection = cursor_text_projection(snapshot, visibility_evidence);
    // The conversation's last message decides which page owns a turn-outcome
    // row, so a paged capture emits it once instead of once per page.
    let carries_conversation_tail = root_ids
        .last()
        .is_some_and(|tail| selected_blobs.contains_key(tail));
    let mut records = Vec::new();
    for (message_order, blob_id) in root_ids.iter().enumerate() {
        let Some((source_position, raw_record_ordinal, blob_bytes)) = selected_blobs.get(blob_id)
        else {
            continue;
        };
        let message: Value = match serde_json::from_slice(blob_bytes) {
            Ok(value) => value,
            Err(_) => continue,
        };
        let role = message
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("assistant");
        let blocks = cursor_message_blocks(&message);
        for (subordinal, block) in blocks.iter().enumerate() {
            let kind = block
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let mut abandoned = false;
            let mut interaction_kind: Option<String> = None;
            let (
                event_role,
                content_text,
                tool_name,
                tool_input_json,
                tool_output_text,
                tool_call_id,
            ) = match kind {
                "text" | "reasoning" => {
                    let text = block
                        .get("text")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    if matches!(kind, "text" | "reasoning") {
                        let key = (blob_id.clone(), subordinal);
                        if projection.suppressed.contains(&key) {
                            continue;
                        }
                        abandoned = projection.abandoned.contains(&key);
                    }
                    let (effective_role, effective_text, kind_override) = if kind == "reasoning" {
                        ("assistant".to_string(), text, None)
                    } else {
                        classify_cursor_text_with_witness(role, &text, witness)
                    };
                    interaction_kind = kind_override;
                    (effective_role, Some(effective_text), None, None, None, None)
                }
                "tool-call" => (
                    "assistant".to_string(),
                    None,
                    block
                        .get("toolName")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                    block.get("args").or_else(|| block.get("input")).cloned(),
                    None,
                    block
                        .get("toolCallId")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                ),
                "tool-result" => (
                    "tool".to_string(),
                    None,
                    block
                        .get("toolName")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                    None,
                    block.get("result").map(|value| match value {
                        Value::String(text) => text.clone(),
                        other => other.to_string(),
                    }),
                    block
                        .get("toolCallId")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                ),
                _ => (
                    role.to_string(),
                    Some(String::new()),
                    None,
                    None,
                    None,
                    None,
                ),
            };
            let event_id_material = format!("cursor:{blob_id}:{subordinal}");
            records.push(StorageV2RenderRecord {
                event_id: Uuid::new_v5(&Uuid::NAMESPACE_URL, event_id_material.as_bytes())
                    .to_string(),
                order_time_us: started_at_us + message_order as i64 * 1_000 + subordinal as i64,
                source_position: *source_position,
                event_subordinal: subordinal as u32,
                role: event_role,
                content_text,
                tool_name,
                tool_input_json,
                tool_output_text,
                tool_call_id,
                thread_id: None,
                branch_kind: if abandoned {
                    Some("abandoned".to_string())
                } else {
                    (kind == "reasoning").then(|| "reasoning".to_string())
                },
                parent_uuid: None,
                interaction_kind,
                raw_record_ordinal: *raw_record_ordinal,
            });
        }
    }
    if carries_conversation_tail {
        append_cursor_turn_failure(
            &mut records,
            visibility_evidence,
            started_at_us,
            provider_error,
        );
    }
    Ok(records)
}

/// Cursor reports a failed turn only through its hook stream: the store keeps
/// whatever prose the attempt produced (suppressed or abandoned, never a head
/// reply) and says nothing about the outcome. Without this row a failed turn
/// reads as a session that simply stopped talking — which is exactly what the
/// phone showed while the terminal showed an error.
///
/// Exactly one row, on the page that carries the conversation's last message,
/// and only while nothing has superseded the failure. `event_key` is derived
/// per render object, so appending to every page of a paged capture would put
/// one duplicate in the timeline per page.
pub(super) fn append_cursor_turn_failure(
    records: &mut Vec<StorageV2RenderRecord>,
    visibility_evidence: Option<&crate::cursor_visibility::CursorVisibilityEvidence>,
    started_at_us: i64,
    provider_error: Option<&str>,
) {
    let Some(failure) = visibility_evidence.and_then(|evidence| evidence.unsuperseded_failure())
    else {
        return;
    };
    // Anchor to the last selected record: the row reads raw evidence this
    // envelope already carries rather than inventing a record of its own.
    let Some((source_position, raw_record_ordinal, last_order_time_us)) =
        records.last().map(|record| {
            (
                record.source_position,
                record.raw_record_ordinal,
                record.order_time_us,
            )
        })
    else {
        return;
    };
    let event_id_material = format!("cursor:turn-failed:{}", failure.generation_id);
    // Prefer the provider's own words. Without them the row still has to say
    // something a reader can act on, so it says what is certain: the turn
    // failed, and nothing it produced became a reply.
    let label = match provider_error {
        Some(message) if failure.status == "aborted" => {
            format!("Cursor stopped this turn before it finished: {message}")
        }
        Some(message) => format!("Cursor reported this turn failed: {message}"),
        None if failure.status == "aborted" => {
            "Cursor stopped this turn before it finished.".to_string()
        }
        None => {
            "Cursor reported this turn failed. Its output was not committed as a reply.".to_string()
        }
    };
    records.push(StorageV2RenderRecord {
        event_id: Uuid::new_v5(&Uuid::NAMESPACE_URL, event_id_material.as_bytes()).to_string(),
        parent_uuid: None,
        order_time_us: last_order_time_us.max(started_at_us).saturating_add(1),
        source_position,
        event_subordinal: 0,
        role: "system".to_string(),
        content_text: Some(label),
        tool_name: None,
        tool_input_json: None,
        tool_output_text: None,
        tool_call_id: None,
        thread_id: None,
        branch_kind: None,
        interaction_kind: Some("provider_notification".to_string()),
        raw_record_ordinal,
    });
}

/// Cursor's own record of typed prompts decides authorship when it covers this
/// conversation; otherwise the envelope rules stand on their own.
///
/// A demoted turn is marked, not hidden. `prompt_history.json` may be capped or
/// rotated, so the cost of being wrong has to be a mislabelled row rather than
/// a real message the user can no longer find — the same treatment Claude's
/// background-task envelope already gets.
pub(super) fn classify_cursor_text_with_witness(
    role: &str,
    text: &str,
    witness: Option<&CursorPromptHistory>,
) -> (String, String, Option<String>) {
    let (effective_role, effective_text) = classify_cursor_text(role, text);
    if effective_role != "user" {
        return (effective_role, effective_text, None);
    }
    match witness {
        Some(history) if !history.contains(&effective_text) => (
            "system".to_string(),
            effective_text,
            Some("provider_notification".to_string()),
        ),
        _ => (effective_role, effective_text, None),
    }
}

pub(super) fn classify_cursor_text(role: &str, text: &str) -> (String, String) {
    if role != "user" {
        return (role.to_string(), text.to_string());
    }
    let (effective_text, effective_role) = parser::cursor_user_text(text);
    let effective_role = match effective_role {
        Role::System => "system",
        Role::User => "user",
        _ => role,
    };
    (effective_role.to_string(), effective_text)
}

#[cfg(test)]
pub(crate) fn prepare_next_cursor_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
) -> Result<Option<PreparedStorageV2Envelope>> {
    Ok(
        match prepare_next_cursor_envelope_outcome(conn, capabilities, db_path)? {
            CursorPreparationOutcome::Envelope(prepared) => Some(prepared),
            CursorPreparationOutcome::Current
            | CursorPreparationOutcome::WaitingOnClaim
            | CursorPreparationOutcome::Continue => None,
        },
    )
}

pub(crate) fn prepare_next_cursor_envelope_outcome(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
) -> Result<CursorPreparationOutcome> {
    prepare_next_cursor_envelope_outcome_with_limit(
        conn,
        capabilities,
        db_path,
        capabilities.max_raw_record_bytes,
    )
}

#[cfg(test)]
pub(super) fn prepare_next_cursor_envelope_with_limit(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
    maximum_batch_bytes: u64,
) -> Result<Option<PreparedStorageV2Envelope>> {
    Ok(
        match prepare_next_cursor_envelope_outcome_with_limit(
            conn,
            capabilities,
            db_path,
            maximum_batch_bytes,
        )? {
            CursorPreparationOutcome::Envelope(prepared) => Some(prepared),
            CursorPreparationOutcome::Current
            | CursorPreparationOutcome::WaitingOnClaim
            | CursorPreparationOutcome::Continue => None,
        },
    )
}

pub(super) fn should_wait_for_unclaimed_cursor_source(
    source_was_seen: bool,
    launch_reservation_may_be_pending: bool,
    reset_binding_may_be_pending: bool,
) -> bool {
    // A launch reservation is only meaningful for a brand-new source.  A
    // resumed Cursor conversation can rotate to a new provider store after
    // the source has already been seen, so the exact single-owner rollover
    // predicate must remain authoritative in that case.
    (!source_was_seen && launch_reservation_may_be_pending) || reset_binding_may_be_pending
}

/// Everything outside the database that a pass over a Cursor store depended on:
/// the store's stamp, the launch claim naming its conversation (a claim that
/// arrives later rebinds the source with no store write), and the build (a new
/// build may read a store differently). The same key later means the same
/// inputs.
pub(super) fn cursor_store_rest_key(store_stamp: Option<&str>, launch_binding_key: &str) -> String {
    format!(
        "{}|{launch_binding_key}|{}",
        store_stamp.unwrap_or_default(),
        crate::build_identity::COMMIT
    )
}

/// Whether this Cursor store is exactly as the last complete pass left it and
/// owes the host nothing, so the store need not be opened at all.
///
/// The last pass wrote a rest row when it ended with everything it had
/// captured shipped. A stamp and claim key that still match say the store and
/// the evidence around it did not move; the database is asked again for the
/// rest, because that can change with no store write: the source
/// rotated to a new epoch or parser revision, some epoch of it has records the
/// host has not received (a lane rewound, or a repair of a missing payload,
/// which announces itself the same way), an envelope is waiting for its epoch
/// (blocked or not), or the capture walk no longer says its last cycle finished.
pub(super) fn cursor_store_is_settled(
    conn: &Connection,
    path_text: &str,
    store_stamp: Option<&str>,
) -> Result<bool> {
    let Some(store_stamp) = store_stamp else {
        return Ok(false);
    };
    let Some(rest) = cursor_store_records::load_store_rest(conn, path_text)? else {
        return Ok(false);
    };
    let launch_binding_key = format!(
        "{:?}",
        crate::cursor_launch_binding::launch_binding_state_for_conversation(
            &rest.conversation_uuid
        )?
    );
    if rest.rest_key != cursor_store_rest_key(Some(store_stamp), &launch_binding_key) {
        return Ok(false);
    }
    let opaque_source_id = cursor_store::cursor_opaque_source_id(&rest.conversation_uuid);
    if source_epoch::active_source_epoch(conn, "cursor", &opaque_source_id)?
        != Some(rest.source_epoch)
        || source_epoch::active_source_revision(conn, "cursor", &opaque_source_id)?.as_deref()
            != Some(CURSOR_PARSER_REVISION)
        || cursor_store_records::oldest_undrained_epoch(conn, "cursor", &opaque_source_id)?
            .is_some()
        || pending_source_envelope::exists_for_epoch(conn, rest.source_epoch)?
    {
        return Ok(false);
    }
    Ok(cursor_store_records::capture_walk(conn, rest.source_epoch)?
        .store_is_unchanged_since_last_cycle(Some(store_stamp)))
}

pub(super) fn prepare_next_cursor_envelope_outcome_with_limit(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
    maximum_batch_bytes: u64,
) -> Result<CursorPreparationOutcome> {
    let canonical_path = stable_source_path(db_path);
    let path_text = canonical_path.to_string_lossy();
    if let Some(pending) = pending_source_envelope::load_for_path(conn, &path_text)? {
        let prepared = pending_to_prepared(pending.clone())?;
        if pending.blocked_at.is_some() {
            tracing::debug!(
                source_epoch = %pending.source_epoch,
                "Deferring blocked Cursor envelope to lineage selection"
            );
        } else {
            let oversized_unattempted = pending.raw_bytes > maximum_batch_bytes
                && pending.range_end.saturating_sub(pending.range_start) > 1
                && pending.attempt_count == 0
                && maximum_batch_bytes < capabilities.max_raw_record_bytes;
            let obsolete_unattempted_render = pending.attempt_count == 0
                && prepared
                    .envelope
                    .render
                    .as_ref()
                    .is_none_or(|render| render.parser_revision != CURSOR_PARSER_REVISION);
            if !(oversized_unattempted || obsolete_unattempted_render)
                || !pending_source_envelope::discard_unattempted(
                    conn,
                    pending.source_epoch,
                    &pending.envelope_id,
                )?
            {
                return Ok(CursorPreparationOutcome::Envelope(prepared));
            }
        }
    }
    let metadata_before = db_path
        .metadata()
        .with_context(|| format!("reading Cursor store metadata {}", db_path.display()))?;
    let store_incarnation = identity_from_metadata(&metadata_before)
        .context("Cursor store has no stable file incarnation")?;
    // Stamped before anything is read from the store, so a write that lands
    // while this pass reads is seen as a change by the next one.
    let store_stamp = wal_database_stamp(db_path);
    if cursor_store_is_settled(conn, &path_text, store_stamp.as_deref())? {
        return Ok(CursorPreparationOutcome::Current);
    }
    // Cursor records its working directory in the sidecar beside the store, not
    // in the transcript, so recover it here and let the shared project
    // derivation do the rest. Absent or unreadable leaves the session
    // unattributed, exactly as before.
    let workspace_facts = cursor_store::cursor_workspace_facts(db_path);
    let mut store_snapshot = cursor_store::read_cursor_render_snapshot(db_path)?;
    let identity_after_render = identity_from_metadata(
        &db_path
            .metadata()
            .with_context(|| format!("rechecking Cursor store metadata {}", db_path.display()))?,
    );
    if !file_identities_match(
        Some(store_incarnation.as_str()),
        identity_after_render.as_deref(),
    ) {
        anyhow::bail!("Cursor store file changed identity during root capture");
    }
    let snapshot =
        cursor_store::cursor_store_raw_snapshot_from(&store_snapshot, store_incarnation.clone())?;
    let opaque_source_id = cursor_store::cursor_opaque_source_id(&snapshot.conversation_uuid);
    let source_was_seen =
        source_epoch::active_source_incarnation(conn, "cursor", &opaque_source_id)?.is_some();
    let store_is_fresh = snapshot
        .created_at_ms
        .and_then(DateTime::from_timestamp_millis)
        .map(|created_at| {
            Utc::now().signed_duration_since(created_at) <= chrono::Duration::seconds(5)
        })
        .or_else(|| {
            metadata_before
                .created()
                .ok()
                .and_then(|created| created.elapsed().ok())
                .map(|age| age <= Duration::from_secs(5))
        })
        .unwrap_or(false);
    let launch_reservation_may_be_pending =
        crate::cursor_launch_binding::launch_reservation_may_be_pending()?;
    let launch_binding_state = crate::cursor_launch_binding::launch_binding_state_for_conversation(
        &snapshot.conversation_uuid,
    )?;
    let launch_binding_key = format!("{launch_binding_state:?}");
    let claimed_binding = match launch_binding_state {
        crate::cursor_launch_binding::CursorLaunchBindingState::Managed(binding) => Some(binding),
        crate::cursor_launch_binding::CursorLaunchBindingState::Pending => {
            return Ok(CursorPreparationOutcome::WaitingOnClaim);
        }
        crate::cursor_launch_binding::CursorLaunchBindingState::Unclaimed
            if should_wait_for_unclaimed_cursor_source(
                source_was_seen,
                launch_reservation_may_be_pending,
                store_is_fresh
                    && crate::cursor_launch_binding::reset_binding_may_be_pending(
                        &snapshot.conversation_uuid,
                    )?,
            ) =>
        {
            // A live managed owner can rotate an already-seen Cursor
            // conversation (including native Resume) before its foreground
            // hook publishes the new provider identity.  The exact
            // reset_binding_may_be_pending predicate requires one active
            // managed state and its observed predecessor, so this does not
            // turn historical Shadow sources into an unbounded wait.
            return Ok(CursorPreparationOutcome::WaitingOnClaim);
        }
        crate::cursor_launch_binding::CursorLaunchBindingState::Unclaimed => None,
    };
    let claimed_session_id = claimed_binding
        .as_ref()
        .map(|binding| binding.session_id.clone());
    let visibility_evidence = claimed_binding
        .as_ref()
        .map(|binding| {
            crate::cursor_visibility::load_cursor_visibility_evidence(
                &binding.session_id,
                &snapshot.conversation_uuid,
            )
            .map(|evidence| {
                if evidence.is_none() {
                    tracing::warn!(
                        session_id = binding.session_id,
                        conversation_id = snapshot.conversation_uuid,
                        reason = "missing_provider_receipt",
                        "Suppressing managed Cursor assistant text"
                    );
                }
                evidence.unwrap_or_default()
            })
        })
        .transpose()?;
    // Do not consume raw records while the provider's turn receipt is still
    // racing the terminal provider receipt. Both managed adapters wake the
    // shipper after that receipt, so the settled turn is captured without
    // permanently losing its render.
    if let Some(wait) = visibility_evidence
        .as_ref()
        .and_then(|evidence| evidence.unsettled_reason())
    {
        tracing::warn!(
            session_id = claimed_session_id.as_deref().unwrap_or_default(),
            conversation_id = snapshot.conversation_uuid,
            reason = wait.as_str(),
            "Waiting for managed Cursor visibility evidence"
        );
        return Ok(CursorPreparationOutcome::Current);
    }
    if visibility_evidence.is_some() {
        if let cursor_store::RootMessageBlobIds::Parsed(root_ids) =
            &store_snapshot.root_message_blob_ids
        {
            for ids in root_ids.chunks(256) {
                store_snapshot
                    .blob_rows
                    .extend(cursor_store::read_cursor_blob_rows(db_path, ids)?);
            }
        }
    }
    let previous_root_ids =
        cursor_store_root::previous_message_blob_ids(conn, &snapshot.conversation_uuid)?;
    let root_relation = match snapshot.root_blob_id.as_deref() {
        Some(_) => cursor_store_root::classify_cursor_root(
            conn,
            &snapshot.conversation_uuid,
            &snapshot.root_message_blob_ids,
        )?,
        None => cursor_store_root::CursorRootOrderRelation::Inconclusive,
    };
    let incarnation = snapshot.store_incarnation.clone();
    let existing_len =
        cursor_store_records::active_cursor_record_count(conn, "cursor", &opaque_source_id)?;
    let active_incarnation =
        source_epoch::active_source_incarnation(conn, "cursor", &opaque_source_id)?;
    let active_render_revision =
        source_epoch::active_source_revision(conn, "cursor", &opaque_source_id)?;
    let parser_replay_required =
        existing_len > 0 && active_render_revision.as_deref() != Some(CURSOR_PARSER_REVISION);
    let source_len_before_capture = if parser_replay_required
        || root_relation == cursor_store_root::CursorRootOrderRelation::Rewrite
        || !file_identities_match(active_incarnation.as_deref(), Some(incarnation.as_str()))
    {
        0
    } else {
        existing_len
    };
    let resolution = source_epoch::observe_source(
        conn,
        "cursor",
        &opaque_source_id,
        &incarnation,
        source_len_before_capture,
        SourceLane::Durable,
        0,
        Some(CURSOR_PARSER_REVISION),
        claimed_session_id.as_deref(),
        if parser_replay_required {
            SourceChangeHint::Rewrite
        } else {
            root_relation.source_change_hint()
        },
    )?;
    let newly_referenced_ids = match (&previous_root_ids, &snapshot.root_message_blob_ids) {
        (Some(previous), cursor_store::RootMessageBlobIds::Parsed(current))
            if current.starts_with(previous) =>
        {
            current[previous.len()..].to_vec()
        }
        // Initial capture streams every blob through the bounded page walker;
        // explicit reference records are only needed for already-spooled
        // orphan blobs that a later root extension makes visible.
        (None, cursor_store::RootMessageBlobIds::Parsed(_)) => Vec::new(),
        _ => Vec::new(),
    };
    store_snapshot
        .blob_rows
        .extend(cursor_store::read_cursor_blob_rows(
            db_path,
            &newly_referenced_ids,
        )?);
    let mut capture_records = snapshot.records.clone();
    capture_records.extend(cursor_store::root_reference_records(
        &store_snapshot,
        &snapshot.store_incarnation,
        &newly_referenced_ids,
    )?);
    cursor_store_records::append_unseen_cursor_records(
        conn,
        resolution.source_epoch,
        &capture_records,
    )?;
    // Commit root ordering only after every reference needed to render this
    // transition is durable. A crash before here safely replays and dedupes.
    if let Some(root_blob_id) = snapshot.root_blob_id.as_deref() {
        cursor_store_root::record_cursor_root(
            conn,
            &snapshot.conversation_uuid,
            root_blob_id,
            &snapshot.root_message_blob_ids,
        )?;
    }
    let mut streamed_records = Vec::new();
    let mut streamed_bytes = 0usize;
    let walk = cursor_store_records::capture_walk(conn, resolution.source_epoch)?;
    let blob_page_rows = if walk.repairing {
        usize::MAX
    } else {
        CURSOR_BLOB_PAGE_ROWS
    };
    // Blob ids are unordered, so a walk restarts at the head to find a blob
    // inserted below where the last one ended. That is only worth doing when the
    // store has changed since a walk last reached its end: a store at rest can
    // hold nothing new, and re-reading its every blob each pass is what an
    // unchanged large store used to cost.
    let store_is_unchanged = walk.store_is_unchanged_since_last_cycle(store_stamp.as_deref());
    let blob_visit = if store_is_unchanged {
        cursor_store::CursorBlobVisit {
            last_blob_id: None,
            has_more: false,
        }
    } else {
        cursor_store::visit_cursor_blob_records(
            db_path,
            &snapshot.conversation_uuid,
            &snapshot.store_incarnation,
            walk.after_blob_id.as_deref(),
            blob_page_rows,
            |record| {
                let record_hash = cursor_store_records::cursor_record_hash(&record);
                if cursor_store_records::cursor_record_exists(
                    conn,
                    resolution.source_epoch,
                    &record_hash,
                )? {
                    return Ok(true);
                }
                if record.len() as u64 > capabilities.max_raw_record_bytes {
                    anyhow::bail!(
                        "one Cursor raw record exceeds the negotiated storage-v2 object bound"
                    );
                }
                if !streamed_records.is_empty()
                    && streamed_bytes.saturating_add(record.len()) > MAX_RAW_BATCH_BYTES
                {
                    return Ok(false);
                }
                streamed_bytes = streamed_bytes.saturating_add(record.len());
                streamed_records.push(record);
                Ok(true)
            },
        )?
    };
    let identity_after_blobs = identity_from_metadata(
        &db_path
            .metadata()
            .with_context(|| format!("rechecking Cursor store metadata {}", db_path.display()))?,
    );
    if !file_identities_match(
        Some(store_incarnation.as_str()),
        identity_after_blobs.as_deref(),
    ) {
        anyhow::bail!("Cursor store file changed identity during blob capture");
    }
    cursor_store_records::append_unseen_cursor_records(
        conn,
        resolution.source_epoch,
        &streamed_records,
    )?;
    // The ID is a bounded-page continuation, not an append high-water mark:
    // Cursor blob hashes are unordered. Confirmed EOF clears the continuation
    // so the next capture can discover newly inserted lower-sorting IDs, unless
    // the store is provably the one that walk finished against. A cycle keeps
    // the stamp it began with; only a walk from the head takes this pass's.
    let cycle_stamp = if walk.after_blob_id.is_some() {
        walk.cycle_stamp.as_deref()
    } else {
        store_stamp.as_deref()
    };
    cursor_store_records::store_capture_cursor(
        conn,
        resolution.source_epoch,
        blob_visit.last_blob_id.as_deref(),
        cycle_stamp,
    )?;
    let source_capture_has_more = blob_visit.has_more;
    let captured_logical_len =
        cursor_store_records::cursor_record_count(conn, resolution.source_epoch)?;
    // Refresh max_observed_len after adding local records.  The renderer
    // revision is stable across ordinary root appends and deliberately rotates
    // the epoch when replay is required by a parser upgrade.
    let resolution = source_epoch::observe_source(
        conn,
        "cursor",
        &opaque_source_id,
        &incarnation,
        captured_logical_len,
        SourceLane::Durable,
        source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?,
        Some(CURSOR_PARSER_REVISION),
        None,
        SourceChangeHint::None,
    )?;
    let active_source_epoch = resolution.source_epoch;
    let target_source_epoch =
        cursor_store_records::oldest_undrained_epoch(conn, "cursor", &opaque_source_id)?
            .unwrap_or(active_source_epoch);
    let resolution = source_epoch::resolution_for_epoch(conn, target_source_epoch)?;
    let logical_len = cursor_store_records::cursor_record_count(conn, target_source_epoch)?;
    let wire_predecessor = source_epoch::wire_predecessor_for_epoch(conn, target_source_epoch)?;

    if let Some(blocked) = pending_source_envelope::load_for_epoch(conn, target_source_epoch)? {
        if blocked.blocked_at.is_some() {
            return pending_to_prepared(blocked).map(CursorPreparationOutcome::Envelope);
        }
    }
    let range_start =
        source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?;
    if range_start >= logical_len {
        // Whether the walk also finished against this same store state is
        // asked again by `cursor_store_is_settled` when the row is used.
        let store_is_settled = !source_capture_has_more
            && target_source_epoch == active_source_epoch
            && !walk.repairing
            && store_stamp.is_some();
        if store_is_settled {
            cursor_store_records::record_store_rest(
                conn,
                &path_text,
                &cursor_store_records::StoreRest {
                    source_epoch: active_source_epoch,
                    conversation_uuid: snapshot.conversation_uuid.clone(),
                    rest_key: cursor_store_rest_key(store_stamp.as_deref(), &launch_binding_key),
                },
            )?;
        }
        return Ok(
            if source_capture_has_more && target_source_epoch == active_source_epoch {
                CursorPreparationOutcome::Continue
            } else {
                CursorPreparationOutcome::Current
            },
        );
    }
    let selected = cursor_store_records::cursor_records_from(
        conn,
        resolution.source_epoch,
        range_start,
        capabilities.max_records,
        capabilities.max_raw_record_bytes,
    )?;
    let mut selected_bytes = 0u64;
    let selected = selected
        .into_iter()
        .take_while(|record| {
            let record_bytes = record.bytes.len() as u64;
            if selected_bytes > 0
                && selected_bytes.saturating_add(record_bytes) > maximum_batch_bytes
            {
                return false;
            }
            // One source record is atomic. It may exceed the live target but
            // never the negotiated per-record capability enforced above.
            selected_bytes = selected_bytes.saturating_add(record_bytes);
            true
        })
        .collect::<Vec<_>>();
    let Some(last) = selected.last() else {
        return Ok(CursorPreparationOutcome::Continue);
    };
    let range_end = last
        .source_position
        .checked_add(1)
        .context("Cursor source position overflow")?;
    let raw_bytes = selected.iter().try_fold(0u64, |total, record| {
        total
            .checked_add(
                u64::try_from(record.bytes.len())
                    .context("Cursor raw record length exceeds u64")?,
            )
            .context("Cursor raw bytes overflow")
    })?;
    let identity = EnvelopeIdentity {
        tenant_id: capabilities.tenant_id.clone(),
        machine_id: capabilities.machine_id.clone(),
        provider: "cursor".to_string(),
        opaque_source_id: opaque_source_id.clone(),
        source_epoch: resolution.source_epoch,
        range_kind: RangeKind::RecordOrdinal,
        range_start,
        range_end,
        record_hashes: storage_v2_contract::hash_records(
            &selected
                .iter()
                .map(|record| record.bytes.clone())
                .collect::<Vec<_>>(),
        ),
    };
    // A normal Cursor store is durable but not watchable as a managed Helm
    // session.  A verified probe binding is persisted by source_epoch so that
    // expiry cannot split an already-bound conversation mid-archive.
    let managed_session_id = resolution.bound_session_id.clone();
    let session_id = managed_session_id.clone().unwrap_or_else(|| {
        cursor_store::longhouse_session_id_for_cursor(&snapshot.conversation_uuid)
    });
    let (started_at, last_activity_at) = cursor_store::cursor_session_timestamps(
        store_snapshot.created_at_ms,
        store_snapshot.updated_at_ms,
        visibility_evidence.as_ref(),
    )
    .unwrap_or_else(|| {
        // The wire requires a clock even for raw-only archives. Keep the
        // established epoch fallback explicit when Cursor supplied no clock.
        tracing::warn!(
            path = %db_path.display(),
            "Cursor store has no source clock; using the stable source epoch clock"
        );
        let opened_at = DateTime::parse_from_rfc3339(&resolution.opened_at)
            .expect("source epoch opened_at is generated internally")
            .with_timezone(&Utc);
        (opened_at, opened_at)
    });
    let prompt_witness = cursor_human_prompt_witness(&store_snapshot, db_path);
    // Only worth reading when a failure is actually going to be reported.
    let provider_error = visibility_evidence
        .as_ref()
        .and_then(|evidence| evidence.unsuperseded_failure())
        .and_then(|_| {
            crate::cursor_visibility::cursor_projection_turn_error(&snapshot.conversation_uuid)
        });
    let mut render_records = cursor_render_records(
        &store_snapshot,
        &selected,
        started_at.timestamp_micros(),
        visibility_evidence.as_ref(),
        prompt_witness.as_ref(),
        provider_error.as_deref(),
    )?;
    let previous_provider_session_id = if range_start == 0 {
        if let Some(managed_session_id) = managed_session_id.as_deref() {
            source_epoch::previous_provider_session_id(
                conn,
                resolution.source_epoch,
                "cursor",
                managed_session_id,
            )?
            .or_else(|| {
                claimed_binding
                    .as_ref()
                    .and_then(|binding| binding.previous_provider_session_id.clone())
            })
            .or(FileState::new(conn).previous_provider_session_id(
                &stable_source_path(db_path).to_string_lossy(),
                managed_session_id,
                "cursor",
            )?)
        } else {
            None
        }
    } else {
        None
    };
    source_epoch::record_provider_session_id(
        conn,
        resolution.source_epoch,
        &snapshot.conversation_uuid,
    )?;
    if previous_provider_session_id
        .as_deref()
        .is_some_and(|previous| previous != snapshot.conversation_uuid.as_str())
    {
        insert_conversation_reset_boundary(
            &mut render_records,
            resolution.source_epoch,
            range_start,
            &resolution.opened_at,
            &session_id,
            previous_provider_session_id.as_deref().unwrap_or_default(),
            &snapshot.conversation_uuid,
        )?;
    }
    if let Some(thread_id) = claimed_binding
        .as_ref()
        .and_then(|binding| binding.thread_id.as_ref())
    {
        for record in &mut render_records {
            record.thread_id = Some(thread_id.clone());
        }
    }
    let render_generation = cursor_render_generation_id(&session_id);
    let has_reply_evidence = render_records
        .iter()
        .any(|record| record.role == "assistant" || record.role == "tool");
    let event_count = render_records.len();
    let render = (!render_records.is_empty()).then(|| StorageV2Render {
        generation_id: render_generation.to_string(),
        parser_revision: CURSOR_PARSER_REVISION.to_string(),
        ordering_revision: "cursor-root-order-v1".to_string(),
        records: render_records,
    });
    let render_ready = render.is_some();
    let prepared = PreparedStorageV2Envelope {
        envelope: StorageV2Envelope {
            protocol_version: 2,
            tenant_id: capabilities.tenant_id.clone(),
            machine_id: capabilities.machine_id.clone(),
            session_id,
            provider: "cursor".to_string(),
            opaque_source_id,
            source_epoch: resolution.source_epoch.to_string(),
            predecessor_source_epoch: wire_predecessor.map(|value| value.to_string()),
            epoch_opened_at: resolution.opened_at,
            range_kind: "record_ordinal".to_string(),
            range_start,
            range_end,
            render,
            media: Vec::new(),
            session: StorageV2SessionFacts {
                provider_session_id: Some(snapshot.conversation_uuid.clone()),
                environment: "local".to_string(),
                project: workspace_facts
                    .as_ref()
                    .and_then(|(_, project, _)| project.clone()),
                cwd: workspace_facts.as_ref().map(|(cwd, _, _)| cwd.clone()),
                git_repo: workspace_facts
                    .as_ref()
                    .and_then(|(_, _, git_repo)| git_repo.clone()),
                git_branch: None,
                started_at: started_at.to_rfc3339(),
                last_activity_at: last_activity_at.to_rfc3339(),
                ended_at: None,
                origin_kind: Some("cursor_store".to_string()),
                hidden_from_default_timeline: managed_session_id.is_none() || !render_ready,
                launch_actor: None,
                launch_surface: None,
                // Cursor stores no subagent lineage.
                is_subagent: false,
                parent_provider_session_id: None,
                parent_tool_call_id: None,
                workflow_run_id: None,
            },
            records: selected
                .into_iter()
                .map(|record| StorageV2Record {
                    source_position: record.source_position,
                    data_b64: BASE64_STANDARD.encode(record.bytes),
                })
                .collect(),
            facts: Vec::new(),
            expected_envelope_id: hex_hash(storage_v2_contract::envelope_id(&identity)?),
        },
        source_epoch: resolution.source_epoch,
        range_start,
        range_end,
        event_count,
        has_reply_evidence,
        raw_bytes,
        has_more: range_end < logical_len
            || source_capture_has_more
            || target_source_epoch != active_source_epoch,
        media_objects: Vec::new(),
    };
    persist_prepared(conn, &path_text, prepared).map(CursorPreparationOutcome::Envelope)
}
