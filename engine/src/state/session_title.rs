use anyhow::Result;
use chrono::Utc;
use rusqlite::Connection;

use crate::pipeline::parser::{ParseResult, Role};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionTitleRow {
    pub title: String,
    pub first_user_message: String,
}

pub fn observe_parse_result(
    conn: &Connection,
    session_id: &str,
    parse_result: &ParseResult,
) -> Result<()> {
    let Some((first_user_message, title)) = parse_result.events.iter().find_map(|event| {
        if !matches!(event.role, Role::User) {
            return None;
        }
        let message = event.content_text.as_deref()?.trim();
        prompt_title(message).map(|title| (message, title))
    }) else {
        return Ok(());
    };
    conn.execute(
        "INSERT INTO session_title_state
            (session_id, title, first_user_message, source, updated_at)
         VALUES (?1, ?2, ?3, 'prompt', ?4)
         ON CONFLICT(session_id) DO NOTHING",
        rusqlite::params![
            session_id,
            title,
            truncate_chars(first_user_message, 2_000),
            Utc::now().to_rfc3339(),
        ],
    )?;
    Ok(())
}

pub fn get(conn: &Connection, session_id: &str) -> Result<Option<SessionTitleRow>> {
    let mut stmt = conn.prepare(
        "SELECT title, first_user_message
         FROM session_title_state WHERE session_id = ?1",
    )?;
    let mut rows = stmt.query([session_id])?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    Ok(Some(SessionTitleRow {
        title: row.get(0)?,
        first_user_message: row.get(1)?,
    }))
}

fn prompt_title(text: &str) -> Option<String> {
    let line = text.lines().find_map(|raw| {
        let candidate = raw
            .trim()
            .trim_start_matches('#')
            .trim()
            .replace("\"\"\"", "")
            .replace("'''", "");
        if candidate.is_empty()
            || candidate.starts_with("[Image ")
            || candidate.starts_with("LONGHOUSE_")
            || !candidate.chars().any(char::is_alphanumeric)
        {
            None
        } else {
            Some(candidate)
        }
    })?;
    let words: Vec<&str> = line
        .split_whitespace()
        .filter(|word| !word.starts_with("http://") && !word.starts_with("https://"))
        .collect();
    if words.is_empty() {
        return None;
    }
    let truncated = words.len() > 8;
    let mut title = words.into_iter().take(8).collect::<Vec<_>>().join(" ");
    title = truncate_chars(title.trim_end_matches([',', '.', ';', ':', '—', '-']), 80);
    if truncated && !title.ends_with('…') {
        if title.chars().count() >= 80 {
            title = truncate_chars(&title, 79);
        }
        title.push('…');
    }
    (!title.is_empty()).then_some(title)
}

fn truncate_chars(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        return value.to_string();
    }
    value.chars().take(max_chars).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pipeline::parser::{ParsedEvent, SessionMetadata};
    use chrono::Utc;

    fn parse_result(text: &str) -> ParseResult {
        ParseResult {
            events: vec![ParsedEvent {
                uuid: "event-1".to_string(),
                session_id: "provider-session".to_string(),
                timestamp: Utc::now(),
                role: Role::User,
                content_text: Some(text.to_string()),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                tool_call_id: None,
                source_offset: 0,
                raw_type: "user".to_string(),
                raw_line: None,
            }],
            source_lines: Vec::new(),
            media_objects: Vec::new(),
            last_good_offset: 1,
            metadata: SessionMetadata::default(),
            candidate_records: 1,
        }
    }

    #[test]
    fn stores_a_stable_prompt_title_once() {
        let conn = crate::state::db::open_db(Some(std::path::Path::new(":memory:"))).unwrap();
        observe_parse_result(
            &conn,
            "session-1",
            &parse_result("[Image #1]\n\nwhy is opencode stuck on naming session today"),
        )
        .unwrap();
        observe_parse_result(&conn, "session-1", &parse_result("a later prompt")).unwrap();

        let row = get(&conn, "session-1").unwrap().unwrap();
        assert_eq!(row.title, "why is opencode stuck on naming session today");
        assert!(row.first_user_message.starts_with("[Image #1]"));
    }

    #[test]
    fn skips_internal_control_messages_before_the_real_prompt() {
        let conn = crate::state::db::open_db(Some(std::path::Path::new(":memory:"))).unwrap();
        let mut parsed = parse_result("LONGHOUSE_OPENCODE_NOREPLY_internal");
        let mut user = parsed.events[0].clone();
        user.uuid = "event-2".to_string();
        user.content_text = Some("fix the archive title path".to_string());
        parsed.events.push(user);

        observe_parse_result(&conn, "session-2", &parsed).unwrap();

        let row = get(&conn, "session-2").unwrap().unwrap();
        assert_eq!(row.title, "fix the archive title path");
    }
}
