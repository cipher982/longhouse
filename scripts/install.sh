#!/usr/bin/env bash
#
# Longhouse One-Liner Installer
#
# Usage:
#   curl -fsSL https://get.longhouse.ai/install.sh | bash
#
# Environment:
#   http_proxy/https_proxy Proxy settings (honored automatically)
#
set -euo pipefail

# Track if PATH was updated (for final message)
PATH_UPDATED=0

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Logging functions
info() { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step() { echo -e "\n${BOLD}${CYAN}==> $*${NC}"; }

# Detect platform
detect_platform() {
    local os arch

    case "$(uname -s)" in
        Darwin) os="darwin" ;;
        Linux) os="linux" ;;
        MINGW*|MSYS*|CYGWIN*) os="windows" ;;
        *) error "Unsupported OS: $(uname -s)"; exit 1 ;;
    esac

    case "$(uname -m)" in
        x86_64|amd64) arch="x86_64" ;;
        arm64|aarch64) arch="arm64" ;;
        *) error "Unsupported architecture: $(uname -m)"; exit 1 ;;
    esac

    # Detect WSL
    if [[ -f /proc/version ]] && grep -qi microsoft /proc/version; then
        warn "Running in WSL (Windows Subsystem for Linux)"
        warn "  - systemd may not be available for background services"
        warn "  - You can still run 'longhouse connect' manually"
        echo ""
    fi

    echo "${os}-${arch}"
}

# Check if command exists
has_command() {
    command -v "$1" &>/dev/null
}

# Install uv (Python package manager)
install_uv() {
    if has_command uv; then
        info "uv already installed: $(uv --version)"
        return 0
    fi

    step "Installing uv (Python package manager)"
    curl -LsSf https://astral.sh/uv/install.sh | sh

    # Source the path update
    export PATH="$HOME/.local/bin:$PATH"

    if has_command uv; then
        success "uv installed: $(uv --version)"
    else
        error "uv installation failed"
        exit 1
    fi
}

# Install Python via uv
install_python() {
    step "Ensuring Python 3.12+ is available"

    # Check if uv can already find Python
    if uv python find 3.12 &>/dev/null 2>&1; then
        local py_path
        py_path=$(uv python find 3.12 2>/dev/null)
        info "Python found: $py_path"
        return 0
    fi

    info "Installing Python 3.12 via uv..."
    uv python install 3.12

    success "Python 3.12 installed"
}

# Install Longhouse CLI
install_longhouse() {
    step "Installing Longhouse CLI"

    # Package source defaults to the stable PyPI package and can be overridden
    # for local/dev installs and isolated release validation.
    local pkg_source="${LONGHOUSE_PKG_SOURCE:-longhouse}"
    local custom_source=0
    local install_source="pypi"

    if [[ "$pkg_source" != "longhouse" ]]; then
        custom_source=1
        install_source="custom"
    fi

    # Install the longhouse package as a tool
    if [[ "$custom_source" -eq 1 ]]; then
        info "Installing longhouse from configured source..."
        # Non-PyPI sources can otherwise reuse a stale cached wheel and miss
        # recent code changes during disposable installer validation.
        uv tool uninstall longhouse 2>/dev/null || true
        uv tool install --force --no-cache "$pkg_source"
    elif uv tool list 2>/dev/null | grep -q "^longhouse"; then
        info "Upgrading existing longhouse installation..."
        uv tool upgrade longhouse || {
            # If upgrade fails (e.g., installed from different source), reinstall
            info "Reinstalling longhouse..."
            uv tool uninstall longhouse 2>/dev/null || true
            uv tool install "$pkg_source"
        }
    else
        info "Installing longhouse..."
        uv tool install "$pkg_source"
    fi

    # Ensure uv tools bin is in PATH
    export PATH="$HOME/.local/bin:$PATH"

    if has_command longhouse; then
        local -a record_install_args
        record_install_args=(
            record-install
            --install-method uv
            --install-source "$install_source"
            --package-name longhouse
            --channel stable
        )
        if [[ "$custom_source" -eq 1 ]]; then
            record_install_args+=(--package-ref "$pkg_source")
        fi
        success "longhouse installed: $(longhouse --version 2>/dev/null || echo 'installed')"
        if ! longhouse "${record_install_args[@]}" >/dev/null 2>&1; then
            warn "Could not write Longhouse install metadata"
        fi
    else
        error "longhouse installation failed"
        error "Try adding ~/.local/bin to your PATH:"
        error "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        exit 1
    fi
}

