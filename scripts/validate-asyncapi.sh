#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# validate-asyncapi.sh – fails if asyncapi/chat.yml is not spec-valid.
# ---------------------------------------------------------------------------
# The helper mirrors the logic in regen-ws-code.sh but only validates; no
# code-generation.  We rely on the AsyncAPI CLI if available, otherwise fall
# back to bunx/npx.

set -euo pipefail

SPEC_FILE="$(git rev-parse --show-toplevel)/asyncapi/chat.yml"

if [[ ! -f "$SPEC_FILE" ]]; then
  echo "❌ AsyncAPI spec not found at $SPEC_FILE" >&2
  exit 1
fi

# Re-use helper to run with timeout if available.
_run() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 15 "$@"
  else
    "$@"
  fi
}

if TMP_OUT=$(mktemp 2>/dev/null); then
  :
else
  TMP_OUT=".asyncapi_validate.log"
  touch "$TMP_OUT"
fi

if command -v asyncapi >/dev/null 2>&1; then
  _run asyncapi validate "$SPEC_FILE" >"$TMP_OUT" 2>&1
  EXIT_CODE=$?
else
  if [ -x "node_modules/.bin/asyncapi" ]; then
    _run node_modules/.bin/asyncapi validate "$SPEC_FILE" >"$TMP_OUT" 2>&1
    EXIT_CODE=$?
  elif command -v bunx >/dev/null 2>&1; then
    _run bunx @asyncapi/cli validate "$SPEC_FILE" >"$TMP_OUT" 2>&1 || EXIT_CODE=$?
  else
    _run npx --yes @asyncapi/cli validate "$SPEC_FILE" >"$TMP_OUT" 2>&1 || EXIT_CODE=$?
  fi
fi

if [[ ${EXIT_CODE:-0} -ne 0 ]]; then
  cat "$TMP_OUT" >&2
  echo "❌ AsyncAPI spec validation failed" >&2
  exit $EXIT_CODE
fi

rm -f "$TMP_OUT"
echo "✅ AsyncAPI spec is valid"
