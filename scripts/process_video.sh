#!/bin/bash
#
# Process video: concatenate scenes, add audio, compress for web
#
# Outputs:
#   videos/<scenario>/master-raw.<ext> - Raw concat (lossless stream copy)
#   videos/<scenario>/master.mp4       - High quality with audio (crf 18)
#   public/videos/<scenario>.mp4       - Web optimized (crf 23)
#
# Usage:
#   ./scripts/process_video.sh product-demo
#   ./scripts/process_video.sh product-demo --skip-audio
#   ./scripts/process_video.sh product-demo --web-only    # Skip master, just web output
#   ./scripts/process_video.sh product-demo --no-gaps      # Skip black gap insertion
#   ./scripts/process_video.sh product-demo --no-gaps --web-only  # Multiple flags OK
#

set -e

SCENARIO="${1:-product-demo}"

# Parse flags (all positional args after scenario)
SKIP_AUDIO=false
WEB_ONLY=false
NO_GAPS=false
shift || true
for arg in "$@"; do
    case "$arg" in
        --skip-audio) SKIP_AUDIO=true ;;
        --web-only)   WEB_ONLY=true ;;
        --no-gaps)    NO_GAPS=true ;;
        *) echo "Unknown flag: $arg"; exit 1 ;;
    esac
done
VIDEO_DIR="videos/$SCENARIO"
OUTPUT_DIR="apps/zerg/frontend-web/public/videos"
WEB_OUTPUT="$OUTPUT_DIR/$SCENARIO.mp4"
MASTER_RAW=""
MASTER_MP4="$VIDEO_DIR/master.mp4"

WEB_SCALE_FILTER="scale='min(1920,iw)':-2"

echo "🎬 Processing $SCENARIO..."

# Check prerequisites
if [ ! -f "$VIDEO_DIR/scenes.txt" ]; then
    echo "❌ Error: scenes.txt not found. Run 'make video-record' first."
    exit 1
fi

if ! command -v ffmpeg &> /dev/null; then
    echo "❌ Error: ffmpeg not found. Install with: brew install ffmpeg"
    exit 1
fi

# =============================================================================
# Step 0: Generate black gap clip (if needed)
# =============================================================================
BLACK_GAP="videos/black-300ms.mov"
if [ "$NO_GAPS" = false ]; then
    if [ ! -f "$BLACK_GAP" ]; then
        echo "  [0/4] Generating 0.3s black gap clip..."
        ffmpeg -y -f lavfi \
            -i "color=black:s=1920x1080:d=0.3:r=30,format=yuv422p10le" \
            -c:v prores_ks -profile:v 3 \
            "$BLACK_GAP" 2>/dev/null
        echo "    ✓ $(basename "$BLACK_GAP")"
    fi
fi

# =============================================================================
# Step 1: Concatenate scene videos (LOSSLESS - stream copy)
# =============================================================================
echo "  [1/4] Concatenating scenes (lossless)..."
CONCAT_INPUT="$VIDEO_DIR/concat.txt"

rm -f "$CONCAT_INPUT"
SCENE_INDEX=0
while read -r line; do
    filename=$(echo "$line" | sed "s/file '//;s/'//")
    if [ -f "$VIDEO_DIR/$filename" ]; then
        # Insert black gap between scenes (not before first)
        if [ "$NO_GAPS" = false ] && [ "$SCENE_INDEX" -gt 0 ]; then
            echo "file '$(pwd)/$BLACK_GAP'" >> "$CONCAT_INPUT"
        fi
        echo "file '$(pwd)/$VIDEO_DIR/$filename'" >> "$CONCAT_INPUT"
        SCENE_INDEX=$((SCENE_INDEX + 1))
    else
        echo "    Warning: Video not found: $VIDEO_DIR/$filename"
    fi
done < "$VIDEO_DIR/scenes.txt"

if [ "$NO_GAPS" = false ] && [ "$SCENE_INDEX" -gt 1 ]; then
    echo "    ✓ Inserted $((SCENE_INDEX - 1)) black gaps (0.3s each)"
fi

