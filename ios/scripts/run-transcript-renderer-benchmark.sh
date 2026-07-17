#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

renderer="${IOS_TRANSCRIPT_BENCHMARK_RENDERER:-snapshot-webkit}"
temperature="${IOS_TRANSCRIPT_BENCHMARK_TEMPERATURE:-cold}"
debugger="${IOS_TRANSCRIPT_BENCHMARK_DEBUGGER:-none}"
build_mode="${IOS_TRANSCRIPT_BENCHMARK_BUILD_MODE:-debug}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
xcode_version="$(xcodebuild -version | paste -sd ' ' -)"
output_dir="${IOS_TRANSCRIPT_BENCHMARK_OUTPUT:-$ROOT/artifacts/ios-transcript-benchmark/$timestamp-$renderer}"
derived_data="${IOS_TRANSCRIPT_BENCHMARK_DERIVED_DATA:-$HOME/Library/Developer/Xcode/DerivedData/LonghouseIOS-TranscriptBenchmark}"
destination="${IOS_TRANSCRIPT_BENCHMARK_DESTINATION:-}"
build_settings=()
case "$build_mode" in
  debug)
    build_label="Debug"
    ;;
  optimized)
    build_label="Debug-Optimized"
    build_settings+=(
      SWIFT_OPTIMIZATION_LEVEL=-O
      GCC_OPTIMIZATION_LEVEL=s
      DEBUG_INFORMATION_FORMAT=dwarf
    )
    ;;
  *)
    echo "Unsupported IOS_TRANSCRIPT_BENCHMARK_BUILD_MODE: $build_mode" >&2
    exit 2
    ;;
esac

if [[ -z "$destination" ]]; then
  destination="$(python3 scripts/ci/select_ios_simulator.py ios/XcodeHarness/LonghouseIOS.xcodeproj LonghouseChatStress)"
fi

mkdir -p "$output_dir" "$derived_data"
result_bundle="$output_dir/benchmark.xcresult"
console_log="$output_dir/console.log"
rm -rf "$result_bundle"

echo "Transcript renderer benchmark"
echo "  renderer:    $renderer"
echo "  temperature: $temperature"
echo "  build:       $build_label"
echo "  destination: $destination"
echo "  artifacts:   $output_dir"

set +e
IOS_TRANSCRIPT_BENCHMARK_RENDERER="$renderer" \
IOS_TRANSCRIPT_BENCHMARK_TEMPERATURE="$temperature" \
IOS_TRANSCRIPT_BENCHMARK_DEBUGGER="$debugger" \
xcodebuild \
  -project ios/XcodeHarness/LonghouseIOS.xcodeproj \
  -scheme LonghouseChatStress \
  -destination "$destination" \
  -derivedDataPath "$derived_data" \
  -resultBundlePath "$result_bundle" \
  -only-testing:LonghouseChatStressUITests/TranscriptRendererBenchmarkUITests/testAgentCoreV1 \
  "${build_settings[@]}" \
  test 2>&1 | tee "$console_log"
test_status=${PIPESTATUS[0]}
set -e

if grep -q 'TRANSCRIPT_BENCHMARK_RESULT ' "$console_log"; then
  python3 ios/scripts/extract-transcript-benchmark-result.py \
    "$console_log" \
    "$output_dir/result.json" \
    --set "buildConfiguration=$build_label" \
    --set "collectedAtUTC=$timestamp" \
    --set "debugger=$debugger" \
    --set "destination=$destination" \
    --set "runTemperature=$temperature" \
    --set "xcodeVersion=$xcode_version"
fi

echo "Benchmark artifacts: $output_dir"
exit "$test_status"
