//! The plaintext-http rule for Runtime Host addresses.
//!
//! Native clients send the device token and every transcript to the address
//! they are pointed at. https is always fine. Plaintext http is fine to
//! loopback and to Tailscale addresses (WireGuard encrypts that transport),
//! refused to a LAN or private address unless the user opted in, and refused
//! everywhere else.
//!
//! One rule, four clients: this module, `server/zerg/services/plaintext_http.py`,
//! `desktop/.../PlaintextHTTP.swift` and `ios/Sources/Shared/Auth/PlaintextHTTP.swift`.
//! `schemas/plaintext-http-vectors.json` is the shared case list all four read
//! in their tests; change the rule there first.
//!
//! Self-contained on purpose: both the `longhouse` facade and the engine
//! include this file.

use std::net::{Ipv4Addr, Ipv6Addr};
use std::path::Path;
use std::str::FromStr;

use anyhow::{bail, Result};

pub const OPT_IN_ENV: &str = "LONGHOUSE_ALLOW_INSECURE_HTTP";
pub const OPT_IN_FLAG: &str = "--allow-insecure-http";
/// The machine-state field that keeps the opt-in beside the address it covers.
pub const STATE_FIELD: &str = "allow_insecure_http";

const TAILSCALE_SUFFIX: &str = ".ts.net";
const LOCAL_SUFFIX: &str = ".local";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostClass {
    Loopback,
    Tailscale,
    Lan,
    Public,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Allowed,
    AllowedWarn,
    RefusedLan,
    RefusedPublic,
    Invalid,
}

impl Outcome {
    pub fn usable(self) -> bool {
        matches!(self, Outcome::Allowed | Outcome::AllowedWarn)
    }
}

/// Only canonical dotted-quad IPv4 (no leading zeros) is an address here; a
/// URL parser may read other spellings differently, so they stay names.
fn parse_ipv4(host: &str) -> Option<Ipv4Addr> {
    let mut octets = [0u8; 4];
    let mut parts = host.split('.');
    for octet in &mut octets {
        let part = parts.next()?;
        if part.is_empty()
            || part.len() > 3
            || !part.bytes().all(|byte| byte.is_ascii_digit())
            || (part.len() > 1 && part.starts_with('0'))
        {
            return None;
        }
        *octet = part.parse().ok()?;
    }
    parts.next().is_none().then(|| Ipv4Addr::from(octets))
}

pub fn classify_ipv4(address: Ipv4Addr) -> HostClass {
    let [a, b, _, _] = address.octets();
    if a == 127 {
        HostClass::Loopback
    } else if a == 100 && (b & 0xc0) == 64 {
        HostClass::Tailscale
    } else if a == 10
        || (a == 172 && (b & 0xf0) == 16)
        || (a == 192 && b == 168)
        || (a == 169 && b == 254)
    {
        HostClass::Lan
    } else {
        HostClass::Public
    }
}

pub fn classify_ipv6(address: Ipv6Addr) -> HostClass {
    let segments = address.segments();
    if address == Ipv6Addr::LOCALHOST {
        HostClass::Loopback
    } else if segments[0] == 0xfd7a && segments[1] == 0x115c && segments[2] == 0xa1e0 {
        HostClass::Tailscale
    } else if (segments[0] & 0xffc0) == 0xfe80 || (segments[0] & 0xfe00) == 0xfc00 {
        HostClass::Lan
    } else {
        // IPv4-mapped addresses land here on purpose: they are not judged by
        // the IPv4 they wrap.
        HostClass::Public
    }
}

/// Classify a URL host: case-folded, one trailing dot removed.
pub fn classify_host(host: &str) -> HostClass {
    let host = host.trim().to_ascii_lowercase();
    let host = host.strip_suffix('.').unwrap_or(&host);
    if host == "localhost" {
        return HostClass::Loopback;
    }
    if host.contains(':') {
        return match (!host.contains('%'))
            .then(|| Ipv6Addr::from_str(host).ok())
            .flatten()
        {
            Some(address) => classify_ipv6(address),
            None => HostClass::Public,
        };
    }
    if let Some(address) = parse_ipv4(host) {
        return classify_ipv4(address);
    }
    if host.is_empty() || host.split('.').any(str::is_empty) {
        return HostClass::Public;
    }
    if host.ends_with(TAILSCALE_SUFFIX) && host.len() > TAILSCALE_SUFFIX.len() {
        HostClass::Tailscale
    } else if host.ends_with(LOCAL_SUFFIX) && host.len() > LOCAL_SUFFIX.len() {
        HostClass::Lan
    } else {
        HostClass::Public
    }
}

