---
name: quick-search
description: "Quick web search shortcut that directly dispatches to web_search tool."
emoji: "🔎"
tool_dispatch: web_search
always: true
---

# Quick Search

A shortcut skill that directly dispatches to the `web_search` tool.

This skill demonstrates the `tool_dispatch` feature which creates a wrapper tool
that inherits the target tool's schema and forwards calls.

## Usage

When this skill is active, invoking `skill_quick-search` is equivalent to calling
`web_search` directly, with the skill's context added.

## Example

```
skill_quick-search(query="Python 3.12 new features")
```

This is functionally equivalent to:

```
web_search(query="Python 3.12 new features")
```
