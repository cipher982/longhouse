#!/usr/bin/env bash
# Focused legacy-pair conversion regression for scripts/install.sh.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
SOURCE_DIR="$TEST_ROOT/source"
BIN_DIR="$TEST_ROOT/bin"
shopt -s nullglob

cleanup() {
    local status=$?
    rm -rf "$TEST_ROOT"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$SOURCE_DIR" "$BIN_DIR"

write_fake_facade() {
    local path="$1" identity="$2"
    cat > "$path" <<EOF
#!/usr/bin/env bash
expected_identity="$identity"
case "\${1:-}" in
    verify-pair)
        engine_path="\${LONGHOUSE_ENGINE_BIN:-\$(dirname "\$0")/longhouse-engine}"
        [[ -x "\$engine_path" ]]
        [[ "\$("\$engine_path" build-identity)" == "\$expected_identity" ]]
        ;;
    build-identity)
        printf '%s\\n' "\$expected_identity"
        ;;
    *)
        exit 0
        ;;
esac
EOF
    chmod 755 "$path"
}

write_fake_engine() {
    local path="$1" identity="$2"
    cat > "$path" <<EOF
#!/usr/bin/env bash
case "\${1:-}" in
    build-identity)
        printf '%s\\n' "$identity"
        ;;
    *)
        exit 0
        ;;
esac
EOF
    chmod 755 "$path"
}

write_fake_pair() {
    local directory="$1" identity="$2"
    mkdir -p "$directory"
    write_fake_facade "$directory/longhouse" "$identity"
    write_fake_engine "$directory/longhouse-engine" "$identity"
}

write_mv_probe() {
    cat > "$BIN_DIR/mv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "-T" ]]; then
    destination="${3:-}"
else
    destination="${2:-}"
fi

identity() {
    local path="$1"
    if [[ -x "$path" ]]; then
        "$path" build-identity 2>/dev/null || true
    fi
}

assert_state() {
    local expected_current="$1" expected_public="$2"
    local current_identity="" facade_identity="" engine_identity=""
    current_identity="$(identity "$LONGHOUSE_TEST_CURRENT/longhouse")"
    facade_identity="$(identity "$LONGHOUSE_TEST_FACADE")"
    engine_identity="$(identity "$LONGHOUSE_TEST_ENGINE")"
    [[ "$current_identity" == "$expected_current" ]] || exit 98
    [[ "$facade_identity" == "$expected_public" ]] || exit 98
    [[ "$engine_identity" == "$expected_public" ]] || exit 98
    printf '%s current=%s facade=%s engine=%s\n' "${LONGHOUSE_TEST_PHASE:-seam}" "$current_identity" "$facade_identity" "$engine_identity" >> "$LONGHOUSE_TEST_SEAM_LOG"
}

if [[ "$destination" == "$LONGHOUSE_TEST_CURRENT" ]]; then
    rename_count="$(<"$LONGHOUSE_TEST_CURRENT_RENAMES")"
    rename_count=$((rename_count + 1))
    printf '%s\n' "$rename_count" > "$LONGHOUSE_TEST_CURRENT_RENAMES"
    if [[ "$rename_count" == "1" ]]; then
        /bin/mv "$@"
        LONGHOUSE_TEST_PHASE=predecessor-current assert_state OLD OLD
    elif [[ "$rename_count" == "2" ]]; then
        if [[ -e "$LONGHOUSE_TEST_FAULT_MARKER" && "$LONGHOUSE_TEST_FAIL_SEAM" != "current" ]]; then
            /bin/mv "$@"
            LONGHOUSE_TEST_PHASE=rollback-current assert_state FORMER OLD
        else
            LONGHOUSE_TEST_PHASE=final-current-before assert_state OLD OLD
            if [[ "$LONGHOUSE_TEST_FAIL_SEAM" == "current" && ! -e "$LONGHOUSE_TEST_FAULT_MARKER" ]]; then
                : > "$LONGHOUSE_TEST_FAULT_MARKER"
                exit 97
            fi
            /bin/mv "$@"
            LONGHOUSE_TEST_PHASE=final-current-after assert_state NEW NEW
            if [[ "$LONGHOUSE_TEST_FAIL_SEAM" == "published" && ! -e "$LONGHOUSE_TEST_FAULT_MARKER" ]]; then
                : > "$LONGHOUSE_TEST_FAULT_MARKER"
                exit 97
            fi
        fi
    else
        /bin/mv "$@"
        if [[ "$LONGHOUSE_TEST_FAIL_SEAM" == "published" && "$rename_count" == "3" ]]; then
            LONGHOUSE_TEST_PHASE=rollback-predecessor assert_state OLD OLD
        else
            LONGHOUSE_TEST_PHASE=rollback-current assert_state FORMER OLD
        fi
    fi
    exit 0
fi

if [[ "$destination" == "$LONGHOUSE_TEST_FACADE" || "$destination" == "$LONGHOUSE_TEST_ENGINE" ]]; then
    LONGHOUSE_TEST_PHASE=public-before assert_state OLD OLD
    if [[ "$destination" == "$LONGHOUSE_TEST_FACADE" && "$LONGHOUSE_TEST_FAIL_SEAM" == "facade" && ! -e "$LONGHOUSE_TEST_FAULT_MARKER" ]]; then
        : > "$LONGHOUSE_TEST_FAULT_MARKER"
        exit 97
    fi
    if [[ "$destination" == "$LONGHOUSE_TEST_ENGINE" && "$LONGHOUSE_TEST_FAIL_SEAM" == "engine" && ! -e "$LONGHOUSE_TEST_FAULT_MARKER" ]]; then
        : > "$LONGHOUSE_TEST_FAULT_MARKER"
        exit 97
    fi
    /bin/mv "$@"
    LONGHOUSE_TEST_PHASE=public-after assert_state OLD OLD
    exit 0
fi

exec /bin/mv "$@"
EOF
    chmod 755 "$BIN_DIR/mv"
}

