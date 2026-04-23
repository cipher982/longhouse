#!/usr/bin/env bash
set -euo pipefail

# Download a stock upstream `codex` release asset and drop the plain
# executable at --output. We used to carry a fork that patched codex's
# app-server WS backpressure handling; that's now handled by the
# engine-side TCP relay (engine/src/codex_ws_relay.rs), so managed
# Codex just ships whatever stock openai/codex publishes.
#
# The resulting binary at --output is the raw upstream `codex`, identical
# to what you'd get from `brew install codex` minus Homebrew's shim
# layer. The launcher installed by longhouse connect --install is still
# the one in server/zerg/services/runtime_artifacts.py; that's the
# piece that hardcodes `-c check_for_update_on_startup=false`.

usage() {
    cat <<'EOF'
Usage: download-managed-codex.sh --output <path> [--target <triple>]

Options:
  --output <path>      Destination path for the codex binary (required)
  --target <triple>    Target triple to download. Default inferred from
                       host (aarch64-apple-darwin, x86_64-unknown-linux-gnu,
                       aarch64-unknown-linux-gnu). Override with one of the
                       upstream-published triples.

Environment overrides:
  MANAGED_CODEX_UPSTREAM_VERSION  Upstream release tag without the `rust-v`
                                  prefix (e.g. 0.124.0). Defaults to the
                                  pinned version below. The tag actually
                                  fetched is always `rust-v${VERSION}`.
  MANAGED_CODEX_UPSTREAM_REPO     Upstream repo. Defaults to openai/codex.
EOF
}

DEFAULT_UPSTREAM_VERSION="0.124.0"
UPSTREAM_VERSION="${MANAGED_CODEX_UPSTREAM_VERSION:-${DEFAULT_UPSTREAM_VERSION}}"
UPSTREAM_REPO="${MANAGED_CODEX_UPSTREAM_REPO:-openai/codex}"
UPSTREAM_TAG="rust-v${UPSTREAM_VERSION}"

OUTPUT_PATH=""
TARGET_TRIPLE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)
            OUTPUT_PATH="${2:-}"
            shift 2
            ;;
        --target)
            TARGET_TRIPLE="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -z "${OUTPUT_PATH}" ]]; then
    echo "--output is required" >&2
    usage >&2
    exit 1
fi

if [[ -z "${TARGET_TRIPLE}" ]]; then
    host_os="$(uname -s)"
    host_arch="$(uname -m)"
    case "${host_os}:${host_arch}" in
        Darwin:arm64|Darwin:aarch64)
            TARGET_TRIPLE="aarch64-apple-darwin"
            ;;
        Darwin:x86_64)
            TARGET_TRIPLE="x86_64-apple-darwin"
            ;;
        Linux:aarch64|Linux:arm64)
            TARGET_TRIPLE="aarch64-unknown-linux-gnu"
            ;;
        Linux:x86_64)
            TARGET_TRIPLE="x86_64-unknown-linux-gnu"
            ;;
        *)
            echo "Cannot infer codex target triple for ${host_os}:${host_arch}; pass --target explicitly." >&2
            exit 1
            ;;
    esac
fi

ASSET_NAME="codex-${TARGET_TRIPLE}.tar.gz"
ASSET_URL="https://github.com/${UPSTREAM_REPO}/releases/download/${UPSTREAM_TAG}/${ASSET_NAME}"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/longhouse-codex-download.XXXXXX")"
trap 'rm -rf "${WORKDIR}"' EXIT

echo "Downloading stock codex ${UPSTREAM_VERSION} (${TARGET_TRIPLE})..."
curl --fail --silent --show-error --location \
    --retry 5 --retry-connrefused \
    --output "${WORKDIR}/asset.tar.gz" \
    "${ASSET_URL}"

tar -xzf "${WORKDIR}/asset.tar.gz" -C "${WORKDIR}"

# Upstream's tarballs put the binary at either `codex-<triple>` or `codex`.
BIN_PATH=""
for candidate in "${WORKDIR}/codex-${TARGET_TRIPLE}" "${WORKDIR}/codex"; do
    if [[ -x "${candidate}" ]]; then
        BIN_PATH="${candidate}"
        break
    fi
done

if [[ -z "${BIN_PATH}" ]]; then
    echo "Could not locate codex binary inside ${ASSET_NAME}; contents:" >&2
    ls -la "${WORKDIR}" >&2
    exit 1
fi

mkdir -p "$(dirname "${OUTPUT_PATH}")"
cp "${BIN_PATH}" "${OUTPUT_PATH}"
chmod +x "${OUTPUT_PATH}"

echo "Staged stock codex at ${OUTPUT_PATH}"
echo "Source: ${ASSET_URL}"
echo "Version: ${UPSTREAM_VERSION}"
