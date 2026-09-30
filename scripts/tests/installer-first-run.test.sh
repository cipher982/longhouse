#!/usr/bin/env bash
# First-run regressions for scripts/install.sh found by the 2026-09-30 cold-install proof
# (control-plane docs/specs/first-users-gtm.md, "Phase 0 cold-install proof"):
#   1. the native pair is staged beside its destination, never in $TMPDIR (a noexec /tmp made
#      `verify-pair` fail with a bare "Permission denied");
#   2. `longhouse machine repair` exits 0 even when it reports the service failed, so the
#      installer read "Connected" on a box with no systemd user session;
#   3. a Mac account that cannot write /Applications is told so before anything downloads.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
SOURCE_DIR="$TEST_ROOT/source"
trap 'rm -rf "$TEST_ROOT"' EXIT
mkdir -p "$SOURCE_DIR"

# A native pair whose facade answers just enough of the device CLI for a first run.
cat > "$SOURCE_DIR/longhouse" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
    verify-pair) exit 0 ;;
    build-identity) echo "fake 0.0.0 (test)" ;;
    auth)
        mkdir -p "${LONGHOUSE_HOME:-$HOME/.longhouse}/machine"
        printf '{\n  "machine_name": "fake-box"\n}\n' > "${LONGHOUSE_HOME:-$HOME/.longhouse}/machine/state.json"
        ;;
    machine) echo "Longhouse repair: checking the local service and retained state."
             echo "${LONGHOUSE_TEST_REPAIR_VERDICT:?}"
             echo "Machine State"
             [[ -z "${LONGHOUSE_TEST_REPAIR_EXTRA:-}" ]] || echo "$LONGHOUSE_TEST_REPAIR_EXTRA" ;;
    *) exit 0 ;;
esac
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$SOURCE_DIR/longhouse-engine"
chmod 755 "$SOURCE_DIR/longhouse" "$SOURCE_DIR/longhouse-engine"

run_installer() {
    local name="$1"; shift
    local home_dir="$TEST_ROOT/home-$name"
    mkdir -p "$home_dir" "$TEST_ROOT/tmp-$name"
    set +e
    env \
        HOME="$home_dir" LONGHOUSE_HOME="$home_dir/.longhouse" LONGHOUSE_TELEMETRY=0 \
        TMPDIR="$TEST_ROOT/tmp-$name" PATH="/usr/bin:/bin:/usr/sbin:/sbin" SHELL=/bin/bash \
        "$@" \
        bash "$ROOT_DIR/scripts/install.sh" >"$TEST_ROOT/$name.log" 2>&1
    STATUS=$?
    set -e
}

fail() { printf 'FAIL: %s\n' "$1" >&2; sed -e 's/\x1b\[[0-9;]*m//g' "$TEST_ROOT/$2.log" >&2; exit 1; }

# 1. staging never touches $TMPDIR, and leaves nothing behind
run_installer staging LONGHOUSE_NATIVE_BIN_DIR="$SOURCE_DIR"
[[ "$STATUS" == 0 ]] || fail "installer failed with a local pair" staging
[[ -z "$(ls -A "$TEST_ROOT/tmp-staging")" ]] || fail "installer staged files in \$TMPDIR: $(ls -A "$TEST_ROOT/tmp-staging")" staging
leftovers=("$TEST_ROOT/home-staging/.local/share/longhouse"/stage.*)
[[ ! -e "${leftovers[0]}" ]] || fail "staging directory left behind: ${leftovers[0]}" staging

# 2a. a failed service start is an installer failure, not "Connected"
run_installer repair-failed LONGHOUSE_NATIVE_BIN_DIR="$SOURCE_DIR" LONGHOUSE_URL=http://127.0.0.1:1 \
    LONGHOUSE_DEVICE_TOKEN=test-token \
    LONGHOUSE_TEST_REPAIR_VERDICT="Longhouse failed to activate the Machine Agent service artifact (failed)"
[[ "$STATUS" != 0 ]] || fail "installer reported success although the Machine Agent service failed" repair-failed
grep -q "did not start" "$TEST_ROOT/repair-failed.log" || fail "no explanation for the failed service start" repair-failed
! grep -q "Connected as" "$TEST_ROOT/repair-failed.log" || fail "installer claimed Connected after a failed start" repair-failed

