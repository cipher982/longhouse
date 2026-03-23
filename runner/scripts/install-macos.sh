#!/bin/bash
# Longhouse Runner - macOS Installation (launchd)
# Called by install.sh after registration

set -e

RUNNER_NAME="$1"
RUNNER_SECRET="$2"
LONGHOUSE_URL="$3"
BINARY_URL="$4"

if [ -z "$RUNNER_NAME" ] || [ -z "$RUNNER_SECRET" ] || [ -z "$LONGHOUSE_URL" ] || [ -z "$BINARY_URL" ]; then
  echo "Error: Missing required arguments" >&2
  echo "Usage: install-macos.sh <runner_name> <runner_secret> <longhouse_url> <binary_url>" >&2
  exit 1
fi

# Detect architecture
ARCH=$(uname -m)
case "$ARCH" in
  arm64|aarch64) PLATFORM="darwin-arm64" ;;
  x86_64) PLATFORM="darwin-x64" ;;
  *) echo "Error: Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

# Directories
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/longhouse"
STATE_DIR="$HOME/.local/state/longhouse"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

# Create directories
mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$STATE_DIR" "$LAUNCH_AGENTS_DIR"

# Download binary
BINARY_PATH="$BIN_DIR/longhouse-runner"
DOWNLOAD_URL="${BINARY_URL}/longhouse-runner-${PLATFORM}"

echo "Downloading runner binary from $DOWNLOAD_URL..."
if ! curl -fsSL "$DOWNLOAD_URL" -o "$BINARY_PATH"; then
  echo "Error: Failed to download runner binary" >&2
  exit 1
fi
chmod +x "$BINARY_PATH"

# Save credentials to env file
ENV_FILE="$CONFIG_DIR/runner.env"
cat > "$ENV_FILE" <<EOF
LONGHOUSE_URL=$LONGHOUSE_URL
RUNNER_NAME=$RUNNER_NAME
RUNNER_SECRET=$RUNNER_SECRET
EOF
chmod 600 "$ENV_FILE"

echo "Credentials saved to $ENV_FILE"

# Create launchd plist
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

echo "LaunchAgent created at $PLIST_FILE"

# Unload existing service if present
if launchctl list 2>/dev/null | grep -q "com.longhouse.runner"; then
  echo "Stopping existing runner..."
  launchctl unload "$PLIST_FILE" 2>/dev/null || true
fi

# Load and start service
echo "Starting runner service..."
launchctl load "$PLIST_FILE"

echo ""
echo "Runner installed and started successfully!"
echo ""
echo "Management commands:"
echo "  launchctl stop com.longhouse.runner      # Stop"
echo "  launchctl start com.longhouse.runner     # Start"
echo "  tail -f $STATE_DIR/runner.log            # View logs"
echo ""
echo "To uninstall:"
echo "  curl -fsSL ${LONGHOUSE_URL}/api/runners/uninstall.sh | bash"
