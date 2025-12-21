# Ops & Infrastructure Access

**Version:** 2.0
**Date:** December 2025
**Status:** SSH-First Architecture
**Philosophy:** Give agents SSH access. They're smart enough to run commands.

---

## 1. Problem & Opportunity

### v1.0 Approach (ANTI-PATTERN)

**Problem:** Agents need operational data
**Solution:** Build API endpoints wrapping every system tool

```
/api/ops/context        → docker ps, git SHA, migrations
/api/ops/logs           → docker logs
/api/ops/support-bundle → gzipped JSON export
```

**Issues:**

1. Builds wrappers for commands agents could run directly
2. Every new operation requires new endpoint
3. Agents limited to pre-programmed data exports
4. API maintenance burden grows with every ops task

### v2.0 Approach (SIMPLIFIED)

**Problem:** Agents need operational data
**Solution:** Give them SSH access

```python
# Agent has ssh_exec tool
agent.ssh_exec('cube', 'docker ps')
agent.ssh_exec('clifford', 'df -h')
agent.ssh_exec('cube', 'docker logs postgres --tail 100')
```

**Benefits:**

1. Agent can run ANY command (df, docker, git, journalctl, etc.)
2. No API maintenance - agents compose commands dynamically
3. Terminal is the primitive - agents already understand it
4. Agents can discover and adapt (try commands, read man pages)

---

## 2. Architecture

### SSH Access for Agents

**Configuration:**

```python
# In system instructions
agent.system_instructions = """
You have SSH access to these servers:
- cube (100.70.237.79) - Home GPU server, AI workloads
- clifford (REDACTED_IP) - Production VPS, web apps
- zerg (5.161.92.127) - Project server
- slim (135.181.204.0) - EU VPS

You can run any shell command via ssh_exec(host, command).
Figure out what commands are needed for the task.
"""

# ssh_exec tool implementation
async def ssh_exec(host: str, command: str, timeout: int = 30):
    """Execute command on remote host."""

    # Validate host against allowlist
    allowed_hosts = ['cube', 'clifford', 'zerg', 'slim']
    if host not in allowed_hosts:
        raise PermissionError(f"SSH access denied to {host}")

    # Execute via SSH key
    result = await run_ssh_command(
        host=resolve_host(host),
        command=command,
        key_path='~/.ssh/id_ed25519',
        timeout=timeout
    )

    return result.stdout
```

**Safety boundaries:**

- Host allowlist (only known servers)
- Timeout per command (default 30s, max 300s)
- Audit trail (all commands logged to tool_calls/\*.txt)
- Read-only by default (sudo requires explicit grant)

---

## 3. What Agents Can Do

### Check Docker Status

```python
# Agent reasoning: "I need to see running containers"
result = ssh_exec('cube', 'docker ps --format "{{.Names}}\t{{.Status}}"')

# Interprets output naturally
if 'postgres' not in result:
    return "Postgres container is not running on cube"
```

### Check Logs

```python
# Agent reasoning: "I should check postgres logs for errors"
logs = ssh_exec('cube', 'docker logs postgres --tail 100 | grep ERROR')

# Analyzes log patterns
if 'connection refused' in logs:
    return "Postgres is having connection issues"
```

### Check Disk Space

```python
# Agent reasoning: "df -h shows disk usage clearly"
result = ssh_exec('cube', 'df -h /')

# Parses output (LLMs understand df -h format)
if '95%' in result:
    return "Cube disk is 95% full - critical, need cleanup"
```

### Check Migrations

```python
# Agent reasoning: "Alembic current shows migration state"
result = ssh_exec('cube', 'docker exec zerg-backend-1 alembic current')

# Interprets alembic output
if 'head' not in result:
    return "Database migrations are out of date. Run: alembic upgrade head"
```

---

## 4. Comparison: API Wrappers vs SSH Access

### v1.0: API Wrapper Approach

```python
# Backend code (maintenance burden)
@router.get("/api/ops/docker-status")
async def get_docker_status():
    result = subprocess.run(['docker', 'ps'], capture_output=True)
    containers = parse_docker_output(result.stdout)
    return {"containers": containers}

@router.get("/api/ops/disk-usage")
async def get_disk_usage():
    result = subprocess.run(['df', '-h'], capture_output=True)
    usage = parse_df_output(result.stdout)
    return {"filesystems": usage}

# Agent code
status = http_request('/api/ops/docker-status')
disk = http_request('/api/ops/disk-usage')

# Problem: Need new endpoint for every operation
# To check logs? → Build /api/ops/logs
# To check migrations? → Build /api/ops/migrations
# To check backups? → Build /api/ops/backups
# ...endless API endpoints
```

### v2.0: SSH Access Approach

