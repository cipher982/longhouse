#!/usr/bin/env bash
set -euo pipefail

# Generate brand assets from the canonical SVG master.
# The menu bar and panel icons are severity variants generated from the same geometry.
# Requires ImageMagick and Playwright's Chromium runtime.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT_DIR}/branding/longhouse-logo-master.svg"
PUBLIC_DIR="${ROOT_DIR}/public"
PANEL_GREEN_OUT="${ROOT_DIR}/../desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghousePanelIconGreen.png"
PANEL_WARNING_OUT="${ROOT_DIR}/../desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghousePanelIconYellow.png"
PANEL_CRITICAL_OUT="${ROOT_DIR}/../desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghousePanelIconRed.png"
PANEL_NEUTRAL_OUT="${ROOT_DIR}/../desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghousePanelIconGray.png"

if [[ ! -f "${SRC}" ]]; then
  echo "Master logo not found at ${SRC}" >&2
  exit 1
fi

mkdir -p "${PUBLIC_DIR}"

echo "Copying canonical SVG…"
cp "${SRC}" "${PUBLIC_DIR}/longhouse-logo.svg"

echo "Generating favicon base (512px)…"
node "${ROOT_DIR}/scripts/render-svg-asset.mjs" "${SRC}" "${PUBLIC_DIR}/favicon-512.png" 512 512

echo "Generating favicons (32px, 16px, ICO)…"
magick "${PUBLIC_DIR}/favicon-512.png" -resize 32x32 "${PUBLIC_DIR}/favicon-32.png"
magick "${PUBLIC_DIR}/favicon-512.png" -resize 16x16 "${PUBLIC_DIR}/favicon-16.png"
magick "${PUBLIC_DIR}/favicon-16.png" "${PUBLIC_DIR}/favicon-32.png" "${PUBLIC_DIR}/favicon.ico"

echo "Generating Apple touch icon (180px)…"
magick "${PUBLIC_DIR}/favicon-512.png" -resize 180x180 "${PUBLIC_DIR}/apple-touch-icon.png"

echo "Generating iOS app icon (1024px, opaque)…"
# App icons must be opaque; the mascot sits on the web page's soot ground under
# its hearth light, inset so the home-screen squircle never clips the helmet.
IOS_ICON_OUT="${ROOT_DIR}/../ios/Resources/Assets.xcassets/AppIcon.appiconset/app-icon-1024.png"
IOS_TMP=$(mktemp -d)
node "${ROOT_DIR}/scripts/render-svg-asset.mjs" "${SRC}" "${IOS_TMP}/mascot.png" 1024 1024
magick "${IOS_TMP}/mascot.png" -trim +repage -resize 800x800 "${IOS_TMP}/mascot-800.png"
magick -size 1024x1024 xc:'#0B0908' \
  \( -size 1400x1400 radial-gradient:'rgba(233,185,73,0.22)'-'rgba(233,185,73,0)' -gravity north -crop 1024x1024+188+0 +repage \) \
  -compose over -composite \
  "${IOS_TMP}/mascot-800.png" -gravity center -geometry +0+8 -compose over -composite \
  -alpha off "${IOS_ICON_OUT}"
rm -rf "${IOS_TMP}"

echo "Generating panel status variants from master logo geometry…"
mkdir -p "$(dirname "${PANEL_GREEN_OUT}")"
# Source icon geometry should be edge-to-edge; panel variants add their own inset.
node "${ROOT_DIR}/scripts/render-menubar-icon.mjs" "${SRC}" "${PANEL_GREEN_OUT}" 72 72 green 0.08
node "${ROOT_DIR}/scripts/render-menubar-icon.mjs" "${SRC}" "${PANEL_WARNING_OUT}" 72 72 yellow 0.08
node "${ROOT_DIR}/scripts/render-menubar-icon.mjs" "${SRC}" "${PANEL_CRITICAL_OUT}" 72 72 red 0.08
node "${ROOT_DIR}/scripts/render-menubar-icon.mjs" "${SRC}" "${PANEL_NEUTRAL_OUT}" 72 72 gray 0.08

echo "Generating macOS app icon (AppIcon.icns)…"
ICNS_OUT="${ROOT_DIR}/../artifacts/runtime-packaging/stage/Longhouse.app/Contents/Resources/AppIcon.icns"
if command -v iconutil &>/dev/null; then
  ICONSET_DIR=$(mktemp -d)/AppIcon.iconset
  mkdir -p "${ICONSET_DIR}"
  for sz in 16 32 128 256 512; do
    magick "${PUBLIC_DIR}/favicon-512.png" -resize "${sz}x${sz}" "${ICONSET_DIR}/icon_${sz}x${sz}.png"
    double=$((sz * 2))
    if [ "${double}" -le 1024 ]; then
      magick "${PUBLIC_DIR}/favicon-512.png" -resize "${double}x${double}" "${ICONSET_DIR}/icon_${sz}x${sz}@2x.png"
    fi
  done
  mkdir -p "$(dirname "${ICNS_OUT}")"
  iconutil -c icns "${ICONSET_DIR}" -o "${ICNS_OUT}"
  rm -rf "$(dirname "${ICONSET_DIR}")"
  echo "  → ${ICNS_OUT}"
else
  echo "  ⚠ iconutil not found (macOS only), skipping .icns generation"
fi

# og-image.png is not generated here: it's a code-derived screenshot (real
# timeline capture + wedge headline + master logo) built by
# scripts/generate-og-image.mjs (repo root), not this ImageMagick gradient
# plate. Run that script directly to regenerate it.

echo "Done. Assets written to ${PUBLIC_DIR}"
