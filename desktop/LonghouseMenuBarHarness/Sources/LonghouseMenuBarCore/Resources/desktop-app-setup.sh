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

app_bundle_root() {
  local script_dir
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  cd "$script_dir/../../.." && pwd
}

app_bundle_version() {
  local app_bundle info_plist
  app_bundle="$(app_bundle_root)"
  info_plist="$app_bundle/Contents/Info.plist"
  [[ -f "$info_plist" ]] || return 1

  /usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$info_plist" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$info_plist" 2>/dev/null
}

package_source() {
  if [[ -n "${LONGHOUSE_PKG_SOURCE:-}" ]]; then
    printf '%s\n' "$LONGHOUSE_PKG_SOURCE"
    return 0
  fi

  local version
  version="$(app_bundle_version 2>/dev/null || true)"
  if [[ -n "$version" && "$version" != 0.0.0-dev* && "$version" != 0.0.0-smoke* ]]; then
    printf 'longhouse==%s\n' "$version"
    return 0
  fi

  printf 'longhouse\n'
}

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
  local pkg_source upgrade_target
  export PATH="$HOME/.local/bin:$PATH"
  pkg_source="$(package_source)"
  upgrade_target="$pkg_source"

  log "Installing Longhouse CLI from ${pkg_source}..."
  if [[ "$pkg_source" == "longhouse" || "$pkg_source" == longhouse==* ]]; then
    if uv tool list 2>/dev/null | grep -q "^longhouse"; then
      uv tool upgrade "$upgrade_target" || {
        log "Reinstalling Longhouse CLI..."
        uv tool uninstall longhouse 2>/dev/null || true
        uv tool install "$pkg_source"
      }
    else
      uv tool install "$pkg_source"
    fi
  else
    uv tool uninstall longhouse 2>/dev/null || true
    uv tool install --force --no-cache "$pkg_source" || {
      log "Reinstalling Longhouse CLI..."
      uv tool uninstall longhouse 2>/dev/null || true
      uv tool install "$pkg_source"
    }
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
