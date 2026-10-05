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
    let cleaned = sanitize_title_text(text);
    let line = cleaned.lines().find_map(|raw| {
        let candidate = strip_heading_prefix(raw);
        if candidate.is_empty()
            || candidate.starts_with("LONGHOUSE_")
            || is_path_like_title(candidate)
            || !candidate.chars().any(char::is_alphanumeric)
        {
            None
        } else {
            Some(candidate)
        }
    })?;

    let mut words = line.split_whitespace();
    let mut title = String::with_capacity(line.len().min(80));
    let mut char_count = 0;
    for word in words.by_ref().take(8) {
        if char_count > 0 {
            push_title_chars(&mut title, " ", &mut char_count);
        }
        push_title_chars(&mut title, word, &mut char_count);
    }
    let truncated_by_words = words.next().is_some();
    if truncated_by_words {
        while title
            .chars()
            .next_back()
            .is_some_and(|character| matches!(character, ',' | '.' | ';' | ':' | '—' | '-'))
        {
            title.pop();
        }
        while title.ends_with(' ') {
            title.pop();
        }
        title.push('…');
    }
    if title.chars().count() > 80 {
        title = title.chars().take(79).collect();
        while title.ends_with(' ') {
            title.pop();
        }
        title.push('…');
    }
    (!title.is_empty()).then_some(title)
}

fn push_title_chars(output: &mut String, text: &str, char_count: &mut usize) {
    for character in text.chars().take(81usize.saturating_sub(*char_count)) {
        output.push(character);
        *char_count += 1;
    }
}

// Keep this ordered cleanup in sync with server/zerg/services/session_title.py.
fn sanitize_title_text(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut cleaned = String::with_capacity(text.len().min(512));
    let mut index = 0;

    while index < bytes.len() {
        let remaining = &text[index..];
        if matches!(bytes[index], 0x00..=0x08 | 0x0b..=0x0c | 0x0e..=0x1f | 0x7f) {
            index += 1;
            cleaned.push(' ');
            continue;
        }

        if remaining.starts_with("```") {
            if let Some(close) = remaining[3..].find("```") {
                index += 3 + close + 3;
            } else {
                index += 3;
                while bytes
                    .get(index)
                    .is_some_and(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
                {
                    index += 1;
                }
            }
            cleaned.push(' ');
            continue;
        }

        if remaining.starts_with("![") {
            if let Some(label_end) = remaining[2..].find(']') {
                let target_start = label_end + 3;
                if remaining
                    .get(target_start..)
                    .is_some_and(|after_label| after_label.starts_with('('))
                {
                    if let Some(target_end) = remaining[target_start + 1..].find(')') {
                        index += target_start + target_end + 2;
                        cleaned.push(' ');
                        continue;
                    }
                }
            }
        }

        if remaining
            .get(..6)
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case("[image"))
        {
            if let Some(end) = remaining.find(']') {
                index += end + 1;
                cleaned.push(' ');
                continue;
            }
        }

        if remaining.starts_with('[') {
            if let Some(label_end) = remaining[1..].find(']') {
                let target_start = label_end + 2;
                if remaining
                    .get(target_start..)
                    .is_some_and(|after_label| after_label.starts_with('('))
                {
                    if let Some(target_end) = remaining[target_start + 1..].find(')') {
                        cleaned.push_str(&remaining[1..label_end + 1]);
                        index += target_start + target_end + 2;
                        continue;
                    }
                }
            }
        }

        if remaining.starts_with("http://")
            || remaining.starts_with("https://")
            || remaining.starts_with("www.")
        {
            index += remaining.find(char::is_whitespace).unwrap_or(remaining.len());
            cleaned.push(' ');
            continue;
        }

        if remaining.starts_with('`') {
            if let Some(end) = remaining[1..].find('`') {
                index += end + 2;
                cleaned.push(' ');
                continue;
            }
        }

        if remaining.starts_with("<｜") {
            if let Some(end) = remaining.find('>') {
                index += end + 1;
                cleaned.push(' ');
                continue;
            }
        }

        if remaining.starts_with("<|") {
            if let Some(end) = remaining[2..].find("|>") {
                let token = &remaining[2..end + 4];
                if !token.contains('<') && !token.contains('>') {
                    index += end + 4;
                    cleaned.push(' ');
                    continue;
                }
            }
        }

        let tag_start = if remaining.starts_with("</") { 2 } else { 1 };
        if remaining.starts_with('<')
            && remaining
                .as_bytes()
                .get(tag_start)
                .is_some_and(u8::is_ascii_alphabetic)
        {
            if let Some(end) = remaining[tag_start..].find('>') {
                index += tag_start + end + 1;
                cleaned.push(' ');
                continue;
            }
        }

        if bytes[index] == b'"' || bytes[index] == b'\'' {
            let quote = bytes[index];
            let mut end = index + 1;
            while bytes
                .get(end)
                .is_some_and(|byte| *byte == b'"' || *byte == b'\'')
            {
                end += 1;
            }
            if end - index >= 3
                && bytes[index + 1] == quote
                && bytes[index + 2] == quote
            {
                index += 3;
                cleaned.push(' ');
                continue;
            }
            if end - index >= 2
                && cleaned
                    .rsplit('\n')
                    .next()
                    .is_some_and(|prefix| prefix.trim().is_empty())
            {
                index = end;
                cleaned.push(' ');
                continue;
            }
        }

        let character = remaining.chars().next().expect("index is on a character boundary");
        cleaned.push(character);
        index += character.len_utf8();
    }

    cleaned
}

