#!/usr/bin/env bash
#
# Longhouse One-Liner Installer
#
# Usage:
#   curl -fsSL https://get.longhouse.ai/install.sh | bash
#
# Environment:
#   LONGHOUSE_NO_WIZARD=1  Skip onboarding wizard
#   LONGHOUSE_API_URL      Custom API URL (default: http://localhost:8080)
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

    # Package source - defaults to PyPI, can be overridden for dev installs
    local pkg_source="${LONGHOUSE_PKG_SOURCE:-longhouse}"

    # Install the longhouse package as a tool
    if uv tool list 2>/dev/null | grep -q "^longhouse"; then
        info "Upgrading existing longhouse installation..."
        uv tool upgrade longhouse || {
            # If upgrade fails (e.g., installed from different source), reinstall
            info "Reinstalling from source..."
            uv tool uninstall longhouse 2>/dev/null || true
            uv tool install "$pkg_source"
        }
    else
        info "Installing longhouse from source..."
        uv tool install "$pkg_source"
    fi

    # Ensure uv tools bin is in PATH
    export PATH="$HOME/.local/bin:$PATH"

    if has_command longhouse; then
        success "longhouse installed: $(longhouse --version 2>/dev/null || echo 'installed')"
    else
        error "longhouse installation failed"
        error "Try adding ~/.local/bin to your PATH:"
        error "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        exit 1
    fi
}

# Install Claude Code shim
install_claude_shim() {
    step "Checking Claude Code integration"

    # Check if claude command exists
    if ! has_command claude; then
        warn "Claude Code not found"
        warn "  Install from: https://docs.anthropic.com/claude-code"
        warn "  Skipping shim installation (you can still use Longhouse without it)"
        return 0
    fi

    # Check if shim already installed
    local claude_path
    claude_path=$(which claude)

    if [[ "$claude_path" == *"longhouse"* ]]; then
        info "Claude shim already installed"
        return 0
    fi

    # Look for existing shim installer
    local shim_script="$HOME/.longhouse/install-claude-shim.sh"

    if [[ -f "$shim_script" ]]; then
        info "Running Claude shim installer..."
        bash "$shim_script"
        success "Claude shim installed"
    else
        # Create a simple shim that wraps claude
        info "Creating Claude shim..."

        local claude_hooks_dir="$HOME/.claude/hooks"
        mkdir -p "$claude_hooks_dir"

        # Create PostToolUse hook for session shipping
        cat > "$claude_hooks_dir/post_tool_use.sh" << 'EOF'
#!/bin/bash
# Ship session events to Longhouse after each tool use
# This is a fire-and-forget background call
if command -v longhouse &>/dev/null; then
    longhouse ship --quiet &>/dev/null &
fi
exit 0
EOF
        chmod +x "$claude_hooks_dir/post_tool_use.sh"

        success "Claude hooks installed for session shipping"
    fi
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

            if [[ -f "$profile" ]] && ! grep -q ".local/bin" "$profile"; then
                echo "" >> "$profile"
                echo "# Added by Longhouse installer" >> "$profile"
                echo "$path_line" >> "$profile"
                profile_updated=true
                info "Updated $profile"
            fi
            ;;

        zsh)
            local profile="$HOME/.zshrc"
            if [[ -f "$profile" ]] && ! grep -q ".local/bin" "$profile"; then
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

# Run onboarding wizard
run_onboard() {
    if [[ "${LONGHOUSE_NO_WIZARD:-}" == "1" ]]; then
        info "Skipping onboarding wizard (LONGHOUSE_NO_WIZARD=1)"
        return 0
    fi

    step "Running onboarding wizard"
    echo ""

    if ! has_command longhouse; then
        warn "longhouse command not found, skipping wizard"
        return 0
    fi

    # When piped from curl, stdin is consumed by the pipe.
    # Try to reconnect to the real terminal via /dev/tty.
    if [[ -t 0 ]]; then
        # Interactive terminal available directly
        longhouse onboard || {
            warn "Onboarding wizard exited with error"
            warn "You can run it again with: longhouse onboard"
        }
    elif : < /dev/tty 2>/dev/null; then
        # Stdin is pipe but TTY is accessible - redirect from /dev/tty
        info "Reconnecting to terminal for interactive setup..."
        longhouse onboard < /dev/tty || {
            warn "Onboarding wizard exited with error"
            warn "You can run it again with: longhouse onboard"
        }
    else
        # No TTY available (Docker, CI, headless) - use non-interactive mode
        info "No TTY available, using QuickStart defaults"
        longhouse onboard --quick || {
            warn "Onboarding wizard exited with error"
            warn "You can run it again with: longhouse onboard"
        }
    fi
}

# Print final instructions
print_success() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "============================================"
    echo "  Longhouse installed successfully!"
    echo "============================================"
    echo -e "${NC}"
    echo ""
    echo "Quick start:"
    echo "  longhouse serve      Start the server"
    echo "  longhouse connect    Sync Claude sessions"
    echo "  longhouse status     Show configuration"
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

    local platform
    platform=$(detect_platform)
    info "Platform: $platform"

    # Install dependencies
    install_uv
    install_python
    install_longhouse

    # Optional: Claude integration
    install_claude_shim

    # Update PATH in shell profile
    update_shell_profile

    # Verify everything works
    verify_installation

    # Run onboarding wizard
    run_onboard

    # Done!
    print_success
}

# Run main
main "$@"
