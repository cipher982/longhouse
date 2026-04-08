#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: macos-set-github-trust-secrets.sh --p12 <path> --p12-password <password> --identity <identity> --apple-id <apple-id> --team-id <team-id> --app-password <password> [options]

Push the macOS signing and notarization secrets required by the Longhouse
runtime release workflow into GitHub Actions secrets.

Options:
  --repo <owner/repo>         GitHub repository (default: cipher982/longhouse).
  --p12 <path>                Developer ID .p12 bundle path.
  --p12-password <password>   Password used for the .p12 bundle.
  --identity <identity>       Developer ID signing identity string.
  --apple-id <apple-id>       Apple ID for notarytool.
  --team-id <team-id>         Apple Developer Team ID.
  --app-password <password>   App-specific password used by notarytool.
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

REPO="cipher982/longhouse"
P12_PATH=""
P12_PASSWORD=""
SIGNING_IDENTITY=""
APPLE_ID=""
TEAM_ID=""
APP_PASSWORD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      require_value "$1" "${2:-}"
      REPO="$2"
      shift 2
      ;;
    --p12)
      require_value "$1" "${2:-}"
      P12_PATH="$2"
      shift 2
      ;;
    --p12-password)
      require_value "$1" "${2:-}"
      P12_PASSWORD="$2"
      shift 2
      ;;
    --identity)
      require_value "$1" "${2:-}"
      SIGNING_IDENTITY="$2"
      shift 2
      ;;
    --apple-id)
      require_value "$1" "${2:-}"
      APPLE_ID="$2"
      shift 2
      ;;
    --team-id)
      require_value "$1" "${2:-}"
      TEAM_ID="$2"
      shift 2
      ;;
    --app-password)
      require_value "$1" "${2:-}"
      APP_PASSWORD="$2"
      shift 2
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

if [[ -z "$P12_PATH" || -z "$P12_PASSWORD" || -z "$SIGNING_IDENTITY" || -z "$APPLE_ID" || -z "$TEAM_ID" || -z "$APP_PASSWORD" ]]; then
  usage >&2
  exit 1
fi

if [[ ! -f "$P12_PATH" ]]; then
  echo "P12 bundle not found: $P12_PATH" >&2
  exit 1
fi

P12_BASE64="$(base64 < "$P12_PATH" | tr -d '\n')"

gh secret set MACOS_SIGNING_CERT_P12_BASE64 --repo "$REPO" --body "$P12_BASE64"
gh secret set MACOS_SIGNING_CERT_PASSWORD --repo "$REPO" --body "$P12_PASSWORD"
gh secret set MACOS_SIGNING_IDENTITY --repo "$REPO" --body "$SIGNING_IDENTITY"
gh secret set MACOS_NOTARY_APPLE_ID --repo "$REPO" --body "$APPLE_ID"
gh secret set MACOS_NOTARY_TEAM_ID --repo "$REPO" --body "$TEAM_ID"
gh secret set MACOS_NOTARY_APP_PASSWORD --repo "$REPO" --body "$APP_PASSWORD"

cat <<EOF
Updated GitHub secrets for ${REPO}:
  MACOS_SIGNING_CERT_P12_BASE64
  MACOS_SIGNING_CERT_PASSWORD
  MACOS_SIGNING_IDENTITY
  MACOS_NOTARY_APPLE_ID
  MACOS_NOTARY_TEAM_ID
  MACOS_NOTARY_APP_PASSWORD
EOF
