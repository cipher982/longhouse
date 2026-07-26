#!/usr/bin/env bash
# Verify the public installer can install a paired native device CLI without
# any Python or uv executable available to it.
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
NODE_BIN="$(command -v node)"
PYTHON_BIN="$(command -v python3)"
TEST_ROOT="$(mktemp -d)"
PAIR_DIR="$TEST_ROOT/pair"
HOME_DIR="$TEST_ROOT/home"
RUNTIME_PORT_FILE="$TEST_ROOT/runtime-port"
RUNTIME_PID=""
REMOTE_RELEASE="${LONGHOUSE_NATIVE_SMOKE_REMOTE:-0}"
EXPECTED_COMMIT="${LONGHOUSE_NATIVE_SMOKE_EXPECTED_COMMIT:-}"
EXPECTED_VERSION="${LONGHOUSE_NATIVE_SMOKE_EXPECTED_VERSION:-}"

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

mkdir -p "$PAIR_DIR" "$HOME_DIR/traps"
if [[ "$REMOTE_RELEASE" == "1" ]]; then
  [[ -n "$EXPECTED_COMMIT" && -n "$EXPECTED_VERSION" ]]
else
  python3 "$ROOT_DIR/scripts/build/generate_build_identity.py" >/dev/null
  cargo build --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse --bin longhouse-engine >/dev/null
  cp "$ROOT_DIR/engine/target/ci/longhouse" "$PAIR_DIR/longhouse"
  cp "$ROOT_DIR/engine/target/ci/longhouse-engine" "$PAIR_DIR/longhouse-engine"
fi

for command in python python3 uv pip; do
  cat > "$HOME_DIR/traps/$command" <<'EOF'
#!/usr/bin/env sh
echo "unexpected Python-path invocation: $0" >&2
exit 97
EOF
  chmod 755 "$HOME_DIR/traps/$command"
done

install_pair() {
  local -a env_args=(
    "HOME=$HOME_DIR"
    "PATH=$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin"
    "SHELL=/bin/bash"
    "LONGHOUSE_TELEMETRY=0"
  )
  if [[ "$REMOTE_RELEASE" != "1" ]]; then
    env_args+=("LONGHOUSE_NATIVE_BIN_DIR=$PAIR_DIR")
  fi
  env "${env_args[@]}" bash "$ROOT_DIR/scripts/install.sh" >/dev/null
}

install_pair

first_release="$(readlink "$HOME_DIR/.local/share/longhouse/current")"
install_pair
second_release="$(readlink "$HOME_DIR/.local/share/longhouse/current")"
[[ "$first_release" != "$second_release" ]]

installed="$HOME_DIR/.local/bin/longhouse"
[[ -x "$installed" ]]
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" "$installed" verify-pair >/dev/null
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" "$installed" local-health --fast --json >/dev/null
if [[ "$REMOTE_RELEASE" == "1" ]]; then
  identity="$(HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" "$installed" build-identity --json)"
  LONGHOUSE_SMOKE_IDENTITY="$identity" LONGHOUSE_SMOKE_EXPECTED_COMMIT="$EXPECTED_COMMIT" LONGHOUSE_SMOKE_EXPECTED_VERSION="$EXPECTED_VERSION" \
    "$NODE_BIN" -e '
const payload = JSON.parse(process.env.LONGHOUSE_SMOKE_IDENTITY);
if (payload.facade?.commit !== process.env.LONGHOUSE_SMOKE_EXPECTED_COMMIT) throw new Error(`commit mismatch: ${payload.facade?.commit}`);
if (payload.facade?.version !== process.env.LONGHOUSE_SMOKE_EXPECTED_VERSION) throw new Error(`version mismatch: ${payload.facade?.version}`);
'
fi

