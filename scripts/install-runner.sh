#!/usr/bin/env bash
#
# Swarmlet Runner Installation Script
#
# Installs and configures the Swarmlet runner daemon as a systemd service.
# Creates a dedicated non-root user, securely stores credentials, and applies
# systemd hardening for defense in depth.
#
# Usage:
#   curl -sSL https://swarmlet.com/install-runner.sh | bash -s -- \
#     --name cube \
#     --token <enrollment-token> \
#     --url wss://api.swarmlet.com \
#     --version v0.1.0
#
# Options:
#   --name    Runner name (required)
#   --token   Enrollment token from Swarmlet platform (required)
#   --url     Swarmlet API URL (default: wss://api.swarmlet.com)
#   --version Runner release tag to install (default: latest)
#   --insecure  Allow non-WSS URLs (ws://) for local development
#   --capabilities  Comma-separated capabilities (default: exec.readonly)
#

set -euo pipefail

# ----- Constants -----
RUNNER_USER="swarmlet"
INSTALL_DIR="/opt/swarmlet-runner"
CONFIG_DIR="/etc/swarmlet"
CONFIG_FILE="${CONFIG_DIR}/runner.env"
SERVICE_NAME="swarmlet-runner"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
GITHUB_REPO="daverosedavis/zerg"
RUNNER_VERSION="${RUNNER_VERSION:-latest}"

# ----- Colors for output -----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ----- Helper Functions -----