if [ ! -s "$CONCAT_INPUT" ]; then
    echo "❌ Error: No video files found to concatenate"
    exit 1
fi

FIRST_LINE=$(head -n 1 "$VIDEO_DIR/scenes.txt")
FIRST_FILE=$(echo "$FIRST_LINE" | sed "s/file '//;s/'//")
EXT="${FIRST_FILE##*.}"
MASTER_RAW="$VIDEO_DIR/master-raw.$EXT"

# Lossless concat (stream copy, no re-encoding)
ffmpeg -y -f concat -safe 0 -i "$CONCAT_INPUT" \
    -c copy \
    "$MASTER_RAW" 2>/dev/null

MASTER_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$MASTER_RAW" 2>/dev/null)
echo "    ✓ $(basename "$MASTER_RAW") (${MASTER_DURATION%.*}s, raw quality)"

# =============================================================================
# Step 2: Prepare audio track
# =============================================================================
AUDIO_DIR="$VIDEO_DIR/audio"
HAS_AUDIO=false
AUDIO_DURATION=""

if [ "$SKIP_AUDIO" = false ] && [ -d "$AUDIO_DIR" ] && [ "$(ls -A "$AUDIO_DIR"/*.mp3 2>/dev/null)" ]; then
    echo "  [2/4] Building audio track..."

    AUDIO_CONCAT="$AUDIO_DIR/tracks.txt"
    rm -f "$AUDIO_CONCAT"

    while read -r line; do
        filename=$(echo "$line" | sed "s/file '//;s/'//")
        base_name=$(basename "$filename")
        base_name="${base_name%.*}"
        audio_file="$AUDIO_DIR/$base_name.mp3"
        if [ -f "$audio_file" ]; then
            echo "file '$(pwd)/$audio_file'" >> "$AUDIO_CONCAT"
        fi
    done < "$VIDEO_DIR/scenes.txt"

    if [ -s "$AUDIO_CONCAT" ]; then
        ffmpeg -y -f concat -safe 0 -i "$AUDIO_CONCAT" \
            -c:a libmp3lame -q:a 0 \
            "$VIDEO_DIR/voiceover.mp3" 2>/dev/null

        AUDIO_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VIDEO_DIR/voiceover.mp3" 2>/dev/null)
        HAS_AUDIO=true
        echo "    ✓ voiceover.mp3 (${AUDIO_DURATION%.*}s)"
    fi
else
    echo "  [2/4] Skipping audio"
fi

# =============================================================================
# Step 3: Create master MP4 (high quality, single encode)
# =============================================================================
# Check for background music
MUSIC_FILE="$VIDEO_DIR/music.mp3"
HAS_MUSIC=false
if [ "$HAS_AUDIO" = true ] && [ -f "$MUSIC_FILE" ]; then
    HAS_MUSIC=true
    echo "    ♪ Background music detected: $(basename "$MUSIC_FILE")"
fi

if [ "$WEB_ONLY" = true ]; then
    echo "  [3/4] Skipping master.mp4 (--web-only)"
    ENCODE_SOURCE="$MASTER_RAW"
else
    echo "  [3/4] Creating master.mp4 (high quality, crf 18)..."

    if [ "$HAS_AUDIO" = true ] && [ "$HAS_MUSIC" = true ]; then
        # Mix voiceover + background music (music at 8% volume, 3s fade-out at end)
        ffmpeg -y \
            -i "$MASTER_RAW" \
            -i "$VIDEO_DIR/voiceover.mp3" \
            -i "$MUSIC_FILE" \
            -t "$AUDIO_DURATION" \
            -filter_complex "[2:a]volume=0.08,afade=t=out:st=$(echo "$AUDIO_DURATION - 3" | bc):d=3[music];[1:a][music]amix=inputs=2:duration=first:dropout_transition=3[aout]" \
            -c:v libx264 -crf 18 -preset slow \
            -map 0:v -map "[aout]" \
            -c:a aac -b:a 192k \
            -pix_fmt yuv420p \
            "$MASTER_MP4" 2>/dev/null
    elif [ "$HAS_AUDIO" = true ]; then
        # Voiceover only (no music)
        # Use -t to trim video to audio duration (more reliable than -shortest with stream copy)
        ffmpeg -y \
            -i "$MASTER_RAW" \
            -i "$VIDEO_DIR/voiceover.mp3" \
            -t "$AUDIO_DURATION" \
            -c:v libx264 -crf 18 -preset slow \
            -c:a aac -b:a 192k \
            -map 0:v -map 1:a \
            -pix_fmt yuv420p \
            "$MASTER_MP4" 2>/dev/null
    else
        ffmpeg -y \
            -i "$MASTER_RAW" \
            -c:v libx264 -crf 18 -preset slow \
            -pix_fmt yuv420p \
            "$MASTER_MP4" 2>/dev/null
    fi

    MASTER_SIZE=$(du -h "$MASTER_MP4" | cut -f1)
    echo "    ✓ master.mp4 ($MASTER_SIZE)"
    ENCODE_SOURCE="$MASTER_MP4"
