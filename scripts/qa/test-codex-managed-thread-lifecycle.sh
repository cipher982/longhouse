#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$SCRIPT_DIR/repro-codex-remote-backpressure.sh"

if [[ ! -x "$HARNESS" ]]; then
  echo "missing harness: $HARNESS" >&2
  exit 1
fi

ENGINE="${ENGINE:-longhouse-engine}"
CODEX_BIN="${CODEX_BIN:-$HOME/.longhouse/runtimes/codex/current/codex}"

echo "Managed Codex lifecycle regression suite"
echo "  engine: $ENGINE"
echo "  codex:  $CODEX_BIN"
echo ""

echo "[1/4] Expect idle-thread subscribe race before first turn"
ENGINE="$ENGINE" \
CODEX_BIN="$CODEX_BIN" \
MODE=text \
SUBSCRIBE_PHASE=preturn \
EXPECTED_FAILURE_PATTERN='no rollout found for thread id' \
bash "$HARNESS" --lines 32
echo ""

echo "[2/4] Probe the post-turn boundary; it may still race rollout materialization"
ENGINE="$ENGINE" \
CODEX_BIN="$CODEX_BIN" \
MODE=text \
SUBSCRIBE_PHASE=postturn \
EXPECTED_FAILURE_PATTERN='no rollout found for thread id' \
bash "$HARNESS" --lines 32
echo ""

echo "[3/4] Expect managed text burst to survive after rollout-backed subscribe"
ENGINE="$ENGINE" \
CODEX_BIN="$CODEX_BIN" \
MODE=text \
SUBSCRIBE_PHASE=after_rollout \
bash "$HARNESS" --lines 500
echo ""

echo "[4/4] Expect managed command burst to survive after rollout-backed subscribe"
ENGINE="$ENGINE" \
CODEX_BIN="$CODEX_BIN" \
MODE=command \
SUBSCRIBE_PHASE=after_rollout \
bash "$HARNESS" --lines 12000
