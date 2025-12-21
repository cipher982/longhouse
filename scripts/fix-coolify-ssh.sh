#!/usr/bin/env bash
#
# Fix Coolify SSH Multiplexing Contention Issues
#
# This script applies recommended SSH configuration to prevent exit code 255 failures
# caused by health check and deployment contention on multiplexed SSH connections.
#
# Usage: ./fix-coolify-ssh.sh [server] [--dry-run]
#   server: SSH host running Coolify (default: clifford)
#   --dry-run: Show what would be changed without applying
#
# Root Cause: ServerConnectionCheckJob health checks refresh mux connections during deploys
# Fix: Disable health check mux usage, increase timeouts
# See: https://github.com/coollabsio/coolify/issues/6736

set -euo pipefail

SERVER="${1:-clifford}"
DRY_RUN=false

if [[ "${2:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

CONTAINER="coolify"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_header() {
    echo -e "${BLUE}## $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

echo "========================================"
echo "Coolify SSH Multiplexing Fix"
echo "Server: $SERVER"
if [[ "$DRY_RUN" == "true" ]]; then
    echo "Mode: DRY RUN (no changes will be made)"
fi
echo "========================================"
echo

# 1. Backup current configuration
print_header "1. Backup Current Configuration"
BACKUP_FILE="/tmp/coolify-env-backup-$(date +%Y%m%d-%H%M%S).env"
echo "Creating backup of current .env..."

if [[ "$DRY_RUN" == "false" ]]; then
    ssh "$SERVER" "docker exec $CONTAINER cat /var/www/html/.env 2>/dev/null" > "$BACKUP_FILE" || {
        print_warning "Could not backup .env file (may not exist yet)"
        touch "$BACKUP_FILE"
    }
    print_success "Backup saved to: $BACKUP_FILE"
else
    echo "Would save backup to: $BACKUP_FILE"
fi
echo

# 2. Recommended SSH configuration
print_header "2. Recommended SSH Configuration"
echo "Applying the following settings:"
echo
echo "  SSH_MUX_HEALTH_CHECK_ENABLED=false"
echo "    Defense-in-depth (health checks already use disableMux=true)"
echo "    Ensures health checks never interfere with deployments"
echo
echo "  SSH_MUX_PERSIST_TIME=7200"
echo "    Keeps mux connections alive for 2 hours (up from 1 hour default)"
echo "    Reduces connection churn and re-establishment overhead"
echo
echo "  SSH_MUX_MAX_AGE=3600"
echo "    Refreshes connections after 1 hour (up from 30 min default)"
echo "    Reduces forced refreshes during long deployments"
echo "    CRITICAL: 30 min is too aggressive for typical deployment times"
echo
echo "  SSH_MAX_RETRIES=5"
echo "    Retries SSH operations 5 times on failure (up from 3)"
echo "    Provides more resilience against transient contention"
echo

# 3. Apply configuration
print_header "3. Applying Configuration"

if [[ "$DRY_RUN" == "true" ]]; then
    print_warning "DRY RUN: Would add/update these variables in .env"
    echo
    echo "Variables to set:"
    echo "  SSH_MUX_HEALTH_CHECK_ENABLED=false"
    echo "  SSH_MUX_PERSIST_TIME=7200"
    echo "  SSH_MUX_MAX_AGE=3600"
    echo "  SSH_MAX_RETRIES=5"
else
    # Create a script to run on the host (file is mounted from /data/coolify/source/.env)
    CONFIG_SCRIPT=$(cat <<'EOF'
#!/bin/bash
ENV_FILE="/data/coolify/source/.env"

# Function to set or update env var
set_env_var() {
    local key="$1"
    local value="$2"

    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        # Update existing
        sed -i "s/^${key}=.*/${key}=${value}/" "$ENV_FILE"
        echo "Updated: ${key}=${value}"
    else
        # Add new
        echo "${key}=${value}" >> "$ENV_FILE"
        echo "Added: ${key}=${value}"
    fi
}

# Apply settings
set_env_var "SSH_MUX_HEALTH_CHECK_ENABLED" "false"
set_env_var "SSH_MUX_PERSIST_TIME" "7200"
set_env_var "SSH_MUX_MAX_AGE" "3600"
set_env_var "SSH_MAX_RETRIES" "5"

echo "Configuration applied successfully"
EOF
)

    echo "Updating .env file on host..."
    ssh "$SERVER" "sudo bash -c '$CONFIG_SCRIPT'"
    print_success "Configuration applied"
fi
echo

# 4. Restart Coolify to apply changes
print_header "4. Restart Coolify"
echo "Coolify needs to be restarted for env changes to take effect"
echo

if [[ "$DRY_RUN" == "true" ]]; then
    print_warning "DRY RUN: Would restart Coolify container"
else
    echo -n "Restart Coolify now? [y/N] "
    read -r response
    if [[ "$response" =~ ^[Yy]$ ]]; then
        echo "Restarting Coolify container..."
        ssh "$SERVER" "docker restart $CONTAINER"

        echo "Waiting for Coolify to start..."
        sleep 10

        # Check if it's running
        STATUS=$(ssh "$SERVER" "docker inspect -f '{{.State.Status}}' $CONTAINER 2>/dev/null" || echo "unknown")
        if [[ "$STATUS" == "running" ]]; then
            print_success "Coolify restarted successfully"
        else
            print_error "Coolify may not have started correctly (status: $STATUS)"
            echo "Check logs: ssh $SERVER 'docker logs $CONTAINER'"
        fi
    else
        print_warning "Restart skipped. Changes will not take effect until Coolify restarts."
        echo "Restart manually with: ssh $SERVER 'docker restart $CONTAINER'"
    fi
fi
echo

# 5. Verify configuration
print_header "5. Verification"

if [[ "$DRY_RUN" == "false" ]]; then
    echo "Checking applied configuration..."
    VERIFICATION=$(ssh "$SERVER" "docker exec $CONTAINER cat /var/www/html/.env 2>/dev/null | grep -E 'SSH_MUX_HEALTH_CHECK_ENABLED|SSH_MUX_PERSIST_TIME|SSH_MUX_MAX_AGE|SSH_MAX_RETRIES' || echo ''")

    if [[ -n "$VERIFICATION" ]]; then
        print_success "Configuration verified:"
        echo "$VERIFICATION" | sed 's/^/  /'
    else
        print_warning "Could not verify configuration (may need container restart)"
        echo "After restart, run: ./diagnose-coolify-ssh.sh $SERVER"
    fi
else
    echo "Skipped (dry run mode)"
fi
echo

# 6. Clean up old mux sockets (optional)
print_header "6. Cleanup Old Mux Sockets (Optional)"
echo "Old mux sockets may still exist and cause issues"
echo

if [[ "$DRY_RUN" == "false" ]]; then
    echo -n "Remove all existing mux sockets? [y/N] "
    read -r response
    if [[ "$response" =~ ^[Yy]$ ]]; then
        echo "Removing mux sockets..."
        ssh "$SERVER" "docker exec $CONTAINER find /var/www/html/storage/app/ssh/mux/ -name 'mux_*' -delete 2>/dev/null || true"
        print_success "Mux sockets removed. New connections will be created as needed."
    else
        echo "Skipped. Existing sockets will expire naturally."
    fi
else
    echo "Would remove mux sockets in /var/www/html/storage/app/ssh/mux/"
fi
echo

# Summary
print_header "Summary"
echo

if [[ "$DRY_RUN" == "false" ]]; then
    print_success "SSH configuration has been updated!"
    echo
    echo "Next steps:"
    echo "1. Monitor deployments for the next few hours"
    echo "2. Check for exit code 255 errors"
    echo "3. Run diagnostics: ./diagnose-coolify-ssh.sh $SERVER"
    echo
    echo "If issues persist:"
    echo "- Check docs/investigations/coolify-ssh-255.md"
    echo "- Consider upgrading Coolify to latest version"
    echo "- Review recent Coolify logs: ssh $SERVER 'docker logs $CONTAINER --tail 100'"
else
    print_warning "DRY RUN COMPLETE - No changes were made"
    echo
    echo "To apply these changes, run:"
    echo "  ./fix-coolify-ssh.sh $SERVER"
fi
echo
echo "Backup file: $BACKUP_FILE"
echo "Keep this backup in case you need to revert changes"
