#!/usr/bin/env bash
# Build the WASM front-end **without** starting a server.
# Used by Playwright E2E tests (wasm-server.js will serve the built `www/`).

set -euo pipefail

echo "[build-only] 🔧 building frontend (debug)…" >&2

# --------------------------------------------------
# Prerequisites
# --------------------------------------------------
command -v wasm-pack >/dev/null 2>&1 || {
  echo "[build-only] ❌ wasm-pack not found (install with 'cargo install wasm-pack')." >&2
  exit 1
}

# --------------------------------------------------
# Environment   (allows API_BASE_URL override via repo-root .env)
# --------------------------------------------------
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
fi

BUILD_ENV="${BUILD_ENV:-debug}"

# Determine build mode based on BUILD_ENV
if [[ "${BUILD_ENV}" == "production" ]]; then
  WASM_PACK_MODE="--release"
  WASM_PACK_DESC="release"
else
  WASM_PACK_MODE="--dev"
  WASM_PACK_DESC="debug"
fi
# Configure ports from .env with fallback defaults
BACKEND_PORT="${BACKEND_PORT:-8001}"

# In dev builds we prefer the runtime JS override for API base URL to avoid
# hard-coding cross-origin endpoints at compile-time (which can conflict with
# CSP and cause fetches to the wrong host).  Ensure the compile-time
# API_BASE_URL is unset so `option_env!("API_BASE_URL")` evaluates to None and
# the runtime `window.API_BASE_URL` provided via config.js takes effect.
if [[ "${BUILD_ENV}" != "production" ]]; then
  unset API_BASE_URL || true
fi

# --------------------------------------------------
# Build – dev profile, debuginfo, target web
# --------------------------------------------------

# Record the build step – the actual `wasm-pack build` command is invoked
# *after* we ensure a writable TMPDIR below.  Keeping the banner here avoids
# altering the original log order while preventing an accidental line-continuation
# without a command (which would cause a syntax error in `bash`).
echo "[build-only] 🏗  wasm-pack build (${WASM_PACK_DESC}) …" >&2

# Ensure a writable TMPDIR for systems with restricted /var directories (e.g. sandboxed CI)
TMPDIR_OVERRIDDEN="${TMPDIR:-}"
if [[ -z "$TMPDIR_OVERRIDDEN" || ! -w "$TMPDIR_OVERRIDDEN" ]]; then
  export TMPDIR="$(pwd)/.tmp_build"
  mkdir -p "$TMPDIR"
fi

RUSTFLAGS="--cfg getrandom_backend=\"wasm_js\" -C debuginfo=2" \
  wasm-pack build ${WASM_PACK_MODE} --target web --out-dir pkg

# Copy the generated files to www directory
echo "[build-only] 📦 copying WASM artifacts to www..." >&2
cp pkg/agent_platform_frontend.js www/
cp pkg/agent_platform_frontend_bg.wasm www/
cp pkg/agent_platform_frontend.d.ts www/
cp pkg/agent_platform_frontend_bg.wasm.d.ts www/

# Ensure output dir exists (wasm-pack creates it but be safe)
mkdir -p www

# Build ID used for cache-busting query parameters across JS/WASM
TIMESTAMP=$(date +%s)

# --------------------------------------------------
# bootstrap.js – mirrors build-debug.sh minus live server
# --------------------------------------------------
echo "[build-only] ✍️  writing bootstrap.js …" >&2
cat <<'JS' > www/bootstrap.js
import init, { init_api_config_js } from './agent_platform_frontend.js';

async function main() {
  await init();
  if (window.API_BASE_URL) {
    init_api_config_js(window.API_BASE_URL);
  }
}

main();
JS

# Patch bootstrap import to include version so the module graph is refreshed
sed -i.bak -e "s|'./agent_platform_frontend.js'|'./agent_platform_frontend.js?v=${TIMESTAMP}'|g" www/bootstrap.js && rm -f www/bootstrap.js.bak

# --------------------------------------------------
# Generate index.html from template with dynamic values
# --------------------------------------------------
echo "[build-only] 🧩 generating index.html from template …" >&2
CACHE_BUST_TAG="<meta name=\"cache-bust\" content=\"${TIMESTAMP}\">"

if [[ "${BUILD_ENV}" == "production" ]]; then
  BACKEND_URL=""
  BACKEND_WS_URL=""
else
  BACKEND_URL="http://localhost:${BACKEND_PORT}"
  BACKEND_WS_URL="ws://localhost:${BACKEND_PORT}"
fi

sed \
  -e "s|{{BACKEND_URL}}|${BACKEND_URL}|g" \
  -e "s|{{BACKEND_WS_URL}}|${BACKEND_WS_URL}|g" \
  -e "s|{{CACHE_BUST}}|${CACHE_BUST_TAG}|g" \
  www/index.html.template > www/index.html

# Ensure the HTML references the versioned bootstrap
sed -i.bak -e "s|src=\"bootstrap.js\"|src=\"bootstrap.js?v=${TIMESTAMP}\"|g" www/index.html && rm -f www/index.html.bak

# Ensure the glue fetches the matching WASM URL (same version id)
sed -i.bak -e "s|new URL('agent_platform_frontend_bg.wasm', import.meta.url)|new URL('agent_platform_frontend_bg.wasm?v=${TIMESTAMP}', import.meta.url)|g" www/agent_platform_frontend.js && rm -f www/agent_platform_frontend.js.bak

# --------------------------------------------------
# config.js – tiny placeholder so <script src="config.js"> doesn't 404
# --------------------------------------------------
echo "[build-only] ✍️  writing config.js …" >&2
if [[ "${BUILD_ENV}" == "production" ]]; then
  cat <<EOF > www/config.js
// Auto-generated by frontend/build-only.sh
window.__APP_CONFIG__ = window.__APP_CONFIG__ || {};
window.__APP_CONFIG__.BUILD = 'production';
// In production, rely on same-origin or compile-time API_BASE_URL; no runtime override here.
EOF
else
  cat <<EOF > www/config.js
// Auto-generated by frontend/build-only.sh
window.__APP_CONFIG__ = window.__APP_CONFIG__ || {};
window.__APP_CONFIG__.BUILD = '${BUILD_ENV}';
// Prefer runtime API base URL so dev CSP remains simple and requests go to local backend
window.API_BASE_URL = 'http://localhost:${BACKEND_PORT}';
EOF
fi

echo "[build-only] ✅ build complete (output in frontend/www/)" >&2
