# Session Resume Lab

Standalone test harness to understand Claude Code session resume behavior
before integrating into Zerg.

## Quick Start

```bash
cd /Users/davidrose/git/zerg/scripts/session-resume-lab

# Install deps
uv sync

# Interactive mode - type messages, see what happens
uv run python lab.py

# Or run the benchmark suite
uv run python profiler.py
```

## What This Tests

1. **Session Creation** - How sessions are created and stored
2. **Resume Behavior** - How `--resume` picks up context
3. **Stream Output** - What events come through `--output-format stream-json`
4. **Turn-by-Turn** - Multiple resume cycles simulating chat
5. **Timing** - TTFT, resume overhead, multi-turn latency

## Lab Scripts

### lab.py - Interactive Exploration

```bash
# Interactive chat mode (default)
uv run python lab.py

# Specific tests
uv run python lab.py --test create      # Create a session
uv run python lab.py --test resume      # Resume existing session
uv run python lab.py --test chat        # Multi-turn simulation
uv run python lab.py --test inspect     # Inspect session files
uv run python lab.py --test interactive # Interactive chat
```

**Interactive commands:**
- `/sessions` - List all sessions for workspace
- `/inspect` - Show current session content
- `/new` - Start fresh session
- `/quit` - Exit

### profiler.py - Benchmarks & Analysis

```bash
# Full benchmark suite
uv run python profiler.py --mode benchmark

# Inspect event stream format
uv run python profiler.py --mode events

# Analyze session files
uv run python profiler.py --mode sessions
```

**Benchmark outputs:**
- Time to first token (TTFT)
- Resume overhead vs fresh session
- Multi-turn latency trends
- Session file growth

## What You'll Learn

### Session Storage
Sessions live at: `~/.claude/projects/{encoded_cwd}/{session_id}.jsonl`

The `encoded_cwd` is the workspace path with non-alphanumeric chars → dashes:
```
/Users/david/git/zerg → -Users-david-git-zerg
```

### Event Stream Format
With `--output-format stream-json`, Claude emits newline-delimited JSON:

```json
{"type": "system", "session_id": "abc123..."}
{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
{"type": "result", "result": "..."}
```

Key event types:
- `system` - Session metadata
- `assistant` - Claude's response chunks
- `user` - User message echo
- `result` - Final result

### Resume Behavior
`--resume {session_id}` loads the session file and continues from there.
Claude has full context of prior conversation.

### Key Metrics to Watch
- **TTFT (Time to First Token)** - How long before streaming starts
- **Resume overhead** - Extra latency vs fresh session (context loading)
- **Session growth** - How fast .jsonl files grow per turn

## Architecture Validation

This lab validates the turn-by-turn approach:

```
Turn 1: claude -p "message" --print --output-format stream-json
        → Creates session, streams events
        → Session ID captured from stream or file

Turn 2: claude --resume {id} -p "next message" --print --output-format stream-json
        → Resumes with full context
        → Streams response
        → Session file updated

Turn N: Repeat...
```

Each turn is an independent process. Session continuity via `--resume`.

## Files

```
session-resume-lab/
├── README.md           # This file
├── pyproject.toml      # Dependencies
├── lab.py              # Interactive exploration
├── profiler.py         # Benchmarks & analysis
└── workspace/          # Test workspace (created on first run)
    └── README.md
```
