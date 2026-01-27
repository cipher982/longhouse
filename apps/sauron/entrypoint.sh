#!/bin/bash
set -e

# Always create .ssh directory
mkdir -p ~/.ssh

# Normalize DB URL env vars (jobs expect LIFE_HUB_DB_URL, core expects DATABASE_URL)
if [ -z "$DATABASE_URL" ] && [ -n "$LIFE_HUB_DB_URL" ]; then
    export DATABASE_URL="$LIFE_HUB_DB_URL"
fi
if [ -z "$LIFE_HUB_DB_URL" ] && [ -n "$DATABASE_URL" ]; then
    export LIFE_HUB_DB_URL="$DATABASE_URL"
fi

# Decode SSH key from environment variable (avoids unreliable Coolify mounts)
if [ -n "$SSH_PRIVATE_KEY_B64" ]; then
    echo "$SSH_PRIVATE_KEY_B64" | base64 -d > ~/.ssh/id_rsa
    chmod 600 ~/.ssh/id_rsa
    echo "SSH key installed from environment variable"
else
    echo "WARNING: SSH_PRIVATE_KEY_B64 not set - SSH operations will fail"
fi

# Disable strict host key checking for Tailscale/dynamic IPs
cat > ~/.ssh/config << 'EOF'
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
EOF
chmod 600 ~/.ssh/config

exec "$@"
