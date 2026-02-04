#!/usr/bin/env bash
set -euo pipefail

LONGHOUSE_DIR="$HOME/.longhouse"
BIN_DIR="$LONGHOUSE_DIR/bin"
REAL_DIR="$LONGHOUSE_DIR/real"
REAL_FILE="$REAL_DIR/claude"
ENV_FILE="$LONGHOUSE_DIR/env"

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() {
  echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
  echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warn() {
  echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $*" >&2
}

ensure_dirs() {
  mkdir -p "$BIN_DIR" "$REAL_DIR"
}

resolve_real_claude() {
  local candidate
  candidate="$(command -v claude || true)"
  if [[ -n "$candidate" && "$candidate" != "$BIN_DIR/claude" ]]; then
    echo "$candidate"
    return 0
  fi

  local path_no_shim
  path_no_shim="$(printf '%s' "$PATH" | tr ':' '\n' | awk -v bin="$BIN_DIR" '$0 != bin' | paste -sd ':' -)"
  if [[ -n "$path_no_shim" ]]; then
    candidate="$(PATH="$path_no_shim" command -v claude || true)"
  fi

  if [[ -z "$candidate" ]]; then
    if [[ -f "$REAL_FILE" ]]; then
      local from_file
      from_file="$(cat "$REAL_FILE" 2>/dev/null || true)"
      if [[ -n "$from_file" ]]; then
        echo "$from_file"
        return 0
      fi
    fi
    return 1
  fi

  echo "$candidate"
  return 0
}

write_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    cat <<'ENVEOF' > "$ENV_FILE"
# Longhouse PATH shim (auto-generated)
export PATH="$HOME/.longhouse/bin:$PATH"
ENVEOF
    return 0
  fi

  if ! grep -q "\\.longhouse/bin" "$ENV_FILE" 2>/dev/null; then
    printf '\nexport PATH="$HOME/.longhouse/bin:$PATH"\n' >> "$ENV_FILE"
  fi
}

append_line_if_missing() {
  local file="$1"
  local line="$2"

  if [[ ! -f "$file" ]]; then
    touch "$file"
  fi

  if ! grep -Fq "${line}" "$file" 2>/dev/null; then
    printf '\n%s\n' "$line" >> "$file"
  fi
}

update_shell_rc() {
  local shell_name
  shell_name="$(basename "${SHELL:-}")"

  local source_line='[ -f "$HOME/.longhouse/env" ] && source "$HOME/.longhouse/env"'

  case "$shell_name" in
    zsh)
      append_line_if_missing "$HOME/.zprofile" "$source_line"
      ;;
    bash)
      if [[ -f "$HOME/.bash_profile" ]]; then
        append_line_if_missing "$HOME/.bash_profile" "$source_line"
      else
        append_line_if_missing "$HOME/.bashrc" "$source_line"
      fi
      ;;
    *)
      log_warn "Unsupported shell '$shell_name'."
      log_warn "Please add this line to your shell init file:"
      log_warn "  $source_line"
      return 1
      ;;
  esac

  return 0
}

write_shim() {
  cat <<'SHIMEOF' > "$BIN_DIR/claude"
#!/usr/bin/env bash
set -euo pipefail

REAL_FILE="$HOME/.longhouse/real/claude"
if [[ -f "$REAL_FILE" ]]; then
  REAL_CLAUDE="$(cat "$REAL_FILE")"
else
  REAL_CLAUDE=""
fi

if [[ -z "${REAL_CLAUDE}" || ! -x "${REAL_CLAUDE}" ]]; then
  echo "Longhouse: cannot find the real Claude binary."
  echo "Install Claude Code first: https://docs.anthropic.com/en/docs/claude-code"
  exit 1
fi

export LONGHOUSE_WRAPPED=1
exec "${REAL_CLAUDE}" "$@"
SHIMEOF

  chmod +x "$BIN_DIR/claude"
}

verify_install() {
  local shell_path
  shell_path="${SHELL:-}"

  if [[ -z "$shell_path" || ! -x "$shell_path" ]]; then
    log_warn "Cannot verify PATH automatically (unknown shell)."
    return 1
  fi

  local resolved
  resolved="$($shell_path -lc 'command -v claude' 2>/dev/null || true)"
  if [[ "$resolved" == "$BIN_DIR/claude" ]]; then
    log_success "Claude shim is active ($resolved)"
    return 0
  fi

  log_warn "Claude shim not detected in a fresh shell."
  log_warn "If you use a custom shell setup, add this line manually:"
  log_warn "  [ -f \"$HOME/.longhouse/env\" ] && source \"$HOME/.longhouse/env\""
  return 1
}

main() {
  log_info "Installing Longhouse Claude shim..."

  ensure_dirs

  local real_claude
  if ! real_claude="$(resolve_real_claude)"; then
    log_error "Claude binary not found. Install Claude Code first."
    exit 1
  fi

  printf '%s' "$real_claude" > "$REAL_FILE"
  log_info "Claude binary: $real_claude"

  write_env_file
  update_shell_rc || true
  write_shim

  if verify_install; then
    log_success "Install complete. Open a new terminal and run: claude"
  else
    log_warn "Install complete, but PATH verification failed."
    log_warn "Open a new terminal and run: claude"
  fi
}

main "$@"
