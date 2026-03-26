#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOSTED_INSTANCE_HELPER="${HOSTED_INSTANCE_HELPER:-$ROOT_DIR/scripts/lib/hosted-instance.sh}"

if [[ ! -f "$HOSTED_INSTANCE_HELPER" ]]; then
  echo "Hosted instance helper missing: $HOSTED_INSTANCE_HELPER" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$HOSTED_INSTANCE_HELPER"

INSTANCE_SUBDOMAIN="david010"
TARGET_CWD="$ROOT_DIR"
SAMPLES="3"
API_TIMEOUT_SECS="90"
VERIFY_TIMEOUT_SECS="45"
DELAY_SECS="0"
PROJECT_BASE="managed-local-codex-launch-profile"
DISPLAY_NAME_BASE="Managed Local Codex Launch Profile"
LOOP_MODE="manual"
RUNNER_LOG_PATH="${HOME}/.local/share/longhouse-runner/state/runner.log"
MACHINE_NAME="${MACHINE_NAME:-}"
PRINT_JSON="0"
KEEP_SESSION="0"

if [[ -z "$MACHINE_NAME" && -f "$HOME/.claude/longhouse-machine-name" ]]; then
  MACHINE_NAME="$(tr -d '\r\n' < "$HOME/.claude/longhouse-machine-name")"
fi

usage() {
  cat <<'EOF'
Usage:
  scripts/hosted-managed-local-codex-launch-profile.sh [options]

Launch real hosted managed-local Codex sessions and profile the launch path.
Reports:
  - launch API latency
  - time until local tmux session appears
  - time until Codex reaches an idle prompt
  - runner-side job timings (preflight, hooks, tmux launch, has-session)

Requirements:
  - CONTROL_PLANE_ADMIN_TOKEN or ADMIN_TOKEN
  - this machine already connected as a runner to the target tenant
  - uv and tmux available locally

Options:
  --subdomain <name>          Hosted instance subdomain (default: david010)
  --cwd <path>                Working directory for the managed-local launch
  --samples <n>               Number of samples to collect (default: 3)
  --api-timeout-secs <n>      HTTP timeout for the launch POST (default: 90)
  --verify-timeout-secs <n>   Wait for tmux/Codex readiness after launch (default: 45)
  --delay-secs <n>            Delay between samples (default: 0)
  --project-base <name>       Base project label
  --display-name-base <name>  Base display name
  --loop-mode <mode>          manual|assist|autopilot (default: manual)
  --runner-log-path <path>    Local runner log path
  --machine-name <name>       Explicit machine label override
  --json                      Print JSON instead of prose
  --keep-session              Leave tmux sessions running after profiling
  -h, --help                  Show help
EOF
}

while (($# > 0)); do
  case "$1" in
    --subdomain)
      [[ -n "${2:-}" ]] || { echo "--subdomain requires a value" >&2; exit 1; }
      INSTANCE_SUBDOMAIN="$2"
      shift 2
      ;;
    --cwd)
      [[ -n "${2:-}" ]] || { echo "--cwd requires a value" >&2; exit 1; }
      TARGET_CWD="$2"
      shift 2
      ;;
    --samples)
      [[ -n "${2:-}" ]] || { echo "--samples requires a value" >&2; exit 1; }
      SAMPLES="$2"
      shift 2
      ;;
    --api-timeout-secs)
      [[ -n "${2:-}" ]] || { echo "--api-timeout-secs requires a value" >&2; exit 1; }
      API_TIMEOUT_SECS="$2"
      shift 2
      ;;
    --verify-timeout-secs)
      [[ -n "${2:-}" ]] || { echo "--verify-timeout-secs requires a value" >&2; exit 1; }
      VERIFY_TIMEOUT_SECS="$2"
      shift 2
      ;;
    --delay-secs)
      [[ -n "${2:-}" ]] || { echo "--delay-secs requires a value" >&2; exit 1; }
      DELAY_SECS="$2"
      shift 2
      ;;
    --project-base)
      [[ -n "${2:-}" ]] || { echo "--project-base requires a value" >&2; exit 1; }
      PROJECT_BASE="$2"
      shift 2
      ;;
    --display-name-base)
      [[ -n "${2:-}" ]] || { echo "--display-name-base requires a value" >&2; exit 1; }
      DISPLAY_NAME_BASE="$2"
      shift 2
      ;;
    --loop-mode)
      [[ -n "${2:-}" ]] || { echo "--loop-mode requires a value" >&2; exit 1; }
      LOOP_MODE="$2"
      shift 2
      ;;
    --runner-log-path)
      [[ -n "${2:-}" ]] || { echo "--runner-log-path requires a value" >&2; exit 1; }
      RUNNER_LOG_PATH="$2"
      shift 2
      ;;
    --machine-name)
      [[ -n "${2:-}" ]] || { echo "--machine-name requires a value" >&2; exit 1; }
      MACHINE_NAME="$2"
      shift 2
      ;;
    --json)
      PRINT_JSON="1"
      shift
      ;;
    --keep-session)
      KEEP_SESSION="1"
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

