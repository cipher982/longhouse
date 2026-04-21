//! Build identity wiring for longhouse-engine.
//!
//! Reads `.build/build-identity.json` at the repo root and re-emits the
//! fields as `cargo::rustc-env=LONGHOUSE_BUILD_*`. The engine then reads
//! them via `env!(...)`, so a missing or malformed identity file fails
//! the build instead of silently producing a binary with no provenance.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn repo_identity_path() -> PathBuf {
    if let Ok(override_path) = env::var("LONGHOUSE_BUILD_IDENTITY_PATH") {
        return PathBuf::from(override_path);
    }
    // engine/Cargo.toml lives one level below the repo root.
    let manifest_dir = env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR unset");
    Path::new(&manifest_dir)
        .parent()
        .map(|p| p.join(".build").join("build-identity.json"))
        .expect("engine/ must live below a parent directory")
}

fn fail(msg: &str) -> ! {
    // cargo::warning surfaces in `cargo build`; the exit ensures the build fails.
    println!("cargo::error={}", msg);
    std::process::exit(1);
}

fn extract_string(raw: &str, key: &str) -> Option<String> {
    // Tiny hand-rolled JSON field extractor — keeps build.rs free of build
    // dependencies so first compile doesn't rebuild the world.
    let needle = format!("\"{}\"", key);
    let key_idx = raw.find(&needle)?;
    let after = &raw[key_idx + needle.len()..];
    let colon = after.find(':')?;
    let tail = after[colon + 1..].trim_start();

    if let Some(stripped) = tail.strip_prefix('"') {
        let end = stripped.find('"')?;
        return Some(stripped[..end].to_string());
    }
    // Non-string values (bool): pull until the next delimiter.
    let end = tail
        .find(|c: char| c == ',' || c == '}' || c.is_whitespace())
        .unwrap_or(tail.len());
    Some(tail[..end].to_string())
}

fn main() {
    let path = repo_identity_path();
    println!("cargo::rerun-if-changed={}", path.display());
    println!("cargo::rerun-if-env-changed=LONGHOUSE_BUILD_IDENTITY_PATH");

    let raw = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(err) => fail(&format!(
            "build identity missing at {} ({}). Run scripts/build/generate_build_identity.py first.",
            path.display(),
            err
        )),
    };

    // String fields must be present and non-empty. Mirror the strict
    // validation in server/zerg/build_info.py so the engine refuses to
    // compile against a malformed identity the Python side would reject.
    let mut fields = std::collections::HashMap::new();
    for key in ["version", "commit", "commit_short", "built_at", "channel"] {
        let value = match extract_string(&raw, key) {
            Some(v) => v,
            None => fail(&format!(
                "build identity at {} missing field {:?}",
                path.display(),
                key
            )),
        };
        if value.trim().is_empty() {
            fail(&format!(
                "build identity at {} has empty {:?} field",
                path.display(),
                key
            ));
        }
        let env_name = format!("LONGHOUSE_BUILD_{}", key.to_uppercase());
        println!("cargo::rustc-env={}={}", env_name, value);
        fields.insert(key, value);
    }

    // `dirty` must be a JSON bool (true/false). Reject numeric, string,
    // or missing variants so Python and Rust can never disagree on dev
    // vs release formatting.
    let raw_dirty = match extract_string(&raw, "dirty") {
        Some(v) => v,
        None => fail(&format!(
            "build identity at {} missing field \"dirty\"",
            path.display()
        )),
    };
    let dirty = match raw_dirty.as_str() {
        "true" => true,
        "false" => false,
        other => fail(&format!(
            "build identity at {} has non-bool dirty value {:?}",
            path.display(),
            other
        )),
    };
    println!("cargo::rustc-env=LONGHOUSE_BUILD_DIRTY={}", dirty);

    // Channel must be one of the values Python allows, otherwise the
    // qualified format drifts (e.g. an "rc" channel would silently fall
    // into the dev branch and emit "-dev+..." for a real release).
    let channel = fields.get("channel").expect("channel missing").as_str();
    if channel != "release" && channel != "dev" {
        fail(&format!(
            "build identity at {} has invalid channel {:?} (expected \"dev\" or \"release\")",
            path.display(),
            channel
        ));
    }

    // Pre-format the display string at build time so clap's `version = ...`
    // can take a `&'static str`. Mirrors BuildIdentity::qualified().
    let version = fields.get("version").expect("version missing");
    let short = fields.get("commit_short").expect("commit_short missing");
    let qualified = if channel == "release" {
        format!("{version} ({short})")
    } else if dirty {
        format!("{version}-dev+{short}.dirty")
    } else {
        format!("{version}-dev+{short}")
    };
    println!("cargo::rustc-env=LONGHOUSE_BUILD_QUALIFIED={}", qualified);
}