fn strip_heading_prefix(line: &str) -> &str {
    let mut prefix_end = 0;
    let mut whitespace_count = 0;
    for (index, character) in line.char_indices() {
        if !character.is_whitespace() || whitespace_count == 3 {
            break;
        }
        whitespace_count += 1;
        prefix_end = index + character.len_utf8();
    }
    let rest = &line[prefix_end..];
    let heading_end = rest.bytes().take_while(|byte| *byte == b'#').count();
    if (1..=6).contains(&heading_end)
        && rest[heading_end..]
            .chars()
            .next()
            .is_some_and(char::is_whitespace)
    {
        rest[heading_end..].trim()
    } else {
        line.trim()
    }
}

fn is_path_like_title(text: &str) -> bool {
    let path = text.trim();
    if path.is_empty() || path.chars().any(char::is_whitespace) {
        return false;
    }
    let unix_path = ["/Users/", "/home/", "/private/", "/tmp/", "/var/"]
        .iter()
        .any(|prefix| path.starts_with(prefix));
    let bytes = path.as_bytes();
    let windows_path = bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'/' | b'\\');
    (unix_path || windows_path) && (path.contains('/') || path.contains('\\'))
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
                parent_uuid: None,
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
            provider_facts: Vec::new(),
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

    #[test]
    fn sanitizes_real_world_first_messages_for_timeline_titles() {
        let conn = crate::state::db::open_db(Some(std::path::Path::new(":memory:"))).unwrap();
        let samples = [
            (
                "\"\"\"<attachment>\n Handoff — recovered first-tester reliability batch",
                "Handoff — recovered first-tester reliability batch",
            ),
            (
                concat!(
                    "/Users/davidrose/git/obsidian_vault/AI-Sessions/2026-10-04-athena-image-tool-fla",
                    "\n\nreview and help me pick this task up before Friday"
                ),
                "review and help me pick this task up…",
            ),
            (
                "\"\"Saurabh Chakravarty [4:53 PM]",
                "Saurabh Chakravarty [4:53 PM]",
            ),
        ];

        for (index, (message, expected_title)) in samples.iter().enumerate() {
            let session_id = format!("session-{index}");
            observe_parse_result(&conn, &session_id, &parse_result(message)).unwrap();

            let row = get(&conn, &session_id).unwrap().unwrap();
            assert_eq!(row.title, *expected_title);
        }
    }
}
