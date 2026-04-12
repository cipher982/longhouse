#!/usr/bin/env bash
set -euo pipefail

# Generate brand assets from the canonical SVG master.
# The menu bar icon is a two-tone derivative generated from the same geometry.
# Requires ImageMagick and Playwright's Chromium runtime.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT_DIR}/branding/longhouse-logo-master.svg"
PUBLIC_DIR="${ROOT_DIR}/public"
MENUBAR_OUT="${ROOT_DIR}/../desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/Resources/LonghouseMenuIcon.png"

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

echo "Generating maskable icons (192px, 512px)…"
magick "${PUBLIC_DIR}/favicon-512.png" -resize 192x192 "${PUBLIC_DIR}/maskable-icon-192.png"
cp "${PUBLIC_DIR}/favicon-512.png" "${PUBLIC_DIR}/maskable-icon-512.png"

echo "Generating menu bar icon from master logo geometry…"
mkdir -p "$(dirname "${MENUBAR_OUT}")"
node "${ROOT_DIR}/scripts/render-menubar-icon.mjs" "${SRC}" "${MENUBAR_OUT}" 36 36

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

echo "Generating social preview (1200x630)…"
magick \
  -size 1200x630 gradient:'#0072ff-#00c6ff' \
  \( -size 1200x630 canvas:'#0a0a0f' -alpha set -channel A -evaluate set 30% +channel \) \
  -compose over -composite \
  \( "${PUBLIC_DIR}/favicon-512.png" -resize 320x320 \) -gravity West -geometry +120+0 -composite \
  -gravity Northwest -font 'Helvetica-Bold' -pointsize 120 -fill '#ffffff' -annotate +500+200 'Longhouse' \
  -gravity Northwest -font 'Helvetica' -pointsize 52 -fill '#e6f7ff' -annotate +500+320 'AI Agent Platform' \
  "${PUBLIC_DIR}/og-image.png"

echo "Done. Assets written to ${PUBLIC_DIR}"
