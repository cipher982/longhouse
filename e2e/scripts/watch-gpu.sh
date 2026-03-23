#!/bin/bash
# Simple GPU watcher - just samples and prints
# No browsers, no automation, no hanging

echo "🔍 Watching GPU utilization..."
echo "Open http://localhost:8888/standalone-gpu-test.html in your browser"
echo "Toggle effects and watch the GPU % change"
echo ""
echo "Press Ctrl+C to stop"
echo ""

while true; do
    gpu=$(ioreg -r -d 1 -c IOAccelerator 2>/dev/null | grep "Device Utilization %" | head -1 | grep -o '"Device Utilization %"=[0-9]*' | cut -d= -f2)

    if [ -z "$gpu" ]; then
        gpu=0
    fi

    # Clear line and print
    printf "\r🎮 GPU: %3d%%   " "$gpu"

    sleep 0.5
done
