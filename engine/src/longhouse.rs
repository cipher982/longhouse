//! Native human-facing Longhouse device facade.
//!
//! This intentionally starts small: it proves the public executable, paired
//! engine resolution, and build-identity boundary before provider launch is
//! moved here. It never falls back to Python or uv.

#[path = "build_identity.rs"]
mod build_identity;

use anyhow::Context;
use clap::{Parser, Subcommand};
use serde::Serialize;
use std::path::PathBuf;
use std::process::Command;

#[derive(Parser)]
#[command(name = "longhouse", about = "Native Longhouse device CLI")]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// Print this facade's build identity and its paired engine path.
    BuildIdentity {
        #[arg(long)]
        json: bool,
    },
    /// Verify the paired engine is present and built from the same commit.
    VerifyPair,
}

#[derive(Serialize)]
struct PairIdentity {
    facade: build_identity::BuildIdentity,
    engine_path: String,
    engine: serde_json::Value,
}

fn paired_engine_path() -> anyhow::Result<PathBuf> {
    if let Some(override_path) = std::env::var_os("LONGHOUSE_ENGINE_BIN") {
        return Ok(PathBuf::from(override_path));
    }
    let exe = std::fs::canonicalize(std::env::current_exe().context("resolve native longhouse executable")?)
        .context("resolve native longhouse executable path")?;
    let dir = exe.parent().context("native longhouse executable has no parent")?;
    Ok(dir.join(if cfg!(windows) { "longhouse-engine.exe" } else { "longhouse-engine" }))
}

fn pair_identity() -> anyhow::Result<PairIdentity> {
    let engine_path = paired_engine_path()?;
    if !engine_path.is_file() {
        anyhow::bail!("paired longhouse-engine not found at {}", engine_path.display());
    }
    let output = Command::new(&engine_path)
        .args(["build-identity", "--json"])
        .output()
        .with_context(|| format!("run paired engine {}", engine_path.display()))?;
    if !output.status.success() {
        anyhow::bail!("paired engine build-identity failed with {}", output.status);
    }
    let engine: serde_json::Value = serde_json::from_slice(&output.stdout)
        .context("paired engine returned invalid build identity JSON")?;
    let facade = build_identity::BuildIdentity::current();
    let engine_commit = engine.get("commit").and_then(serde_json::Value::as_str).unwrap_or_default();
    if engine_commit != facade.commit {
        anyhow::bail!("native longhouse/engine build mismatch: facade {} engine {}", facade.commit_short, engine_commit);
    }
    Ok(PairIdentity { facade, engine_path: engine_path.display().to_string(), engine })
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command.unwrap_or(Commands::BuildIdentity { json: false }) {
        Commands::BuildIdentity { json } => {
            let pair = pair_identity()?;
            if json { println!("{}", serde_json::to_string_pretty(&pair)?); }
            else { println!("{}", pair.facade.qualified()); }
        }
        Commands::VerifyPair => {
            let pair = pair_identity()?;
            println!("paired engine: {} ({})", pair.engine_path, pair.facade.commit_short);
        }
    }
    Ok(())
}
