//! Small Unicode-safe text truncation helpers.

/// Return the last `max_chars` Unicode scalar values from `text`.
pub fn truncate_tail_chars(text: &str, max_chars: usize) -> String {
    if max_chars == 0 {
        return String::new();
    }
    let char_count = text.chars().count();
    if char_count <= max_chars {
        return text.to_string();
    }
    text.chars().skip(char_count - max_chars).collect()
}

#[cfg(test)]
mod tests {
    use super::truncate_tail_chars;

    #[test]
    fn tail_truncation_keeps_unicode_boundaries() {
        assert_eq!(truncate_tail_chars("before🙂after", 6), "🙂after");
    }

    #[test]
    fn zero_char_truncation_returns_empty() {
        assert_eq!(truncate_tail_chars("hello", 0), "");
    }
}
