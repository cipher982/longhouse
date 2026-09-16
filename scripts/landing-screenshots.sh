#!/usr/bin/env bash
# Regenerate the landing page's product showcase images (Timeline, Search,
# Session Detail) from the current web app.
#
# Each view renders the real app against curated fixture data
# (scripts/ui-fixtures/landingShowcase.ts) through `make ui-capture`, which
# starts and stops Vite itself and seals every API call, so nothing depends on
# a running backend or a demo database, and no real account data can leak into
# a published image. Re-run after any timeline or session-detail UI change.
set -euo pipefail

cd "$(dirname "$0")/.."

OUT_DIR="web/public/images/landing"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# Desktop views at 16:9 to match the showcase frame, and the app's own phone
# layout for small screens; both at 2x for retina. Keep in step with the
# aspect ratios in .app-screenshot-content (web/src/styles/landing.css).
DESKTOP="1152x648@2"
PHONE="390x720@2"

capture() {
  local page="$1" scene="$2" name="$3" viewport="$4"
  make ui-capture PAGE="$page" SCENE="$scene" VIEWPORT="$viewport" NO_TRACE=1 OUTPUT="$SCRATCH/$name" >/dev/null
  local shot="$SCRATCH/$name/$page.png"
  if [ ! -s "$shot" ]; then
    echo "landing-screenshots: no capture for $name" >&2
    exit 1
  fi
  cp "$shot" "$OUT_DIR/$name.png"
  # The PNG master feeds README.md and scripts/generate-og-image.mjs; quantized
  # it is ~1MB instead of ~2.6MB with no visible change to UI text.
  pngquant --quality 80-95 --speed 1 --force --ext .png "$OUT_DIR/$name.png"
  magick "$OUT_DIR/$name.png" -quality 82 "$OUT_DIR/$name.webp"
  echo "  $OUT_DIR/$name.{png,webp}"
}

echo "Capturing landing showcase images..."
capture timeline landing timeline-preview "$DESKTOP"
capture timeline landing-search search-preview "$DESKTOP"
capture session-detail landing-session session-detail-preview "$DESKTOP"
capture timeline landing timeline-preview-mobile "$PHONE"
capture timeline landing-search search-preview-mobile "$PHONE"
capture session-detail landing-session session-detail-preview-mobile "$PHONE"
echo "Done. Bump the ?v= stamps in web/src/components/landing/ProductShowcase.tsx."
