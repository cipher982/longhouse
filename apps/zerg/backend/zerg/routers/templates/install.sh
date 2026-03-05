#!/bin/bash
# Longhouse Runner - Universal Installer
# Detects OS, registers runner, and installs as native service
#
# macOS: launchd LaunchAgent
# Linux: systemd user service

set -e

# Configuration (can be overridden via env vars)
# Values are pre-validated and shell-escaped by the server
ENROLL_TOKEN="${ENROLL_TOKEN:-__ENROLL_TOKEN__}"
RUNNER_NAME="${RUNNER_NAME:-__RUNNER_NAME_EXPR__}"
LONGHOUSE_URL="${LONGHOUSE_URL:-__API_URL__}"
BINARY_URL="${BINARY_URL:-__BINARY_URL__}"

# Validate required vars
if [ -z "$ENROLL_TOKEN" ]; then
  echo "Error: ENROLL_TOKEN is required" >&2
  exit 1
fi

if [ -z "$LONGHOUSE_URL" ]; then
  echo "Error: LONGHOUSE_URL is required" >&2
  exit 1
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

# JSON parsing helper
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
  # Don't print response - may contain secrets on partial success
  exit 1
}

# Parse response (don't print raw response - contains runner_secret)
RUNNER_SECRET=$(parse_json "$RESPONSE" "runner_secret")
RUNNER_NAME=$(parse_json "$RESPONSE" "name")

if [ -z "$RUNNER_SECRET" ]; then
  if command -v python3 >/dev/null 2>&1 || command -v node >/dev/null 2>&1; then
    echo "Error: Failed to parse runner credentials from response" >&2
    echo "Hint: Server may have returned an error. Check your enrollment token." >&2
  else
    echo "Error: Please install python3 or node to parse JSON response" >&2
  fi
  exit 1
fi

echo "Runner registered successfully!"
echo ""

# Platform-specific installation
case "$OS_TYPE" in
  macos)
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
    # Restrict state dir permissions (logs may contain sensitive output)
    chmod 700 "$STATE_DIR"
    touch "$STATE_DIR/runner.log"
    chmod 600 "$STATE_DIR/runner.log"

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
    # Restrict config dir permissions (contains secrets)
    chmod 700 "$CONFIG_DIR"

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
    cat > "$SERVICE_FILE" <<'SERVICEEOF'
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
SERVICEEOF

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
    echo "Note: User services only run while you are logged in."
    echo "For always-on servers: loginctl enable-linger \$USER"
    ;;
esac

echo ""
echo "To uninstall:"
echo "  curl -fsSL ${LONGHOUSE_URL}/api/runners/uninstall.sh | bash"
