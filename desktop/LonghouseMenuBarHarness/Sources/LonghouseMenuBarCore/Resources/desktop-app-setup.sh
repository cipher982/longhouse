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
  log "Native Longhouse CLI ready."
}

configure_machine_if_authorized() {
  if [[ -z "${LONGHOUSE_DEVICE_TOKEN:-}" || -z "${LONGHOUSE_RUNTIME_URL:-}" ]]; then
    log "Native binaries are installed. Sign in to a Runtime Host in Longhouse.app to authorize this Mac."
    return
  fi

  log "Authorizing this Mac with the configured Runtime Host..."
  longhouse auth --url "$LONGHOUSE_RUNTIME_URL"
  longhouse machine repair --repair-service
  log "Native Machine Agent service is configured."
}

main() {
  install_native_pair
  configure_machine_if_authorized
  log "Longhouse setup finished. Return to Longhouse.app and click Refresh."
}

main "$@"