# 2b. a rejected repair (no machine state, unsupported platform, ...) is a failure too
run_installer repair-rejected LONGHOUSE_NATIVE_BIN_DIR="$SOURCE_DIR" LONGHOUSE_URL=http://127.0.0.1:1 \
    LONGHOUSE_DEVICE_TOKEN=test-token \
    LONGHOUSE_TEST_REPAIR_VERDICT="Longhouse needs complete machine state before native service repair can run (rejected_machine_state_incomplete)"
[[ "$STATUS" != 0 ]] || fail "installer reported success although repair was rejected" repair-rejected

# 2b'. a start that produced no fresh health evidence installs, but does not claim sessions will appear
run_installer repair-pending LONGHOUSE_NATIVE_BIN_DIR="$SOURCE_DIR" LONGHOUSE_URL=http://127.0.0.1:1 \
    LONGHOUSE_DEVICE_TOKEN=test-token \
    LONGHOUSE_TEST_REPAIR_VERDICT="Repair ran, but useful Machine Agent service is not yet verified (recovery_pending)"
[[ "$STATUS" == 0 ]] || fail "an unverified start must not fail the install" repair-pending
grep -q "has not confirmed it is healthy" "$TEST_ROOT/repair-pending.log" || fail "no warning for an unverified start" repair-pending
! grep -q "sessions will appear" "$TEST_ROOT/repair-pending.log" || fail "installer promised sessions after an unverified start" repair-pending
grep -q "First run longhouse local-health" "$TEST_ROOT/repair-pending.log" || fail "closing text does not send the user to local-health" repair-pending

# 2c. a recovered service still connects
run_installer repair-ok LONGHOUSE_NATIVE_BIN_DIR="$SOURCE_DIR" LONGHOUSE_URL=http://127.0.0.1:1 \
    LONGHOUSE_DEVICE_TOKEN=test-token \
    LONGHOUSE_TEST_REPAIR_VERDICT="The local Machine Agent is running again (service_recovered)"
[[ "$STATUS" == 0 ]] || fail "a recovered service must still install" repair-ok
grep -q "Connected as fake-box" "$TEST_ROOT/repair-ok.log" || fail "no Connected line after a recovered service" repair-ok

# 2d. only the verdict line decides: a later diagnostic ending in (failed) does not fail a recovered install
run_installer repair-noted LONGHOUSE_NATIVE_BIN_DIR="$SOURCE_DIR" LONGHOUSE_URL=http://127.0.0.1:1 \
    LONGHOUSE_DEVICE_TOKEN=test-token \
    LONGHOUSE_TEST_REPAIR_VERDICT="The local Machine Agent is running again (service_recovered)" \
    LONGHOUSE_TEST_REPAIR_EXTRA="Note: an earlier attempt (failed)"
[[ "$STATUS" == 0 ]] || fail "a diagnostic line failed a recovered install" repair-noted

# 3. an unwritable Applications dir stops the Mac install before any download
mkdir -p "$TEST_ROOT/shim"
cat > "$TEST_ROOT/shim/uname" <<'EOF'
#!/bin/sh
case "$1" in -s) echo Darwin ;; -m) echo arm64 ;; *) /usr/bin/uname "$@" ;; esac
EOF
chmod 755 "$TEST_ROOT/shim/uname"
: > "$TEST_ROOT/not-a-directory"
run_installer mac-readonly PATH="$TEST_ROOT/shim:/usr/bin:/bin" LONGHOUSE_MACOS_APP_INSTALL_DIR="$TEST_ROOT/not-a-directory/Applications"
[[ "$STATUS" != 0 ]] || fail "installer continued although Longhouse.app cannot be installed" mac-readonly
grep -q "cannot write to" "$TEST_ROOT/mac-readonly.log" || fail "no explanation for the unwritable Applications dir" mac-readonly
! grep -q "Downloading Longhouse" "$TEST_ROOT/mac-readonly.log" || fail "installer downloaded before checking the Applications dir" mac-readonly

echo "installer first-run: ok"
