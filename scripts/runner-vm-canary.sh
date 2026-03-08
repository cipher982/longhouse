#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SCRIPT="$SCRIPT_DIR/runner-vm-canary-host.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/hosted-instance.sh"

log() {
  printf '[runner-vm-canary] %s\n' "$*"
}

usage() {
  cat <<'USAGE'
Usage:
  scripts/runner-vm-canary.sh

Environment:
  INSTANCE_SUBDOMAIN        Hosted instance subdomain (default: david010)
  RUNNER_VM_HOST            SSH host that provisions disposable VMs (default: cube)
  RUNNER_VM_PREFIX          Runner/VM name prefix (default: lh-vm-canary)
  RUNNER_VM_RELEASE         Ubuntu release alias (default: noble)
  RUNNER_VM_MEMORY_MB       Guest memory in MB (default: 2048)
  RUNNER_VM_CPU             vCPU count (default: 2)
  RUNNER_VM_DISK_GB         Disk size in GB (default: 10)
  RUNNER_VM_WAIT_TIMEOUT    Guest SSH wait timeout in seconds (default: 300)
  RUNNER_ONLINE_TIMEOUT     Wait timeout for hosted runner online state (default: 120)
  RUNNER_VM_GUEST_ARCH      Override amd64|arm64 guest arch
  RUNNER_VM_TMPDIR          Disk-backed temp dir on VM host
  KEEP_VM                   Keep VM after script exits (default: 0)
  KEEP_RUNNER               Skip runner revoke on cleanup (default: 0)
  RUNNER_COMMAND            Oikos command to execute (default: hostname -s)
USAGE
}

parse_json_field() {
  local json_payload="$1"
  local field="$2"
  python3 - "$json_payload" "$field" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
value = payload.get(sys.argv[2], "")
if isinstance(value, (dict, list)):
    print(json.dumps(value, separators=(",", ":")))
else:
    print(value)
PY
}

json_escape() {
  python3 - "$1" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1]))
PY
}

parse_runner_match() {
  local json_payload="$1"
  local runner_name="$2"
  python3 - "$json_payload" "$runner_name" <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
name = sys.argv[2]
for runner in payload.get("runners", []):
    if runner.get("name") != name:
        continue
    caps = runner.get("capabilities") or []
    print(f"{runner.get('id','')}\t{runner.get('status','')}\t{','.join(caps)}")
    sys.exit(0)
sys.exit(1)
PY
}

