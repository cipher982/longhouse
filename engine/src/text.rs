//! Small Unicode-safe text truncation helpers.

/// Return the first `max_chars` Unicode scalar values from `text`.
pub fn truncate_head_chars(text: &str, max_chars: usize) -> String {
    if max_chars == 0 {
        return String::new();
    }
    text.chars().take(max_chars).collect()
}

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
    use super::{truncate_head_chars, truncate_tail_chars};

    #[test]
    fn head_truncation_keeps_unicode_boundaries() {
        assert_eq!(truncate_head_chars("hello🙂world", 6), "hello🙂");
    }

    #[test]
    fn tail_truncation_keeps_unicode_boundaries() {
        assert_eq!(truncate_tail_chars("before🙂after", 6), "🙂after");
    }

    #[test]
    fn zero_char_truncation_returns_empty() {
        assert_eq!(truncate_head_chars("hello", 0), "");
        assert_eq!(truncate_tail_chars("hello", 0), "");
    }
}
