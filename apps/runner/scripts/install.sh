#!/bin/bash
# Longhouse Runner - Universal Installer
# Detects OS, registers runner, and delegates to platform-specific installer
#
# Usage:
#   curl -fsSL https://api.longhouse.ai/api/runners/install.sh?enroll_token=xxx | bash
#
# Environment variables (override query params):
#   ENROLL_TOKEN    - Required enrollment token
#   RUNNER_NAME     - Runner name (default: hostname)
#   LONGHOUSE_URL   - API URL (provided by server)
#   BINARY_URL      - Binary download URL (provided by server)

set -e

# These are replaced by the backend when serving the script
ENROLL_TOKEN="${ENROLL_TOKEN:-__ENROLL_TOKEN__}"
RUNNER_NAME="${RUNNER_NAME:-__RUNNER_NAME__}"
LONGHOUSE_URL="${LONGHOUSE_URL:-__LONGHOUSE_URL__}"
BINARY_URL="${BINARY_URL:-__BINARY_URL__}"

# Validate required vars
if [ -z "$ENROLL_TOKEN" ] || [ "$ENROLL_TOKEN" = "__ENROLL_TOKEN__" ]; then
  echo "Error: ENROLL_TOKEN is required" >&2
  exit 1
fi

if [ -z "$LONGHOUSE_URL" ] || [ "$LONGHOUSE_URL" = "__LONGHOUSE_URL__" ]; then
  echo "Error: LONGHOUSE_URL is required" >&2
  exit 1
fi

# Default runner name to hostname
if [ -z "$RUNNER_NAME" ] || [ "$RUNNER_NAME" = "__RUNNER_NAME__" ]; then
  RUNNER_NAME="$(hostname)"
fi

echo "======================================"
echo "Longhouse Runner Installer"
echo "======================================"
echo ""
echo "Runner Name: $RUNNER_NAME"
echo "API URL: $LONGHOUSE_URL"
echo ""

# Detect OS
OS="$(uname -s)"
case "$OS" in
  Darwin) OS_TYPE="macos" ;;
  Linux) OS_TYPE="linux" ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "Error: Windows is not yet supported. Coming soon!" >&2
    exit 1
    ;;
  *)
    echo "Error: Unsupported operating system: $OS" >&2
    exit 1
    ;;
esac

echo "Detected OS: $OS_TYPE"
echo ""

# Check for required tools
if ! command -v curl >/dev/null 2>&1; then
  echo "Error: curl is required but not installed" >&2
  exit 1
fi

# JSON parsing - try python3 first, then node
parse_json() {
  local json="$1"
  local field="$2"

  if command -v python3 >/dev/null 2>&1; then
    echo "$json" | python3 -c "import sys, json; print(json.load(sys.stdin).get('$field', ''))"
  elif command -v node >/dev/null 2>&1; then
    echo "$json" | node -e "const d=JSON.parse(require('fs').readFileSync(0,'utf-8'));console.log(d['$field']||'')"
  else
    echo ""
  fi
}

# Register runner with backend
echo "Registering runner '$RUNNER_NAME' with Longhouse..."

REGISTER_URL="${LONGHOUSE_URL}/api/runners/register"
RESPONSE=$(curl -sf -X POST "$REGISTER_URL" \
  -H "Content-Type: application/json" \
  -d "{\"enroll_token\": \"$ENROLL_TOKEN\", \"name\": \"$RUNNER_NAME\"}" 2>&1) || {
  echo "Error: Failed to register runner. Check your enrollment token." >&2
  echo "Response: $RESPONSE" >&2
  exit 1
}

# Parse response
RUNNER_SECRET=$(parse_json "$RESPONSE" "runner_secret")
RUNNER_NAME=$(parse_json "$RESPONSE" "name")

if [ -z "$RUNNER_SECRET" ]; then
  if command -v python3 >/dev/null 2>&1 || command -v node >/dev/null 2>&1; then
    echo "Error: Failed to parse runner credentials from response" >&2
    echo "Response: $RESPONSE" >&2
  else
    echo "Error: Please install python3 or node to parse JSON response" >&2
  fi
  exit 1
fi

echo "Runner registered successfully!"
echo ""

# Download and run platform-specific installer
# The platform installers are embedded inline to avoid extra network requests

