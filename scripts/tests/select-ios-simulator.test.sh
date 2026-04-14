#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SELECTOR="$ROOT_DIR/scripts/ci/select_ios_simulator.py"

run_case() {
  local case_name="$1"
  local expected="$2"
  local xcodebuild_output="$3"
  local devices_json="${4:-}"
  local runtimes_json="${5:-}"
  local created_udid="${6:-}"
  local expected_create_args="${7:-}"

  if [[ -z "$devices_json" ]]; then
    devices_json='{"devices":{}}'
  fi
  if [[ -z "$runtimes_json" ]]; then
    runtimes_json='{"runtimes":[]}'
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' RETURN

  mkdir -p "$tmp_dir/bin"
  printf '%s\n' "$xcodebuild_output" > "$tmp_dir/xcodebuild.txt"
  printf '%s\n' "$devices_json" > "$tmp_dir/devices.json"
  printf '%s\n' "$runtimes_json" > "$tmp_dir/runtimes.json"
  : > "$tmp_dir/create.log"

  cat > "$tmp_dir/bin/xcodebuild" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cat "$FAKE_XCODEBUILD_OUTPUT_FILE"
EOF
  chmod +x "$tmp_dir/bin/xcodebuild"

  cat > "$tmp_dir/bin/xcrun" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "simctl list devices available -j")
    cat "$FAKE_SIMCTL_DEVICES_FILE"
    ;;
  "simctl list runtimes available -j")
    cat "$FAKE_SIMCTL_RUNTIMES_FILE"
    ;;
  "simctl create "*)
    printf '%s\n' "$*" > "$FAKE_SIMCTL_CREATE_LOG"
    printf '%s\n' "$FAKE_CREATED_UDID"
    ;;
  *)
    echo "unexpected xcrun invocation: $*" >&2
    exit 1
    ;;
esac
EOF
  chmod +x "$tmp_dir/bin/xcrun"

  local output
  output="$(
    PATH="$tmp_dir/bin:$PATH" \
    FAKE_XCODEBUILD_OUTPUT_FILE="$tmp_dir/xcodebuild.txt" \
    FAKE_SIMCTL_DEVICES_FILE="$tmp_dir/devices.json" \
    FAKE_SIMCTL_RUNTIMES_FILE="$tmp_dir/runtimes.json" \
    FAKE_SIMCTL_CREATE_LOG="$tmp_dir/create.log" \
    FAKE_CREATED_UDID="$created_udid" \
    python3 "$SELECTOR" fake.xcodeproj FakeScheme
  )"

  if [[ "$output" != "$expected" ]]; then
    echo "$case_name: expected '$expected' but got '$output'" >&2
    exit 1
  fi

  if [[ -n "$expected_create_args" ]]; then
    local create_args
    create_args="$(<"$tmp_dir/create.log")"
    if [[ "$create_args" != "$expected_create_args" ]]; then
      echo "$case_name: expected create args '$expected_create_args' but got '$create_args'" >&2
      exit 1
    fi
  fi

  rm -rf "$tmp_dir"
  trap - RETURN
}

run_case \
  "prefers concrete iPhone destinations" \
  "platform=iOS Simulator,id=IPHONE-UDID" \
  $'\nAvailable destinations for the "Longhouse" scheme:\n\t{ platform:iOS Simulator, id:IPAD-UDID, OS:18.0, name:iPad Air }\n\t{ platform:iOS Simulator, id:IPHONE-UDID, OS:18.1, name:iPhone 17 }\n'

run_case \
  "falls back to any concrete iOS simulator when no iPhone exists" \
  "platform=iOS Simulator,id=IPAD-ONLY-UDID" \
  $'\nAvailable destinations for the "Longhouse" scheme:\n\t{ platform:iOS Simulator, id:dvtdevice-DVTiOSDeviceSimulatorPlaceholder-iphonesimulator:placeholder, name:Any iOS Simulator Device }\n\t{ platform:iOS Simulator, id:IPAD-ONLY-UDID, OS:18.2, name:iPad Pro }\n'

run_case \
  "creates a simulator when none are pre-created" \
  "platform=iOS Simulator,id=CREATED-UDID" \
  $'\nAvailable destinations for the "Longhouse" scheme:\n\t{ platform:iOS Simulator, id:dvtdevice-DVTiOSDeviceSimulatorPlaceholder-iphonesimulator:placeholder, name:Any iOS Simulator Device }\n' \
  '{"devices":{"com.apple.CoreSimulator.SimRuntime.iOS-18-2":[]}}' \
  '{"runtimes":[{"identifier":"com.apple.CoreSimulator.SimRuntime.iOS-18-2","name":"iOS 18.2","version":"18.2","isAvailable":true,"supportedDeviceTypes":[{"name":"iPad Pro","identifier":"com.apple.CoreSimulator.SimDeviceType.iPad-Pro","productFamily":"iPad"},{"name":"iPhone 17","identifier":"com.apple.CoreSimulator.SimDeviceType.iPhone-17","productFamily":"iPhone"}]}]}' \
  "CREATED-UDID" \
  "simctl create Longhouse CI iPhone 17 com.apple.CoreSimulator.SimDeviceType.iPhone-17 com.apple.CoreSimulator.SimRuntime.iOS-18-2"

echo "select_ios_simulator helper tests passed"
