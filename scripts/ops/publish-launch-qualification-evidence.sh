#!/usr/bin/env bash
set -euo pipefail

# Publish a complete, source-bound qualification tree to the cube ARC
# qualification lane. This is intentionally an operator-side transport: the
# measured report is the component that verifies artifact provenance against
# the source checkout before the hosted signer accepts it.

SOURCE_SHA="${1:-}"
INPUT_ROOT="${2:-}"
REMOTE_HOST="${LONGHOUSE_QUALIFICATION_HOST:-cube}"
REMOTE_ROOT="/var/lib/longhouse/qualification"

if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "usage: $0 <lowercase-source-sha> <evidence-root>" >&2
    exit 2
fi
if [[ -z "$INPUT_ROOT" || ! -d "$INPUT_ROOT" ]]; then
    echo "evidence root must be an existing directory" >&2
    exit 2
fi
[[ "$REMOTE_HOST" =~ ^[A-Za-z0-9._:-]+$ ]] || {
    echo "qualification host contains unsupported characters" >&2
    exit 2
}
INPUT_ROOT="$(cd "$INPUT_ROOT" && pwd -P)"

for relative in matrix native-health provider-harness dogfood/episodes dogfood/challenges; do
    path="$INPUT_ROOT/$relative"
    [[ -d "$path" && ! -L "$path" ]] || {
        echo "missing or symlinked evidence directory: $path" >&2
        exit 1
    }
    find -P "$path" -type f -print -quit | grep -q . || {
        echo "evidence directory is empty: $path" >&2
        exit 1
    }
done

if find -P "$INPUT_ROOT/matrix" "$INPUT_ROOT/native-health" \
    "$INPUT_ROOT/provider-harness" "$INPUT_ROOT/dogfood/episodes" \
    "$INPUT_ROOT/dogfood/challenges" -type l -print -quit | grep -q .; then
    echo "evidence contains a symlink" >&2
    exit 1
fi

while IFS= read -r -d '' path; do
    [[ ! -L "$path" && -f "$path" ]] || {
        echo "evidence contains a symlink or non-regular file: $path" >&2
        exit 1
    }
    relative="${path#"$INPUT_ROOT"/}"
    [[ "$relative" != *$'\n'* && "$relative" != *$'\r'* ]] || {
        echo "evidence path contains a newline: $relative" >&2
        exit 1
    }
done < <(find -P "$INPUT_ROOT/matrix" "$INPUT_ROOT/native-health" \
    "$INPUT_ROOT/provider-harness" "$INPUT_ROOT/dogfood/episodes" \
    "$INPUT_ROOT/dogfood/challenges" -type f -print0)

target="$REMOTE_ROOT/$SOURCE_SHA"
lock="$REMOTE_ROOT/.publish.$SOURCE_SHA.lock"
staging=""

cleanup() {
    if [[ -n "$staging" ]]; then
        ssh "$REMOTE_HOST" "sudo rm -rf -- '$staging'" || true
    fi
    ssh "$REMOTE_HOST" "sudo rmdir -- '$lock'" || true
}
trap cleanup EXIT

staging="$(ssh "$REMOTE_HOST" "
    set -eu
    sudo install -d -o root -g root -m 0755 '$REMOTE_ROOT'
    sudo mkdir '$lock'
    sudo mktemp -d '$REMOTE_ROOT/.incoming.$SOURCE_SHA.XXXXXX'
")"

if ! tar -C "$INPUT_ROOT" -cf - matrix native-health provider-harness \
    dogfood/episodes dogfood/challenges \
    | ssh "$REMOTE_HOST" "sudo tar -C '$staging' -xf -"; then
    exit 1
fi

ssh "$REMOTE_HOST" "
    set -eu
    for relative in matrix native-health provider-harness dogfood/episodes dogfood/challenges; do
        path='$staging'/\$relative
        test -d \"\$path\"
        test \"\$(find -P \"\$path\" -type f | wc -l)\" -gt 0
        test -z \"\$(find -P \"\$path\" -type l -print -quit)\"
    done
    sudo find '$staging' -type d -exec chmod 0755 {} +
    sudo find '$staging' -type f -exec chmod 0644 {} +
    test ! -e '$target'
    sudo mv -- '$staging' '$target'
"
staging=""
trap - EXIT

echo "published qualification evidence for $SOURCE_SHA to $REMOTE_HOST:$target"
