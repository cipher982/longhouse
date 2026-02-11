# Skill Format Reference

Longhouse skills are `SKILL.md` files with YAML frontmatter + markdown body. Place them in `~/.longhouse/skills/<skill-name>/SKILL.md`.

## Format

```markdown
---
name: my-skill
description: "One-line summary shown in skill index."
emoji: "🔧"
homepage: "https://example.com/docs"
primary_env: "MY_API_KEY"
requires:
  bins: [jq]
  env: [MY_API_KEY]
user_invocable: true
model_invocable: true
always: false
os: [darwin, linux]
---

# My Skill

Markdown body with instructions, examples, and tool usage docs.
The body is injected into the agent prompt when the skill is activated.
```

## Frontmatter Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes* | directory name | Alphanumeric, hyphens, underscores only |
| `description` | No | `""` | One-line summary for skill index |
| `emoji` | No | `""` | UI display icon |
| `homepage` | No | `""` | Link to external docs |
| `primary_env` | No | `""` | Primary env var (UI hint) |
| `requires.bins` | No | `[]` | Required binaries (all must exist) |
| `requires.any_bins` | No | `[]` | At least one must exist |
| `requires.env` | No | `[]` | Required environment variables |
| `requires.config` | No | `[]` | Required config keys |
| `user_invocable` | No | `true` | Can users invoke via `/skill-name` |
| `model_invocable` | No | `true` | Can the model auto-select this skill |
| `always` | No | `false` | Always include in prompt |
| `os` | No | `[]` | Restrict to OS list (`darwin`, `linux`, `win32`) |

*If `name` is omitted, the parent directory name is used.

## Skill Loading

Skills load from three locations (in priority order):
1. **Bundled** -- shipped with Longhouse (`zerg/skills/bundled/`)
2. **Workspace** -- per-project (`.longhouse/skills/` in repo root)
3. **User** -- global (`~/.longhouse/skills/`)

By default, only the **index** (name + description) is injected into the system prompt. Full content loads when a user invokes `/skill-name` or the model auto-selects based on the description.

## Migrating from Claude Code

Claude Code stores skills in `~/.claude/skills/*/`. Each directory may contain a `.md` file with optional YAML frontmatter.

To migrate, use the included script:

```bash
python scripts/migrate_claude_skills.py          # migrate to ~/.longhouse/skills/
python scripts/migrate_claude_skills.py --dry-run # preview without writing
python scripts/migrate_claude_skills.py -o ./my-skills  # custom output path
```

The script reads each `.md` file, preserves existing frontmatter fields, and writes a Longhouse-compatible `SKILL.md`. If Claude Code frontmatter includes `name`, `description`, or `emoji`, those are carried over.

## Migrating from Cursor

Cursor stores rules in `~/.cursor/rules/` as standalone files (`.md`, `.mdc`, or plain text). These typically have no frontmatter.

```bash
python scripts/migrate_cursor_rules.py           # migrate to ~/.longhouse/skills/
python scripts/migrate_cursor_rules.py --dry-run  # preview without writing
python scripts/migrate_cursor_rules.py -o ./my-skills   # custom output path
```

Each rule file becomes a skill directory with a `SKILL.md` containing a minimal frontmatter header (name derived from filename, description from the first line of content).
