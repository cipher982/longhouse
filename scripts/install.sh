#!/usr/bin/env bash
#
# Longhouse One-Liner Installer
#
# Usage:
#   curl -fsSL https://get.longhouse.ai/install.sh | bash
#
# Environment:
#   http_proxy/https_proxy Proxy settings (honored automatically)
#   LONGHOUSE_INSTALL_VERSION Pin the native release version (for release gates/debugging)
#   LONGHOUSE_NATIVE_BIN_DIR  Explicit directory containing paired longhouse
#                             and longhouse-engine binaries (local/dev only)
#
set -euo pipefail

# Track if PATH was updated (for final message)
PATH_UPDATED=0
INSTALL_COMPLETED=0
CURRENT_INSTALL_STAGE="startup"
INSTALL_TELEMETRY_SOURCE=""
INSTALL_TELEMETRY_PACKAGE_REF=""
INSTALL_RELEASE_VERSION=""

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

telemetry_enabled() {
    local raw="${LONGHOUSE_TELEMETRY:-}"
    raw="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
    case "$raw" in
        0|false|no|off) return 1 ;;
    esac
    if [[ "${DO_NOT_TRACK:-}" == "1" ]]; then
        return 1
    fi
    case "$(printf '%s' "${CI:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes) return 1 ;;
    esac
    return 0
}

telemetry_safe_label() {
    printf '%s' "${1:-}" | tr -cd 'A-Za-z0-9_.:-' | cut -c1-80
}

telemetry_os_name() {
    case "$(uname -s 2>/dev/null || true)" in
        Darwin) printf 'darwin' ;;
        Linux) printf 'linux' ;;
        MINGW*|MSYS*|CYGWIN*) printf 'windows' ;;
        *) printf 'unknown' ;;
    esac
}

telemetry_arch() {
    case "$(uname -m 2>/dev/null || true)" in
        x86_64|amd64) printf 'x86_64' ;;
        arm64|aarch64) printf 'arm64' ;;
        *) printf 'unknown' ;;
    esac
}

telemetry_libc() {
    if [[ "$(uname -s 2>/dev/null || true)" != "Linux" ]]; then
        printf 'n/a'
        return 0
    fi
    local ldd_line
    ldd_line="$(ldd --version 2>&1 | head -1 | tr '[:upper:]' '[:lower:]' || true)"
    if [[ "$ldd_line" == *musl* ]]; then
        printf 'musl'
    elif [[ "$ldd_line" == *glibc* || "$ldd_line" == *"gnu libc"* ]]; then
        printf 'glibc'
    else
        printf 'unknown'
    fi
}

