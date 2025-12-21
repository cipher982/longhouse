#!/usr/bin/env bash
#
# Coolify SSH Multiplexing Diagnostics
#
# This script analyzes SSH multiplexing configuration and status on a Coolify instance
# to diagnose intermittent exit code 255 failures during deployments.
#
# Usage: ./diagnose-coolify-ssh.sh [server]
#   server: SSH host running Coolify (default: clifford)
#
# Root Cause: SSH multiplexing contention between deployments and health checks
# See: https://github.com/coollabsio/coolify/issues/6736
#      https://github.com/coollabsio/coolify/pull/7503

set -euo pipefail

SERVER="${1:-clifford}"
CONTAINER="coolify"

echo "========================================"
echo "Coolify SSH Multiplexing Diagnostics"
echo "Server: $SERVER"
echo "========================================"
echo

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

# 1. Check Coolify version
print_header "1. Coolify Version"
VERSION=$(ssh "$SERVER" "docker inspect $CONTAINER --format '{{.Config.Image}}'" 2>/dev/null || echo "unknown")
echo "Docker image: $VERSION"

# Try to get specific version
SPECIFIC_VERSION=$(ssh "$SERVER" "docker exec $CONTAINER cat /var/www/html/config/constants.php 2>/dev/null | grep \"'version' =>\" | head -1" || echo "")
if [[ -n "$SPECIFIC_VERSION" ]]; then
    echo "Config version: $SPECIFIC_VERSION"
    # Extract version number
    if [[ "$SPECIFIC_VERSION" =~ beta\.([0-9]+) ]]; then
        BETA_NUM="${BASH_REMATCH[1]}"
        if [[ "$BETA_NUM" -ge 458 ]]; then
            print_success "PR #7503 fix IS included (version >= beta.458)"
        else
            print_warning "PR #7503 fix NOT included (need >= beta.458, have beta.$BETA_NUM)"
            echo "          The disableMultiplexing parameter is not available"
        fi
    fi
else
    print_warning "Could not determine exact version"
fi
echo

# 2. Check SSH environment variables
print_header "2. SSH Configuration (Environment Variables)"
echo "Checking container environment and .env file..."

SSH_VARS=$(ssh "$SERVER" "docker exec $CONTAINER env 2>/dev/null | grep -E 'SSH_|MUX_' || echo ''")
if [[ -z "$SSH_VARS" ]]; then
    print_warning "No SSH/MUX environment variables set"
    echo "          Using Coolify defaults from config/constants.php:"
    echo "          - MUX_ENABLED: true"
    echo "          - SSH_MUX_PERSIST_TIME: 3600 (1 hour)"
    echo "          - SSH_MUX_HEALTH_CHECK_ENABLED: true"
    echo "          - SSH_MUX_HEALTH_CHECK_TIMEOUT: 5"
    echo "          - SSH_MUX_MAX_AGE: 1800 (30 minutes)"
    echo "          - SSH_MAX_RETRIES: 3"
else
    print_success "Found SSH/MUX environment variables:"
    echo "$SSH_VARS" | sed 's/^/          /'
fi
echo

# 3. Check mux socket files
print_header "3. SSH Multiplexing Sockets"
MUX_SOCKETS=$(ssh "$SERVER" "docker exec $CONTAINER ls -lh /var/www/html/storage/app/ssh/mux/ 2>/dev/null | tail -n +2 || echo ''")
if [[ -z "$MUX_SOCKETS" ]]; then
    print_warning "No mux socket files found"
    echo "          This is normal if no SSH connections are currently active"
else
    SOCKET_COUNT=$(echo "$MUX_SOCKETS" | wc -l | tr -d ' ')
    print_success "Found $SOCKET_COUNT active mux socket(s):"
    echo "$MUX_SOCKETS" | sed 's/^/          /'
    echo
    echo "Socket details:"
    while IFS= read -r line; do
        if [[ "$line" =~ (mux_[a-z0-9_]+) ]]; then
            SOCKET="${BASH_REMATCH[1]}"
            echo "  Socket: $SOCKET"
            # Get age of socket file
            AGE=$(ssh "$SERVER" "docker exec $CONTAINER stat -c '%Y' /var/www/html/storage/app/ssh/mux/$SOCKET 2>/dev/null || echo ''")
            if [[ -n "$AGE" ]]; then
                NOW=$(date +%s)
                SECONDS_OLD=$((NOW - AGE))
                MINUTES_OLD=$((SECONDS_OLD / 60))
                echo "    Age: ${MINUTES_OLD} minutes (${SECONDS_OLD}s)"
                if [[ $SECONDS_OLD -gt 1800 ]]; then
                    print_warning "    Socket is older than SSH_MUX_MAX_AGE (30 min)"
                fi
            fi
        fi
    done <<< "$MUX_SOCKETS"
fi
echo

# 4. Test SSH connectivity from Coolify container
print_header "4. SSH Connectivity Test"
echo "Testing SSH from Coolify container to remote servers..."

