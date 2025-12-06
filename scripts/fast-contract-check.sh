#!/bin/bash
set -e

# Run fast contract validation using native Rust binary (not WASM)
cd frontend
cargo run --bin contract_validator --quiet 2>/dev/null || {
    echo "Failed to build/run contract validator"
    exit 1
}
