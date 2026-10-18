#!/usr/bin/env bash
# Run a command under the wisp bench lock when this job is on the wisp runner; run it as is
# anywhere else. wisp has 8 GB and one simulator, so a release build, an archive or a
# simulator run waits for whatever else holds /tmp/longhouse-bench.lock (bench.sh and the
# native-test bootstrap take the same lock).
#
#   scripts/ci/with-bench-lock.sh <command...>
set -euo pipefail

if [[ "${RUNNER_ENVIRONMENT:-}" == "self-hosted" && "${RUNNER_NAME:-}" == "wisp" ]]; then
  exec lockf -k /tmp/longhouse-bench.lock "$@"
fi
exec "$@"
