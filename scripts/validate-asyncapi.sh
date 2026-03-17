#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

bunx @asyncapi/cli validate schemas/ws-protocol-asyncapi.yml
