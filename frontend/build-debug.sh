#!/bin/bash
set -e

# Load *repository-root* .env so frontend uses the same configuration as backend
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

if [ -f "$ENV_FILE" ]; then
  # Enable automatic export of all assigned variables
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
fi

echo "[build-debug] HELLO compiling WASM (${WASM_PACK_DESC})..."

# Ensure a writable TMPDIR – some sandboxed CI runners mount the default
# /var/folders/... macOS location read-only which breaks Cargo during
# compilation (it needs to create temporary crates & metadata files).  We
# create a local directory inside the repository as a safe fallback.

if [[ -z "${TMPDIR:-}" || ! -w "${TMPDIR}" ]]; then
  export TMPDIR="$(pwd)/.tmp_build"
  mkdir -p "$TMPDIR"
fi

# Configure ports from .env with fallback defaults
BACKEND_PORT="${BACKEND_PORT:-8001}"
FRONTEND_PORT="${FRONTEND_PORT:-8002}"
BUILD_ENV="${BUILD_ENV:-debug}"

# Determine build mode based on BUILD_ENV
if [[ "${BUILD_ENV}" == "production" ]]; then
  WASM_PACK_MODE="--release"
  WASM_PACK_DESC="release"
else
  WASM_PACK_MODE="--dev"
  WASM_PACK_DESC="debug"
fi

# Ensure compile-time API_BASE_URL does not force cross-origin during dev –
# we will set it at runtime via config.js so CSP stays aligned with localhost.
if [[ "${BUILD_ENV}" != "production" ]]; then
  unset API_BASE_URL || true
fi

# -------------------------------------------------------------
# RUSTFLAGS — preserve existing debuginfo flag *and* opt-in to
# the getrandom JS backend required for wasm32-unknown-unknown.
# When RUSTFLAGS is set directly (instead of via .cargo/config.toml)
# it overrides the project-level configuration, so we must include
# the `--cfg getrandom_backend="wasm_js"` flag here as well.
# -------------------------------------------------------------

RUSTFLAGS="--cfg getrandom_backend=\"wasm_js\" -C debuginfo=2" \
  wasm-pack build ${WASM_PACK_MODE} --target web --out-dir pkg

# Copy the generated files to www directory
echo "[build-debug] copying WASM artifacts to www..."
cp pkg/agent_platform_frontend.js www/
cp pkg/agent_platform_frontend_bg.wasm www/
cp pkg/agent_platform_frontend.d.ts www/
cp pkg/agent_platform_frontend_bg.wasm.d.ts www/

# -------------------------------------------------------------
# Generate bootstrap.js expected by index.html
# -------------------------------------------------------------
echo "[build-debug] writing bootstrap.js …"
cat > www/bootstrap.js <<'EOF'
import init, { init_api_config_js } from './agent_platform_frontend.js';

async function main() {
  await init();            // loads wasm & runs #[wasm_bindgen(start)]
  // Optional dev override: only set if provided at runtime
  if (window.API_BASE_URL) {
    init_api_config_js(window.API_BASE_URL);
  }
}

main();
EOF

# Generate index.html from template with dynamic values
echo "[build-debug] generating index.html from template for ports ${BACKEND_PORT}/${FRONTEND_PORT} …"
TIMESTAMP=$(date +%s)
CACHE_BUST_TAG="<meta name=\"cache-bust\" content=\"${TIMESTAMP}\">"

# Set backend URLs for CSP: dev allows localhost; production allows api.swarmlet.com
if [[ "${BUILD_ENV}" == "production" ]]; then
  BACKEND_URL="https://api.swarmlet.com"
  BACKEND_WS_URL="wss://api.swarmlet.com"
else
  BACKEND_URL="http://localhost:${BACKEND_PORT}"
  BACKEND_WS_URL="ws://localhost:${BACKEND_PORT}"
fi

