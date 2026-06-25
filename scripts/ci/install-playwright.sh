#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OS_NAME="${LONGHOUSE_PLAYWRIGHT_UNAME:-$(uname -s)}"
APT_SOURCES_DIR="${LONGHOUSE_APT_SOURCES_DIR:-/etc/apt/sources.list.d}"

browser_args=("$@")
if [[ "${#browser_args[@]}" -eq 0 ]]; then
  browser_args=(chromium)
fi

with_deps=0
if [[ "$OS_NAME" == "Linux" ]]; then
  with_deps=1
fi
if [[ "${LONGHOUSE_PLAYWRIGHT_WITH_DEPS:-}" != "" ]]; then
  with_deps="$LONGHOUSE_PLAYWRIGHT_WITH_DEPS"
fi

disabled_sources=()

move_file() {
  local src="$1"
  local dst="$2"
  if [[ -w "$(dirname "$src")" ]]; then
    mv "$src" "$dst"
  else
    sudo mv "$src" "$dst"
  fi
}

restore_apt_sources() {
  local pair backup original
  for pair in "${disabled_sources[@]:-}"; do
    backup="${pair%%::*}"
    original="${pair#*::}"
    if [[ -e "$backup" && ! -e "$original" ]]; then
      move_file "$backup" "$original"
    fi
  done
}
trap restore_apt_sources EXIT

disable_problematic_microsoft_sources() {
  [[ "$with_deps" == "1" ]] || return 0
  [[ "$OS_NAME" == "Linux" ]] || return 0
  [[ -d "$APT_SOURCES_DIR" ]] || return 0

  local source_file backup
  while IFS= read -r -d '' source_file; do
    if ! grep -q "packages.microsoft.com" "$source_file"; then
      continue
    fi
    backup="${source_file}.longhouse-disabled"
    if [[ -e "$backup" ]]; then
      echo "Playwright apt source already disabled: $source_file" >&2
      continue
    fi
    echo "Temporarily disabling Microsoft apt source for Playwright install: $source_file" >&2
    move_file "$source_file" "$backup"
    disabled_sources+=("${backup}::${source_file}")
  done < <(find "$APT_SOURCES_DIR" -maxdepth 1 -type f \( -name "*.list" -o -name "*.sources" \) -print0)
}

disable_problematic_microsoft_sources

install_args=(playwright install)
if [[ "$with_deps" == "1" ]]; then
  install_args+=(--with-deps)
fi
if [[ "${LONGHOUSE_PLAYWRIGHT_ONLY_SHELL:-}" == "1" ]]; then
  install_args+=(--only-shell)
fi

cd "$ROOT_DIR/e2e"
bunx "${install_args[@]}" "${browser_args[@]}"
