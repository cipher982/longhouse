#!/bin/bash
# Longhouse Runner - Linux Installation (systemd user service)
# Called by install.sh after registration

set -e

RUNNER_NAME="$1"
RUNNER_SECRET="$2"
LONGHOUSE_URL="$3"
BINARY_URL="$4"

if [ -z "$RUNNER_NAME" ] || [ -z "$RUNNER_SECRET" ] || [ -z "$LONGHOUSE_URL" ] || [ -z "$BINARY_URL" ]; then
  echo "Error: Missing required arguments" >&2
  echo "Usage: install-linux.sh <runner_name> <runner_secret> <longhouse_url> <binary_url>" >&2
  exit 1
fi

# Detect architecture
ARCH=$(uname -m)
case "$ARCH" in
  aarch64|arm64) PLATFORM="linux-arm64" ;;
  x86_64) PLATFORM="linux-x64" ;;
  *) echo "Error: Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

# Directories
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/longhouse"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

# Create directories
mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$SYSTEMD_USER_DIR"

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

# Create systemd user service
SERVICE_FILE="$SYSTEMD_USER_DIR/longhouse-runner.service"

cat > "$SERVICE_FILE" <<EOF
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

echo "Systemd user service created at $SERVICE_FILE"

# Reload systemd user daemon
systemctl --user daemon-reload

# Stop existing service if running
if systemctl --user is-active --quiet longhouse-runner 2>/dev/null; then
  echo "Stopping existing runner..."
  systemctl --user stop longhouse-runner
fi

# Enable and start service
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
echo "Note: By default, user services only run while you're logged in."
echo "For always-on servers, enable lingering: loginctl enable-linger \$USER"
echo ""
echo "To uninstall:"
echo "  curl -fsSL ${LONGHOUSE_URL}/api/runners/uninstall.sh | bash"
