#!/bin/zsh
set -euo pipefail

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

install_uv() {
  if has_command uv; then
    log "uv already installed: $(uv --version)"
    return
  fi

  log "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"

  has_command uv || fail "uv installation failed"
  log "uv installed: $(uv --version)"
}

install_python() {
  if uv python find 3.12 >/dev/null 2>&1; then
    log "Python ready: $(uv python find 3.12)"
    return
  fi

  log "Installing Python 3.12 via uv..."
  uv python install 3.12
}

install_longhouse() {
  export PATH="$HOME/.local/bin:$PATH"

  log "Installing Longhouse CLI..."
  if uv tool list 2>/dev/null | grep -q "^longhouse"; then
    uv tool upgrade longhouse || {
      log "Reinstalling Longhouse CLI..."
      uv tool uninstall longhouse 2>/dev/null || true
      uv tool install longhouse
    }
  else
    uv tool install longhouse
  fi

  has_command longhouse || fail "longhouse installation failed"
  log "Longhouse CLI ready: $(longhouse --version 2>/dev/null || echo installed)"
}

run_setup() {
  export PATH="$HOME/.local/bin:$PATH"
  log "Running Longhouse setup..."
  longhouse onboard --no-browser
}

main() {
  install_uv
  install_python
  install_longhouse
  run_setup
  log "Longhouse setup complete. Return to Longhouse.app and click Refresh."
}

main "$@"
