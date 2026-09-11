#!/usr/bin/env bash
# Focused first-install conversion regression for scripts/install.sh.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
HOME_DIR="$TEST_ROOT/home"
SOURCE_DIR="$TEST_ROOT/source"
BIN_DIR="$TEST_ROOT/bin"
FAULT_MARKER="$TEST_ROOT/fault-seen"
INSTALL_LOG="$TEST_ROOT/install.log"

cleanup() {
    local status=$?
    rm -rf "$TEST_ROOT"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$HOME_DIR/.local/bin" "$SOURCE_DIR" "$BIN_DIR"

write_fake_binary() {
    local path="$1"
    cat > "$path" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
    verify-pair|build-identity) exit 0 ;;
    *) exit 0 ;;
esac
EOF
    chmod 755 "$path"
}

write_fake_binary "$HOME_DIR/.local/bin/longhouse"
write_fake_binary "$HOME_DIR/.local/bin/longhouse-engine"
cp "$HOME_DIR/.local/bin/longhouse" "$TEST_ROOT/legacy-longhouse"
cp "$HOME_DIR/.local/bin/longhouse-engine" "$TEST_ROOT/legacy-longhouse-engine"
write_fake_binary "$SOURCE_DIR/longhouse"
write_fake_binary "$SOURCE_DIR/longhouse-engine"

# Fail only at the first legacy facade replacement. The check proves that the
# public facade is never asked to point at a missing or incomplete current pair.
cat > "$BIN_DIR/mv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

destination=""
if [[ "${1:-}" == "-h" || "${1:-}" == "-T" ]]; then
    destination="${3:-}"
fi
if [[ "$destination" == "$HOME/.local/bin/longhouse" && ! -e "$LONGHOUSE_TEST_FAULT_MARKER" ]]; then
    current="$HOME/.local/share/longhouse/current"
    if [[ ! -L "$current" || ! -x "$current/longhouse" || ! -x "$current/longhouse-engine" ]]; then
        printf '%s\n' "conversion exposed an incomplete current pair" >&2
        exit 96
    fi
    : > "$LONGHOUSE_TEST_FAULT_MARKER"
    exit 97
fi
exec /bin/mv "$@"
EOF
chmod 755 "$BIN_DIR/mv"

set +e
env \
    HOME="$HOME_DIR" \
    LONGHOUSE_HOME="$HOME_DIR/.longhouse" \
    LONGHOUSE_NATIVE_BIN_DIR="$SOURCE_DIR" \
    LONGHOUSE_TELEMETRY=0 \
    LONGHOUSE_TEST_FAULT_MARKER="$FAULT_MARKER" \
    PATH="$BIN_DIR:/usr/bin:/bin:/usr/sbin:/sbin" \
    SHELL=/bin/bash \
    bash "$ROOT_DIR/scripts/install.sh" >"$INSTALL_LOG" 2>&1
status=$?
set -e

[[ "$status" != 0 ]] || { printf '%s\n' "fault injection unexpectedly passed" >&2; exit 1; }
[[ -e "$FAULT_MARKER" ]] || {
    printf '%s\n' "installer attempted legacy cutover before publishing current" >&2
    cat "$INSTALL_LOG" >&2
    exit 1
}

[[ -f "$HOME_DIR/.local/bin/longhouse" && ! -L "$HOME_DIR/.local/bin/longhouse" ]]
[[ -f "$HOME_DIR/.local/bin/longhouse-engine" && ! -L "$HOME_DIR/.local/bin/longhouse-engine" ]]
cmp "$TEST_ROOT/legacy-longhouse" "$HOME_DIR/.local/bin/longhouse"
cmp "$TEST_ROOT/legacy-longhouse-engine" "$HOME_DIR/.local/bin/longhouse-engine"
[[ ! -e "$HOME_DIR/.local/share/longhouse/current" ]]
printf '%s\n' "native installer conversion rollback regression passed"
