# hatch-agent

Unified agent runner library for running AI coding agents headlessly (Claude Code, Codex, Gemini).

## Installation

```bash
# For terminal use
uv tool install -e ~/git/zerg/packages/hatch-agent

# As a library dependency
uv add hatch-agent
```

## CLI Usage

```bash
hatch "What is 2+2?"
hatch -b codex "Write unit tests"
hatch -b bedrock --cwd /path/to/project "Fix the bug"
hatch --json "Analyze this" | jq .output
```

## Library Usage

```python
from hatch import run, Backend

result = await run(
    prompt="Fix the bug",
    backend=Backend.ZAI,
    cwd="/path/to/workspace",
    timeout_s=300,
)
if result.ok:
    print(result.output)
else:
    print(f"Failed: {result.error}")
```

## Backends

| Backend | CLI Tool | Auth |
|---------|----------|------|
| `zai` | Claude Code | `ZAI_API_KEY` |
| `bedrock` | Claude Code | AWS profile |
| `codex` | OpenAI Codex | `OPENAI_API_KEY` |
| `gemini` | Google Gemini | OAuth |

## Features

- Zero dependencies (stdlib only)
- Prompt via stdin (avoids ARG_MAX limits)
- Container-aware (Docker/Kubernetes)
- Async + sync APIs
- JSON output mode