assert_identity() {
    local path="$1" expected="$2"
    [[ "$("$path" build-identity)" == "$expected" ]]
}

assert_no_temporary_paths() {
    local home_dir="$1" leftovers=()
    local native_root="$home_dir/.local/share/longhouse"
    leftovers+=("$native_root"/.current-*)
    leftovers+=("$native_root"/.previous-*)
    leftovers+=("$native_root"/.predecessor-current-*)
    leftovers+=("$home_dir/.local/bin"/.longhouse-native-*)
    leftovers+=("$home_dir/.local/bin"/.longhouse-engine-native-*)
    [[ "${#leftovers[@]}" == "0" ]]
}

run_installer() {
    local home_dir="$1" fail_seam="$2" log_path="$3"
    local native_root="$home_dir/.local/share/longhouse"
    env \
        HOME="$home_dir" \
        LONGHOUSE_HOME="$home_dir/.longhouse" \
        LONGHOUSE_NATIVE_BIN_DIR="$SOURCE_DIR" \
        LONGHOUSE_TELEMETRY=0 \
        LONGHOUSE_TEST_CURRENT="$native_root/current" \
        LONGHOUSE_TEST_CURRENT_RENAMES="$home_dir/current-renames" \
        LONGHOUSE_TEST_ENGINE="$home_dir/.local/bin/longhouse-engine" \
        LONGHOUSE_TEST_FACADE="$home_dir/.local/bin/longhouse" \
        LONGHOUSE_TEST_FAIL_SEAM="$fail_seam" \
        LONGHOUSE_TEST_FAULT_MARKER="$home_dir/fault-seen" \
        LONGHOUSE_TEST_SEAM_LOG="$home_dir/seams.log" \
        PATH="$BIN_DIR:/usr/bin:/bin:/usr/sbin:/sbin" \
        SHELL=/bin/bash \
        bash "$ROOT_DIR/scripts/install.sh" >"$log_path" 2>&1
}

