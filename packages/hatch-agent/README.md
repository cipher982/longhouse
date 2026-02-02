# hatch-agent

Headless agent runner (Claude Code, Codex, Gemini) used by Longhouse.

## Install

```bash
uv tool install -e ~/git/zerg/packages/hatch-agent
```

## Usage

```bash
hatch "What is 2+2?"
hatch -b codex "Write unit tests"
hatch --json "Analyze this" | jq .output
```

## Library

```python
from hatch import run, Backend

result = await run(prompt="Fix the bug", backend=Backend.ZAI)
print(result.output if result.ok else result.error)
```
