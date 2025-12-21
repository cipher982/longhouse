# Coolify SSH Diagnostics & Fix Scripts

Quick reference for managing Coolify SSH multiplexing issues.

## Scripts

### diagnose-coolify-ssh.sh
**Purpose**: Analyze SSH multiplexing configuration and health

**Usage:**
```bash
./diagnose-coolify-ssh.sh [server]
```

**Default server**: clifford

**What it checks:**
- Coolify version and PR #7503 fix status
- SSH environment variables
- Active mux socket files and ages
- SSH connectivity to managed servers
- Recent SSH errors in logs
- Health check and deployment configuration

**When to run:**
- Before applying fixes
- After applying fixes (verification)
- When investigating deployment failures
- Monthly health check

---

### fix-coolify-ssh.sh
**Purpose**: Apply recommended SSH configuration tuning

**Usage:**
```bash
# Dry run (see changes without applying)
./fix-coolify-ssh.sh [server] --dry-run

# Apply fix
./fix-coolify-ssh.sh [server]
```

**Default server**: clifford

**What it does:**
1. Backs up current .env to `/tmp/coolify-env-backup-*.env`
2. Sets recommended SSH configuration:
   - `SSH_MUX_HEALTH_CHECK_ENABLED=false`
   - `SSH_MUX_PERSIST_TIME=7200` (2 hours)
   - `SSH_MUX_MAX_AGE=3600` (1 hour)
   - `SSH_MAX_RETRIES=5`
3. Offers to restart Coolify (required for changes to take effect)
4. Verifies configuration applied
5. Optionally cleans up old mux sockets

**Expected result:** 30% → <5% deployment failure rate

---

## Quick Troubleshooting

### Deployment failing with exit 255?
```bash
# 1. Check current status
./diagnose-coolify-ssh.sh clifford

# 2. Apply fix if not already done
./fix-coolify-ssh.sh clifford

# 3. Watch logs for improvement
ssh clifford "docker logs -f coolify 2>&1 | grep -i 'deploy\|255'"
```

### Verify fix is working
```bash
# Check configuration
ssh clifford "docker exec coolify cat /var/www/html/.env | grep SSH_"

# Check mux socket ages
ssh clifford "docker exec coolify ls -lh /var/www/html/storage/app/ssh/mux/"

# Run full diagnostics
./diagnose-coolify-ssh.sh clifford
```

### Revert changes
```bash
# Find your backup
ls -lt /tmp/coolify-env-backup-*.env | head -1

# Restore backup
BACKUP="/tmp/coolify-env-backup-YYYYMMDD-HHMMSS.env"
scp "$BACKUP" clifford:/tmp/restore.env
ssh clifford "docker cp /tmp/restore.env coolify:/var/www/html/.env"
ssh clifford "docker restart coolify"
```

---

## Configuration Values

### Default (Before Fix)
```bash
MUX_ENABLED: true
SSH_MUX_PERSIST_TIME: 3600  (1 hour)
SSH_MUX_MAX_AGE: 1800        (30 minutes) ⚠️ Too aggressive
SSH_MUX_HEALTH_CHECK_ENABLED: true
SSH_MAX_RETRIES: 3
```

### Recommended (After Fix)
```bash
MUX_ENABLED: true
SSH_MUX_PERSIST_TIME: 7200           (2 hours)
SSH_MUX_MAX_AGE: 3600                (1 hour) ✓
SSH_MUX_HEALTH_CHECK_ENABLED: false  (defense-in-depth)
SSH_MAX_RETRIES: 5                   (more resilient)
```

### Nuclear Option (If all else fails)
```bash
MUX_ENABLED: false  # ⚠️ Severe performance penalty
```

---

## Understanding the Fix

### The Problem
1. Deployments use SSH multiplexing for speed
2. Sockets refresh after 30 minutes (`SSH_MUX_MAX_AGE`)
3. If deployment runs during/after expiry, socket closes mid-deploy
4. Deploy fails with exit 255
5. Retry succeeds (new socket established)

### The Solution
- Double socket lifetime (30 min → 60 min)
- Reduce forced refreshes during deployments
- More retries for resilience
- Health checks already bypass mux (built-in fix)

### Why It Works
- Longer socket life = fewer refresh cycles
- Fewer refreshes = less chance of mid-deploy contention
- More retries = better recovery from edge cases

---

## Related Documentation

- **Full investigation**: `../docs/investigations/coolify-ssh-255.md`
- **Executive summary**: `../docs/investigations/coolify-ssh-255-summary.md`
- **Coolify debugging**: `../docs/COOLIFY_DEBUGGING.md`
- **Deployment guide**: `../docs/DEPLOYMENT.md`

---

## Support

**Upstream Issues:**
- https://github.com/coollabsio/coolify/issues/6736
- https://github.com/coollabsio/coolify/pull/7503

**Version Status:**
- Fix merged: Coolify 4.0.0-beta.458 (Dec 5, 2024)
- Partial fix in: beta.454 (health checks bypass mux)
- Full fix in: beta.458+ (deployment control + health check)

**Next Steps:**
1. Apply configuration fix (this directory)
2. Monitor for 24 hours
3. Schedule upgrade to beta.458+ when convenient
