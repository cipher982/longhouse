# Install Guide

## Quick Install (Recommended)

```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
```

This installs everything and runs the onboarding wizard automatically. You'll be up and running in under a minute.

## What the Installer Does

1. **Installs uv** (Python package manager) if not present
2. **Ensures Python 3.12+** via `uv python install`
3. **Installs `longhouse`** as a uv tool (`~/.local/bin/longhouse`)
4. **Sets up Claude Code hooks** for automatic session shipping (if Claude Code is installed)
5. **Updates your shell profile** to add `~/.local/bin` to PATH
6. **Runs `longhouse onboard`** -- the setup wizard

Set `LONGHOUSE_NO_WIZARD=1` to skip the wizard during install.

## Onboarding Wizard

Run manually anytime with `longhouse onboard`. The wizard walks through 7 steps:

| Step | What it does |
|------|-------------|
| 1. Dependencies | Checks for Claude Code (optional) and existing config |
| 2. Server setup | Starts `longhouse serve --daemon` on port 8080 |
| 3. Session shipping | Installs background shipper service (launchd/systemd) via `longhouse connect --install` |
| 4. Verification | Emits a test event to confirm the pipeline works |
| 5. Demo data | Seeds sample sessions so you see something immediately |
| 6. Config | Saves settings to `~/.config/longhouse/config.toml` |
| 7. PATH check | Verifies `longhouse` and `claude` are reachable in a fresh shell |

Use `longhouse onboard --quick` to accept all defaults non-interactively.

### Wizard Flags

| Flag | Effect |
|------|--------|
| `--quick` / `-q` | Accept all defaults, no prompts |
| `--host` | Bind address (default: `127.0.0.1`) |
| `--port` / `-p` | Server port (default: `8080`) |
| `--no-server` | Skip starting the server |
| `--no-shipper` | Skip shipper/service installation |
| `--no-demo` | Skip demo data seeding |

## Connect Flow

`longhouse connect` continuously syncs Claude Code sessions to your Longhouse instance.

**What `--install` does:**
1. Creates a background service (launchd plist on macOS, systemd unit on Linux)
2. Installs Claude Code hooks (`~/.claude/hooks/`) so sessions ship on every Stop event
3. Registers Longhouse as an MCP server for Claude Code and Codex CLI
4. Verifies PATH in a fresh shell

**Other modes:**
- `longhouse connect` -- foreground file watcher (sub-second sync)
- `longhouse connect --poll` -- polling mode (fallback)
- `longhouse connect --hooks-only` -- hooks + MCP only, no background daemon
- `longhouse ship` -- one-shot sync of all pending sessions

**Service management:**
```bash
longhouse connect --status     # Check if service is running
longhouse connect --uninstall  # Stop and remove service
```

## Manual Install (no pipe-to-bash)

```bash
# 1. Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install longhouse as a uv tool
uv tool install longhouse

# 3. Run the onboarding wizard
longhouse onboard

# 4. Start the server
longhouse serve
```

Or via pip: `pip install longhouse && longhouse onboard`.

## Troubleshooting

### `longhouse: command not found`

The installer adds `~/.local/bin` to your shell profile, but the current terminal hasn't picked it up yet.

```bash
source ~/.zshrc   # or ~/.bashrc / ~/.bash_profile
# -- or --
export PATH="$HOME/.local/bin:$PATH"
```

Run `longhouse doctor` to verify everything is working.

### Server won't start (port in use)

```bash
lsof -i :8080              # Find what's using the port
longhouse serve --port 8081 # Use a different port
```

### Hooks not firing / sessions not shipping

```bash
longhouse connect --status  # Check background service
longhouse doctor            # Full diagnosis
longhouse ship --verbose    # Manual sync to test
```

### `claude: command not found`

Claude Code must be installed separately. See [docs.anthropic.com/claude-code](https://docs.anthropic.com/claude-code). After installing, run `longhouse connect --install` to set up hooks.

### Upgrading

```bash
uv tool upgrade longhouse
longhouse doctor
```

## Data Locations

| Path | Contents |
|------|----------|
| `~/.longhouse/` | Database, server logs, PID file |
| `~/.config/longhouse/config.toml` | Configuration |
| `~/.local/bin/longhouse` | CLI binary |
| `~/.claude/hooks/` | Claude Code hook scripts |