# Managed launches fail closed without coordination authority, matching the
# Claude contract, so the fixture Runtime Host issues a session-scoped token the
# way a real one does. Registration is only accepted when the response echoes the
# launcher's own session id and a non-empty run id, so the fixture must read the
# request body rather than return a constant.
cat > "$TEST_ROOT/fake-runtime.js" <<'RUNTIME_EOF'
const http = require("http");
const TOKEN = "native-installer-smoke-coordination";
http
  .createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", () => {
      res.writeHead(200, { "content-type": "application/json" });
      let payload = {};
      try {
        payload = JSON.parse(body || "{}");
      } catch (_) {
        payload = {};
      }
      if (/coordination-token/.test(req.url || "")) {
        res.end(JSON.stringify({ coordination_token: TOKEN }));
        return;
      }
      if (payload && payload.session_id) {
        res.end(
          JSON.stringify({
            session_id: payload.session_id,
            run_id: "native-installer-smoke-run",
            coordination_token: TOKEN,
          }),
        );
        return;
      }
      res.end(JSON.stringify({ items: [] }));
    });
  })
  .listen(0, "127.0.0.1", function () {
    console.log(this.address().port);
  });
RUNTIME_EOF
node "$TEST_ROOT/fake-runtime.js" >"$RUNTIME_PORT_FILE" &
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

# Cursor's installed surface is entirely native: hook installation must point
# at the paired engine rather than leaving Python shims in the device path.
mkdir -p "$HOME_DIR/.cursor"
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" \
  "$installed" cursor configure --cursor-dir "$HOME_DIR/.cursor" >/dev/null
grep -q 'cursor-lifecycle-hook' "$HOME_DIR/.cursor/hooks.json"
grep -q 'cursor-permission-hook' "$HOME_DIR/.cursor/hooks.json"
! grep -q 'longhouse-cursor-hook.py\|longhouse-cursor-permission-hook.py' "$HOME_DIR/.cursor/hooks.json"

# Exercise the native forkpty path under a real pseudo-terminal without a
# Cursor installation. The fixture implements only Cursor's create-chat and
# resumed TUI contracts and proves no Python fallback participates.
cat > "$HOME_DIR/traps/cursor-agent" <<'EOF'
#!/usr/bin/env sh
if [ "$1" = "create-chat" ]; then
  printf '%s\n' '00000000-0000-0000-0000-000000000001'
  exit 0
fi
printf '%s\n' 'CURSOR_NATIVE_PTY_OK'
sleep 1
exit "${LONGHOUSE_FAKE_CURSOR_EXIT:-0}"
EOF
chmod 755 "$HOME_DIR/traps/cursor-agent"
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" \
  "$PYTHON_BIN" "$ROOT_DIR/scripts/ci/run-in-pty.py" \
  "$installed" cursor --cwd "$HOME_DIR" --cursor-bin "$HOME_DIR/traps/cursor-agent" \
  >"$HOME_DIR/cursor-pty.out" 2>"$HOME_DIR/cursor-pty.err" || {
    echo "native cursor PTY launch failed:" >&2
    cat "$HOME_DIR/cursor-pty.err" >&2
    cat "$HOME_DIR/cursor-pty.out" >&2
    exit 1
  }
grep -q 'CURSOR_NATIVE_PTY_OK' "$HOME_DIR/cursor-pty.out"
set +e
HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin" \
  LONGHOUSE_FAKE_CURSOR_EXIT=7 "$PYTHON_BIN" "$ROOT_DIR/scripts/ci/run-in-pty.py" \
  "$installed" cursor --cwd "$HOME_DIR" --cursor-bin "$HOME_DIR/traps/cursor-agent" \
  >/dev/null
cursor_exit=$?
set -e
[[ "$cursor_exit" == "7" ]]

# The managed-provider seams have hermetic upstream fixtures. Keep those
# canaries in the installer lane so a fresh native install cannot regress a
# provider bridge without exercising its transcript/control contract.
if [[ "$REMOTE_RELEASE" != "1" ]]; then
  cargo test --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse-engine codex_app_server_canary -- --nocapture
  cargo test --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse-engine claude_channel -- --nocapture
  cargo test --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse-engine opencode_control -- --nocapture
fi
echo "native installer smoke passed"