log_info() {
  echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
  echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warn() {
  echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $*" >&2
}

check_root() {
  if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (required for systemd and user creation)"
    log_info "Try: sudo $0 $*"
    exit 1
  fi
}

validate_token() {
  local token="$1"
  # Basic validation: token should be non-empty and alphanumeric with hyphens/underscores
  if [[ ! "$token" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    log_error "Invalid enrollment token format"
    log_info "Token must be alphanumeric with only hyphens and underscores"
    exit 1
  fi
}

validate_url() {
  local url="$1"
  local allow_insecure="${2:-false}"

  if [[ "$url" =~ ^ws:// ]]; then
    if [[ "$allow_insecure" != "true" ]]; then
      log_error "Insecure URL (ws://) detected in production install"
      log_info "Use wss:// for secure connection, or pass --insecure for local dev"
      exit 1
    else
      log_warn "Using insecure WebSocket connection (ws://)"
    fi
  elif [[ ! "$url" =~ ^wss:// ]]; then
    log_error "Invalid URL format: $url"
    log_info "URL must start with wss:// (or ws:// with --insecure flag)"
    exit 1
  fi
}

validate_runner_version() {
  local version="$1"
  if [[ -z "$version" ]]; then
    log_error "Runner version is required (--version or RUNNER_VERSION)"
    exit 1
  fi

  if [[ "$version" == "main" || "$version" == "master" || "$version" == "HEAD" ]]; then
    log_error "Runner version must be a release tag (not $version)"
    exit 1
  fi
}

resolve_runner_release_tag() {
  if [[ "$RUNNER_VERSION" != "latest" ]]; then
    echo "$RUNNER_VERSION"
    return 0
  fi

  local api_url="https://api.github.com/repos/${GITHUB_REPO}/releases/latest"
  local tag
  tag=$(curl -fsSL "$api_url" | grep -m 1 '"tag_name"' | cut -d'"' -f4 || true)
  if [[ -z "$tag" ]]; then
    log_error "Failed to resolve latest runner release tag"
    log_info "Set --version <tag> to install a specific release."
    exit 1
  fi

  echo "$tag"
}

build_runner_archive_url() {
  local tag="$1"
  echo "https://github.com/${GITHUB_REPO}/archive/refs/tags/${tag}.tar.gz"
}

normalize_runner_version() {
  local version="$1"
  version="${version#v}"
  version="${version#runner-}"
  version="${version#runner-v}"
  echo "$version"
}

verify_runner_package_version() {
  local runner_dir="$1"
  if [[ "$RUNNER_VERSION" == "latest" ]]; then
    return 0
  fi

  local pkg_version
  pkg_version=$(grep -m 1 '"version"' "$runner_dir/package.json" | cut -d'"' -f4 || true)
  if [[ -z "$pkg_version" ]]; then
    log_error "Failed to read runner package version for verification"
    exit 1
  fi

  local normalized_version
  normalized_version=$(normalize_runner_version "$RUNNER_VERSION")
  if [[ "$pkg_version" != "$normalized_version" ]]; then
    log_error "Runner version mismatch: expected ${normalized_version}, got ${pkg_version}"
    exit 1
  fi
}

install_bun() {
  if command -v bun &> /dev/null; then
    log_info "Bun is already installed: $(bun --version)"
    return 0
  fi

  log_info "Installing Bun..."
  if ! curl -fsSL https://bun.sh/install | bash; then
    log_error "Failed to install Bun"
    exit 1
  fi

  # Add Bun to PATH for this session
  export BUN_INSTALL="$HOME/.bun"
  export PATH="$BUN_INSTALL/bin:$PATH"

  # Create symlink for system-wide access
  if [[ -f "$HOME/.bun/bin/bun" ]]; then
    ln -sf "$HOME/.bun/bin/bun" /usr/local/bin/bun
    log_success "Bun installed successfully"
  else
    log_error "Bun binary not found after installation"
    exit 1
  fi
}

create_swarmlet_user() {
  if id "$RUNNER_USER" &>/dev/null; then
    log_info "User '$RUNNER_USER' already exists"
  else
    log_info "Creating user '$RUNNER_USER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$RUNNER_USER"
    log_success "User '$RUNNER_USER' created"
  fi
}

download_runner_code() {
  log_info "Downloading runner code to $INSTALL_DIR..."

  # Remove old installation if exists
  if [[ -d "$INSTALL_DIR" ]]; then
    log_warn "Removing existing installation at $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
  fi

  # Create temporary directory for download and extraction
  local temp_dir
  temp_dir=$(mktemp -d)
  trap "rm -rf '$temp_dir'" EXIT

  local release_tag
  release_tag=$(resolve_runner_release_tag)
  local archive_url
  archive_url=$(build_runner_archive_url "$release_tag")

  log_info "Fetching runner release ${release_tag}..."
  if ! curl -fsSL "$archive_url" -o "$temp_dir/runner.tar.gz"; then
    log_error "Failed to download runner release: $archive_url"
    exit 1
  fi

  local root_dir
  root_dir=$(tar -tzf "$temp_dir/runner.tar.gz" | head -n 1 | cut -d"/" -f1)
  if [[ -z "$root_dir" ]]; then
    log_error "Failed to inspect runner archive"
    exit 1
  fi

  tar -xzf "$temp_dir/runner.tar.gz" -C "$temp_dir"

  local runner_dir="$temp_dir/$root_dir/apps/runner"
  if [[ ! -d "$runner_dir" ]]; then
    log_error "Runner directory not found in release archive"
    exit 1
  fi

  verify_runner_package_version "$runner_dir"

  # Copy only the runner app
  mkdir -p "$INSTALL_DIR"
  cp -r "$runner_dir/"* "$INSTALL_DIR/"

  # Install dependencies
  log_info "Installing dependencies..."
  cd "$INSTALL_DIR"
  if ! bun install; then
    log_error "Failed to install dependencies"
    exit 1
  fi

  # Set ownership
  chown -R "$RUNNER_USER:$RUNNER_USER" "$INSTALL_DIR"

  log_success "Runner code downloaded and configured"
}

create_config_file() {
  local runner_name="$1"
  local runner_secret="$2"
  local swarmlet_url="$3"
  local capabilities="${4:-exec.readonly}"

  log_info "Creating configuration at $CONFIG_FILE..."

  # Create config directory
  mkdir -p "$CONFIG_DIR"

  # Write config file
  cat > "$CONFIG_FILE" <<EOF
# Swarmlet Runner Configuration
# This file contains sensitive credentials - do not share or commit to version control

SWARMLET_URL=$swarmlet_url
RUNNER_NAME=$runner_name
RUNNER_SECRET=$runner_secret
RUNNER_CAPABILITIES=$capabilities

# Optional tuning (defaults are usually fine)
HEARTBEAT_INTERVAL_MS=30000
RECONNECT_DELAY_MS=5000
MAX_RECONNECT_DELAY_MS=60000
EOF

  # Secure permissions: 600 (read/write for root only)
  chmod 600 "$CONFIG_FILE"
  chown root:root "$CONFIG_FILE"

  log_success "Configuration file created with secure permissions (600, root-owned)"
}

create_systemd_service() {
  log_info "Creating systemd service at $SERVICE_FILE..."

  cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=Swarmlet Runner Daemon
Documentation=https://swarmlet.com/docs/runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=swarmlet
Group=swarmlet
WorkingDirectory=/opt/swarmlet-runner
EnvironmentFile=/etc/swarmlet/runner.env

# Start command
ExecStart=/usr/local/bin/bun run src/index.ts

# Restart policy
Restart=always
RestartSec=5s

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/opt/swarmlet-runner
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictNamespaces=true

# Resource limits
LimitNOFILE=65536
TasksMax=512

[Install]
WantedBy=multi-user.target
EOF

  # Set permissions
  chmod 644 "$SERVICE_FILE"

  log_success "Systemd service file created"
}

enroll_runner() {
  local enrollment_token="$1"
  local runner_name="$2"
  local swarmlet_url="$3"

  log_info "Registering runner with Swarmlet platform..."

  # Convert ws:// or wss:// to http:// or https:// for API
  local api_url="${swarmlet_url//ws:/http:}"
  api_url="${api_url//wss:/https:}"

  # Call registration endpoint
  local response
  local http_code
  http_code=$(curl -s -w "%{http_code}" -o /tmp/runner_enroll_response.json -X POST "${api_url}/api/runners/register" \
    -H "Content-Type: application/json" \
    -d "{\"enroll_token\": \"${enrollment_token}\", \"name\": \"${runner_name}\"}")

  response=$(cat /tmp/runner_enroll_response.json 2>/dev/null || echo "")
  rm -f /tmp/runner_enroll_response.json

  if [[ "$http_code" != "200" && "$http_code" != "201" ]]; then
    log_error "Failed to register runner (HTTP $http_code)"
    if [[ -n "$response" ]]; then
      local detail
      detail=$(echo "$response" | grep -o '"detail":"[^"]*"' | cut -d'"' -f4)
      [[ -n "$detail" ]] && log_info "Error: $detail"
    fi
    exit 1
  fi

  # Parse response for secret
  local runner_secret
  runner_secret=$(echo "$response" | grep -o '"secret":"[^"]*"' | cut -d'"' -f4)

  if [[ -z "$runner_secret" ]]; then
    log_error "Failed to register runner - no secret in response"
    log_info "Server response: $response"
    exit 1
  fi

  log_success "Runner registered successfully"
  echo "$runner_secret"
}

start_and_enable_service() {
  log_info "Reloading systemd daemon..."
  systemctl daemon-reload

  log_info "Enabling service to start on boot..."
  systemctl enable "$SERVICE_NAME"

  log_info "Starting service..."
  systemctl start "$SERVICE_NAME"

  log_success "Service started and enabled"
}

verify_connection() {
  log_info "Verifying runner connection..."

  # Wait a few seconds for connection
  sleep 3

  # Check service status
  if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    log_error "Service is not running!"
    log_info "Check logs with: journalctl -u $SERVICE_NAME -n 50"
    exit 1
  fi

  # Check logs for success message
  if journalctl -u "$SERVICE_NAME" -n 20 --no-pager | grep -q "Connected to Swarmlet"; then
    log_success "Runner connected to Swarmlet platform"
  else
    log_warn "Service is running but connection status unclear"
    log_info "Check logs with: journalctl -u $SERVICE_NAME -f"
  fi
}

# ----- Main Installation Logic -----

main() {
  local runner_name=""
  local enrollment_token=""
  local swarmlet_url="wss://api.swarmlet.com"
  local allow_insecure="false"
  local capabilities="exec.readonly"
  local runner_version="$RUNNER_VERSION"

  # Parse arguments
  while [[ $# -gt 0 ]]; do
    case $1 in
      --name)
        runner_name="$2"
        shift 2
        ;;
      --token)
        enrollment_token="$2"
        shift 2
        ;;
      --url)
        swarmlet_url="$2"
        shift 2
        ;;
      --version)
        runner_version="$2"
        shift 2
        ;;
      --insecure)
        allow_insecure="true"
        shift
        ;;
      --capabilities)
        capabilities="$2"
        shift 2
        ;;
      *)
        log_error "Unknown option: $1"
        log_info "Usage: $0 --name <name> --token <token> [--url <url>] [--version <tag>] [--insecure] [--capabilities <caps>]"
        exit 1
        ;;
    esac
  done

  # Validate required arguments
  if [[ -z "$runner_name" ]]; then
    log_error "Runner name is required (--name)"
    exit 1
  fi

  if [[ -z "$enrollment_token" ]]; then
    log_error "Enrollment token is required (--token)"
    exit 1
  fi

  # Print banner
  echo ""
  echo "=========================================="
  echo "  Swarmlet Runner Installation"
  echo "=========================================="
  echo ""

  # Validate inputs
  check_root "$@"
  validate_token "$enrollment_token"
  validate_url "$swarmlet_url" "$allow_insecure"
  RUNNER_VERSION="$runner_version"
  validate_runner_version "$RUNNER_VERSION"

  # Installation steps
  log_info "Starting installation for runner: $runner_name"

  install_bun
  create_swarmlet_user
  download_runner_code

  # Enroll with platform to get secret
  runner_secret=$(enroll_runner "$enrollment_token" "$runner_name" "$swarmlet_url")

  create_config_file "$runner_name" "$runner_secret" "$swarmlet_url" "$capabilities"
  create_systemd_service
  start_and_enable_service
  verify_connection

  # Success message
  echo ""
  echo "=========================================="
  log_success "Installation complete!"
  echo "=========================================="
  echo ""
  echo "Runner Name:    $runner_name"
  echo "Swarmlet URL:   $swarmlet_url"
  echo "Capabilities:   $capabilities"
  echo "Runner Version: $RUNNER_VERSION"
  echo ""
  echo "Service Status:"
  systemctl status "$SERVICE_NAME" --no-pager | head -n 10
  echo ""
  echo "Useful commands:"
  echo "  View logs:    journalctl -u $SERVICE_NAME -f"
  echo "  Restart:      systemctl restart $SERVICE_NAME"
  echo "  Stop:         systemctl stop $SERVICE_NAME"
  echo "  Status:       systemctl status $SERVICE_NAME"
  echo ""
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
