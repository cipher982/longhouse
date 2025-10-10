#!/usr/bin/env bash
set -euo pipefail

# Generate web icon assets for Swarmlet from the master logo.
# Requires ImageMagick (magick command).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT_DIR}/branding/swarm-logo-master.png"
PUBLIC_DIR="${ROOT_DIR}/public"

if [[ ! -f "${SRC}" ]]; then
  echo "Master logo not found at ${SRC}" >&2
  exit 1
fi

mkdir -p "${PUBLIC_DIR}"

echo "Generating favicon base (512px)…"
magick "${SRC}" -resize 512x512 "${PUBLIC_DIR}/favicon-512.png"

echo "Generating favicons (32px, 16px, ICO)…"
magick "${PUBLIC_DIR}/favicon-512.png" -resize 32x32 "${PUBLIC_DIR}/favicon-32.png"
magick "${PUBLIC_DIR}/favicon-512.png" -resize 16x16 "${PUBLIC_DIR}/favicon-16.png"
magick "${PUBLIC_DIR}/favicon-16.png" "${PUBLIC_DIR}/favicon-32.png" "${PUBLIC_DIR}/favicon-512.png" -colors 256 "${PUBLIC_DIR}/favicon.ico"

echo "Generating Apple touch icon (180px)…"
magick "${PUBLIC_DIR}/favicon-512.png" -resize 180x180 "${PUBLIC_DIR}/apple-touch-icon.png"

echo "Generating maskable icons (192px, 512px)…"
magick "${PUBLIC_DIR}/favicon-512.png" -resize 192x192 "${PUBLIC_DIR}/maskable-icon-192.png"
magick "${PUBLIC_DIR}/favicon-512.png" -resize 512x512 "${PUBLIC_DIR}/maskable-icon-512.png"

echo "Generating social preview (1200x630)…"
magick \
  -size 1200x630 gradient:'#0072ff-#00c6ff' \
  \( -size 1200x630 canvas:'#0a0a0f' -alpha set -channel A -evaluate set 30% +channel \) \
  -compose over -composite \
  \( "${PUBLIC_DIR}/favicon-512.png" -resize 320x320 \) -gravity West -geometry +120+0 -composite \
  -gravity Northwest -font 'Helvetica-Bold' -pointsize 120 -fill '#ffffff' -annotate +500+200 'Swarmlet' \
  -gravity Northwest -font 'Helvetica' -pointsize 52 -fill '#e6f7ff' -annotate +500+320 'AI Agent Platform' \
  "${PUBLIC_DIR}/og-image.png"

echo "Done. Assets written to ${PUBLIC_DIR}"