# Generate index.html from template
sed \
  -e "s|{{BACKEND_PORT}}|${BACKEND_PORT}|g" \
  -e "s|{{BACKEND_URL}}|${BACKEND_URL}|g" \
  -e "s|{{BACKEND_WS_URL}}|${BACKEND_WS_URL}|g" \
  -e "s|{{CACHE_BUST}}|${CACHE_BUST_TAG}|g" \
  www/index.html.template > www/index.html

# Cache-bust module graph and WASM to prevent JS/WASM version skew
# 1) Ensure bootstrap.js itself is re-fetched
sed -i.bak -e "s|src=\"bootstrap.js\"|src=\"bootstrap.js?v=${TIMESTAMP}\"|g" www/index.html && rm -f www/index.html.bak
# 2) Ensure the ESM import of the glue uses the same version
sed -i.bak -e "s|'./agent_platform_frontend.js'|'./agent_platform_frontend.js?v=${TIMESTAMP}'|g" www/bootstrap.js && rm -f www/bootstrap.js.bak
# 3) Ensure the glue fetches the matching WASM URL
sed -i.bak -e "s|new URL('agent_platform_frontend_bg.wasm', import.meta.url)|new URL('agent_platform_frontend_bg.wasm?v=${TIMESTAMP}', import.meta.url)|g" www/agent_platform_frontend.js && rm -f www/agent_platform_frontend.js.bak

# Write config.js – in production do not set a localhost API base
echo "[build-debug] writing config.js …"
if [[ "${BUILD_ENV}" == "production" ]]; then
  cat > www/config.js <<EOF
window.__APP_CONFIG__ = window.__APP_CONFIG__ || {};
window.__APP_CONFIG__.BUILD = 'production';
// In production, rely on same-origin '/api' or compile-time API_BASE_URL.
EOF
else
  cat > www/config.js <<EOF
window.__APP_CONFIG__ = window.__APP_CONFIG__ || {};
window.__APP_CONFIG__.BUILD = '${BUILD_ENV}';
// Dev: point to local backend for convenience
window.API_BASE_URL = 'http://localhost:${BACKEND_PORT}';
window.WS_BASE_URL = 'ws://localhost:${BACKEND_PORT}';
EOF
fi

# ---------------------------------------------------------------------------
# React prototype bundle (served alongside Rust UI)
# ---------------------------------------------------------------------------

REACT_DIR="${ROOT_DIR}/frontend-web"
REACT_DIST="${REACT_DIR}/dist"

if [ -d "${REACT_DIR}" ]; then
  echo "[build-debug] building React prototype via Vite …"
  (cd "${REACT_DIR}" && npm run build >/dev/null)
  echo "[build-debug] copying React dist -> www/react …"
  rm -rf www/react
  mkdir -p www/react
  cp -R "${REACT_DIST}/"* www/react/
  echo "[build-debug] React bundle ready at /react/index.html"
fi

# If BUILD_ONLY environment variable is set, exit here without starting dev server
if [ "${BUILD_ONLY:-}" = "true" ]; then
  echo "[build-debug] BUILD_ONLY=true, skipping dev server startup"
  echo "[build-debug] Build completed successfully - www/ directory contains processed files"
  exit 0
fi

echo "[build-debug] starting dev server on http://localhost:${FRONTEND_PORT} …"
cd www

# Create a simple HTTP server with cache control headers
cat > server.py << 'EOF'
import http.server
import socketserver
import sys

class CacheControlHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Disable caching for HTML files to ensure CSP updates are loaded
        if self.path.endswith('.html') or self.path == '/':
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        super().end_headers()

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
with socketserver.TCPServer(("", PORT), CacheControlHandler) as httpd:
    print(f"Serving at http://localhost:{PORT}")
    httpd.serve_forever()
EOF

if command -v open >/dev/null 2>&1; then
  (sleep 2 && open "http://localhost:${FRONTEND_PORT}/ui-switch.html") >/dev/null 2>&1 &
elif command -v xdg-open >/dev/null 2>&1; then
  (sleep 2 && xdg-open "http://localhost:${FRONTEND_PORT}/ui-switch.html") >/dev/null 2>&1 &
fi

python3 server.py ${FRONTEND_PORT}
