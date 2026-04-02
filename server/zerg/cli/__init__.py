"""Longhouse CLI module.

Provides command-line tools for interacting with Longhouse:
- longhouse serve: Start the Longhouse server (SQLite default, zero config)
- longhouse status: Show database and configuration status
- longhouse claude: Launch a Longhouse Claude session on this machine
- longhouse codex: Launch a Longhouse Codex session on this machine
- longhouse peers: Discover live peer sessions around the current repo
- longhouse message: Send a directed message to another session
- longhouse tail: Read the recent event tail for a session
- longhouse check-messages: Inspect durable queued messages for a session
- longhouse ack-message: Mark a durable message as handled
- longhouse sessions get: Inspect a single session
- longhouse sessions events: Inspect session events with filters
- longhouse ship: One-shot sync of Claude Code sessions to Longhouse
- longhouse connect: Foreground engine sync (watch + fallback scan)
- longhouse wrap: Opt-in default-launcher wrappers for claude/codex
- longhouse auth: Authenticate with remote Longhouse server
- longhouse doctor: Self-diagnosis for server health, shipper status, config
"""
