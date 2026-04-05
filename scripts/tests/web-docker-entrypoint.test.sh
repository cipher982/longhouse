#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

mkdir -p "$TMPDIR/bin"
cat > "$TMPDIR/bin/nginx" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$TMPDIR/bin/nginx"

cat > "$TMPDIR/nginx.conf.template" <<'EOF'
events {}
http {
  include /tmp/csp.conf;
  server {
    listen 80;
    location /api {
      proxy_pass http://${BACKEND_HOST};
    }
  }
}
EOF

CONFIG_FILE="$TMPDIR/config.js"
CSP_FILE="$TMPDIR/csp.conf"
NGINX_CONF="$TMPDIR/nginx.conf"
NGINX_TEMPLATE="$TMPDIR/nginx.conf.template"

PATH="$TMPDIR/bin:$PATH" \
CONFIG_FILE="$CONFIG_FILE" \
CSP_FILE="$CSP_FILE" \
NGINX_TEMPLATE="$NGINX_TEMPLATE" \
NGINX_CONF="$NGINX_CONF" \
API_BASE_URL="" \
WS_BASE_URL="" \
PUBLIC_API_URL="" \
PUBLIC_WS_URL="" \
UMAMI_WEBSITE_ID="runtime-site" \
UMAMI_SCRIPT_SRC="https://analytics.example/script.js" \
UMAMI_DOMAINS="longhouse.ai" \
sh "$ROOT_DIR/../web/docker-entrypoint.sh"

grep -F 'window.__UMAMI_WEBSITE_ID__ = "runtime-site";' "$CONFIG_FILE" >/dev/null || {
  echo "Expected config.js to include runtime Umami website id"
  exit 1
}

grep -F 'window.__UMAMI_SCRIPT_SRC__ = "https://analytics.example/script.js";' "$CONFIG_FILE" >/dev/null || {
  echo "Expected config.js to include runtime Umami script src"
  exit 1
}

grep -F 'window.__UMAMI_DOMAINS__ = "longhouse.ai";' "$CONFIG_FILE" >/dev/null || {
  echo "Expected config.js to include runtime Umami domains"
  exit 1
}

grep -F 'window.API_BASE_URL = "";' "$CONFIG_FILE" >/dev/null || {
  echo "Expected same-origin mode to leave API_BASE_URL empty"
  exit 1
}

grep -F 'https://analytics.example' "$CSP_FILE" >/dev/null || {
  echo "Expected CSP to allow runtime Umami origin"
  exit 1
}

echo "web docker entrypoint tests passed"