# Get list of servers from Coolify database
SERVERS=$(ssh "$SERVER" "docker exec $CONTAINER psql -U coolify -d coolify -t -c \"SELECT name, ip, user FROM servers WHERE deleted_at IS NULL;\" 2>/dev/null || echo ''")
if [[ -n "$SERVERS" ]]; then
    while IFS='|' read -r name ip user; do
        # Trim whitespace
        name=$(echo "$name" | xargs)
        ip=$(echo "$ip" | xargs)
        user=$(echo "$user" | xargs)

        if [[ -n "$name" && -n "$ip" ]]; then
            echo -n "  Testing $name ($user@$ip)... "
            TEST_RESULT=$(ssh "$SERVER" "docker exec $CONTAINER timeout 5 ssh -o ConnectTimeout=3 -o StrictHostKeyChecking=no ${user}@${ip} 'echo ok' 2>&1" || echo "failed")
            if [[ "$TEST_RESULT" == "ok" ]]; then
                print_success "OK"
            else
                print_error "FAILED"
                echo "    Error: $TEST_RESULT" | sed 's/^/    /'
            fi
        fi
    done <<< "$SERVERS"
else
    print_warning "Could not query servers from database"
fi
echo

# 5. Check recent SSH-related errors in logs
print_header "5. Recent SSH Errors (Last 50 Lines)"
ERRORS=$(ssh "$SERVER" "docker logs $CONTAINER 2>&1 | grep -i 'exit.*255\|ssh.*fail\|multiplex.*error' | tail -50 || echo ''")
if [[ -z "$ERRORS" ]]; then
    print_success "No recent SSH exit 255 or multiplexing errors found"
else
    print_warning "Found SSH-related errors in logs:"
    echo "$ERRORS" | sed 's/^/          /'
fi
echo

# 6. Check ServerConnectionCheckJob configuration
print_header "6. ServerConnectionCheckJob Analysis"
echo "Checking health check job configuration..."

# Check if job is using disableMux parameter
CODE_CHECK=$(ssh "$SERVER" "docker exec $CONTAINER grep -n 'disableMux' /var/www/html/app/Jobs/ServerConnectionCheckJob.php 2>/dev/null || echo ''")
if [[ -n "$CODE_CHECK" ]]; then
    print_success "ServerConnectionCheckJob has disableMux parameter"
    echo "$CODE_CHECK" | sed 's/^/          /'

    # Check default value
    if echo "$CODE_CHECK" | grep -q 'disableMux = true'; then
        print_success "Health checks disable multiplexing by default (prevents contention)"
    else
        print_warning "Health checks may use multiplexing (potential for contention)"
    fi
else
    print_error "Could not find disableMux parameter in ServerConnectionCheckJob"
    echo "          This indicates the PR #7503 fix is NOT present"
fi
echo

# 7. Check deployment job configuration
print_header "7. Deployment Configuration"
echo "Checking if deployments use multiplexing..."

DEPLOY_MUX=$(ssh "$SERVER" "docker exec $CONTAINER grep -A5 'generateSshCommand' /var/www/html/app/Traits/ExecuteRemoteCommand.php 2>/dev/null | head -10 || echo ''")
if [[ -n "$DEPLOY_MUX" ]]; then
    if echo "$DEPLOY_MUX" | grep -q 'disableMultiplexing'; then
        print_success "ExecuteRemoteCommand supports disableMultiplexing parameter"
    else
        print_warning "ExecuteRemoteCommand does not have disableMultiplexing parameter"
    fi
fi
echo

# Summary and recommendations
print_header "Summary & Recommendations"
echo

# Determine if running old version
if [[ "$VERSION" =~ beta\.[0-9]+ ]]; then
    if [[ "$SPECIFIC_VERSION" =~ beta\.([0-9]+) ]]; then
        BETA_NUM="${BASH_REMATCH[1]}"
        if [[ "$BETA_NUM" -lt 458 ]]; then
            print_error "ACTION REQUIRED: Upgrade Coolify to beta.458 or later"
            echo "          Your version (beta.$BETA_NUM) lacks the SSH multiplexing fix"
            echo "          See: https://github.com/coollabsio/coolify/pull/7503"
            echo
        fi
    fi
fi

# Check if health checks are running frequently
echo "Current Issues:"
echo "1. Health checks (ServerConnectionCheckJob) run every 10 seconds"
echo "2. They refresh mux connections via ensureMultiplexedConnection()"
echo "3. If health check runs during deployment, it can:"
echo "   - Close the mux socket (refreshMultiplexedConnection)"
echo "   - Kill the connection mid-deploy"
echo "   - Cause exit code 255"
echo
echo "Fix Options:"
echo
echo "Option A: Disable health check mux usage (Recommended)"
if [[ -z "$SSH_VARS" ]] || ! echo "$SSH_VARS" | grep -q 'SSH_MUX_HEALTH_CHECK_ENABLED=false'; then
    echo "  Run: ./fix-coolify-ssh.sh $SERVER"
    echo "  This sets SSH_MUX_HEALTH_CHECK_ENABLED=false"
else
    print_success "Already configured (SSH_MUX_HEALTH_CHECK_ENABLED=false)"
fi
echo
echo "Option B: Increase health check timeout"
echo "  Set SSH_MUX_HEALTH_CHECK_TIMEOUT=30 (default: 5)"
echo "  Gives more time before declaring connection dead"
echo
echo "Option C: Increase mux persist time"
echo "  Set SSH_MUX_PERSIST_TIME=7200 (default: 3600)"
echo "  Keeps connections alive longer"
echo
echo "Option D: Upgrade Coolify (if older than beta.458)"
echo "  Newer versions have better contention handling"
echo
echo "For more details, see:"
echo "  docs/investigations/coolify-ssh-255.md"
