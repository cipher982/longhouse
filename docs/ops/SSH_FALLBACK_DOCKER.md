# SSH Fallback in Docker Containers

## Why This Matters

Workers running in Docker containers cannot use SSH aliases from `~/.ssh/config` on the host machine. The fallback mechanism builds raw SSH commands using concrete connection details from user context.

## Prerequisites Checklist

For SSH fallback to work in containerized workers:

- ✅ **SSH private key mounted** into container filesystem
- ✅ **Correct file permissions** (private key should not be group/world-readable)
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
```

## Host Key Verification (Current Behavior)

The current `ssh_exec` implementation disables host key verification (`StrictHostKeyChecking=no`) and writes host keys to a temp file under `/tmp` inside the container. This is convenient for single-user/dev, but it has MITM risk.

If you want verified host keys, change `apps/zerg/backend/zerg/tools/builtin/ssh_tools.py` to enable strict checking and/or accept a mounted known_hosts path.

## Troubleshooting

| Error | Likely Cause | Fix |
|-------|--------------|-----|
| `Permission denied (publickey)` | Key not mounted or wrong permissions | Check volume mount, run `chmod 600` on key |
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

- `docs/completed/WORKER_FALLBACK_SPEC.md` - Full technical specification
- `AGENTS.md` - User context configuration format
