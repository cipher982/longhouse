// Build script for automatic WebSocket type generation
// Runs whenever ws-protocol-asyncapi.yml changes

use std::path::Path;
use std::process::Command;

fn main() {
    let schema_path = "../ws-protocol-asyncapi.yml";
    let generator_path = "../scripts/generate-ws-types-modern.py";

    // Tell Cargo to rerun if schema changes
    println!("cargo:rerun-if-changed={}", schema_path);
    println!("cargo:rerun-if-changed={}", generator_path);

    // Only generate if schema file exists
    if !Path::new(schema_path).exists() {
        println!(
            "cargo:warning=Schema file not found: {}, skipping generation",
            schema_path
        );
        return;
    }

    // Run the modern generator
    let output = Command::new("python3")
        .arg(generator_path)
        .arg(schema_path)
        .output();

    match output {
        Ok(result) => {
            if !result.status.success() {
                let stderr = String::from_utf8_lossy(&result.stderr);
                println!("cargo:warning=WebSocket type generation failed: {}", stderr);
            } else {
                println!("cargo:warning=WebSocket types generated successfully");
            }
        }
        Err(e) => {
            println!(
                "cargo:warning=Failed to run WebSocket type generator: {}",
                e
            );
        }
    }
}
