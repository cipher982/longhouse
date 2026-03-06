#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT_DIR/.env"
  set +a
fi

HOSTED_INSTANCE_HELPER="$ROOT_DIR/scripts/lib/hosted-instance.sh"
if [[ ! -f "$HOSTED_INSTANCE_HELPER" ]]; then
  echo "Hosted instance helper missing: $HOSTED_INSTANCE_HELPER" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$HOSTED_INSTANCE_HELPER"

CONTROL_PLANE_URL="${CONTROL_PLANE_URL:-${CP_URL:-https://control.longhouse.ai}}"
CP_URL="$CONTROL_PLANE_URL"
INSTANCE_SUBDOMAIN="${INSTANCE_SUBDOMAIN:-${1:-}}"

if [[ -z "$INSTANCE_SUBDOMAIN" ]]; then
  echo "Set INSTANCE_SUBDOMAIN or pass it as the first argument." >&2
  exit 1
fi

lh_hosted_resolve_instance "$INSTANCE_SUBDOMAIN"

output_lines=$(cat <<ENVVARS
INSTANCE_ID=$LH_INSTANCE_ID
INSTANCE_SUBDOMAIN=$LH_INSTANCE_SUBDOMAIN
INSTANCE_URL=$LH_INSTANCE_URL
FRONTEND_URL=$LH_INSTANCE_URL
API_URL=$LH_INSTANCE_URL
CONTROL_PLANE_URL=$CONTROL_PLANE_URL
CP_URL=$CONTROL_PLANE_URL
ENVVARS
)

if [[ -n "${GITHUB_ENV:-}" ]]; then
  printf '%s\n' "$output_lines" >> "$GITHUB_ENV"
else
  printf '%s\n' "$output_lines"
fi

echo "Resolved hosted instance $LH_INSTANCE_SUBDOMAIN -> $LH_INSTANCE_URL" >&2
