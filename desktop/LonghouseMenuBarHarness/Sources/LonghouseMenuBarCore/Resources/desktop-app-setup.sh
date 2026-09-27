#!/bin/zsh
# First-run setup for Longhouse.app: make sure the paired native binaries are
# installed, then authorize this Mac with the user's Runtime Host and start the
# Machine Agent service. Longhouse.app opens this in Terminal when local health
# reports `machine_setup_required` (or when the CLI is missing entirely).
set -euo pipefail

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

install_native_pair() {
  if command -v longhouse >/dev/null 2>&1 && longhouse verify-pair >/dev/null 2>&1; then
    log "Native Longhouse CLI is installed."
    return
  fi
  log "Installing paired native Longhouse binaries..."
  curl -fsSL https://get.longhouse.ai/install.sh | bash
  command -v longhouse >/dev/null 2>&1 || fail "native Longhouse installation failed"
  longhouse verify-pair >/dev/null || fail "native Longhouse pair verification failed"
  log "Native Longhouse CLI ready."
}

prompt_runtime_url() {
  local url=""
  log ""
  log "Connect this Mac to your Longhouse."
  log "Enter the address you sign in to, for example https://yourname.longhouse.ai"
  while true; do
    read -r "url?Longhouse URL: "
    [[ -n "$url" && "$url" != http://* && "$url" != https://* ]] && url="https://$url"
    # A host with a dot or port (or localhost) and no whitespace anywhere.
    if [[ "$url" =~ '^https?://([^/[:space:]]*[.:][^/[:space:]]*|localhost)(/[^[:space:]]*)?$' ]]; then
      LONGHOUSE_RUNTIME_URL="${url%/}"
      return
    fi
    log "That does not look like a Longhouse address. Try again."
  done
}

configure_machine() {
  if [[ -n "${LONGHOUSE_DEVICE_TOKEN:-}" && -n "${LONGHOUSE_RUNTIME_URL:-}" ]]; then
    log "Authorizing this Mac with the configured Runtime Host..."
  else
    unset LONGHOUSE_DEVICE_TOKEN
    prompt_runtime_url
    log "Your browser will open. Sign in if asked, then click \"Connect this device\"."
  fi
  # Without LONGHOUSE_DEVICE_TOKEN, `longhouse auth` runs the browser handshake.
  longhouse auth --url "$LONGHOUSE_RUNTIME_URL"
  longhouse machine repair --repair-service
  log "Native Machine Agent service is configured."
}

main() {
  install_native_pair
  configure_machine
  log "Longhouse setup finished. Return to Longhouse.app and click Refresh."
}

main "$@"