resolve_longhouse_cli_version() {
    if ! has_command longhouse; then
        return 1
    fi

    local version_line version
    version_line="$(longhouse --version 2>/dev/null || true)"
    version="$(printf '%s\n' "$version_line" | awk '{print $2}' | tr -d '\r')"
    if [[ -z "$version" ]]; then
        return 1
    fi
    printf '%s\n' "${version#v}"
}

download_and_install_macos_app_release_asset() {
    local cli_version="$1"
    local asset_name=""
    local asset_url=""
    local tmp_dir=""
    local archive_path=""
    local extracted_app=""

    case "$(uname -m)" in
        arm64|aarch64)
            asset_name="Longhouse-macos-arm64.zip"
            ;;
        *)
            error "Direct Longhouse.app fallback is only defined for Apple Silicon Macs"
            return 1
            ;;
    esac

    asset_url="https://github.com/cipher982/longhouse/releases/download/v${cli_version}/${asset_name}"
    tmp_dir="$(mktemp -d)"
    archive_path="$tmp_dir/$asset_name"
    extracted_app="$tmp_dir/Longhouse.app"

    info "Falling back to release asset install for Longhouse.app (${asset_name})"
    info "Release tag: v${cli_version}"

    if ! curl -fL "$asset_url" -o "$archive_path"; then
        rm -rf "$tmp_dir"
        error "Could not download Longhouse.app release asset from $asset_url"
        return 1
    fi

    # Integrity check: if the release publishes a <asset>.sha256 sidecar, verify
    # it. If absent, warn and continue (download is over HTTPS from the canonical
    # GitHub releases host). Set LONGHOUSE_REQUIRE_CHECKSUM=1 to fail closed when
    # no checksum is published.
    local checksum_url="${asset_url}.sha256"
    local checksum_path="$tmp_dir/${asset_name}.sha256"
    if curl -fsL "$checksum_url" -o "$checksum_path" 2>/dev/null; then
        local expected actual
        expected="$(awk '{print $1}' "$checksum_path" | head -1)"
        actual="$(shasum -a 256 "$archive_path" | awk '{print $1}')"
        if [[ -z "$expected" || "$expected" != "$actual" ]]; then
            rm -rf "$tmp_dir"
            error "Checksum mismatch for $asset_name (expected $expected, got $actual)"
            return 1
        fi
        info "Verified Longhouse.app checksum (sha256)"
    elif [[ "${LONGHOUSE_REQUIRE_CHECKSUM:-0}" == "1" ]]; then
        rm -rf "$tmp_dir"
        error "No published checksum for $asset_name and LONGHOUSE_REQUIRE_CHECKSUM=1"
        return 1
    else
        warn "No published checksum for $asset_name; proceeding (HTTPS from github.com)"
    fi

    if ! ditto -x -k "$archive_path" "$tmp_dir"; then
        rm -rf "$tmp_dir"
        error "Could not extract Longhouse.app release asset"
        return 1
    fi

    if [[ ! -d "$extracted_app" ]]; then
        rm -rf "$tmp_dir"
        error "Release asset did not contain Longhouse.app"
        return 1
    fi

    rm -rf "/Applications/Longhouse.app"
    if ! ditto "$extracted_app" "/Applications/Longhouse.app"; then
        rm -rf "$tmp_dir"
        error "Could not copy Longhouse.app into /Applications"
        return 1
    fi

    rm -rf "$tmp_dir"
    success "Longhouse.app installed in /Applications"
    return 0
}

