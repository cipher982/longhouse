#!/bin/bash
# Longhouse Runner - Universal Uninstaller
# Removes runner service and all associated files

set -e

load_env_file() {
  local env_file="$1"
  if [ -f "$env_file" ]; then
    # shellcheck disable=SC1090
    . "$env_file"
  fi
}

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
    ENV_FILE="$HOME/.config/longhouse/runner.env"
    load_env_file "$ENV_FILE"
    INSTALL_ROOT="${RUNNER_INSTALL_ROOT:-$HOME/.local/share/longhouse-runner}"
    LAUNCHER_PATH="${RUNNER_LAUNCHER_PATH:-$HOME/.local/bin/longhouse-runner}"

    # Stop and unload service
    if [ -f "$PLIST_FILE" ]; then
      echo "Stopping and removing launchd service..."
      launchctl unload "$PLIST_FILE" 2>/dev/null || true
      rm -f "$PLIST_FILE"
      echo "LaunchAgent removed"
    else
      echo "LaunchAgent not found (already removed?)"
    fi

    # Remove launcher
    if [ -f "$LAUNCHER_PATH" ]; then
      rm -f "$LAUNCHER_PATH"
      echo "Launcher removed: $LAUNCHER_PATH"
    fi

    # Remove config directory
    CONFIG_DIR="$HOME/.config/longhouse"
    if [ -d "$CONFIG_DIR" ]; then
      rm -rf "$CONFIG_DIR"
      echo "Config removed: $CONFIG_DIR"
    fi

    # Remove versioned install root (versions, downloads, state)
    if [ -d "$INSTALL_ROOT" ]; then
      rm -rf "$INSTALL_ROOT"
      echo "Install root removed: $INSTALL_ROOT"
    fi
    ;;

  linux)
    SERVICE_NAME="longhouse-runner"
    USER_SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
    SERVER_SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    SERVER_ENV_FILE="/etc/longhouse/runner.env"

    if [ -f "$SERVER_SERVICE_FILE" ] || [ -f "$SERVER_ENV_FILE" ]; then
      if [ "$(id -u)" -eq 0 ]; then
        SUDO=""
      else
        if ! command -v sudo >/dev/null 2>&1; then
          echo "Error: Server runner uninstall requires sudo or root privileges" >&2
          exit 1
        fi
        SUDO="sudo"
      fi

      INSTALL_USER="$($SUDO awk -F= '/^User=/{print $2; exit}' "$SERVER_SERVICE_FILE" 2>/dev/null || true)"
      if [ -n "$INSTALL_USER" ]; then
        INSTALL_HOME="$(getent passwd "$INSTALL_USER" 2>/dev/null | cut -d: -f6)"
        if [ -z "$INSTALL_HOME" ] && command -v python3 >/dev/null 2>&1; then
          INSTALL_HOME="$(python3 -c 'import pwd, sys; print(pwd.getpwnam(sys.argv[1]).pw_dir)' "$INSTALL_USER" 2>/dev/null || true)"
        fi
      fi
      if [ -z "$INSTALL_HOME" ]; then
        INSTALL_HOME="$HOME"
      fi
      INSTALL_ROOT="$INSTALL_HOME/.local/share/longhouse-runner"
      LAUNCHER_PATH="$INSTALL_HOME/.local/bin/longhouse-runner"

      # Stop and disable service
      if $SUDO systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo "Stopping systemd service..."
        $SUDO systemctl stop "$SERVICE_NAME"
      fi

      if $SUDO systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo "Disabling systemd service..."
        $SUDO systemctl disable "$SERVICE_NAME"
      fi

      # Remove service file
      if [ -f "$SERVER_SERVICE_FILE" ]; then
        $SUDO rm -f "$SERVER_SERVICE_FILE"
        $SUDO systemctl daemon-reload
        echo "Systemd service removed"
      else
        echo "Systemd service not found (already removed?)"
      fi

      if [ -f "$SERVER_ENV_FILE" ]; then
        $SUDO rm -f "$SERVER_ENV_FILE"
        echo "Config removed: $SERVER_ENV_FILE"
      fi

      if [ -f "$LAUNCHER_PATH" ]; then
        $SUDO rm -f "$LAUNCHER_PATH"
        echo "Launcher removed: $LAUNCHER_PATH"
      fi

      if [ -d "$INSTALL_ROOT" ]; then
        $SUDO rm -rf "$INSTALL_ROOT"
        echo "Install root removed: $INSTALL_ROOT"
      fi
    else
      ENV_FILE="$HOME/.config/longhouse/runner.env"
      load_env_file "$ENV_FILE"
      INSTALL_ROOT="${RUNNER_INSTALL_ROOT:-$HOME/.local/share/longhouse-runner}"
      LAUNCHER_PATH="${RUNNER_LAUNCHER_PATH:-$HOME/.local/bin/longhouse-runner}"

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
      if [ -f "$USER_SERVICE_FILE" ]; then
        rm -f "$USER_SERVICE_FILE"
        systemctl --user daemon-reload
        echo "Systemd service removed"
      else
        echo "Systemd service not found (already removed?)"
      fi

      if [ -f "$LAUNCHER_PATH" ]; then
        rm -f "$LAUNCHER_PATH"
        echo "Launcher removed: $LAUNCHER_PATH"
      fi

      if [ -d "$INSTALL_ROOT" ]; then
        rm -rf "$INSTALL_ROOT"
        echo "Install root removed: $INSTALL_ROOT"
      fi

      # Remove config directory
      CONFIG_DIR="$HOME/.config/longhouse"
      if [ -d "$CONFIG_DIR" ]; then
        rm -rf "$CONFIG_DIR"
        echo "Config removed: $CONFIG_DIR"
      fi
    fi
    ;;
esac

echo ""
echo "Longhouse Runner uninstalled successfully!"
echo ""
echo "Note: The runner registration still exists on the server."
echo "To fully remove, revoke the runner from the Longhouse web UI."
