#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_DIR="$(mktemp -d)"
FAKE_BIN_DIR="$TMP_DIR/bin"
FAKE_ARGS_FILE="$TMP_DIR/infisical-args.txt"
mkdir -p "$FAKE_BIN_DIR" "$TMP_DIR/home"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cat > "$FAKE_BIN_DIR/infisical" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" > "$FAKE_ARGS_FILE"
mode="${FAKE_INFISICAL_MODE:-ok}"
case "$mode" in
  ok)
    cat <<'JSON'
[{"secretKey":"CONTROL_PLANE_ADMIN_TOKEN","secretValue":"token-from-infisical"}]
JSON
    ;;
  missing)
    echo '[]'
    ;;
  empty)
    cat <<'JSON'
[{"secretKey":"CONTROL_PLANE_ADMIN_TOKEN","secretValue":""}]
JSON
    ;;
  *)
    echo "unexpected mode: $mode" >&2
    exit 1
    ;;
esac
FAKE
chmod +x "$FAKE_BIN_DIR/infisical"

export PATH="$FAKE_BIN_DIR:$PATH"
export FAKE_ARGS_FILE
export HOME="$TMP_DIR/home"
unset LH_INFISICAL_GET_BIN
unset CONTROL_PLANE_ADMIN_TOKEN
unset ADMIN_TOKEN
unset CONTROL_PLANE_URL
unset CP_URL
export FAKE_INFISICAL_MODE=ok

# shellcheck disable=SC1091
source "$ROOT_DIR/lib/hosted-instance.sh"

lh_hosted_prepare_control_plane_auth

if [[ "$CONTROL_PLANE_ADMIN_TOKEN" != "token-from-infisical" ]]; then
  echo "Expected CONTROL_PLANE_ADMIN_TOKEN to be loaded from Infisical"
  exit 1
fi

if [[ "$CONTROL_PLANE_URL" != "https://control.longhouse.ai" ]]; then
  echo "Expected CONTROL_PLANE_URL default to be applied"
  exit 1
fi

if ! grep -Fx -- '--projectId' "$FAKE_ARGS_FILE" >/dev/null; then
  echo "Expected Infisical project argument"
  exit 1
fi
if ! grep -Fx -- 'd303262d-e281-4100-aba7-28940cf2741e' "$FAKE_ARGS_FILE" >/dev/null; then
  echo "Expected ops-infra project for control-plane token"
  exit 1
fi
if ! grep -Fx -- '--env' "$FAKE_ARGS_FILE" >/dev/null; then
  echo "Expected Infisical env argument"
  exit 1
fi
if ! grep -Fx -- 'prod' "$FAKE_ARGS_FILE" >/dev/null; then
  echo "Expected prod env for control-plane token"
  exit 1
fi

unset CONTROL_PLANE_ADMIN_TOKEN
export FAKE_INFISICAL_MODE=missing
if lh_infisical_get_secret CONTROL_PLANE_ADMIN_TOKEN >/dev/null 2>&1; then
  echo "Expected missing secret lookup to fail"
  exit 1
fi

export FAKE_INFISICAL_MODE=empty
if lh_infisical_get_secret CONTROL_PLANE_ADMIN_TOKEN >/dev/null 2>&1; then
  echo "Expected empty secret lookup to fail"
  exit 1
fi

export FAKE_INFISICAL_MODE=ok
if lh_infisical_export_secret_if_missing BAD-NAME CONTROL_PLANE_ADMIN_TOKEN >/dev/null 2>&1; then
  echo "Expected invalid shell variable name to fail"
  exit 1
fi

json_payload="$(_lh_hosted_json_object email 'quote"@example.com' subdomain 'demo\slash')"
if [[ "$json_payload" != '{"email":"quote\"@example.com","subdomain":"demo\\slash"}' ]]; then
  echo "Expected hosted JSON helper to escape values safely"
  exit 1
fi

echo "hosted-instance Infisical tests passed"