# Install Longhouse.app into /Applications on macOS
install_macos_app() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        return 0
    fi

    step "Installing Longhouse.app"

    if ! has_command longhouse; then
        error "longhouse command not found; cannot install Longhouse.app"
        exit 1
    fi

    if longhouse runtime-artifact-install desktop-app --help >/dev/null 2>&1; then
        local install_output=""
        if install_output=$(longhouse runtime-artifact-install desktop-app 2>&1); then
            success "Longhouse.app installed in /Applications"
            if [[ -n "$install_output" ]]; then
                printf '%s\n' "$install_output"
            fi
            return 0
        fi

        printf '%s\n' "$install_output" >&2
        error "Could not install Longhouse.app into /Applications"
        exit 1
    fi

    local cli_version=""
    if ! cli_version="$(resolve_longhouse_cli_version)"; then
        error "Could not determine installed Longhouse CLI version for app fallback"
        exit 1
    fi

    if download_and_install_macos_app_release_asset "$cli_version"; then
        return 0
    fi

    local install_output=""
    install_output="$(longhouse --version 2>&1 || true)"
    printf '%s\n' "$install_output" >&2
    error "Could not install Longhouse.app into /Applications via CLI or release fallback"
    exit 1
}

# Update shell profile for PATH
update_shell_profile() {
    step "Updating shell PATH"

    local shell_name
    shell_name=$(basename "$SHELL")

    local profile_updated=false
    local path_line='export PATH="$HOME/.local/bin:$PATH"'

    case "$shell_name" in
        bash)
            local profile="$HOME/.bashrc"
            [[ "$(uname -s)" == "Darwin" ]] && profile="$HOME/.bash_profile"

            if [[ ! -f "$profile" ]]; then
                mkdir -p "$(dirname "$profile")"
                touch "$profile"
            fi

            if ! grep -q ".local/bin" "$profile"; then
                echo "" >> "$profile"
                echo "# Added by Longhouse installer" >> "$profile"
                echo "$path_line" >> "$profile"
                profile_updated=true
                info "Updated $profile"
            fi
            ;;

        zsh)
            local profile="$HOME/.zshrc"
            if [[ ! -f "$profile" ]]; then
                mkdir -p "$(dirname "$profile")"
                touch "$profile"
            fi

            if ! grep -q ".local/bin" "$profile"; then
                echo "" >> "$profile"
                echo "# Added by Longhouse installer" >> "$profile"
                echo "$path_line" >> "$profile"
                profile_updated=true
                info "Updated $profile"
            fi
            ;;

        fish)
            local fish_config="$HOME/.config/fish/config.fish"
            if [[ -f "$fish_config" ]] && ! grep -q ".local/bin" "$fish_config"; then
                echo "" >> "$fish_config"
                echo "# Added by Longhouse installer" >> "$fish_config"
                echo 'fish_add_path $HOME/.local/bin' >> "$fish_config"
                profile_updated=true
                info "Updated $fish_config"
            elif [[ ! -f "$fish_config" ]]; then
                mkdir -p "$HOME/.config/fish"
                echo "# Added by Longhouse installer" > "$fish_config"
                echo 'fish_add_path $HOME/.local/bin' >> "$fish_config"
                profile_updated=true
                info "Created $fish_config"
            fi
            ;;

        *)
            warn "Unknown shell: $shell_name"
            warn "  Add this to your shell profile:"
            warn "  $path_line"
            ;;
    esac

    if $profile_updated; then
        PATH_UPDATED=1
        success "Shell profile updated"
    else
        info "PATH already configured"
    fi
}

