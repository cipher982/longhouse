#!/bin/bash
#
# Process video: concatenate scenes, add audio, compress for web
#
# Usage:
#   ./scripts/process_video.sh product-demo
#   ./scripts/process_video.sh product-demo --skip-audio
#

set -e

SCENARIO="${1:-product-demo}"
SKIP_AUDIO="${2:-}"
VIDEO_DIR="videos/$SCENARIO"
OUTPUT_DIR="apps/zerg/frontend-web/public/videos"
OUTPUT="$OUTPUT_DIR/$SCENARIO.mp4"

echo "🎬 Processing $SCENARIO..."

# Check prerequisites
if [ ! -f "$VIDEO_DIR/scenes.txt" ]; then
    echo "❌ Error: scenes.txt not found. Run 'make video-record' first."
    exit 1
fi

# Check for ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "❌ Error: ffmpeg not found. Install with: brew install ffmpeg"
    exit 1
fi

# 1. Concatenate scene videos
echo "  Concatenating scenes..."
CONCAT_INPUT="$VIDEO_DIR/concat.txt"

# Build concat file with absolute paths
rm -f "$CONCAT_INPUT"
while read -r line; do
    # Extract filename from "file 'name.webm'"
    filename=$(echo "$line" | sed "s/file '//;s/'//")
    if [ -f "$VIDEO_DIR/$filename" ]; then
        echo "file '$(pwd)/$VIDEO_DIR/$filename'" >> "$CONCAT_INPUT"
    else
        echo "  Warning: Video not found: $VIDEO_DIR/$filename"
    fi
done < "$VIDEO_DIR/scenes.txt"

if [ ! -s "$CONCAT_INPUT" ]; then
    echo "❌ Error: No video files found to concatenate"
    exit 1
fi

ffmpeg -y -f concat -safe 0 -i "$CONCAT_INPUT" \
    -c:v libx264 -crf 23 -preset medium \
    "$VIDEO_DIR/combined.mp4" 2>/dev/null

echo "    Combined video: $VIDEO_DIR/combined.mp4"

# 2. Check for audio
AUDIO_DIR="$VIDEO_DIR/audio"
HAS_AUDIO=false

if [ -d "$AUDIO_DIR" ] && [ "$(ls -A "$AUDIO_DIR"/*.mp3 2>/dev/null)" ]; then
    HAS_AUDIO=true
fi

if [ "$SKIP_AUDIO" = "--skip-audio" ]; then
    HAS_AUDIO=false
    echo "  Skipping audio (--skip-audio flag)"
fi

# 3. Process with or without audio
if [ "$HAS_AUDIO" = true ]; then
    echo "  Building audio track..."

    # Generate audio manifest
    AUDIO_CONCAT="$AUDIO_DIR/tracks.txt"
    rm -f "$AUDIO_CONCAT"

    # Get scene order from scenes.txt and match audio files
    while read -r line; do
        # Extract scene ID from filename (e.g., "dashboard-intro.webm" -> "dashboard-intro")
        filename=$(echo "$line" | sed "s/file '//;s/'//;s/.webm//")
        audio_file="$AUDIO_DIR/$filename.mp3"
        if [ -f "$audio_file" ]; then
            echo "file '$(pwd)/$audio_file'" >> "$AUDIO_CONCAT"
        fi
    done < "$VIDEO_DIR/scenes.txt"

    if [ -s "$AUDIO_CONCAT" ]; then
        # Concatenate audio files
        echo "  Concatenating audio..."
        ffmpeg -y -f concat -safe 0 -i "$AUDIO_CONCAT" \
            -c:a libmp3lame -q:a 2 \
            "$VIDEO_DIR/voiceover.mp3" 2>/dev/null

        # Combine video + audio
        echo "  Combining video + audio..."
        ffmpeg -y \
            -i "$VIDEO_DIR/combined.mp4" \
            -i "$VIDEO_DIR/voiceover.mp3" \
            -c:v copy -c:a aac -b:a 128k \
            -map 0:v -map 1:a \
            -shortest \
            "$VIDEO_DIR/with-audio.mp4" 2>/dev/null

        INPUT_FOR_COMPRESS="$VIDEO_DIR/with-audio.mp4"
    else
        echo "  No matching audio files found, using video only"
        INPUT_FOR_COMPRESS="$VIDEO_DIR/combined.mp4"
    fi
else
    echo "  No audio track"
    INPUT_FOR_COMPRESS="$VIDEO_DIR/combined.mp4"
fi

# 4. Compress for web
echo "  Compressing for web..."
mkdir -p "$OUTPUT_DIR"

ffmpeg -y -i "$INPUT_FOR_COMPRESS" \
    -c:v libx264 -crf 26 -preset slow \
    -c:a aac -b:a 128k \
    -movflags +faststart \
    -pix_fmt yuv420p \
    "$OUTPUT" 2>/dev/null

# Get file size
SIZE=$(du -h "$OUTPUT" | cut -f1)
DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$OUTPUT" 2>/dev/null | cut -d. -f1)

echo ""
echo "✅ Done!"
echo "   Output: $OUTPUT"
echo "   Size: $SIZE"
echo "   Duration: ${DURATION}s"
echo ""
echo "Preview with: mpv $OUTPUT"
