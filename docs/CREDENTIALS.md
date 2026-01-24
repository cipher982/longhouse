# Credentials & Personal Tools

Jarvis supports personal integrations for location, health, and notes using per-user encrypted credentials.

## Available Personal Tools

| Tool | Integration | Purpose |
|------|-------------|---------|
| `get_current_location` | Traccar | GPS location |
| `get_whoop_data` | WHOOP | Health metrics (recovery, HRV, sleep) |
| `search_notes` | Obsidian | Search notes via Runner |

## Local Setup

**1. Create credentials file:**
```bash
cp apps/zerg/backend/scripts/personal_credentials.example.json \
   apps/zerg/backend/scripts/personal_credentials.local.json
```

**2. Fill in credentials** (git-ignored):
```json
{
  "traccar": {
    "url": "http://5.161.97.53:5055",
    "username": "admin",
    "password": "your-password",
    "device_id": "1"
  },
  "whoop": {
    "client_id": "your-oauth-app-client-id",
    "client_secret": "your-oauth-app-client-secret",
    "access_token": "from-oauth-flow",
    "refresh_token": "from-oauth-flow"
  },
  "obsidian": {
    "vault_path": "~/git/obsidian_vault",
    "runner_name": "laptop"
  }
}
```

**3. Start dev** (auto-seeds on startup):
```bash
make dev
```

Or seed manually: `make seed-credentials`

## User Context

Backend auto-seeds user context from config files on startup:

| File | Dev Path | Prod Path |
|------|----------|-----------|
| User context | `scripts/user_context.local.json` | `~/.config/zerg/user_context.json` |
| Credentials | `scripts/personal_credentials.local.json` | `~/.config/zerg/personal_credentials.json` |

User context includes:
- **servers**: Names, IPs, purposes (injected into prompts)
- **integrations**: Health trackers, notes apps
- **custom_instructions**: Personal AI preferences

## Production Setup

```bash
# Via SSH
python scripts/seed_personal_credentials.py --email your@email.com

# Or create ~/.config/zerg/personal_credentials.json first
```

## Seeding Commands

```bash
make seed-agents       # Seed Jarvis agents
make seed-credentials  # Seed personal tool credentials
make seed-credentials ARGS="--force"  # Overwrite existing
```

Both are idempotent (safe to run repeatedly).

## Security

- Local config (`*.local.json`) is git-ignored
- Database storage is Fernet-encrypted (AES-GCM)
- Per-user storage in `account_connector_credentials` table
- Change defaults (Traccar default password is `admin`)

## More Details

- **Traccar**: `docs/ops/TRACCAR_QUICKSTART.md`
- **Test connection**: `uv run scripts/test_traccar.py`
- **WHOOP OAuth**: Register at https://developer.whoop.com
- **Obsidian**: Requires Runner on machine with vault