# Verify installation
verify_installation() {
    step "Verifying installation"

    local all_ok=true

    # Check uv
    if has_command uv; then
        success "uv: $(uv --version)"
    else
        error "uv not found"
        all_ok=false
    fi

    # Check Python
    if uv python find 3.12 &>/dev/null 2>&1; then
        success "Python: $(uv python find 3.12)"
    else
        error "Python 3.12 not found"
        all_ok=false
    fi

    # Check longhouse
    if has_command longhouse; then
        success "longhouse: installed"
    else
        error "longhouse not found in PATH"
        all_ok=false
    fi

    # Check claude (optional)
    if has_command claude; then
        success "claude: $(which claude)"
    else
        info "claude: not installed (optional)"
    fi

    if [[ "$(uname -s)" == "Darwin" ]]; then
        if [[ -d "/Applications/Longhouse.app" ]]; then
            success "Longhouse.app: /Applications/Longhouse.app"
        else
            error "Longhouse.app not installed in /Applications"
            all_ok=false
        fi
    fi

    if ! $all_ok; then
        error "Installation verification failed"
        exit 1
    fi

    # Fresh-shell PATH verification
    verify_fresh_shell_path

    success "All checks passed!"
}

# Verify that longhouse and claude are on PATH in a fresh shell
verify_fresh_shell_path() {
    local shell_name shell_bin profile fresh_path marker_line
    shell_name=$(basename "$SHELL")

    # Resolve absolute shell path from $SHELL (handles Homebrew shells);
    # fall back to /bin/zsh or /bin/bash when $SHELL is unknown.
    case "$shell_name" in
        bash|zsh|fish)
            if [[ -x "$SHELL" ]]; then
                shell_bin="$SHELL"
            elif [[ -x "/bin/$shell_name" ]]; then
                shell_bin="/bin/$shell_name"
            else
                return 0
            fi
            ;;
        *)
            # Unknown shell — try common fallbacks
            if [[ -x /bin/zsh ]]; then
                shell_bin="/bin/zsh"; shell_name="zsh"
            elif [[ -x /bin/bash ]]; then
                shell_bin="/bin/bash"; shell_name="bash"
            else
                return 0
            fi
            ;;
    esac

    case "$shell_name" in
        bash)
            profile="$HOME/.bashrc"
            [[ "$(uname -s)" == "Darwin" ]] && profile="$HOME/.bash_profile"
            ;;
        zsh)
            profile="$HOME/.zshrc"
            ;;
        fish)
            profile="$HOME/.config/fish/config.fish"
            ;;
    esac

    [[ ! -f "$profile" ]] && return 0

    local lh_marker="__LH_PATH__"

    # Source the profile in a subshell with minimal PATH to simulate a fresh terminal.
    # - Profile path passed as positional arg ($1 / $argv[1]) to avoid injection.
    # - Interactive mode (-i) so rc files don't early-return for non-interactive shells.
    # - Marker line isolates PATH from noisy profile output.
    # - Source failure gates the marker print.
    if [[ "$shell_name" == "fish" ]]; then
        marker_line=$(HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
            "$shell_bin" -c \
            'source $argv[1] 2>/dev/null; and echo "'"$lh_marker"'=$PATH"' \
            -- "$profile" 2>/dev/null) || return 0
    else
        marker_line=$(HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
            "$shell_bin" -i -c \
            'source "$1" 2>/dev/null && echo "'"$lh_marker"'=$PATH" || exit 1' \
            _ "$profile" 2>/dev/null) || return 0
    fi

    # Extract the PATH value from the marker line (ignore any other output)
    fresh_path=""
    while IFS= read -r line; do
        if [[ "$line" == "${lh_marker}="* ]]; then
            fresh_path="${line#"${lh_marker}="}"
            break
        fi
    done <<< "$marker_line"

    [[ -z "$fresh_path" ]] && return 0

    # Check longhouse — exact segment matching with delimiters
    local longhouse_path
    longhouse_path=$(which longhouse 2>/dev/null) || true
    if [[ -n "$longhouse_path" ]]; then
        local longhouse_dir
        longhouse_dir=$(dirname "$longhouse_path")
        if ! grep -qF ":${longhouse_dir}:" <<< ":${fresh_path}:"; then
            warn "'longhouse' won't be on PATH in a new terminal"
            warn "  Fix: echo 'export PATH=\"$longhouse_dir:\$PATH\"' >> $profile"
            warn "  Then: source $profile"
        fi
    fi

    # Check claude (optional) — exact segment matching with delimiters
    local claude_path
    claude_path=$(which claude 2>/dev/null) || true
    if [[ -n "$claude_path" ]]; then
        local claude_dir
        claude_dir=$(dirname "$claude_path")
        if ! grep -qF ":${claude_dir}:" <<< ":${fresh_path}:"; then
            warn "'claude' won't be on PATH in a new terminal"
            warn "  Fix: echo 'export PATH=\"$claude_dir:\$PATH\"' >> $profile"
            warn "  Then: source $profile"
        fi
    fi
}

