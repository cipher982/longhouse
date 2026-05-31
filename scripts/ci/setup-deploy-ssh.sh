#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DEPLOY_SSH_KEY:-}" ]]; then
  echo "DEPLOY_SSH_KEY is required" >&2
  exit 1
fi

# Deploy target host/user come from CI secrets/vars; defaults are neutral
# placeholders so the public config discloses no private infrastructure.
DEPLOY_HOST="${DEPLOY_HOST:-deploy-host}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
RUNTIME_HOST="${RUNTIME_HOST:-}"
RUNTIME_USER="${RUNTIME_USER:-deploy}"

mkdir -p ~/.ssh
printf '%s\n' "$DEPLOY_SSH_KEY" > ~/.ssh/deploy_key
chmod 600 ~/.ssh/deploy_key

ssh-keyscan -H "$DEPLOY_HOST" >> ~/.ssh/known_hosts 2>/dev/null || true
if [[ -n "$RUNTIME_HOST" ]]; then
  ssh-keyscan -H "$RUNTIME_HOST" >> ~/.ssh/known_hosts 2>/dev/null || true
fi

cat > ~/.ssh/config <<EOF
Host deploy-host
  HostName ${DEPLOY_HOST}
  User ${DEPLOY_USER}
  IdentityFile ~/.ssh/deploy_key
  StrictHostKeyChecking accept-new
EOF

if [[ -n "$RUNTIME_HOST" ]]; then
  cat >> ~/.ssh/config <<EOF
Host runtime-host
  HostName ${RUNTIME_HOST}
  User ${RUNTIME_USER}
  IdentityFile ~/.ssh/deploy_key
  StrictHostKeyChecking accept-new
EOF
fi
