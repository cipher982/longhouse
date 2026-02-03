#!/bin/bash
# Longhouse Runner - Universal Uninstaller
# Removes runner service and all associated files
#
# Usage:
#   curl -fsSL https://api.longhouse.ai/api/runners/uninstall.sh | bash

set -e

echo "======================================"
echo "Longhouse Runner Uninstaller"
echo "======================================"
echo ""

# Detect OS
OS="$(uname -s)"
case "$OS" in
  Darwin) OS_TYPE="macos" ;;
  Linux) OS_TYPE="linux" ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "Error: Windows uninstaller not yet supported" >&2
    exit 1
    ;;
  *)
    echo "Error: Unsupported operating system: $OS" >&2
    exit 1
    ;;
esac

echo "Detected OS: $OS_TYPE"
echo ""

case "$OS_TYPE" in
  macos)
    PLIST_FILE="$HOME/Library/LaunchAgents/com.longhouse.runner.plist"

    # Stop and unload service
    if [ -f "$PLIST_FILE" ]; then
      echo "Stopping and removing launchd service..."
      launchctl unload "$PLIST_FILE" 2>/dev/null || true
      rm -f "$PLIST_FILE"
      echo "LaunchAgent removed"
    else
      echo "LaunchAgent not found (already removed?)"
    fi

    # Remove binary
    BINARY_PATH="$HOME/.local/bin/longhouse-runner"
    if [ -f "$BINARY_PATH" ]; then
      rm -f "$BINARY_PATH"
      echo "Binary removed: $BINARY_PATH"
    fi

    # Remove config directory
    CONFIG_DIR="$HOME/.config/longhouse"
    if [ -d "$CONFIG_DIR" ]; then
      rm -rf "$CONFIG_DIR"
      echo "Config removed: $CONFIG_DIR"
    fi

    # Remove state directory (logs)
    STATE_DIR="$HOME/.local/state/longhouse"
    if [ -d "$STATE_DIR" ]; then
      rm -rf "$STATE_DIR"
      echo "State/logs removed: $STATE_DIR"
    fi
    ;;

  linux)
    SERVICE_NAME="longhouse-runner"
    SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"

    # Stop and disable service
    if systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
      echo "Stopping systemd service..."
      systemctl --user stop "$SERVICE_NAME"
    fi

    if systemctl --user is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
      echo "Disabling systemd service..."
      systemctl --user disable "$SERVICE_NAME"
    fi

    # Remove service file
    if [ -f "$SERVICE_FILE" ]; then
      rm -f "$SERVICE_FILE"
      systemctl --user daemon-reload
      echo "Systemd service removed"
    else
      echo "Systemd service not found (already removed?)"
    fi

    # Remove binary
    BINARY_PATH="$HOME/.local/bin/longhouse-runner"
    if [ -f "$BINARY_PATH" ]; then
      rm -f "$BINARY_PATH"
      echo "Binary removed: $BINARY_PATH"
    fi

    # Remove config directory
    CONFIG_DIR="$HOME/.config/longhouse"
    if [ -d "$CONFIG_DIR" ]; then
      rm -rf "$CONFIG_DIR"
      echo "Config removed: $CONFIG_DIR"
    fi
    ;;
esac

echo ""
echo "Longhouse Runner uninstalled successfully!"
echo ""
echo "Note: The runner registration still exists on the server."
echo "To fully remove, revoke the runner from the Longhouse web UI."
