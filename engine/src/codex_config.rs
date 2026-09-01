//! Building `codex -c key=value` configuration overrides.
//!
//! Codex parses the value half as TOML and falls back to treating it as a raw
//! string literal when that fails. Three call sites used to build these by
//! hand with three different quoting idioms — `serde_json::to_string`, a local
//! escape helper, and bare interpolation — which is three chances to get an
//! embedded quote or backslash wrong. One helper, one behaviour.

/// Codex checks for its own updates at startup. Longhouse owns when the
/// provider binary changes, so every Longhouse-launched codex turns it off.
pub const DISABLE_UPDATE_CHECK: &str = "check_for_update_on_startup=false";

/// `key="value"`, with the value written as a TOML basic string.
pub fn string_override(key: &str, value: &str) -> String {
    format!("{key}={}", toml_basic_string(value))
}

/// `key=<toml>` for a value that is already TOML source — an array, a bool, a
/// number. Use [`string_override`] for anything that came from a user or from
/// the filesystem.
pub fn literal_override(key: &str, toml: &str) -> String {
    format!("{key}={toml}")
}

/// Quote one TOML basic string. Only `\` and `"` need escaping for the values
/// Longhouse passes (paths, ids, model and effort names); a control character
/// would need `\uXXXX`, and none of these sources can produce one.
fn toml_basic_string(value: &str) -> String {
    let escaped = value.replace('\\', "\\\\").replace('"', "\\\"");
    format!("\"{escaped}\"")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn values_are_quoted_and_embedded_quotes_escaped() {
        assert_eq!(string_override("model", "gpt-5.3"), "model=\"gpt-5.3\"");
        assert_eq!(
            string_override("mcp_servers.longhouse.command", "/opt/a b/longhouse"),
            "mcp_servers.longhouse.command=\"/opt/a b/longhouse\""
        );
        assert_eq!(string_override("k", "say \"hi\""), "k=\"say \\\"hi\\\"\"");
        assert_eq!(string_override("k", "C:\\tmp"), "k=\"C:\\\\tmp\"");
    }

    #[test]
    fn literal_values_pass_through_untouched() {
        assert_eq!(
            literal_override("mcp_servers.longhouse.args", "[\"a\",\"b\"]"),
            "mcp_servers.longhouse.args=[\"a\",\"b\"]"
        );
    }
}
