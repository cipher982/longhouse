#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DEPLOY_SSH_KEY:-}" ]]; then
  echo "DEPLOY_SSH_KEY is required" >&2
  exit 1
fi

CLIFFORD_HOST="${CLIFFORD_HOST:-clifford}"
ZERG_HOST="${ZERG_HOST:-}"

mkdir -p ~/.ssh
printf '%s\n' "$DEPLOY_SSH_KEY" > ~/.ssh/deploy_key
chmod 600 ~/.ssh/deploy_key

ssh-keyscan -H "$CLIFFORD_HOST" >> ~/.ssh/known_hosts 2>/dev/null || true
if [[ -n "$ZERG_HOST" ]]; then
  ssh-keyscan -H "$ZERG_HOST" >> ~/.ssh/known_hosts 2>/dev/null || true
fi

cat > ~/.ssh/config <<EOF
Host clifford
  HostName ${CLIFFORD_HOST}
  User drose
  IdentityFile ~/.ssh/deploy_key
  StrictHostKeyChecking accept-new
EOF

if [[ -n "$ZERG_HOST" ]]; then
  cat >> ~/.ssh/config <<EOF
Host zerg
  HostName ${ZERG_HOST}
  User zerg
  IdentityFile ~/.ssh/deploy_key
  StrictHostKeyChecking accept-new
EOF
fi