/// Judge a Runtime Host address under the plaintext-http rule.
///
/// The host is the one the URL parser (the same one `reqwest` dials with)
/// extracts, so the judged address is the dialed one.
pub fn check(url: &str, allow_insecure_http: bool) -> Outcome {
    let text = url.trim();
    let Ok(parsed) = reqwest::Url::parse(text) else {
        return Outcome::Invalid;
    };
    let Some(host) = parsed.host_str() else {
        return Outcome::Invalid;
    };
    match parsed.scheme() {
        "https" => return Outcome::Allowed,
        "http" => {}
        _ => return Outcome::Invalid,
    }
    // A backslash is a path separator to some URL parsers and userinfo to
    // others, so two components could disagree about which host this is.
    if text.contains('\\') {
        return Outcome::Invalid;
    }
    match classify_host(host.trim_start_matches('[').trim_end_matches(']')) {
        HostClass::Loopback | HostClass::Tailscale => Outcome::Allowed,
        HostClass::Lan if allow_insecure_http => Outcome::AllowedWarn,
        HostClass::Lan => Outcome::RefusedLan,
        HostClass::Public => Outcome::RefusedPublic,
    }
}

/// The error a refused address gets; a LAN refusal names the opt-in.
pub fn refusal_message(url: &str, outcome: Outcome) -> String {
    let url = url.trim();
    match outcome {
        Outcome::RefusedLan => format!(
            "Refusing plaintext {url}: http:// to a LAN address sends the device token and every \
             transcript unencrypted. If you trust this network, opt in with {OPT_IN_FLAG} (or \
             {OPT_IN_ENV}=1). Otherwise use https://, or reach the box over Tailscale, where \
             http:// is allowed (a 100.x address or a .ts.net name)."
        ),
        Outcome::RefusedPublic => format!(
            "Refusing plaintext {url}: http:// is allowed only to loopback and Tailscale addresses \
             (100.64.0.0/10, fd7a:115c:a1e0::/48, *.ts.net). Use https:// (Caddy, nginx, or \
             `tailscale serve`)."
        ),
        _ => format!("{url:?} is not an http(s) Longhouse address."),
    }
}

/// The one-line warning printed each time an opted-in LAN address is used.
pub fn insecure_warning(url: &str) -> String {
    format!(
        "WARNING: {} is plain http, allowed by {OPT_IN_FLAG}: the device token and every \
         transcript cross this network unencrypted.",
        url.trim()
    )
}

/// Refuse an address the rule forbids, and print the warning for an opted-in
/// LAN one. Returns the outcome so a caller can tell whether the opt-in was
/// the thing that allowed it.
pub fn enforce(url: &str, allow_insecure_http: bool) -> Result<Outcome> {
    let outcome = check(url, allow_insecure_http);
    match outcome {
        Outcome::Allowed => {}
        Outcome::AllowedWarn => eprintln!("{}", insecure_warning(url)),
        _ => bail!("{}", refusal_message(url, outcome)),
    }
    Ok(outcome)
}

