#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: macos-create-developer-id-csr.sh --email <email> [options]

Generate the private key and CSR needed to request an Apple Developer ID
Application certificate.

Options:
  --email <email>             Email address embedded in the CSR subject.
  --common-name <name>        Subject common name (default: Longhouse Release).
  --country <code>            Two-letter country code (default: US).
  --out-dir <path>            Output directory (default: ~/.longhouse/release/macos-trust).
  --force                     Overwrite existing key/CSR files.
  -h, --help                  Show this help text.
EOF
}

require_value() {
  local flag="$1"
  local value="${2:-}"
  if [[ -z "$value" ]]; then
    echo "Missing value for $flag" >&2
    usage >&2
    exit 1
  fi
}

EMAIL=""
COMMON_NAME="Longhouse Release"
COUNTRY="US"
OUT_DIR="${HOME}/.longhouse/release/macos-trust"
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email)
      require_value "$1" "${2:-}"
      EMAIL="$2"
      shift 2
      ;;
    --common-name)
      require_value "$1" "${2:-}"
      COMMON_NAME="$2"
      shift 2
      ;;
    --country)
      require_value "$1" "${2:-}"
      COUNTRY="$2"
      shift 2
      ;;
    --out-dir)
      require_value "$1" "${2:-}"
      OUT_DIR="$2"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$EMAIL" ]]; then
  usage >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

KEY_PATH="${OUT_DIR}/developer-id.key.pem"
CSR_PATH="${OUT_DIR}/developer-id.csr.pem"

if [[ "$FORCE" != "1" ]]; then
  if [[ -e "$KEY_PATH" || -e "$CSR_PATH" ]]; then
    echo "Refusing to overwrite existing CSR material in ${OUT_DIR}. Use --force if you mean it." >&2
    exit 1
  fi
fi

openssl req \
  -new \
  -newkey rsa:2048 \
  -sha256 \
  -nodes \
  -keyout "$KEY_PATH" \
  -out "$CSR_PATH" \
  -subj "/emailAddress=${EMAIL}/CN=${COMMON_NAME}/C=${COUNTRY}"

chmod 600 "$KEY_PATH"

cat <<EOF
Created Apple signing request material:
  key: ${KEY_PATH}
  csr: ${CSR_PATH}

Next:
  1. Upload the CSR in Apple Developer certificates.
  2. Download the issued Developer ID Application certificate (.cer).
  3. Run scripts/release/macos-build-developer-id-p12.sh with the cert + key.
EOF
