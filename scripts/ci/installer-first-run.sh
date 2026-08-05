#!/usr/bin/env bash
#
# Compatibility entrypoint for the old first-run installer smoke name.
#
# The public installer now installs the native device CLI pair. Its canonical
# disposable smoke is native-installer-smoke.sh; the former Python CLI flow
# (doctor/onboard/serve) no longer describes the installed product surface.
# Keep this filename as a narrow compatibility wrapper for local callers while
# making the native test the only implementation of the installer contract.
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

if [[ "$#" -ne 0 ]]; then
  printf '%s\n' "installer-first-run.sh no longer accepts legacy options; use native-installer-smoke.sh" >&2
  exit 2
fi

exec "$ROOT_DIR/scripts/ci/native-installer-smoke.sh"
