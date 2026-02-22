#!/bin/bash
set -e

# Decode SSH key from environment variable (avoids unreliable volume mounts)
mkdir -p ~/.ssh
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
