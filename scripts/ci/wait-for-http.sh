#!/usr/bin/env bash
set -euo pipefail

URL="${1:-}"
NAME="${2:-$URL}"
MAX_ATTEMPTS="${3:-30}"
SLEEP_SECS="${4:-5}"

if [[ -z "$URL" ]]; then
  echo "Usage: $0 <url> [name] [max_attempts] [sleep_secs]" >&2
  exit 1
fi

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  if curl -sf --max-time 5 "$URL" >/dev/null 2>&1; then
    echo "✓ ${NAME} is up (attempt ${attempt})"
    exit 0
  fi

  echo "  ${NAME} not ready (attempt ${attempt}/${MAX_ATTEMPTS})..."
  sleep "$SLEEP_SECS"
done

echo "✗ ${NAME} did not become healthy" >&2
exit 1
