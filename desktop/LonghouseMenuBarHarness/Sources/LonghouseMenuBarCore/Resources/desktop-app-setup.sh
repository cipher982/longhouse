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
    [[ -n "$url" && "$url" != *://* ]] && url="https://$url"
    # A host with a dot or port (or localhost) and no whitespace anywhere.
    if [[ "$url" =~ '^https?://([^/[:space:]]*[.:][^/[:space:]]*|localhost)(/[^[:space:]]*)?$' ]]; then
      LONGHOUSE_RUNTIME_URL="${url%/}"
      return
    fi
    log "That does not look like a Longhouse address. Try again."
  done
}

# The address this Mac already knows: the installer stores the LONGHOUSE_URL
# its command carried, and `longhouse auth` keeps the last one it connected to.
stored_runtime_url() {
  local state="${LONGHOUSE_HOME:-$HOME/.longhouse}/machine/state.json"
  [[ -r "$state" ]] || return 0
  sed -n 's/^[[:space:]]*"runtime_url":[[:space:]]*"\(https\{0,1\}:\/\/[^"]*\)".*/\1/p' "$state" | head -n 1
}

# Decide what existing history the Machine Agent may import before it can
# start: a stranger's old transcripts can hold code and secrets from any project
# they ever ran an agent in. An explicit LONGHOUSE_IMPORT_SCOPE (now, all, or a
# date) wins, a previous choice on this Mac is kept, and otherwise the user is
# asked (this script runs in a Terminal window).
choose_import_scope() {
  local state_home="${LONGHOUSE_HOME:-$HOME/.longhouse}"
  if ! longhouse machine scope --help >/dev/null 2>&1; then
    log "WARNING: this Longhouse release predates import scopes and imports ALL existing session history."
    log "Update it (curl -fsSL https://get.longhouse.ai/install.sh | bash) before connecting a Mac whose old sessions you do not want uploaded."
    return
  fi
  if [[ -n "${LONGHOUSE_IMPORT_SCOPE:-}" ]]; then
    longhouse machine scope --since "$LONGHOUSE_IMPORT_SCOPE" \
      || fail "LONGHOUSE_IMPORT_SCOPE must be now, all, or a date like 2026-09-01"
    return
  fi
  if [[ -f "$state_home/machine/import-scope.json" || -f "$state_home/agent/longhouse-shipper.db" ]]; then
    log "Keeping this Mac's existing import choice (longhouse machine scope shows it)."
    return
  fi
  if [[ -t 0 ]] && longhouse machine scope --prompt; then
    return
  fi
  # No terminal, or the question was abandoned: import only what starts from now.
  longhouse machine scope --since now \
    || log "WARNING: could not record the import choice; the Machine Agent still defaults to sessions that start from now on."
}

configure_machine() {
  if [[ -n "${LONGHOUSE_DEVICE_TOKEN:-}" && -n "${LONGHOUSE_RUNTIME_URL:-}" ]]; then
    log "Authorizing this Mac with the configured Runtime Host..."
  else
    unset LONGHOUSE_DEVICE_TOKEN
    LONGHOUSE_RUNTIME_URL="${LONGHOUSE_RUNTIME_URL:-$(stored_runtime_url)}"
    if [[ -n "$LONGHOUSE_RUNTIME_URL" ]]; then
      log "Connecting this Mac to $LONGHOUSE_RUNTIME_URL."
    else
      prompt_runtime_url
    fi
    log "Your browser will open. Sign in if asked, then click \"Connect this device\"."
  fi
  # Without LONGHOUSE_DEVICE_TOKEN, `longhouse auth` runs the browser handshake.
  longhouse auth --url "$LONGHOUSE_RUNTIME_URL"
  longhouse machine repair --repair-service
  log "Native Machine Agent service is configured."
}

main() {
  install_native_pair
  choose_import_scope
  configure_machine
  log "Longhouse setup finished. Return to Longhouse.app and click Refresh."
}

main "$@"