TARGET_CWD="$(cd "$TARGET_CWD" && pwd)"

lh_hosted_prepare_target "$INSTANCE_SUBDOMAIN" "" "" "david010"
API_URL="$LH_TARGET_API_URL"
INSTANCE_URL="$LH_TARGET_FRONTEND_URL"

LH_PROFILE_ACCESS_TOKEN=""
LH_PROFILE_DEVICE_TOKEN_ID=""
LH_PROFILE_DEVICE_TOKEN=""

cleanup() {
  if [[ -n "$LH_PROFILE_DEVICE_TOKEN_ID" && -n "$LH_PROFILE_ACCESS_TOKEN" ]]; then
    if ! lh_hosted_revoke_device_token "$LH_PROFILE_ACCESS_TOKEN" "$LH_PROFILE_DEVICE_TOKEN_ID" "$API_URL" >/dev/null 2>&1; then
      echo "Warning: failed to revoke launch-profile device token $LH_PROFILE_DEVICE_TOKEN_ID" >&2
    fi
  fi
}
trap cleanup EXIT

echo "Target tenant: $INSTANCE_SUBDOMAIN ($INSTANCE_URL)" >&2
LH_PROFILE_ACCESS_TOKEN="$(lh_hosted_exchange_login_token "$(lh_hosted_issue_login_token "$LH_INSTANCE_ID")" "$API_URL")"
IFS=$'\t' read -r LH_PROFILE_DEVICE_TOKEN_ID LH_PROFILE_DEVICE_TOKEN <<< \
  "$(lh_hosted_create_device_token "$LH_PROFILE_ACCESS_TOKEN" "$API_URL" "hosted-codex-launch-profile-${INSTANCE_SUBDOMAIN}-${RANDOM}")"

cmd=(
  uv run --project server python -u scripts/managed_local_codex_launch_profile.py
  --api-url "$API_URL"
  --device-token "$LH_PROFILE_DEVICE_TOKEN"
  --cwd "$TARGET_CWD"
  --samples "$SAMPLES"
  --api-timeout-secs "$API_TIMEOUT_SECS"
  --verify-timeout-secs "$VERIFY_TIMEOUT_SECS"
  --delay-secs "$DELAY_SECS"
  --project-base "$PROJECT_BASE"
  --display-name-base "$DISPLAY_NAME_BASE"
  --loop-mode "$LOOP_MODE"
  --runner-log-path "$RUNNER_LOG_PATH"
)

if [[ -n "$MACHINE_NAME" ]]; then
  cmd+=(--machine-name "$MACHINE_NAME")
fi
if [[ "$PRINT_JSON" == "1" ]]; then
  cmd+=(--json)
fi
if [[ "$KEEP_SESSION" == "1" ]]; then
  cmd+=(--keep-session)
fi

cd "$ROOT_DIR"
"${cmd[@]}"
