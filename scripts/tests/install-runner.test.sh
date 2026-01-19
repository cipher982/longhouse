#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../install-runner.sh"

RUNNER_VERSION="v0.1.0"
tag="$(resolve_runner_release_tag)"
if [[ "$tag" != "v0.1.0" ]]; then
  echo "Expected tag to be v0.1.0, got $tag"
  exit 1
fi

expected_url="https://github.com/daverosedavis/zerg/archive/refs/tags/v0.1.0.tar.gz"
archive_url="$(build_runner_archive_url "$tag")"
if [[ "$archive_url" != "$expected_url" ]]; then
  echo "Expected archive URL $expected_url, got $archive_url"
  exit 1
fi

if (RUNNER_VERSION="main"; validate_runner_version "$RUNNER_VERSION"); then
  echo "Expected validate_runner_version to reject main"
  exit 1
fi

echo "install-runner script tests passed"
