#!/usr/bin/env bash
# Verify the public installer can install a paired native device CLI without
# any Python or uv executable available to it.
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
TEST_ROOT="$(mktemp -d)"
PAIR_DIR="$TEST_ROOT/pair"
HOME_DIR="$TEST_ROOT/home"

python3 "$ROOT_DIR/scripts/build/generate_build_identity.py" >/dev/null
cargo build --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse --bin longhouse-engine >/dev/null
mkdir -p "$PAIR_DIR" "$HOME_DIR/traps"
cp "$ROOT_DIR/engine/target/ci/longhouse" "$PAIR_DIR/longhouse"
cp "$ROOT_DIR/engine/target/ci/longhouse-engine" "$PAIR_DIR/longhouse-engine"

for command in python python3 uv pip longhouse-python; do
  cat > "$HOME_DIR/traps/$command" <<'EOF'
#!/usr/bin/env sh
echo "unexpected Python-path invocation: $0" >&2
exit 97
EOF
  chmod 755 "$HOME_DIR/traps/$command"
done

mkdir -p "$HOME_DIR/.local/bin"
cat > "$HOME_DIR/.local/bin/longhouse" <<'EOF'
#!/usr/bin/env sh
echo "legacy Python longhouse shim" >&2
exit 2
EOF
chmod 755 "$HOME_DIR/.local/bin/longhouse"

HOME="$HOME_DIR" \
PATH="$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" \
SHELL=/bin/bash \
LONGHOUSE_NATIVE_BIN_DIR="$PAIR_DIR" \
LONGHOUSE_TELEMETRY=0 \
bash "$ROOT_DIR/scripts/install.sh" >/dev/null

installed="$HOME_DIR/.local/bin/longhouse"
[[ -x "$installed" ]]
[[ -x "$HOME_DIR/.local/bin/longhouse-python" ]]
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" "$installed" verify-pair >/dev/null
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" "$installed" local-health --fast --json >/dev/null
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" "$installed" auth --clear >/dev/null
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" "$installed" machine repair --dry-run --json >/dev/null
[[ ! -e "$HOME_DIR/.claude/hooks/longhouse-permission-gate.py" ]]
echo "native installer smoke passed"
