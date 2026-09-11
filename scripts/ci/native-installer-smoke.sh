#!/usr/bin/env bash
# Verify the public installer can install a paired native device CLI without
# any Python or uv executable available to it.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: scripts/ci/native-installer-smoke.sh

Default: build and smoke the local native pair.
Remote release: LONGHOUSE_NATIVE_SMOKE_REMOTE=1 plus
  LONGHOUSE_NATIVE_SMOKE_EXPECTED_VERSION=<version without v>
  LONGHOUSE_NATIVE_SMOKE_EXPECTED_COMMIT=<full commit SHA>
Optional upgrade: LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG=<published stable vX.Y.Z>
  Install that exact public release before the target in the same disposable HOME.
  No previous release is selected automatically. Local-build mode rejects this option.
Evidence: LONGHOUSE_NATIVE_SMOKE_ARTIFACT_DIR=<directory> retains safe run evidence;
  otherwise evidence stays under a printed temporary root on failure.

This proves native installation and fixture-backed CLI behavior, not hosted viewing,
provider qualification, or service activation. It never activates a user service.
USAGE
  exit 0
fi
[[ $# == 0 ]] || { echo "Unexpected argument; use --help" >&2; exit 2; }

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
PREVIOUS_TAG="${LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG:-}"
EVIDENCE_DIR="${LONGHOUSE_NATIVE_SMOKE_ARTIFACT_DIR:-$TEST_ROOT/evidence}"
mkdir -p "$EVIDENCE_DIR"
EVIDENCE_DIR="$(cd "$EVIDENCE_DIR" && pwd)"
SMOKE_ENV=(
  env -i "HOME=$HOME_DIR" "LONGHOUSE_HOME=$HOME_DIR/.longhouse"
  "PATH=$HOME_DIR/.local/bin:$HOME_DIR/traps:/usr/bin:/bin:/usr/sbin:/sbin"
  "TMPDIR=$TEST_ROOT/tmp" "SHELL=/bin/bash" "TERM=xterm-256color"
  "XDG_CONFIG_HOME=$HOME_DIR/.config" "XDG_DATA_HOME=$HOME_DIR/.local/share"
  "LONGHOUSE_TELEMETRY=0" "LONGHOUSE_SMOKE_NODE=$NODE_BIN"
  "LONGHOUSE_ORIGIN_KIND=test_or_canary" "LONGHOUSE_LAUNCH_ACTOR=automation"
  "LONGHOUSE_LAUNCH_SURFACE=test"
)

cleanup() {
  local status=$?
  if [[ -n "$RUNTIME_PID" ]]; then
    kill "$RUNTIME_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$RUNTIME_PID" 2>/dev/null || break
      sleep 0.1
    done
    kill -9 "$RUNTIME_PID" 2>/dev/null || true
    wait "$RUNTIME_PID" 2>/dev/null || true
  fi
  # No global service was loaded, so never bootout the user's shared label.
  # Keep only explicit evidence, not credentials, downloaded pairs or helper files.
  if [[ "$status" != 0 || -n "${LONGHOUSE_NATIVE_SMOKE_ARTIFACT_DIR:-}" ]]; then
    printf '%s\n' "$status" > "$EVIDENCE_DIR/exit-status"
    rm -rf "$HOME_DIR" "$PAIR_DIR" "$TEST_ROOT/tmp"
    rm -f "$TEST_ROOT/fake-runtime.js" "$TEST_ROOT/run-bounded.js" "$RUNTIME_PORT_FILE"
    echo "native installer smoke evidence: $EVIDENCE_DIR" >&2
    [[ "$EVIDENCE_DIR" == "$TEST_ROOT/"* ]] || rm -rf "$TEST_ROOT"
  else
    rm -rf "$TEST_ROOT"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "owned native installer proof root: $TEST_ROOT"
trap 'echo "native installer smoke failed at line $LINENO" >&2' ERR

mkdir -p "$PAIR_DIR" "$HOME_DIR/traps" "$TEST_ROOT/tmp"
if [[ -n "$PREVIOUS_TAG" && "$REMOTE_RELEASE" != "1" ]]; then
  echo "LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG requires remote-release mode" >&2
  exit 2
fi
if [[ "$REMOTE_RELEASE" == "1" ]]; then
  [[ "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ && "$EXPECTED_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "Remote smoke requires an exact expected commit and release version" >&2
    exit 2
  }
  [[ -z "${LONGHOUSE_INSTALL_VERSION:-}" || "${LONGHOUSE_INSTALL_VERSION#v}" == "$EXPECTED_VERSION" ]] || {
    echo "LONGHOUSE_INSTALL_VERSION conflicts with the expected target version" >&2
    exit 2
  }
  [[ -z "${LONGHOUSE_NATIVE_BIN_DIR:-}" ]] || {
    echo "Remote smoke refuses a local native binary override" >&2
    exit 2
  }
else
  python3 "$ROOT_DIR/scripts/build/generate_build_identity.py" >/dev/null
  python3 "$ROOT_DIR/scripts/build/cargo.py" exec -- build \
    --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci \
    --bin longhouse --bin longhouse-engine >/dev/null
  cp "$(python3 "$ROOT_DIR/scripts/build/cargo.py" artifact --profile ci --bin longhouse)" \
    "$PAIR_DIR/longhouse"
  cp "$(python3 "$ROOT_DIR/scripts/build/cargo.py" artifact --profile ci --bin longhouse-engine)" \
    "$PAIR_DIR/longhouse-engine"
  bash "$ROOT_DIR/scripts/build/build-sqlite-shell.sh" --output "$PAIR_DIR/longhouse-sqlite3"
fi

for command in python python3 uv pip; do
  cat > "$HOME_DIR/traps/$command" <<'EOF'
#!/usr/bin/env sh
echo "unexpected Python-path invocation: $0" >&2
exit 97
EOF
  chmod 755 "$HOME_DIR/traps/$command"
done

# Bound the entire command group, including installer downloads and engine children.
# This host-side harness is Node; Python/uv remain unavailable to installed commands.
cat > "$TEST_ROOT/run-bounded.js" <<'BOUNDED_EOF'
const { spawn } = require("child_process");
const [seconds, executable, ...args] = process.argv.slice(2);
const child = spawn(executable, args, { stdio: "inherit", detached: true });
let failure;
function stop(code) {
  failure = code;
  try { process.kill(-child.pid, "SIGKILL"); } catch (error) {
    if (error.code !== "ESRCH") throw error;
  }
}
const timer = setTimeout(() => {
  console.error(`native smoke command exceeded ${seconds}s`);
  stop(124);
}, Number(seconds) * 1000);
process.on("SIGTERM", () => stop(143));
process.on("SIGINT", () => stop(130));
child.on("error", (error) => {
  clearTimeout(timer);
  console.error(`native smoke could not start command: ${error.code}`);
  process.exit(1);
});
child.on("close", (code) => {
  clearTimeout(timer);
  try { process.kill(-child.pid, "SIGKILL"); } catch (error) {
    if (error.code !== "ESRCH") throw error;
  }
  process.exit(failure ?? code ?? 1);
});
BOUNDED_EOF

smoke_command() {
  "${SMOKE_ENV[@]}" "$NODE_BIN" "$TEST_ROOT/run-bounded.js" "$@"
}

# Only this host-side metadata reader may see the workflow GitHub credential.
# Installed binaries keep the scrubbed SMOKE_ENV; no secret enters argv or artifacts.
github_metadata() {
  "$NODE_BIN" - "$1" <<'GITHUB_EOF'
const url = process.argv[2];
const token = process.env.GH_TOKEN || process.env.GITHUB_TOKEN;
const headers = { Accept: "application/vnd.github+json", "User-Agent": "longhouse-native-installer-smoke" };
if (token) headers.Authorization = `Bearer ${token}`;
(async () => {
  const response = await fetch(url, { headers, redirect: "error", signal: AbortSignal.timeout(45000) });
  if (!response.ok) throw new Error(`GitHub metadata request failed: HTTP ${response.status}`);
  process.stdout.write(await response.text());
})().catch(error => { console.error(error.message); process.exitCode = 1; });
GITHUB_EOF
}

install_pair() {
  local tag="${1:-}" stage="$2"
  # Native installer smoke must not install a menu-bar app from its disposable HOME.
  local -a env_args=("LONGHOUSE_MACOS_APP_INSTALL_DIR=$HOME_DIR/Applications" "LONGHOUSE_INSTALL_MENUBAR=0")
  if [[ "$REMOTE_RELEASE" == "1" ]]; then
    env_args+=("LONGHOUSE_INSTALL_VERSION=$tag")
  else
    env_args+=("LONGHOUSE_NATIVE_BIN_DIR=$PAIR_DIR")
  fi
  smoke_command 600 env "${env_args[@]}" bash "$ROOT_DIR/scripts/install.sh" \
    > "$EVIDENCE_DIR/$stage-install.log" 2>&1
}

check_identity() {
  local stage="$1" version="$2" commit="$3" recovery_required
  recovery_required="$("$NODE_BIN" -e '
const [major, minor, patch] = process.argv[1].split(".").map(Number);
console.log(major > 0 || minor > 1 || (minor === 1 && patch >= 48) ? "1" : "0");
' "$version")"
  smoke_command 60 "$installed" verify-pair > "$EVIDENCE_DIR/$stage-verify-pair.log"
  if [[ "$REMOTE_RELEASE" == "1" && "$recovery_required" == "1" ]]; then
    local recovery_tool="$HOME_DIR/.local/share/longhouse/current/longhouse-sqlite3"
    [[ -x "$recovery_tool" ]] || {
      echo "release install is missing its private SQLite recovery shell: $recovery_tool" >&2
      return 1
    }
    smoke_command 60 "$recovery_tool" -batch :memory: ".help recover" | grep -qF ".recover"
  fi
  smoke_command 60 "$installed" build-identity --json > "$EVIDENCE_DIR/$stage-identity.json"
  "$NODE_BIN" - "$EVIDENCE_DIR/$stage-identity.json" "$version" "$commit" "$HOME_DIR" <<'IDENTITY_EOF'
const fs = require("fs");
const path = require("path");
const [filename, version, commit, home] = process.argv.slice(2);
const payload = JSON.parse(fs.readFileSync(filename, "utf8"));
for (const name of ["facade", "engine"]) {
  const identity = payload[name];
  if (identity?.version !== version || identity?.commit !== commit || identity?.dirty !== false) {
    throw new Error(`${name} identity does not match the clean expected release ${version} (${commit})`);
  }
}
if (payload.engine_path !== fs.realpathSync(path.join(home, ".local/bin/longhouse-engine"))) {
  throw new Error("facade did not execute its disposable installed engine");
}
IDENTITY_EOF
}

snapshot_upgrade_state() {
  local stage="$1"
  smoke_command 60 "$installed" local-health --json > "$EVIDENCE_DIR/$stage-health.json"
  "$NODE_BIN" - "$HOME_DIR" "$EVIDENCE_DIR/$stage-health.json" "$RUNTIME_PORT" \
    > "$EVIDENCE_DIR/$stage-state.json" <<'STATE_EOF'
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const [home, healthPath, port] = process.argv.slice(2);
const machine = JSON.parse(fs.readFileSync(path.join(home, ".longhouse/machine/state.json"), "utf8"));
const health = JSON.parse(fs.readFileSync(healthPath, "utf8"));
if (machine.runtime_url !== `http://127.0.0.1:${port}` || machine.machine_name !== "native-installer-upgrade") {
  throw new Error("previously enrolled machine configuration is missing or changed");
}
if (health.realtime?.runtime_url !== machine.runtime_url || health.realtime?.machine_name !== machine.machine_name ||
    health.realtime?.token_path !== path.join(home, ".longhouse/machine/device-token")) {
  throw new Error("installed engine cannot read the preserved enrollment");
}
const files = {};
for (const relative of [".longhouse/machine/state.json", ".longhouse/machine/device-token", ".cursor/hooks.json"]) {
  const filename = path.join(home, relative);
  const bytes = fs.readFileSync(filename);
  if (!bytes.length) throw new Error(`empty durable state: ${relative}`);
  files[relative] = { sha256: crypto.createHash("sha256").update(bytes).digest("hex"), mode: fs.statSync(filename).mode & 0o777 };
}
// No credential bytes or invented transcript/history are copied to evidence.
console.log(JSON.stringify({ runtime_url: machine.runtime_url, machine_name: machine.machine_name, files }, null, 2));
STATE_EOF
}

"$NODE_BIN" - "$ROOT_DIR/scripts/install.sh" "$EVIDENCE_DIR/installer-source.json" <<'SOURCE_EOF'
const fs = require("fs");
const crypto = require("crypto");
const [source, output] = process.argv.slice(2);
fs.writeFileSync(output, JSON.stringify({
  source: "scripts/install.sh (checkout source, not a release-tag installer)",
  sha256: crypto.createHash("sha256").update(fs.readFileSync(source)).digest("hex"),
  runtime: "JSON API fixture; no hosted page or real-provider viewing proof",
  service_activation: false,
}, null, 2) + "\n");
SOURCE_EOF


# Managed launches fail closed without coordination authority, matching the
# Claude contract, so the fixture Runtime Host issues a session-scoped token the
# way a real one does. Registration is only accepted when the response echoes the
# launcher's own session id, a non-empty run id, and the requested provider's
# native managed transport, so the fixture must read the request body rather than
# return a constant.
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
      if (/launch-outcome/.test(req.url || "")) {
        res.end(JSON.stringify({ recorded: true }));
        return;
      }
      if (payload && payload.session_id) {
        res.end(
          JSON.stringify({
            session_id: payload.session_id,
            run_id: "native-installer-smoke-run",
            coordination_token: TOKEN,
            managed_transport: "cursor_helm",
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
"${SMOKE_ENV[@]}" "$NODE_BIN" "$TEST_ROOT/fake-runtime.js" >"$RUNTIME_PORT_FILE" &
RUNTIME_PID=$!
for _ in $(seq 1 50); do [[ -s "$RUNTIME_PORT_FILE" ]] && break; sleep 0.1; done
[[ -s "$RUNTIME_PORT_FILE" ]]
RUNTIME_PORT="$(head -n 1 "$RUNTIME_PORT_FILE")"

# This is a JSON API fixture, not a served product page. Hosted viewing and
# real-provider qualification belong to the real-client campaign, not this smoke.
installed="$HOME_DIR/.local/bin/longhouse"
if [[ -n "$PREVIOUS_TAG" ]]; then
  [[ "$PREVIOUS_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "Previous tag must name an exact stable vX.Y.Z release" >&2
    exit 2
  }
  "$NODE_BIN" - "$PREVIOUS_TAG" "$EXPECTED_VERSION" <<'VERSION_EOF'
const [prior, target] = process.argv.slice(2).map(value => value.replace(/^v/, "").split(".").map(BigInt));
const differing = prior.findIndex((part, index) => part !== target[index]);
if (differing < 0 || prior[differing] >= target[differing]) throw new Error("target version must advance beyond previous tag");
VERSION_EOF
  release_api="https://api.github.com/repos/cipher982/longhouse"
  github_metadata "$release_api/releases/tags/$PREVIOUS_TAG" > "$EVIDENCE_DIR/previous-release.json"
  "$NODE_BIN" - "$EVIDENCE_DIR/previous-release.json" "$PREVIOUS_TAG" <<'RELEASE_EOF'
const fs = require("fs");
const [filename, tag] = process.argv.slice(2);
const release = JSON.parse(fs.readFileSync(filename, "utf8"));
if (release.tag_name !== tag || release.draft !== false || release.prerelease !== false || !release.published_at) {
  throw new Error("previous tag is not a published stable public release");
}
RELEASE_EOF
  github_metadata "$release_api/commits/$PREVIOUS_TAG" > "$EVIDENCE_DIR/previous-commit.json"
  previous_commit="$("$NODE_BIN" - "$EVIDENCE_DIR/previous-commit.json" <<'COMMIT_EOF'
const fs = require("fs");
const commit = JSON.parse(fs.readFileSync(process.argv[2], "utf8")).sha;
if (!/^[0-9a-f]{40}$/.test(commit)) throw new Error("public previous tag commit missing");
console.log(commit);
COMMIT_EOF
)"
  [[ "$previous_commit" != "$EXPECTED_COMMIT" ]] || { echo "Upgrade must advance build commit" >&2; exit 1; }
  install_pair "$PREVIOUS_TAG" previous
  check_identity previous "${PREVIOUS_TAG#v}" "$previous_commit"
  smoke_command 60 env LONGHOUSE_DEVICE_TOKEN=native-installer-smoke-upgrade-token \
    "$installed" auth --url "http://127.0.0.1:$RUNTIME_PORT" --device native-installer-upgrade \
    > "$EVIDENCE_DIR/previous-auth.log"
  smoke_command 60 "$installed" cursor configure --cursor-dir "$HOME_DIR/.cursor" \
    > "$EVIDENCE_DIR/previous-cursor-configure.log"
  snapshot_upgrade_state previous
else
  install_pair "$EXPECTED_VERSION" first
fi

first_release="$(readlink "$HOME_DIR/.local/share/longhouse/current")"
install_pair "$EXPECTED_VERSION" target
second_release="$(readlink "$HOME_DIR/.local/share/longhouse/current")"
[[ "$first_release" != "$second_release" ]]
[[ -x "$installed" ]]
smoke_command 60 "$installed" verify-pair > /dev/null
smoke_command 60 "$installed" local-health --json > /dev/null
if [[ "$REMOTE_RELEASE" == "1" ]]; then
  check_identity target "$EXPECTED_VERSION" "$EXPECTED_COMMIT"
fi
if [[ -n "$PREVIOUS_TAG" ]]; then
  # Check before target auth/configure can recreate anything the installer lost.
  snapshot_upgrade_state target
  cmp "$EVIDENCE_DIR/previous-state.json" "$EVIDENCE_DIR/target-state.json" || {
    echo "Upgrade changed or lost durable enrollment, credential, or native hooks" >&2
    exit 1
  }
  echo "native upgrade passed: $PREVIOUS_TAG -> v$EXPECTED_VERSION (enrollment and native hooks preserved)"
  install_pair "$PREVIOUS_TAG" rollback
  check_identity rollback "${PREVIOUS_TAG#v}" "$previous_commit"
  snapshot_upgrade_state rollback
  cmp "$EVIDENCE_DIR/previous-state.json" "$EVIDENCE_DIR/rollback-state.json" || {
    echo "Rollback changed or lost durable enrollment, credential, or native hooks" >&2
    exit 1
  }
  install_pair "$EXPECTED_VERSION" restored
  check_identity restored "$EXPECTED_VERSION" "$EXPECTED_COMMIT"
  snapshot_upgrade_state restored
  cmp "$EVIDENCE_DIR/previous-state.json" "$EVIDENCE_DIR/restored-state.json" || {
    echo "Restoring the candidate changed or lost durable enrollment, credential, or native hooks" >&2
    exit 1
  }
  echo "native rollback passed: v$EXPECTED_VERSION -> $PREVIOUS_TAG -> v$EXPECTED_VERSION"
fi

cat > "$HOME_DIR/traps/open" <<'EOF'
#!/usr/bin/env sh
"$LONGHOUSE_SMOKE_NODE" -e '
const target = new URL(process.argv[1]);
const callback = new URL(target.searchParams.get("callback"));
const body = new URLSearchParams({
  state: target.searchParams.get("state"),
  token: "zdt_browser_fixture_token",
}).toString();
const request = require("http").request(
  callback,
  {
    method: "POST",
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      "content-length": Buffer.byteLength(body),
    },
  },
  (response) => process.exit(response.statusCode === 303 ? 0 : 1),
);
request.on("error", () => process.exit(1));
request.setTimeout(10000, () => request.destroy(new Error("callback timed out")));
request.end(body);
' "$1"
EOF
chmod 755 "$HOME_DIR/traps/open"
ln -s open "$HOME_DIR/traps/xdg-open"
smoke_command 60 "$installed" auth --url "http://127.0.0.1:$RUNTIME_PORT" --browser >/dev/null
[[ "$(cat "$HOME_DIR/.longhouse/machine/device-token")" == "zdt_browser_fixture_token" ]]

smoke_command 60 env LONGHOUSE_DEVICE_TOKEN=native-installer-smoke-token \
  "$installed" auth --url "http://127.0.0.1:$RUNTIME_PORT" >/dev/null
# HOME alone does not isolate launchd/systemd's per-user service namespace.
# Exercise native repair planning, but never load/restart the global shipper label.
smoke_command 60 "$installed" machine repair --repair-service --dry-run --json \
  > "$EVIDENCE_DIR/target-repair-plan.json"
"$NODE_BIN" - "$EVIDENCE_DIR/target-repair-plan.json" <<'REPAIR_EOF'
const fs = require("fs");
const plan = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
if (plan.dry_run !== true || plan.state !== "dry_run_planned" || plan.machine_state?.configured !== true) {
  throw new Error("native service repair could not plan from the installed machine state");
}
REPAIR_EOF
[[ ! -e "$HOME_DIR/Library/LaunchAgents/com.longhouse.shipper.plist" ]]
[[ ! -e "$HOME_DIR/.config/systemd/user/longhouse-shipper.service" ]]
[[ -f "$HOME_DIR/.longhouse/machine/state.json" ]]
[[ -f "$HOME_DIR/.longhouse/machine/device-token" ]]
[[ ! -e "$HOME_DIR/.claude/hooks/longhouse-permission-gate.py" ]]

# Cursor's installed surface is entirely native: hook installation must point
# at the paired engine rather than leaving Python shims in the device path.
mkdir -p "$HOME_DIR/.cursor"
smoke_command 60 "$installed" cursor configure --cursor-dir "$HOME_DIR/.cursor" >/dev/null
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
smoke_command 60 "$PYTHON_BIN" "$ROOT_DIR/scripts/ci/run-in-pty.py" --timeout 45 \
  "$installed" cursor --cwd "$HOME_DIR" --cursor-bin "$HOME_DIR/traps/cursor-agent" \
  >"$EVIDENCE_DIR/cursor-pty.out" 2>"$EVIDENCE_DIR/cursor-pty.err" || {
    echo "native cursor PTY launch failed:" >&2
    cat "$EVIDENCE_DIR/cursor-pty.err" >&2
    cat "$EVIDENCE_DIR/cursor-pty.out" >&2
    exit 1
  }
grep -q 'CURSOR_NATIVE_PTY_OK' "$EVIDENCE_DIR/cursor-pty.out"
cursor_exit=0
smoke_command 60 env LONGHOUSE_FAKE_CURSOR_EXIT=7 \
  "$PYTHON_BIN" "$ROOT_DIR/scripts/ci/run-in-pty.py" --timeout 45 \
  "$installed" cursor --cwd "$HOME_DIR" --cursor-bin "$HOME_DIR/traps/cursor-agent" \
  >"$EVIDENCE_DIR/cursor-exit-7.out" 2>"$EVIDENCE_DIR/cursor-exit-7.err" || cursor_exit=$?
[[ "$cursor_exit" == "7" ]]

# The managed-provider seams have hermetic upstream fixtures. Keep those
# canaries in the installer lane so a fresh native install cannot regress a
# provider bridge without exercising its transcript/control contract.
if [[ "$REMOTE_RELEASE" != "1" ]]; then
  python3 "$ROOT_DIR/scripts/build/cargo.py" exec -- test \
    --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse-engine \
    codex_app_server_canary -- --nocapture
  python3 "$ROOT_DIR/scripts/build/cargo.py" exec -- test \
    --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse-engine \
    claude_channel -- --nocapture
  python3 "$ROOT_DIR/scripts/build/cargo.py" exec -- test \
    --manifest-path "$ROOT_DIR/engine/Cargo.toml" --profile ci --bin longhouse-engine \
    opencode_control -- --nocapture
fi
echo "native installer smoke passed"
