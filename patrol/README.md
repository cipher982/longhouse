# Patrol - Autonomous Code Analysis

Runs LLM-powered code patrols to find issues CI can't catch.

## Quick Start

```bash
# Single patrol run (auto-selects target)
python patrol/scripts/patrol.py

# Dry run - see what would execute
python patrol/scripts/patrol.py --dry-run

# Patrol specific file
python patrol/scripts/patrol.py --target apps/zerg/backend/zerg/services/oikos_react_engine.py

# Continuous loop (10 min between runs)
python patrol/scripts/patrol.py --loop --sleep 600

# Run in tmux
tmux new -s patrol
python patrol/scripts/patrol.py --loop
# Ctrl+B D to detach
```

## How It Works

1. **Target Selection**: Picks from hotspot files, avoids recently scanned
2. **Prompt Execution**: Spawns z.ai via `hatch` with focused prompt
3. **Validation**: Hard-checks output for evidence (file:line refs required)
4. **Recording**: Logs all scans (including NO_FINDINGS) to prevent re-scans
5. **Reporting**: Valid findings written to `patrol/reports/`

## Prompts

| ID | Focus |
|----|-------|
| `doc_mismatch` | Docstrings vs actual code behavior |
| `edge_case` | Unhandled inputs/states |
| `race_condition` | Async code without proper sync |

## Evidence Gate

Every finding MUST include:
- File path + line numbers
- Concrete description
- Valid category

Findings without evidence are rejected and logged as `invalid`.

## Registry

Tracks all scans to prevent duplicate work:
- `patrol/registry/scans.jsonl` - scan history
- Default TTL: 7 days per target+prompt combo

```bash
# View stats
python patrol/scripts/registry.py stats

# View recent scans
python patrol/scripts/registry.py
```

## Reports

Valid findings saved to `patrol/reports/`:
```
2026-01-27-143022-doc_mismatch-oikos_react_engine.md
```

Review these manually. Once signal quality is validated, can promote to life-hub tasks or Linear.

## Adding Prompts

Edit `PROMPTS` dict in `patrol.py`. Each prompt must:
1. Take `{target}` placeholder
2. Require JSON output with `status`, `category`, `evidence`
3. Support `NO_FINDINGS` response
