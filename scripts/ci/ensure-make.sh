#!/usr/bin/env bash
set -euo pipefail

if command -v make >/dev/null 2>&1; then
  echo "make available: $(command -v make)"
  exit 0
fi

echo "::group::Install make"

if [[ "$(uname -s)" == "Linux" ]] && command -v apt-get >/dev/null 2>&1; then
  apt_opts=(
    -o Acquire::ForceIPv4=true
    -o Acquire::Retries=5
  )

  for attempt in 1 2 3; do
    echo "Installing make with apt, attempt ${attempt}/3"
    sudo apt-get "${apt_opts[@]}" update || true
    if sudo apt-get "${apt_opts[@]}" install -y --no-install-recommends --fix-missing make; then
      break
    fi
    if [[ "${attempt}" == "3" ]]; then
      echo "apt could not install make after ${attempt} attempts" >&2
      exit 1
    fi
    sleep $((attempt * 5))
  done
elif [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
  brew install make
else
  echo "make is missing and no supported package manager was found" >&2
  exit 1
fi

echo "::endgroup::"

if ! command -v make >/dev/null 2>&1; then
  echo "make is still missing after installation" >&2
  exit 1
fi

echo "make available: $(command -v make)"