fi

# =============================================================================
# Step 4: Create web-optimized output (smaller file, fast start)
# =============================================================================
echo "  [4/4] Creating web output (crf 23, faststart)..."
mkdir -p "$OUTPUT_DIR"

if [ "$WEB_ONLY" = true ] && [ "$HAS_AUDIO" = true ] && [ "$HAS_MUSIC" = true ]; then
    # Single encode from raw + voiceover + music
    ffmpeg -y \
        -i "$MASTER_RAW" \
        -i "$VIDEO_DIR/voiceover.mp3" \
        -i "$MUSIC_FILE" \
        -t "$AUDIO_DURATION" \
        -filter_complex "[2:a]volume=0.08,afade=t=out:st=$(echo "$AUDIO_DURATION - 3" | bc):d=3[music];[1:a][music]amix=inputs=2:duration=first:dropout_transition=3[aout]" \
        -vf "$WEB_SCALE_FILTER" \
        -c:v libx264 -crf 23 -preset medium \
        -map 0:v -map "[aout]" \
        -c:a aac -b:a 128k \
        -movflags +faststart \
        -pix_fmt yuv420p \
        "$WEB_OUTPUT" 2>/dev/null
elif [ "$WEB_ONLY" = true ] && [ "$HAS_AUDIO" = true ]; then
    # Single encode from raw + voiceover (no music)
    ffmpeg -y \
        -i "$MASTER_RAW" \
        -i "$VIDEO_DIR/voiceover.mp3" \
        -t "$AUDIO_DURATION" \
        -vf "$WEB_SCALE_FILTER" \
        -c:v libx264 -crf 23 -preset medium \
        -c:a aac -b:a 128k \
        -map 0:v -map 1:a \
        -movflags +faststart \
        -pix_fmt yuv420p \
        "$WEB_OUTPUT" 2>/dev/null
else
    # Re-encode from master (already has audio baked in)
    ffmpeg -y -i "$ENCODE_SOURCE" \
        -vf "$WEB_SCALE_FILTER" \
        -c:v libx264 -crf 23 -preset medium \
        -c:a aac -b:a 128k \
        -movflags +faststart \
        -pix_fmt yuv420p \
        "$WEB_OUTPUT" 2>/dev/null
fi

# =============================================================================
# Summary
# =============================================================================
WEB_SIZE=$(du -h "$WEB_OUTPUT" | cut -f1)
WEB_DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$WEB_OUTPUT" 2>/dev/null | cut -d. -f1)

# Always show master even if web-only skipped
MASTER_RAW_BASENAME=$(basename "$MASTER_RAW")

printf "\n✅ Done!\n\n"

echo "   Outputs:"
echo "   ├── $VIDEO_DIR/$MASTER_RAW_BASENAME (raw, lossless concat)"
if [ "$WEB_ONLY" != true ]; then
    echo "   ├── $MASTER_MP4 (high quality, crf 18)"
fi
echo "   └── $WEB_OUTPUT ($WEB_SIZE, ${WEB_DURATION}s)"
echo ""
echo "Preview: mpv $WEB_OUTPUT"
if [ "$WEB_ONLY" != true ]; then
    echo "Master:  mpv $MASTER_MP4"
fi
