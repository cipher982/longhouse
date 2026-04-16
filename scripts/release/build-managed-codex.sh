#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: build-managed-codex.sh --output <path> [--source-dir <path>] [--keep-workdir]

Build the Longhouse-managed Codex runtime by cloning a pinned upstream
OpenAI Codex commit, applying the checked-in Longhouse patch, and producing
the raw `codex` binary at the requested output path.

Options:
  --output <path>      Destination path for the built codex binary (required)
  --source-dir <path>  Existing upstream codex checkout to reuse instead of cloning
  --keep-workdir       Keep the temporary checkout/build directory for debugging

Environment overrides:
  MANAGED_CODEX_UPSTREAM_REPO  Upstream repo URL
  MANAGED_CODEX_UPSTREAM_REF   Upstream commit/tag/branch to build
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PATCH_FILE="${ROOT_DIR}/scripts/release/managed-codex.patch"
UPSTREAM_REPO="${MANAGED_CODEX_UPSTREAM_REPO:-https://github.com/openai/codex.git}"
UPSTREAM_REF="${MANAGED_CODEX_UPSTREAM_REF:-71174574adb09a90ebd83e2acfe284a39aaca2cf}"

OUTPUT_PATH=""
SOURCE_DIR="${MANAGED_CODEX_SOURCE_DIR:-}"
KEEP_WORKDIR=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      OUTPUT_PATH="${2:-}"
      shift 2
      ;;
    --source-dir)
      SOURCE_DIR="${2:-}"
      shift 2
      ;;
    --keep-workdir)
      KEEP_WORKDIR=1
      shift
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

if [[ ! -f "${PATCH_FILE}" ]]; then
  echo "Managed Codex patch file not found: ${PATCH_FILE}" >&2
  exit 1
fi

cargo_build_release() {
  export CARGO_NET_GIT_FETCH_WITH_CLI="${CARGO_NET_GIT_FETCH_WITH_CLI:-true}"

  if command -v cargo >/dev/null 2>&1; then
    if cargo --version >/dev/null 2>&1; then
      cargo build --release "$@"
      return 0
    fi
    if cargo +stable --version >/dev/null 2>&1; then
      cargo +stable build --release "$@"
      return 0
    fi
  fi

  if command -v rustup >/dev/null 2>&1 && rustup run stable cargo --version >/dev/null 2>&1; then
    rustup run stable cargo build --release "$@"
    return 0
  fi

  echo "Rust toolchain unavailable for cargo build --release" >&2
  exit 1
}

WORKDIR=""
cleanup() {
  if [[ ${KEEP_WORKDIR} -eq 0 && -n "${WORKDIR}" && -d "${WORKDIR}" ]]; then
    rm -rf "${WORKDIR}"
  fi
}
trap cleanup EXIT

if [[ -n "${SOURCE_DIR}" ]]; then
  SOURCE_DIR="$(cd "${SOURCE_DIR}" && pwd)"
  if [[ ! -d "${SOURCE_DIR}/codex-rs" ]]; then
    echo "Source dir does not look like an OpenAI Codex checkout: ${SOURCE_DIR}" >&2
    exit 1
  fi
  WORKTREE="${SOURCE_DIR}"
else
  WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/longhouse-codex-build.XXXXXX")"
  WORKTREE="${WORKDIR}/src"
  git init "${WORKTREE}" >/dev/null
  git -C "${WORKTREE}" remote add origin "${UPSTREAM_REPO}"
  git -C "${WORKTREE}" fetch --depth 1 origin "${UPSTREAM_REF}"
  git -C "${WORKTREE}" checkout --detach FETCH_HEAD >/dev/null
fi

git -C "${WORKTREE}" apply --check "${PATCH_FILE}"
git -C "${WORKTREE}" apply "${PATCH_FILE}"

cargo_build_release \
  --manifest-path "${WORKTREE}/codex-rs/Cargo.toml" \
  -p codex-cli \
  --bin codex

BUILT_BINARY="${WORKTREE}/codex-rs/target/release/codex"
if [[ ! -x "${BUILT_BINARY}" ]]; then
  echo "Expected built codex binary at ${BUILT_BINARY}" >&2
  exit 1
fi

mkdir -p "$(dirname "${OUTPUT_PATH}")"
cp "${BUILT_BINARY}" "${OUTPUT_PATH}"
chmod +x "${OUTPUT_PATH}"

echo "Built managed Codex binary at ${OUTPUT_PATH}"
echo "Upstream: ${UPSTREAM_REPO}@${UPSTREAM_REF}"
if [[ ${KEEP_WORKDIR} -eq 1 ]]; then
  echo "Workdir: ${WORKTREE}"
fi