prepare_home() {
    local home_dir="$1"
    local native_root="$home_dir/.local/share/longhouse"
    mkdir -p "$home_dir/.local/bin" "$native_root/releases"
    write_fake_pair "$home_dir/.local/bin" OLD
    write_fake_pair "$native_root/releases/former" FORMER
    ln -s releases/former "$native_root/current"
    : > "$home_dir/current-renames"
}

run_failure_scenario() {
    local fail_seam="$1"
    local home_dir="$TEST_ROOT/home-$fail_seam" log_path="$TEST_ROOT/$fail_seam.log"
    local native_root="$home_dir/.local/share/longhouse"
    local status release_entries=()
    prepare_home "$home_dir"

    set +e
    run_installer "$home_dir" "$fail_seam" "$log_path"
    status=$?
    set -e
    [[ "$status" != "0" ]] || { printf '%s\n' "fault injection unexpectedly passed: $fail_seam" >&2; exit 1; }
    [[ -e "$home_dir/fault-seen" ]] || { printf '%s\n' "fault seam was not reached: $fail_seam" >&2; exit 1; }

    assert_identity "$home_dir/.local/bin/longhouse" OLD
    assert_identity "$home_dir/.local/bin/longhouse-engine" OLD
    [[ ! -L "$home_dir/.local/bin/longhouse" ]]
    [[ ! -L "$home_dir/.local/bin/longhouse-engine" ]]
    [[ "$(readlink "$native_root/current")" == "releases/former" ]]
    release_entries=("$native_root/releases"/*)
    [[ "${#release_entries[@]}" == "1" && "${release_entries[0]}" == "$native_root/releases/former" ]]
    assert_no_temporary_paths "$home_dir"
}

run_success_scenario() {
    local home_dir="$TEST_ROOT/home-success" log_path="$TEST_ROOT/success.log"
    local native_root="$home_dir/.local/share/longhouse" current_target candidate_dir
    local release_entries=() legacy_entries=() status
    prepare_home "$home_dir"

    set +e
    run_installer "$home_dir" "" "$log_path"
    status=$?
    set -e
    [[ "$status" == "0" ]] || { cat "$log_path" >&2; exit 1; }

    [[ -L "$home_dir/.local/bin/longhouse" ]]
    [[ -L "$home_dir/.local/bin/longhouse-engine" ]]
    [[ "$(readlink "$home_dir/.local/bin/longhouse")" == "../share/longhouse/current/longhouse" ]]
    [[ "$(readlink "$home_dir/.local/bin/longhouse-engine")" == "../share/longhouse/current/longhouse-engine" ]]
    current_target="$(readlink "$native_root/current")"
    [[ "$current_target" == releases/* && "$current_target" != "releases/former" ]]
    candidate_dir="$native_root/$current_target"
    assert_identity "$candidate_dir/longhouse" NEW
    assert_identity "$candidate_dir/longhouse-engine" NEW
    [[ -d "$native_root/releases/former" ]]
    release_entries=("$native_root/releases"/*)
    [[ "${#release_entries[@]}" == "3" ]]
    legacy_entries=("$native_root/releases"/legacy-*)
    [[ "${#legacy_entries[@]}" == "1" ]]
    "${legacy_entries[0]}/longhouse" verify-pair
    assert_identity "${legacy_entries[0]}/longhouse" OLD
    assert_identity "${legacy_entries[0]}/longhouse-engine" OLD
    assert_no_temporary_paths "$home_dir"
}

write_fake_pair "$SOURCE_DIR" NEW
write_mv_probe
run_failure_scenario facade
run_failure_scenario engine
run_failure_scenario current
run_failure_scenario published
run_success_scenario
printf '%s\n' "native installer legacy conversion boundary regression passed"
