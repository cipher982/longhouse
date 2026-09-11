#!/usr/bin/env bash
set -euo pipefail

# SQLite publishes the source archive and release metadata at sqlite.org. The
# SHA256 is pinned here so the release builder never compiles an unreviewed
# source tree.
readonly SQLITE_VERSION="3.51.3"
readonly SQLITE_ARCHIVE_VERSION="3510300"
readonly SQLITE_SOURCE_URL="https://www.sqlite.org/2026/sqlite-autoconf-${SQLITE_ARCHIVE_VERSION}.tar.gz"
readonly SQLITE_SOURCE_SHA256="81f5be397049b0cae1b167f2225af7646fc0f82e4a9b3c48c9ea3a533e21d77a"

usage() {
    printf 'Usage: %s --output PATH\n' "$0" >&2
}

output_path=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            output_path="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[[ -n "$output_path" ]] || { usage; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "tar is required" >&2; exit 1; }
command -v make >/dev/null 2>&1 || { echo "make is required" >&2; exit 1; }

build_root="$(mktemp -d "${TMPDIR:-/tmp}/longhouse-sqlite3.XXXXXX")"
trap 'rm -rf "$build_root"' EXIT

archive_path="$build_root/sqlite-autoconf-${SQLITE_ARCHIVE_VERSION}.tar.gz"
curl -fsSL "$SQLITE_SOURCE_URL" -o "$archive_path"

if command -v sha256sum >/dev/null 2>&1; then
    actual_sha256="$(sha256sum "$archive_path" | awk '{print $1}')"
else
    actual_sha256="$(shasum -a 256 "$archive_path" | awk '{print $1}')"
fi
if [[ "$actual_sha256" != "$SQLITE_SOURCE_SHA256" ]]; then
    echo "SQLite ${SQLITE_VERSION} source checksum mismatch" >&2
    exit 1
fi

tar -xzf "$archive_path" -C "$build_root"
source_dir="$build_root/sqlite-autoconf-${SQLITE_ARCHIVE_VERSION}"

(
    cd "$source_dir"
    CFLAGS="${CFLAGS:-} -DSQLITE_ENABLE_DBPAGE_VTAB -DSQLITE_ENABLE_RECOVER" \
    LDFLAGS="${LDFLAGS:-} -static" \
    ./configure \
        --disable-readline \
        --disable-shared \
        --enable-static
    make -j"${JOBS:-2}" sqlite3
)

mkdir -p "$(dirname "$output_path")"
install -m 0755 "$source_dir/sqlite3" "$output_path"
