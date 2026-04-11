#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
INSTALLER_URL="https://get.longhouse.ai/install.sh"
LOCAL_SCRIPT="$ROOT_DIR/scripts/install.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/ci/check-public-installer-sync.sh [options]

Verify that the published installer URL resolves to the same script bytes as
the expected local installer source.

Options:
  --url <url>            Installer URL to validate (default: https://get.longhouse.ai/install.sh)
  --local-script <path>  Expected local installer script (default: scripts/install.sh)
USAGE
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '❌ Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

sha256_file() {
  python3 - "$1" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      INSTALLER_URL="${2:-}"
      shift 2
      ;;
    --local-script)
      LOCAL_SCRIPT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '❌ Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

require_cmd curl
require_cmd python3

if [[ ! -f "$LOCAL_SCRIPT" ]]; then
  printf '❌ Local installer script not found: %s\n' "$LOCAL_SCRIPT" >&2
  exit 1
fi

REMOTE_SCRIPT="$(mktemp -t longhouse-public-installer.XXXXXX.sh)"
PUBLISHED_SCRIPT=""
cleanup() {
  rm -f "$REMOTE_SCRIPT"
  if [[ -n "$PUBLISHED_SCRIPT" && -f "$PUBLISHED_SCRIPT" ]]; then
    rm -f "$PUBLISHED_SCRIPT"
  fi
}
trap cleanup EXIT

EFFECTIVE_URL="$(curl -fsSL -o "$REMOTE_SCRIPT" -w '%{url_effective}' "$INSTALLER_URL")"

if cmp -s "$REMOTE_SCRIPT" "$LOCAL_SCRIPT"; then
  printf '✅ Public installer matches %s\n' "$LOCAL_SCRIPT"
  printf '   Source: %s\n' "$EFFECTIVE_URL"
  exit 0
fi

REMOTE_SHA="$(sha256_file "$REMOTE_SCRIPT")"
LOCAL_SHA="$(sha256_file "$LOCAL_SCRIPT")"

printf '❌ Public installer drift detected.\n' >&2
printf '   URL: %s\n' "$INSTALLER_URL" >&2
printf '   Source: %s\n' "$EFFECTIVE_URL" >&2
printf '   Remote sha256: %s\n' "$REMOTE_SHA" >&2
printf '   Local  sha256: %s\n' "$LOCAL_SHA" >&2

if git -C "$ROOT_DIR" rev-parse --verify origin/main >/dev/null 2>&1; then
  PUBLISHED_SCRIPT="$(mktemp -t longhouse-origin-main-installer.XXXXXX.sh)"
  if git -C "$ROOT_DIR" show origin/main:scripts/install.sh > "$PUBLISHED_SCRIPT" 2>/dev/null; then
    if cmp -s "$REMOTE_SCRIPT" "$PUBLISHED_SCRIPT"; then
      AHEAD_COUNT="$(git -C "$ROOT_DIR" rev-list --count origin/main..HEAD 2>/dev/null || printf '0')"
      if [[ "$AHEAD_COUNT" =~ ^[0-9]+$ ]] && [[ "$AHEAD_COUNT" -gt 0 ]]; then
        printf '   Public installer already matches origin/main.\n' >&2
        printf '   Local checkout is ahead by %s commit(s); push main before expecting the vanity URL to change.\n' "$AHEAD_COUNT" >&2
      else
        printf '   Public installer matches origin/main, but this checkout differs.\n' >&2
      fi
      exit 1
    fi

    printf '   Public installer does not match origin/main either.\n' >&2
    printf '   origin/main sha256: %s\n' "$(sha256_file "$PUBLISHED_SCRIPT")" >&2
  fi
fi

exit 1
