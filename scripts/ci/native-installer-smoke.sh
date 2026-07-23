#!/usr/bin/env bash
# Verify the public installer can install a paired native device CLI without
# any Python or uv executable available to it.
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
NODE_BIN="$(command -v node)"
TEST_ROOT="$(mktemp -d)"
PAIR_DIR="$TEST_ROOT/pair"
HOME_DIR="$TEST_ROOT/home"
RUNTIME_PORT_FILE="$TEST_ROOT/runtime-port"
RUNTIME_PID=""

cleanup() {
  if [[ -n "$RUNTIME_PID" ]]; then
    kill "$RUNTIME_PID" 2>/dev/null || true
  fi
  if [[ "$(uname -s)" == "Darwin" ]]; then
    launchctl bootout "gui/$(id -u)" "$HOME_DIR/Library/LaunchAgents/com.longhouse.shipper.plist" 2>/dev/null || true
  fi
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

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

first_release="$(readlink "$HOME_DIR/.local/share/longhouse/current")"
HOME="$HOME_DIR" \
PATH="$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" \
SHELL=/bin/bash \
LONGHOUSE_NATIVE_BIN_DIR="$PAIR_DIR" \
LONGHOUSE_TELEMETRY=0 \
bash "$ROOT_DIR/scripts/install.sh" >/dev/null
second_release="$(readlink "$HOME_DIR/.local/share/longhouse/current")"
[[ "$first_release" != "$second_release" ]]

installed="$HOME_DIR/.local/bin/longhouse"
[[ -x "$installed" ]]
[[ -x "$HOME_DIR/.local/bin/longhouse-python" ]]
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" "$installed" verify-pair >/dev/null
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" "$installed" local-health --fast --json >/dev/null

node -e 'const http=require("http"); http.createServer((_,res)=>{res.writeHead(200,{"content-type":"application/json"});res.end("{\"items\":[]}")}).listen(0,"127.0.0.1",function(){console.log(this.address().port)})' >"$RUNTIME_PORT_FILE" &
RUNTIME_PID=$!
for _ in $(seq 1 50); do [[ -s "$RUNTIME_PORT_FILE" ]] && break; sleep 0.1; done
[[ -s "$RUNTIME_PORT_FILE" ]]
RUNTIME_PORT="$(head -n 1 "$RUNTIME_PORT_FILE")"

cat > "$HOME_DIR/traps/open" <<'EOF'
#!/usr/bin/env sh
"$LONGHOUSE_SMOKE_NODE" -e '
const target = new URL(process.argv[1]);
const callback = new URL(target.searchParams.get("callback"));
callback.searchParams.set("state", target.searchParams.get("state"));
callback.searchParams.set("token", "zdt_browser_fixture_token");
require("http").get(callback, (response) => process.exit(response.statusCode === 200 ? 0 : 1));
' "$1"
EOF
chmod 755 "$HOME_DIR/traps/open"
ln -s open "$HOME_DIR/traps/xdg-open"
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" \
LONGHOUSE_SMOKE_NODE="$NODE_BIN" \
"$installed" auth --url "http://127.0.0.1:$RUNTIME_PORT" --browser >/dev/null
[[ "$(cat "$HOME_DIR/.longhouse/machine/device-token")" == "zdt_browser_fixture_token" ]]

HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" \
LONGHOUSE_DEVICE_TOKEN="native-installer-smoke-token" \
"$installed" auth --url "http://127.0.0.1:$RUNTIME_PORT" >/dev/null
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" \
"$installed" machine repair --repair-service --json >/dev/null
[[ -f "$HOME_DIR/.longhouse/machine/state.json" ]]
[[ -f "$HOME_DIR/.longhouse/machine/device-token" ]]
[[ -f "$HOME_DIR/Library/LaunchAgents/com.longhouse.shipper.plist" || "$(uname -s)" != "Darwin" ]]
[[ ! -e "$HOME_DIR/.claude/hooks/longhouse-permission-gate.py" ]]

# The managed-provider seams have hermetic upstream fixtures. Keep those
# canaries in the installer lane so a fresh native install cannot regress a
# provider bridge without exercising its transcript/control contract.
cargo test --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse-engine codex_app_server_canary -- --nocapture
cargo test --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse-engine claude_channel -- --nocapture
cargo test --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse-engine opencode_control -- --nocapture
echo "native installer smoke passed"
