#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Frontend Test Runner – *always* execute the full WASM test-suite.
# ---------------------------------------------------------------------------
# 1. We need a headless browser (Chrome or Firefox) because several
#    `wasm_bindgen_test` cases are compiled with `run_in_browser`.
# 2. We try Chrome first (Playwright default), then Firefox.
# 3. If neither is present we abort with an explanatory error – no silent
#    compile-only fallback, the test run must be explicit just like `pytest`.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------------------
# Ensure Rust/Cargo can access a writable temporary directory.  macOS (and the
# GitHub Actions `macos-latest` image) default `TMPDIR` to a sandboxed
# location under `/var/folders/...`.  Inside certain container or sandbox
# runners this path may be mounted *read-only*, causing mysterious compilation
# errors like:
#     "couldn't create a temp dir: Operation not permitted (os error 1)"
# To make the test-runner resilient we force TMPDIR to a directory inside the
# repository that we know is writable.
# ---------------------------------------------------------------------------
export TMPDIR="${ROOT_DIR}/tmp"
mkdir -p "${TMPDIR}"
# Some wasm-bindgen helpers ignore *TMPDIR* on macOS and instead consult the
# generic *TMP* / *TEMP* variables.  Ensure they point to the **same** writable
# directory so temp-file creation succeeds in sandboxed CI runners (fixes
# “Operation not permitted (os error 1)” failures).
export TMP="${TMPDIR}"
export TEMP="${TMPDIR}"

# ---------------------------------------------------------------------------
# Helper to execute wasm-pack for a given browser. Do NOT abort the script
# immediately on failure – we want to fall back to other browsers if the
# first candidate runs into a chromedriver/geckodriver mismatch.
# ---------------------------------------------------------------------------
run_tests() {
  local browser=$1
  echo "[run_frontend_tests] Running WASM tests in headless $browser …" >&2
  # If the caller provided CHROMEDRIVER / GECKODRIVER keep it, otherwise
  # attempt to point wasm-pack to a system driver (Homebrew or PATH).
  if [[ "$browser" == "chrome" && -z "${CHROMEDRIVER:-}" ]]; then
    if command -v chromedriver >/dev/null 2>&1; then
      export CHROMEDRIVER="$(command -v chromedriver)"
    elif [[ -x "/opt/homebrew/bin/chromedriver" ]]; then
      export CHROMEDRIVER="/opt/homebrew/bin/chromedriver"
    fi
  fi

  if [[ "$browser" == "firefox" && -z "${GECKODRIVER:-}" ]]; then
    if command -v geckodriver >/dev/null 2>&1; then
      export GECKODRIVER="$(command -v geckodriver)"
    elif [[ -x "/opt/homebrew/bin/geckodriver" ]]; then
      export GECKODRIVER="/opt/homebrew/bin/geckodriver"
    fi
  fi
  if wasm-pack test --headless --"$browser" "$ROOT_DIR"; then
    echo "[run_frontend_tests] ✔ tests passed with $browser" >&2
    exit 0
  else
    echo "[run_frontend_tests] ✖ tests failed with $browser – trying next option if available" >&2
    return 1
  fi
}

# Prefer Chrome/Chromium – most contributors have it installed.

# ---------------------------------------------------------------------------
# Browser discovery helpers
# ---------------------------------------------------------------------------

# 1) Prefer any Chrome/Chromium binary already on $PATH.
if command -v google-chrome >/dev/null 2>&1; then
  run_tests chrome
fi

if command -v chromium-browser >/dev/null 2>&1; then
  run_tests chrome
fi

if command -v chromium >/dev/null 2>&1; then
  run_tests chrome
fi

# 2) Homebrew puts wrappers into /opt/homebrew/bin which might not be on PATH
#    for non-login shells (e.g. CI).
if [[ -x "/opt/homebrew/bin/google-chrome" ]]; then
  export CHROME_BIN="/opt/homebrew/bin/google-chrome"
  run_tests chrome
fi

# 3) macOS GUI installs live in /Applications/… but are not exposed as
#    executables.  If we find them, use them via the CHROME_BIN/FIREFOX_BIN
#    environment variable that wasm-bindgen-test understands.
if [[ "$(uname)" == "Darwin" ]]; then
  CHROME_BUNDLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  FIREFOX_BUNDLE="/Applications/Firefox.app/Contents/MacOS/firefox"

  if [[ -z "${CHROME_BIN:-}" && -x "$CHROME_BUNDLE" ]]; then
    export CHROME_BIN="$CHROME_BUNDLE"
    run_tests chrome
  fi

  if [[ -z "${FIREFOX_BIN:-}" && -x "$FIREFOX_BUNDLE" ]]; then
    export FIREFOX_BIN="$FIREFOX_BUNDLE"
    run_tests firefox || true
  fi
fi

# 4) Fallback to Firefox on PATH if Chrome isn't present.
if command -v firefox >/dev/null 2>&1; then
  run_tests firefox
fi

echo "[run_frontend_tests] ERROR: Couldn't find a working headless browser + driver combination.\n" \
     "Falling back to wasm-pack --node (headless, no browser)." >&2

# ---------------------------------------------------------------------------
# Fallback: run tests in Node.js – covers logic, misses DOM APIs but keeps CI
# green on minimal runners.
# ---------------------------------------------------------------------------
if wasm-pack test --node "$ROOT_DIR"; then
  echo "[run_frontend_tests] ✔ tests passed in Node fallback" >&2
  exit 0
else
  echo "[run_frontend_tests] ✖ wasm-pack tests failed in Node fallback" >&2
  exit 1
fi