parse_oikos_complete() {
  local response_file="$1"
  python3 - "$response_file" <<'PY'
import json
import sys
path = sys.argv[1]
current = None
buf = []
with open(path, encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.rstrip("\n")
        if line.startswith("event: "):
            current = line[7:]
            buf = []
        elif line.startswith("data: "):
            buf.append(line[6:])
        elif not line and current:
            data = "".join(buf).strip()
            if data:
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    current = None
                    buf = []
                    continue
                if current == "oikos_complete":
                    inner = payload.get("payload") or {}
                    print(f"{inner.get('status','')}\t{inner.get('result','')}")
                    sys.exit(0)
            current = None
            buf = []
sys.exit(1)
PY
}

run_host_action() {
  local action="$1"
  local current_host
  current_host="$(hostname -s)"

  if [[ "$RUNNER_VM_HOST" == "localhost" || "$RUNNER_VM_HOST" == "local" || "$RUNNER_VM_HOST" == "$current_host" ]]; then
    local -a local_env=(
      "VM_NAME=$VM_NAME"
      "VM_RELEASE=$RUNNER_VM_RELEASE"
      "VM_MEMORY_MB=$RUNNER_VM_MEMORY_MB"
      "VM_CPU=$RUNNER_VM_CPU"
      "VM_DISK_GB=$RUNNER_VM_DISK_GB"
      "VM_WAIT_TIMEOUT=$RUNNER_VM_WAIT_TIMEOUT"
      "RUNNER_INSTALL_MODE=server"
      "KEEP_VM=$KEEP_VM"
      "LONGHOUSE_URL=$LONGHOUSE_URL"
    )
    if [[ -n "${RUNNER_VM_GUEST_ARCH:-}" ]]; then
      local_env+=("RUNNER_VM_GUEST_ARCH=$RUNNER_VM_GUEST_ARCH")
    fi
    if [[ -n "${RUNNER_VM_TMPDIR:-}" ]]; then
      local_env+=("RUNNER_VM_TMPDIR=$RUNNER_VM_TMPDIR")
    fi
    if [[ "$action" == "provision" ]]; then
      local_env+=("ENROLL_TOKEN=$ENROLL_TOKEN")
    fi
    env "${local_env[@]}" bash "$HOST_SCRIPT" "$action"
  else
    local -a env_parts=(
      "VM_NAME=$(printf '%q' "$VM_NAME")"
      "VM_RELEASE=$(printf '%q' "$RUNNER_VM_RELEASE")"
      "VM_MEMORY_MB=$(printf '%q' "$RUNNER_VM_MEMORY_MB")"
      "VM_CPU=$(printf '%q' "$RUNNER_VM_CPU")"
      "VM_DISK_GB=$(printf '%q' "$RUNNER_VM_DISK_GB")"
      "VM_WAIT_TIMEOUT=$(printf '%q' "$RUNNER_VM_WAIT_TIMEOUT")"
      "RUNNER_INSTALL_MODE=server"
      "KEEP_VM=$(printf '%q' "$KEEP_VM")"
      "LONGHOUSE_URL=$(printf '%q' "$LONGHOUSE_URL")"
    )
    if [[ -n "${RUNNER_VM_GUEST_ARCH:-}" ]]; then
      env_parts+=("RUNNER_VM_GUEST_ARCH=$(printf '%q' "$RUNNER_VM_GUEST_ARCH")")
    fi
    if [[ -n "${RUNNER_VM_TMPDIR:-}" ]]; then
      env_parts+=("RUNNER_VM_TMPDIR=$(printf '%q' "$RUNNER_VM_TMPDIR")")
    fi
    if [[ "$action" == "provision" ]]; then
      env_parts+=("ENROLL_TOKEN=$(printf '%q' "$ENROLL_TOKEN")")
    fi
    local remote_cmd="${env_parts[*]} bash -s -- $(printf '%q' "$action")"
    ssh -o BatchMode=yes "$RUNNER_VM_HOST" "$remote_cmd" < "$HOST_SCRIPT"
  fi
}

wait_for_runner_online() {
  local deadline=$((SECONDS + RUNNER_ONLINE_TIMEOUT))
  while (( SECONDS < deadline )); do
    local runners_json
    runners_json="$(curl -fsSL "${LONGHOUSE_URL%/}/api/runners/" -b "$COOKIE_JAR")"
    local parsed=""
    if parsed="$(parse_runner_match "$runners_json" "$VM_NAME" 2>/dev/null)"; then
      IFS=$'\t' read -r RUNNER_ID RUNNER_STATUS RUNNER_CAPABILITIES <<< "$parsed"
      if [[ "$RUNNER_STATUS" == "online" ]]; then
        log "Runner $VM_NAME is online (${RUNNER_CAPABILITIES:-unknown capabilities})"
        return 0
      fi
    fi
    sleep 2
  done
  printf 'Runner %s did not reach online state within %ss\n' "$VM_NAME" "$RUNNER_ONLINE_TIMEOUT" >&2
  return 1
}

verify_oikos_exec() {
  local prompt="Use runner_exec on ${VM_NAME} to run ${RUNNER_COMMAND} and reply with only the raw output."
  local message_id
  message_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"
  local payload
  payload=$(printf '{"message":%s,"message_id":%s}' "$(json_escape "$prompt")" "$(json_escape "$message_id")")
  local response_file
  response_file="$(mktemp)"
  curl -sS -N -X POST "${LONGHOUSE_URL%/}/api/oikos/chat" \
    -b "$COOKIE_JAR" \
    -H 'Content-Type: application/json' \
    -d "$payload" > "$response_file"
  local parsed
  parsed="$(parse_oikos_complete "$response_file")" || {
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  }
  rm -f "$response_file"
  IFS=$'\t' read -r OIKOS_STATUS OIKOS_RESULT <<< "$parsed"
  if [[ "$OIKOS_STATUS" != "success" ]]; then
    printf 'Oikos runner_exec failed for %s: status=%s result=%s\n' "$VM_NAME" "$OIKOS_STATUS" "$OIKOS_RESULT" >&2
    return 1
  fi
  if [[ "$OIKOS_RESULT" != "$VM_NAME" ]]; then
    printf 'Unexpected runner_exec output for %s: expected %s, got %s\n' "$VM_NAME" "$VM_NAME" "$OIKOS_RESULT" >&2
    return 1
  fi
  log "Oikos verified runner_exec hostname on $VM_NAME"
}

revoke_runner_if_present() {
  if [[ -z "${RUNNER_ID:-}" || "$KEEP_RUNNER" == "1" ]]; then
    return 0
  fi
  local http_code
  http_code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${LONGHOUSE_URL%/}/api/runners/${RUNNER_ID}/revoke" -b "$COOKIE_JAR")"
  if [[ "$http_code" == "200" ]]; then
    log "Revoked disposable runner $VM_NAME (id=$RUNNER_ID)"
  else
    log "Warning: failed to revoke runner $VM_NAME (id=$RUNNER_ID, http=$http_code)"
  fi
}

cleanup() {
  local status=$?
  revoke_runner_if_present || true
  if [[ "$KEEP_VM" != "1" ]]; then
    run_host_action destroy >/dev/null 2>&1 || log "Warning: failed to destroy VM $VM_NAME on $RUNNER_VM_HOST"
  else
    log "Keeping VM $VM_NAME on $RUNNER_VM_HOST for debugging"
  fi
  rm -f "$COOKIE_JAR"
  exit "$status"
}
trap cleanup EXIT

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

INSTANCE_SUBDOMAIN="${INSTANCE_SUBDOMAIN:-david010}"
RUNNER_VM_HOST="${RUNNER_VM_HOST:-cube}"
RUNNER_VM_PREFIX="${RUNNER_VM_PREFIX:-lh-vm-canary}"
RUNNER_VM_RELEASE="${RUNNER_VM_RELEASE:-noble}"
RUNNER_VM_MEMORY_MB="${RUNNER_VM_MEMORY_MB:-2048}"
RUNNER_VM_CPU="${RUNNER_VM_CPU:-2}"
RUNNER_VM_DISK_GB="${RUNNER_VM_DISK_GB:-10}"
RUNNER_VM_WAIT_TIMEOUT="${RUNNER_VM_WAIT_TIMEOUT:-300}"
RUNNER_ONLINE_TIMEOUT="${RUNNER_ONLINE_TIMEOUT:-120}"
RUNNER_VM_GUEST_ARCH="${RUNNER_VM_GUEST_ARCH:-}"
RUNNER_VM_TMPDIR="${RUNNER_VM_TMPDIR:-}"
KEEP_VM="${KEEP_VM:-0}"
KEEP_RUNNER="${KEEP_RUNNER:-0}"
RUNNER_COMMAND="${RUNNER_COMMAND:-hostname -s}"
VM_NAME="${RUNNER_VM_NAME:-${RUNNER_VM_PREFIX}-$(date +%Y%m%d%H%M%S)}"
COOKIE_JAR="$(mktemp)"
RUNNER_ID=""
RUNNER_STATUS=""
RUNNER_CAPABILITIES=""
OIKOS_STATUS=""
OIKOS_RESULT=""

log "Preparing hosted instance auth for ${INSTANCE_SUBDOMAIN}"
lh_hosted_prepare_target "$INSTANCE_SUBDOMAIN" || exit 1
LONGHOUSE_URL="$LH_TARGET_API_URL"
lh_hosted_authenticate_cookie_jar "$INSTANCE_SUBDOMAIN" "$COOKIE_JAR" || exit 1

log "Requesting enroll token from ${LONGHOUSE_URL}"
ENROLL_RESPONSE="$(curl -fsSL -X POST "${LONGHOUSE_URL%/}/api/runners/enroll-token" -b "$COOKIE_JAR" -H 'Content-Type: application/json' -d '{}')"
ENROLL_TOKEN="$(parse_json_field "$ENROLL_RESPONSE" enroll_token)"
if [[ -z "$ENROLL_TOKEN" ]]; then
  printf 'Failed to parse enroll token response\n' >&2
  exit 1
fi

log "Provisioning disposable VM $VM_NAME on $RUNNER_VM_HOST"
PROVISION_OUTPUT="$(run_host_action provision)"
printf '%s\n' "$PROVISION_OUTPUT"

log "Waiting for runner registration to reach online"
wait_for_runner_online

log "Running Oikos runner_exec verification"
verify_oikos_exec

log "Disposable VM runner canary passed"
printf 'RESULT=success\n'
printf 'INSTANCE_SUBDOMAIN=%s\n' "$INSTANCE_SUBDOMAIN"
printf 'RUNNER_VM_HOST=%s\n' "$RUNNER_VM_HOST"
printf 'VM_NAME=%s\n' "$VM_NAME"
printf 'RUNNER_ID=%s\n' "$RUNNER_ID"
printf 'RUNNER_STATUS=%s\n' "$RUNNER_STATUS"
printf 'RUNNER_CAPABILITIES=%s\n' "$RUNNER_CAPABILITIES"
printf 'OIKOS_RESULT=%s\n' "$OIKOS_RESULT"