pub fn env_opt_in() -> bool {
    std::env::var(OPT_IN_ENV)
        .map(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

/// The opt-in stored in `<machine_dir>/state.json` beside the runtime URL,
/// for exactly that address: a different address is a new decision.
pub fn stored_opt_in_for(machine_dir: &Path, url: &str) -> bool {
    let Some(state) = std::fs::read(machine_dir.join("state.json"))
        .ok()
        .and_then(|raw| serde_json::from_slice::<serde_json::Value>(&raw).ok())
    else {
        return false;
    };
    let same_address = state
        .get("runtime_url")
        .and_then(serde_json::Value::as_str)
        .is_some_and(|stored| {
            stored.trim().trim_end_matches('/') == url.trim().trim_end_matches('/')
        });
    same_address
        && state
            .get(STATE_FIELD)
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false)
}

/// Whether this process may use the LAN address `url` over plain http: the
/// environment, or what `longhouse auth` stored with that same address.
pub fn opt_in_enabled(machine_dir: &Path, url: &str) -> bool {
    env_opt_in() || stored_opt_in_for(machine_dir, url)
}

/// The scheme of `url` lowercased, and the rest after `://`, when it is an
/// http(s) address. A scheme is case-insensitive, and the shared rule judges
/// `HTTP://...` like `http://...`.
pub fn split_http_scheme(url: &str) -> Option<(&'static str, &str)> {
    let (scheme, rest) = url.trim().split_once("://")?;
    match scheme.to_ascii_lowercase().as_str() {
        "http" => Some(("http", rest)),
        "https" => Some(("https", rest)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const VECTORS: &str = include_str!("../../schemas/plaintext-http-vectors.json");

    fn vectors() -> serde_json::Value {
        serde_json::from_str(VECTORS).expect("vectors are valid JSON")
    }

    fn class_name(class: HostClass) -> &'static str {
        match class {
            HostClass::Loopback => "loopback",
            HostClass::Tailscale => "tailscale",
            HostClass::Lan => "lan",
            HostClass::Public => "public",
        }
    }

    fn outcome_name(outcome: Outcome) -> &'static str {
        match outcome {
            Outcome::Allowed => "allowed",
            Outcome::AllowedWarn => "allowed_warn",
            Outcome::RefusedLan => "refused_lan",
            Outcome::RefusedPublic => "refused_public",
            Outcome::Invalid => "invalid",
        }
    }

    #[test]
    fn host_classification_matches_the_shared_vectors() {
        let vectors = vectors();
        let cases = vectors["hosts"].as_array().unwrap();
        assert!(cases.len() > 40, "the vectors were not read");
        for case in cases {
            let host = case["host"].as_str().unwrap();
            assert_eq!(
                class_name(classify_host(host)),
                case["class"].as_str().unwrap(),
                "host {host:?}"
            );
        }
    }

    #[test]
    fn url_verdicts_match_the_shared_vectors() {
        let vectors = vectors();
        let cases = vectors["urls"].as_array().unwrap();
        assert!(cases.len() > 40, "the vectors were not read");
        for case in cases {
            let url = case["url"].as_str().unwrap();
            let allow = case["allow_insecure_http"].as_bool().unwrap();
            assert_eq!(
                outcome_name(check(url, allow)),
                case["verdict"].as_str().unwrap(),
                "url {url:?} allow_insecure_http={allow}"
            );
        }
    }

    #[test]
    fn the_tailscale_v4_edges_are_exact() {
        assert_eq!(classify_host("100.63.255.255"), HostClass::Public);
        assert_eq!(classify_host("100.64.0.1"), HostClass::Tailscale);
        assert_eq!(classify_host("100.127.255.255"), HostClass::Tailscale);
        assert_eq!(classify_host("100.128.0.0"), HostClass::Public);
    }

    #[test]
    fn a_lan_refusal_names_the_opt_in_and_a_public_one_does_not() {
        let lan = refusal_message("http://192.168.1.20:8080", Outcome::RefusedLan);
        assert!(lan.contains(OPT_IN_FLAG) && lan.contains(OPT_IN_ENV) && lan.contains("Tailscale"));
        let public = refusal_message("http://demo.longhouse.ai", Outcome::RefusedPublic);
        assert!(!public.contains(OPT_IN_FLAG) && public.contains("https://"));
    }

    #[test]
    fn the_warning_is_one_line_naming_the_address_and_the_opt_in() {
        let warning = insecure_warning(" http://192.168.1.20:8080 ");
        assert!(!warning.contains('\n'));
        assert!(warning.contains("http://192.168.1.20:8080") && warning.contains(OPT_IN_FLAG));
    }

    #[test]
    fn enforce_refuses_with_the_message_and_reports_whether_the_opt_in_was_used() {
        assert_eq!(
            enforce("http://100.64.0.1:8080", false).unwrap(),
            Outcome::Allowed
        );
        assert_eq!(
            enforce("http://192.168.1.20:8080", true).unwrap(),
            Outcome::AllowedWarn
        );
        let lan = enforce("http://192.168.1.20:8080", false).unwrap_err();
        assert!(lan.to_string().contains(OPT_IN_FLAG));
        let public = enforce("http://demo.longhouse.ai", true).unwrap_err();
        assert!(public.to_string().contains("loopback and Tailscale"));
    }

    #[test]
    fn the_stored_opt_in_covers_only_the_address_it_was_stored_with() {
        let lan = "http://192.168.1.20:8080";
        let dir = tempfile::tempdir().unwrap();
        assert!(!stored_opt_in_for(dir.path(), lan));
        std::fs::write(
            dir.path().join("state.json"),
            r#"{"runtime_url":"http://192.168.1.20:8080/","allow_insecure_http":true}"#,
        )
        .unwrap();
        assert!(stored_opt_in_for(dir.path(), lan));
        assert!(stored_opt_in_for(dir.path(), " http://192.168.1.20:8080/ "));
        // A different host, or the same host on another port, is a new decision.
        assert!(!stored_opt_in_for(dir.path(), "http://192.168.1.99:8080"));
        assert!(!stored_opt_in_for(dir.path(), "http://192.168.1.20:9090"));
        std::fs::write(
            dir.path().join("state.json"),
            r#"{"runtime_url":"http://192.168.1.20:8080","allow_insecure_http":false}"#,
        )
        .unwrap();
        assert!(!stored_opt_in_for(dir.path(), lan));
    }

    #[test]
    fn the_scheme_is_case_insensitive() {
        assert_eq!(
            split_http_scheme("HTTP://100.64.0.1:8080"),
            Some(("http", "100.64.0.1:8080"))
        );
        assert_eq!(
            split_http_scheme(" Https://demo.longhouse.ai/ "),
            Some(("https", "demo.longhouse.ai/"))
        );
        assert_eq!(split_http_scheme("ftp://x"), None);
        assert_eq!(split_http_scheme("100.64.0.1:8080"), None);
    }
}