# Print final instructions
print_success() {
    local is_macos=0
    if [[ "$(uname -s)" == "Darwin" ]]; then
        is_macos=1
    fi

    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "============================================"
    echo "  Longhouse installed successfully!"
    echo "============================================"
    echo -e "${NC}"
    echo ""
    if [[ "$is_macos" == "1" ]]; then
        echo "Next:"
        echo "  1. Open /Applications/Longhouse.app"
        echo "  2. Finish setup in the app"
        echo "  3. Find one prior session in the timeline"
        echo ""
        echo "macOS:"
        echo "  The terminal installer only acquires Longhouse."
        echo "  Longhouse.app owns first-run setup, repair, and local status."
    else
        echo "Next:"
        echo "  1. Run longhouse onboard"
        echo "  2. Open http://localhost:8080"
        echo "  3. Find one prior session in the timeline"
    fi
    if has_command claude; then
      echo ""
      echo "Later, when you want control after launch:"
      echo "  longhouse claude"
    elif has_command codex; then
        echo ""
        echo "Later, when you want control after launch:"
        echo "  longhouse codex"
    fi
    echo ""
    echo "Repair tools (only if you need them later):"
    echo "  longhouse doctor            Diagnose local setup issues"
    echo "  longhouse connect --install Repair the machine agent, desktop app, and automatic imports"
    echo ""
    echo "Advanced:"
    echo "  longhouse ship              Import existing sessions once"
    echo ""
    echo "Machine surface:"
    echo "  longhouse wall --json       Read active and recent sessions"
    echo "  longhouse status            Show configuration"
    echo "  longhouse version --check   Check whether a CLI update is available"
    echo "  longhouse upgrade           Upgrade the installed CLI"
    echo ""
    echo "For help: longhouse --help"
    echo "Docs: https://longhouse.ai/docs"
    echo ""

    # Remind about new shell if PATH was updated
    if [[ "$PATH_UPDATED" == "1" ]]; then
        warn "Open a new terminal or run: source ~/.$(basename "$SHELL")rc"
    fi
}

# Main installation flow
main() {
    echo -e "${BOLD}"
    echo "  _                    _                          "
    echo " | |    ___  _ __   __| | _   _  ___  _   _  ___  "
    echo " | |   / _ \| '_ \ / _\` || | | |/ __|| | | |/ __| "
    echo " | |__| (_) | | | | (_| || |_| |\__ \| |_| |\__ \ "
    echo " |_____\___/|_| |_|\__, | \__,_||___/ \__,_||___/ "
    echo "                   |___/                          "
    echo -e "${NC}"
    echo "One-liner installer v1.0"
    echo ""
    if [[ "${LONGHOUSE_TELEMETRY:-}" =~ ^(0|false|no|off)$ ]] || [[ "${DO_NOT_TRACK:-}" == "1" ]]; then
        info "Anonymous install telemetry disabled"
    else
        info "Anonymous install telemetry is enabled (set LONGHOUSE_TELEMETRY=0 or DO_NOT_TRACK=1 to disable)"
        info "Telemetry excludes prompts, paths, secrets, and session contents"
    fi
    echo ""

    local platform
    platform=$(detect_platform)
    info "Platform: $platform"

    # Install dependencies
    install_uv
    install_python
    install_longhouse
    install_macos_app

    # Update PATH in shell profile
    update_shell_profile

    # Verify everything works
    verify_installation

    # Done!
    print_success
}

# Run main
main "$@"
