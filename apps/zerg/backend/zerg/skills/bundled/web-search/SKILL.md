---
name: web-search
description: "Search the web for information using various search providers."
emoji: "🔍"
always: true
---

# Web Search Skill

Search the web to find current information, documentation, or answers.

## Available Tools

- `web_search` - Search the web using configured provider

## Usage

Use web search when you need:

- Current information that may have changed since training
- Documentation for specific versions
- News and recent events
- Verification of facts

## Example

```python
web_search(query="Python 3.12 new features")
```

## Tips

- Be specific in queries for better results
- Include version numbers when searching for documentation
- Use quotes for exact phrase matching
- Combine with `web_fetch` to read full pages
