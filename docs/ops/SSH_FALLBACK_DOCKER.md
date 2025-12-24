# SSH Fallback in Docker Containers

## Why This Matters

Workers running in Docker containers cannot use SSH aliases from `~/.ssh/config` on the host machine. The fallback mechanism builds raw SSH commands using concrete connection details from user context.

## Prerequisites Checklist

For SSH fallback to work in containerized workers:

- ✅ **SSH private key mounted** into container filesystem
- ✅ **Correct file permissions** (600 for private key, 644 for known_hosts)
- ✅ **known_hosts strategy** configured (see options below)
- ✅ **User context configured** with concrete SSH details:
  - `ssh_user` - Remote username (not aliases like "drose@server")
  - `ssh_host` - IP address or FQDN
  - `ssh_port` - Port number (default: 22)

## Docker Compose Example

```yaml
services:
  worker:
    image: zerg-worker
    volumes:
      # Mount SSH private key (read-only)
      - ~/.ssh/id_ed25519:/root/.ssh/id_ed25519:ro

      # Option 1: Mount existing known_hosts
      - ~/.ssh/known_hosts:/root/.ssh/known_hosts:ro

      # Option 2: Use StrictHostKeyChecking=no (less secure)
      # No known_hosts mount needed
    environment:
      # Disable strict host checking if not using known_hosts
      - SSH_OPTS=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
```

## known_hosts Strategies

| Strategy | Security | Setup Complexity |
|----------|----------|------------------|
| Mount host's `known_hosts` | ✅ Verified | Low (if host already has entries) |
| `StrictHostKeyChecking=no` | ⚠️ MITM risk | Lowest (no setup) |
| Pre-populate via `ssh-keyscan` | ✅ Verified | Medium (build-time or init script) |

**Pre-populate example:**
```bash
# In Dockerfile or entrypoint script
ssh-keyscan -H clifford.example.com >> /root/.ssh/known_hosts
ssh-keyscan -H REDACTED_IP >> /root/.ssh/known_hosts
```

## Troubleshooting

| Error | Likely Cause | Fix |
|-------|--------------|-----|
| `Permission denied (publickey)` | Key not mounted or wrong permissions | Check volume mount, run `chmod 600` on key |
| `Host key verification failed` | Missing/mismatched known_hosts entry | Mount known_hosts or disable strict checking |
| `Connection refused` | Wrong port or firewall blocking | Verify `ssh_port` in user context, check firewall |
| `ssh: Could not resolve hostname` | Invalid `ssh_host` | Use IP address instead of hostname |

**Debug command:**
```bash
# Inside container
docker exec -it <container-id> ssh -vvv user@host
```

## Security Considerations

1. **Principle of Least Privilege**
   - Only mount keys needed for target servers
   - Consider using a dedicated SSH key for worker access (not your personal key)

2. **Multi-Tenant Environments**
   - SSH fallback should be disabled or scoped per-user
   - Each user's workers should only access their authorized servers

3. **Key Rotation**
   - If SSH keys change, containers must be restarted to pick up new mounts
   - Use secrets management (Docker secrets, HashiCorp Vault) for production

4. **Audit Trail**
   - SSH commands are logged in worker execution logs
   - Monitor for unauthorized access attempts

## Related Docs

- `docs/work/WORKER_FALLBACK_SPEC.md` - Full technical specification
- `AGENTS.md` - User context configuration format
