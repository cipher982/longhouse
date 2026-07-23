#!/bin/zsh
set -euo pipefail

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

install_native_pair() {
  export PATH="$HOME/.local/bin:$PATH"
  log "Installing paired native Longhouse binaries..."
  curl -fsSL https://get.longhouse.ai/install.sh | bash
  command -v longhouse >/dev/null 2>&1 || fail "native Longhouse installation failed"
  longhouse verify-pair >/dev/null || fail "native Longhouse pair verification failed"
  log "Native Longhouse CLI ready. Finish machine setup in Longhouse.app."
}

main() {
  install_native_pair
  log "Longhouse setup complete. Return to Longhouse.app and click Refresh."
}

main "$@"