case "$OS_TYPE" in
  macos)
    # Inline macOS installer
    ARCH=$(uname -m)
    case "$ARCH" in
      arm64|aarch64) PLATFORM="darwin-arm64" ;;
      x86_64) PLATFORM="darwin-x64" ;;
      *) echo "Error: Unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac

    BIN_DIR="$HOME/.local/bin"
    CONFIG_DIR="$HOME/.config/longhouse"
    STATE_DIR="$HOME/.local/state/longhouse"
    LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

    mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$STATE_DIR" "$LAUNCH_AGENTS_DIR"

    BINARY_PATH="$BIN_DIR/longhouse-runner"
    DOWNLOAD_URL="${BINARY_URL}/longhouse-runner-${PLATFORM}"

    echo "Downloading runner binary ($PLATFORM)..."
    if ! curl -fsSL "$DOWNLOAD_URL" -o "$BINARY_PATH"; then
      echo "Error: Failed to download runner binary from $DOWNLOAD_URL" >&2
      exit 1
    fi
    chmod +x "$BINARY_PATH"

    ENV_FILE="$CONFIG_DIR/runner.env"
    cat > "$ENV_FILE" <<EOF
LONGHOUSE_URL=$LONGHOUSE_URL
RUNNER_NAME=$RUNNER_NAME
RUNNER_SECRET=$RUNNER_SECRET
EOF
    chmod 600 "$ENV_FILE"
    echo "Credentials saved to $ENV_FILE"

    PLIST_FILE="$LAUNCH_AGENTS_DIR/com.longhouse.runner.plist"
    cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.longhouse.runner</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BINARY_PATH</string>
        <string>--envfile</string>
        <string>$ENV_FILE</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$HOME</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$STATE_DIR/runner.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE_DIR/runner.log</string>
</dict>
</plist>
EOF

    if launchctl list 2>/dev/null | grep -q "com.longhouse.runner"; then
      echo "Stopping existing runner..."
      launchctl unload "$PLIST_FILE" 2>/dev/null || true
    fi

    echo "Starting runner service..."
    launchctl load "$PLIST_FILE"

    echo ""
    echo "Runner installed and started successfully!"
    echo ""
    echo "Management commands:"
    echo "  launchctl stop com.longhouse.runner      # Stop"
    echo "  launchctl start com.longhouse.runner     # Start"
    echo "  tail -f $STATE_DIR/runner.log            # View logs"
    ;;

  linux)
    # Inline Linux installer
    ARCH=$(uname -m)
    case "$ARCH" in
      aarch64|arm64) PLATFORM="linux-arm64" ;;
      x86_64) PLATFORM="linux-x64" ;;
      *) echo "Error: Unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac

    BIN_DIR="$HOME/.local/bin"
    CONFIG_DIR="$HOME/.config/longhouse"
    SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

    mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$SYSTEMD_USER_DIR"

    BINARY_PATH="$BIN_DIR/longhouse-runner"
    DOWNLOAD_URL="${BINARY_URL}/longhouse-runner-${PLATFORM}"

    echo "Downloading runner binary ($PLATFORM)..."
    if ! curl -fsSL "$DOWNLOAD_URL" -o "$BINARY_PATH"; then
      echo "Error: Failed to download runner binary from $DOWNLOAD_URL" >&2
      exit 1
    fi
    chmod +x "$BINARY_PATH"

    ENV_FILE="$CONFIG_DIR/runner.env"
    cat > "$ENV_FILE" <<EOF
LONGHOUSE_URL=$LONGHOUSE_URL
RUNNER_NAME=$RUNNER_NAME
RUNNER_SECRET=$RUNNER_SECRET
EOF
    chmod 600 "$ENV_FILE"
    echo "Credentials saved to $ENV_FILE"

    SERVICE_FILE="$SYSTEMD_USER_DIR/longhouse-runner.service"
    cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=Longhouse Runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/longhouse-runner --envfile %h/.config/longhouse/runner.env
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=3

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload

    if systemctl --user is-active --quiet longhouse-runner 2>/dev/null; then
      echo "Stopping existing runner..."
      systemctl --user stop longhouse-runner
    fi

    echo "Starting runner service..."
    systemctl --user enable longhouse-runner
    systemctl --user start longhouse-runner

    echo ""
    echo "Runner installed and started successfully!"
    echo ""
    echo "Management commands:"
    echo "  systemctl --user stop longhouse-runner   # Stop"
    echo "  systemctl --user start longhouse-runner  # Start"
    echo "  journalctl --user -u longhouse-runner -f # View logs"
    echo ""
    echo "Note: User services only run while you're logged in."
    echo "For always-on servers: loginctl enable-linger \$USER"
    ;;
esac

echo ""
echo "To uninstall:"
echo "  curl -fsSL ${LONGHOUSE_URL}/api/runners/uninstall.sh | bash"
