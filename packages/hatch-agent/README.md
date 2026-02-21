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

<!-- readme-test: verifies install from repo and CLI entrypoint -->
```readme-test
{
  "name": "hatch-agent-install",
  "mode": "smoke",
  "workdir": ".",
  "timeout": 60,
  "steps": [
    "uv venv .tmp-hatch-readme-venv --python 3.12 -q",
    ". .tmp-hatch-readme-venv/bin/activate",
    "uv pip install -e packages/hatch-agent -q",
    "hatch --help | head -3"
  ],
  "cleanup": [
    "rm -rf .tmp-hatch-readme-venv"
  ]
}
```