```python
# Backend code (minimal)
# ssh_exec tool already exists - no additional endpoints needed

# Agent code
status = ssh_exec('cube', 'docker ps')
disk = ssh_exec('cube', 'df -h /')
logs = ssh_exec('cube', 'docker logs postgres --tail 100')
migrations = ssh_exec('cube', 'docker exec backend alembic current')
backups = ssh_exec('cube', 'kopia snapshot list')

# Benefit: Agent composes any command needed
# No API maintenance burden
```

---

## 5. What About `/api/ops/summary`?

**Keep it for human dashboards:**

```python
# Useful for Grafana, status pages, human operators
GET /api/ops/summary
{
  "agents": 5,
  "active_runs": 2,
  "budget_remaining": 75.43,
  "uptime_seconds": 86400
}
```

**But don't expand it into operational telemetry API.**

For agent diagnostics, SSH access is more flexible:

- Agents run commands directly
- Agents can discover what's available (`ls /var/log`, `docker ps --help`)
- Agents adapt to new scenarios without code changes

---

## 6. Frontend Telemetry

**Keep the frontend beacon idea (it's good):**

```typescript
// apps/zerg/frontend-web/src/jarvis/lib/error-beacon.ts (example location)
window.addEventListener("error", (event) => {
  navigator.sendBeacon(
    "/api/ops/frontend-log",
    JSON.stringify({
      build_hash: BUILD_HASH,
      route: window.location.pathname,
      error: event.message,
      stack: event.error?.stack,
      timestamp: Date.now(),
    }),
  );
});
```

**Why keep:**

- Browser errors can't be captured via SSH
- Beacon is lightweight, non-blocking
- Agents can query `/api/ops/frontend-logs` for recent errors

**But don't over-instrument:**

- Just capture errors and critical warnings
- Don't log every console.log
- Ring buffer (last 500 entries), not persistent storage

---

## 7. Implementation

### Current State ✅

```python
# ssh_exec tool exists
tools/builtin/ssh.py

# Agents can use it
supervisor → spawn_worker("Check cube") → worker.ssh_exec('cube', 'df -h')
```

### What to Keep

1. **ssh_exec tool** ✅
   - Already implemented
   - Host validation
   - Timeout handling
   - Audit logging

2. **Frontend error beacon** ✅ (or add if not exists)
   - Captures browser errors
   - Sends to backend
   - Agents can query

3. **GET /api/ops/summary** ✅
   - Human-readable dashboard data
   - Grafana integration

### What NOT to Build

1. ❌ `/api/ops/context` (unified JSON snapshot)
   - Agents can query directly via SSH
   - Pre-computing data limits agent flexibility

2. ❌ `/api/ops/logs` (log tailing endpoint)
   - Agents can run: `docker logs <container>`
   - Wrapping it in API adds no value

3. ❌ `/api/ops/support-bundle` (gzipped export)
   - Agents query what they need in real-time
   - Pre-computed bundles assume fixed data needs

4. ❌ `/api/ops/docker-status`, `/api/ops/disk-usage`, etc.
   - Agents run: `docker ps`, `df -h` directly
   - Building wrappers is maintenance burden

---

## 8. Security Model

**SSH access is safe when properly scoped:**

### 8.1 Host Allowlist

```python
# In ssh_exec tool
ALLOWED_HOSTS = {
    'cube': '100.70.237.79',
    'clifford': 'REDACTED_IP',
    'zerg': '5.161.92.127',
    'slim': '135.181.204.0'
}

# Agents can ONLY SSH to these hosts
# Requests to other hosts are rejected
```

### 8.2 Command Safety Layer

**Dangerous patterns require explicit override:**

```python
DANGEROUS_PATTERNS = [
    r'rm\s+-rf',              # Recursive delete
    r'>\s*/dev/sd',           # Write to block device
    r'DROP\s+(TABLE|DATABASE)', # SQL destruction
    r'docker\s+rm\s+-f',      # Force remove containers
    r'docker\s+stop',         # Stop containers
    r'systemctl\s+(stop|disable)', # Service disruption
    r'chmod\s+777',           # Security risk
    r'curl.*\|\s*(ba)?sh',    # Pipe to shell
    r'wget.*\|\s*(ba)?sh',    # Pipe to shell
]

async def ssh_exec(host: str, command: str, allow_destructive: bool = False):
    """Execute command on remote host with safety checks."""

    # 1. Validate host
    if host not in ALLOWED_HOSTS:
        raise PermissionError(f"SSH access denied to {host}")

    # 2. Check for dangerous patterns
    if not allow_destructive:
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                raise DangerousCommandError(
                    f"Command matches dangerous pattern: {pattern}\n"
                    f"Use allow_destructive=True if intentional."
                )

    # 3. Execute with timeout
    async with asyncio.timeout(timeout):
        result = await run_ssh(host, command)

    # 4. Log everything
    log_to_artifact(host, command, result)

    return result
```

**Why allow_destructive flag exists:**

- Agents CAN run destructive commands when needed (like cleaning disk space)
- But they must be explicit about it
- Prevents accidental destruction from prompt injection
- Creates clear audit trail of intentional destructive actions

### 8.3 Command Auditing

```
All ssh_exec calls logged to:
/data/swarmlet/workers/{worker_id}/tool_calls/NNN_ssh_exec.txt

Format:
Host: cube
Command: df -h
Timestamp: 2025-12-07T20:33:17Z
Exit code: 0
Allow_destructive: false
Duration_ms: 1847
Output:
[... command output ...]
```

**Audit retention:** 30 days minimum, longer for destructive commands.

### 8.4 Privilege Model

```bash
# SSH user 'zerg-agent' has limited permissions:

# CAN do (no sudo required):
- Read files in /var/log/
- Run docker ps, docker logs, docker stats
- Run df, free, top, ps, netstat
- Read /etc/* configs

# REQUIRES sudo (granted per-host):
- docker stop/start/restart
- systemctl commands
- Writing to /etc/
- Package management (apt, yum)

# NEVER allowed:
- Root shell access
- SSH key modification
- User management
```

**Server setup required:**

```bash
# On each managed server:
sudo useradd -m -s /bin/bash zerg-agent
sudo usermod -aG docker zerg-agent  # Docker access without sudo

# Limited sudo for specific commands only:
echo "zerg-agent ALL=(ALL) NOPASSWD: /usr/bin/docker stop *, /usr/bin/docker start *" \
  | sudo tee /etc/sudoers.d/zerg-agent
```

### 8.5 Timeout Enforcement

```python
# Timeouts are guardrails, not heuristics
SSH_TIMEOUTS = {
    'default': 30,      # Most commands
    'max': 300,         # Long-running (du, tar, etc.)
    'interactive': 0,   # Not supported (vim, less, etc.)
}

# Agent can request longer timeout (up to max):
ssh_exec('cube', 'tar -czf backup.tar.gz /data', timeout=120)
```

### 8.6 Rate Limiting

```python
# Per-worker limits:
MAX_SSH_COMMANDS_PER_WORKER = 20  # Prevent infinite loops
MAX_BYTES_RETURNED = 1_000_000    # 1MB output limit
MAX_CONCURRENT_SSH = 3            # Don't overwhelm servers

# Enforcement:
if worker.ssh_count >= MAX_SSH_COMMANDS_PER_WORKER:
    raise RateLimitError("Worker exceeded SSH command limit")
```

---

## 9. Example: Agent Diagnosing Issue

**User:** "Why is the app slow?"

**Agent reasoning:**

```
1. "I should check if containers are running"
   → ssh_exec('clifford', 'docker ps')
   → Sees: All containers running

2. "Let me check resource usage"
   → ssh_exec('clifford', 'docker stats --no-stream')
   → Sees: postgres using 94% memory

3. "I should check postgres logs for slow queries"
   → ssh_exec('clifford', 'docker logs postgres | grep "slow query"')
   → Sees: Multiple slow queries on users table

4. "Let me check database indexes"
   → ssh_exec('clifford', 'docker exec postgres psql -U zerg -c "\\d users"')
   → Sees: No index on frequently queried column

Result: "Your app is slow because postgres is using 94% memory due to
        slow queries. The users table is missing an index on the email
        column. You should add an index."
```

**Key:** Agent composed 4 different commands dynamically. No API endpoints needed.

---

## 10. Migration from v1.0

### Remove (Don't Build)

```python
# DELETE these planned endpoints:
@router.get("/api/ops/context")       # Unified snapshot
@router.get("/api/ops/logs")          # Log tailing
@router.get("/api/ops/support-bundle") # Pre-computed bundle
@router.get("/api/ops/docker-status") # Docker wrapper
@router.get("/api/ops/disk-usage")    # Disk wrapper

# Agents use SSH instead:
ssh_exec(host, command)
```

### Keep

```python
# KEEP: Human dashboard endpoints
@router.get("/api/ops/summary")       # For Grafana
@router.get("/api/ops/timeseries")    # For trending
@router.ws("/ops/events")             # For live alerts

# KEEP: Frontend telemetry
@router.post("/api/ops/frontend-log") # Browser errors
@router.get("/api/ops/frontend-logs") # Query errors
```

---

_End of Specification v2.0_

**Summary:** Give agents SSH access to infrastructure. They'll figure out what commands to run. Don't build API wrappers for operational tools - it's maintenance burden with no flexibility gain. Frontend error beacon is valuable (keep it). Human dashboards are valuable (keep them). But for agent diagnostics: terminal access > API endpoints.