telemetry_package_ref_kind() {
    local package_ref="${1:-}"
    if [[ -z "$package_ref" ]]; then
        printf 'unversioned'
    elif [[ "$package_ref" == v* ]]; then
        printf 'release_version'
    elif [[ "$package_ref" == http://* || "$package_ref" == https://* || "$package_ref" == git+* ]]; then
        printf 'url'
    elif [[ "$package_ref" == /* || "$package_ref" == ./* || "$package_ref" == ../* ]]; then
        printf 'local_path'
    else
        printf 'custom'
    fi
}

telemetry_prior_installer() {
    if has_command longhouse; then
        printf 'path'
    else
        printf 'none'
    fi
}

telemetry_install_id() {
    local longhouse_home="${LONGHOUSE_HOME:-$HOME/.longhouse}"
    local install_id_path="$longhouse_home/install-id"
    local install_id=""

    if [[ -f "$install_id_path" ]]; then
        install_id="$(head -1 "$install_id_path" 2>/dev/null | tr -cd 'A-Za-z0-9_.:-' | cut -c1-128 || true)"
        if [[ -n "$install_id" ]]; then
            printf '%s' "$install_id"
            return 0
        fi
    fi

    if has_command uuidgen; then
        install_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"
    elif [[ -r /proc/sys/kernel/random/uuid ]]; then
        install_id="$(cat /proc/sys/kernel/random/uuid)"
    elif has_command openssl; then
        install_id="$(openssl rand -hex 16 2>/dev/null || true)"
    fi
    if [[ -z "$install_id" ]]; then
        install_id="$(date +%s)-$$-${RANDOM:-0}"
    fi
    install_id="$(telemetry_safe_label "$install_id")"

    mkdir -p "$longhouse_home" 2>/dev/null || true
    chmod 700 "$longhouse_home" 2>/dev/null || true
    printf '%s\n' "$install_id" > "$install_id_path" 2>/dev/null || true
    printf '%s' "$install_id"
}

emit_installer_telemetry() {
    local event_name="$1"
    local stage="${2:-unknown}"
    local install_source="${3:-}"
    local package_ref="${4:-}"
    local exit_code="${5:-0}"

    telemetry_enabled || return 0
    has_command curl || return 0

    local endpoint="${LONGHOUSE_TELEMETRY_ENDPOINT:-https://control.longhouse.ai/api/acquisition/events}"
    [[ -z "$endpoint" ]] && return 0

    local install_id os_name arch shell_name libc package_ref_kind prior_installer props exit_prop
    install_id="$(telemetry_install_id)"
    os_name="$(telemetry_os_name)"
    arch="$(telemetry_arch)"
    shell_name="$(telemetry_safe_label "$(basename "${SHELL:-unknown}")")"
    libc="$(telemetry_libc)"
    package_ref_kind="$(telemetry_package_ref_kind "$package_ref")"
    prior_installer="$(telemetry_prior_installer)"
    stage="$(telemetry_safe_label "$stage")"
    install_source="$(telemetry_safe_label "$install_source")"

    exit_prop=""
    if [[ "$exit_code" =~ ^[0-9]+$ && "$exit_code" != "0" ]]; then
        exit_prop=",\"exit_code\":$exit_code"
    fi

    props="\"stage\":\"$stage\",\"shell\":\"$shell_name\",\"libc\":\"$libc\",\"package_ref_kind\":\"$package_ref_kind\",\"prior_installer\":\"$prior_installer\"$exit_prop"
    local payload
    payload="{\"event_name\":\"$event_name\",\"install_id\":\"$install_id\",\"source\":\"installer\",\"version\":null,\"os_name\":\"$os_name\",\"arch\":\"$arch\",\"command\":\"install_sh\",\"install_method\":\"native_binary\",\"install_source\":\"$install_source\",\"channel\":\"stable\",\"topology\":null,\"ci\":false,\"props\":{$props}}"

    curl -fsS --max-time 1.5 \
        -H "Content-Type: application/json" \
        -H "User-Agent: longhouse-installer/1.0" \
        --data "$payload" \
        "$endpoint" >/dev/null 2>&1 || true
}

record_install_failure() {
    local status="${1:-1}"
    if [[ "$INSTALL_COMPLETED" != "1" ]]; then
        emit_installer_telemetry \
            "install_failure" \
            "$CURRENT_INSTALL_STAGE" \
            "$INSTALL_TELEMETRY_SOURCE" \
            "$INSTALL_TELEMETRY_PACKAGE_REF" \
            "$status"
    fi
}

trap 'record_install_failure "$?"' ERR

# Detect platform
detect_platform() {
    local os arch

    case "$(uname -s)" in
        Darwin) os="darwin" ;;
        Linux) os="linux" ;;
        MINGW*|MSYS*|CYGWIN*) os="windows" ;;
        *) error "Unsupported OS: $(uname -s)"; record_install_failure 1; exit 1 ;;
    esac

    case "$(uname -m)" in
        x86_64|amd64) arch="x86_64" ;;
        arm64|aarch64) arch="arm64" ;;
        *) error "Unsupported architecture: $(uname -m)"; record_install_failure 1; exit 1 ;;
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

sha256_file() {
    if has_command shasum; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi
}

native_target() {
    case "$(detect_platform)" in
        darwin-arm64) printf 'darwin-arm64' ;;
        linux-x86_64) printf 'linux-x64' ;;
        linux-arm64) printf 'linux-arm64' ;;
        *) error "No native Longhouse release is available for $(detect_platform)"; return 1 ;;
    esac
}

resolve_release_version() {
    if [[ -n "${LONGHOUSE_INSTALL_VERSION:-}" ]]; then printf '%s\n' "${LONGHOUSE_INSTALL_VERSION#v}"; return 0; fi
    local latest_url
    latest_url="$(curl -fsSIL -o /dev/null -w '%{url_effective}' https://github.com/cipher982/longhouse/releases/latest)"
    case "$latest_url" in */releases/tag/v*) printf '%s\n' "${latest_url##*/releases/tag/v}" ;; *) error "Could not resolve the latest Longhouse native release"; return 1 ;; esac
}

verify_release_checksum() {
    local checksums="$1" asset="$2" downloaded="$3" expected actual
    expected="$(awk -v asset="$asset" '$2 == asset || $2 == "*" asset { print $1; exit }' "$checksums")"
    actual="$(sha256_file "$downloaded")"
    [[ -n "$expected" && "$expected" == "$actual" ]]
}

install_native_pair() {
    step "Installing Longhouse"
    CURRENT_INSTALL_STAGE="native_binary_install"
    local target source_dir="${LONGHOUSE_NATIVE_BIN_DIR:-}" version base_url tmp_dir checksums facade_asset engine_asset
    local native_bin_dir="$HOME/.local/bin" native_root="$HOME/.local/share/longhouse" release_id release_dir
    local current_link="$native_root/current" next_current existing_facade legacy_facade
    target="$(native_target)"; facade_asset="longhouse-${target}"; engine_asset="longhouse-engine-${target}"; tmp_dir="$(mktemp -d)"
    if [[ -n "$source_dir" ]]; then
        INSTALL_TELEMETRY_SOURCE="local"; INSTALL_TELEMETRY_PACKAGE_REF="$source_dir"; INSTALL_RELEASE_VERSION=""
        [[ -x "$source_dir/longhouse" && -x "$source_dir/longhouse-engine" ]] || { error "LONGHOUSE_NATIVE_BIN_DIR must contain executable longhouse and longhouse-engine binaries"; rm -rf "$tmp_dir"; return 1; }
        cp "$source_dir/longhouse" "$tmp_dir/longhouse"; cp "$source_dir/longhouse-engine" "$tmp_dir/longhouse-engine"
    else
        INSTALL_TELEMETRY_SOURCE="release"; version="$(resolve_release_version)"; INSTALL_RELEASE_VERSION="$version"; INSTALL_TELEMETRY_PACKAGE_REF="v$version"; base_url="https://github.com/cipher982/longhouse/releases/download/v${version}"; checksums="$tmp_dir/local-runtime-checksums.txt"
        info "Downloading Longhouse v$version for $target"
        curl -fsSL "$base_url/local-runtime-checksums.txt" -o "$checksums" && curl -fsSL "$base_url/$facade_asset" -o "$tmp_dir/longhouse" && curl -fsSL "$base_url/$engine_asset" -o "$tmp_dir/longhouse-engine" || { rm -rf "$tmp_dir"; return 1; }
        verify_release_checksum "$checksums" "$facade_asset" "$tmp_dir/longhouse" || { error "Checksum mismatch for $facade_asset"; rm -rf "$tmp_dir"; return 1; }
        verify_release_checksum "$checksums" "$engine_asset" "$tmp_dir/longhouse-engine" || { error "Checksum mismatch for $engine_asset"; rm -rf "$tmp_dir"; return 1; }
    fi
    chmod 755 "$tmp_dir/longhouse" "$tmp_dir/longhouse-engine"
    "$tmp_dir/longhouse" verify-pair >/dev/null || { rm -rf "$tmp_dir"; return 1; }
    release_id="${version:-local}-${tmp_dir##*/}"
    release_dir="$native_root/releases/$release_id"
    mkdir -p "$native_bin_dir" "$release_dir"
    mv "$tmp_dir/longhouse" "$release_dir/longhouse"
    mv "$tmp_dir/longhouse-engine" "$release_dir/longhouse-engine"
    "$release_dir/longhouse" verify-pair >/dev/null || { rm -rf "$tmp_dir"; return 1; }
    existing_facade="$native_bin_dir/longhouse"; legacy_facade="$native_bin_dir/longhouse-python"
    next_current="$native_root/.current-${tmp_dir##*/}"
    ln -s "releases/$release_id" "$next_current"
    ln -s "../share/longhouse/current/longhouse" "$native_bin_dir/.longhouse-native"
    ln -s "../share/longhouse/current/longhouse-engine" "$native_bin_dir/.longhouse-engine-native"
    mv "$next_current" "$current_link"
    if [[ -e "$existing_facade" || -L "$existing_facade" ]] && ! "$existing_facade" verify-pair >/dev/null 2>&1; then
        [[ ! -e "$legacy_facade" ]] || { error "Refusing to overwrite existing $legacy_facade; move it before installing the native CLI"; rm -rf "$tmp_dir"; return 1; }
        mv "$existing_facade" "$legacy_facade"; info "Quarantined the previous Python CLI as $legacy_facade"
    fi
    if ! mv "$native_bin_dir/.longhouse-native" "$existing_facade"; then
        [[ -e "$legacy_facade" ]] && mv "$legacy_facade" "$existing_facade"
        return 1
    fi
    mv "$native_bin_dir/.longhouse-engine-native" "$native_bin_dir/longhouse-engine"
    rm -rf "$tmp_dir"; "$native_bin_dir/longhouse" verify-pair >/dev/null
    export PATH="$native_bin_dir:$PATH"
    emit_installer_telemetry "native_binary_install" "$CURRENT_INSTALL_STAGE" "$INSTALL_TELEMETRY_SOURCE" "$INSTALL_TELEMETRY_PACKAGE_REF" "0"
    success "Longhouse installed: $($native_bin_dir/longhouse build-identity)"
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

    CURRENT_INSTALL_STAGE="desktop_app_install"
    step "Installing Longhouse.app"

    if ! has_command longhouse; then
        error "longhouse command not found; cannot install Longhouse.app"
        record_install_failure 1
        exit 1
    fi

    if [[ -z "$INSTALL_RELEASE_VERSION" ]]; then
        info "Skipping Longhouse.app for an explicit local native binary pair"
        return 0
    fi

    if ! download_and_install_macos_app_release_asset "$INSTALL_RELEASE_VERSION"; then
        error "Could not install Longhouse.app from the matching native release"
        record_install_failure 1
        exit 1
    fi
}

# Update shell profile for PATH
update_shell_profile() {
    CURRENT_INSTALL_STAGE="shell_path_update"
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
    CURRENT_INSTALL_STAGE="verification"
    step "Verifying installation"

    local all_ok=true

    # Check longhouse
    if has_command longhouse; then
        if longhouse verify-pair >/dev/null 2>&1; then
            success "longhouse: native pair verified"
        else
            error "longhouse native pair is invalid"
            all_ok=false
        fi
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

    if [[ "$(uname -s)" == "Darwin" && -n "$INSTALL_RELEASE_VERSION" ]]; then
        if [[ -d "/Applications/Longhouse.app" ]]; then
            success "Longhouse.app: /Applications/Longhouse.app"
        else
            error "Longhouse.app not installed in /Applications"
            all_ok=false
        fi
    fi

    if ! $all_ok; then
        error "Installation verification failed"
        record_install_failure 1
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
    echo "Native device commands:"
    echo "  longhouse auth --url <url>  Store a device token from LONGHOUSE_DEVICE_TOKEN"
    echo "  longhouse local-health --fast --json"
    echo "  longhouse machine repair --dry-run"
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
    emit_installer_telemetry "install_attempt" "installer_start" "" "" "0"

    install_native_pair
    install_macos_app

    # Update PATH in shell profile
    update_shell_profile

    # Verify everything works
    verify_installation

    # Done!
    INSTALL_COMPLETED=1
    print_success
}

# Run main
main "$@"
