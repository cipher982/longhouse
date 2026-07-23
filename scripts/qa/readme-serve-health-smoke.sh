#!/usr/bin/env bash
set -euo pipefail

TMP_DIR="$(mktemp -d -t longhouse-readme-serve-XXXXXX)"
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

DATABASE_URL="sqlite:///$TMP_DIR/test.db" LLM_DISABLED=1 longhouse-server serve --port "$PORT" &
SERVER_PID="$!"

for _ in $(seq 1 20); do
  if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null; then
    break
  fi
  sleep 1
done

curl -sf "http://127.0.0.1:$PORT/api/health" |
  python3 -c 'import json,sys; p=json.load(sys.stdin); assert p.get("status") == "healthy", p'
