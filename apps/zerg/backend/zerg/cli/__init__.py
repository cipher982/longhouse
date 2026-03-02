"""Longhouse CLI module.

Provides command-line tools for interacting with Longhouse:
- longhouse serve: Start the Longhouse server (SQLite default, zero config)
- longhouse status: Show database and configuration status
- longhouse ship: One-shot sync of Claude Code sessions to Longhouse
- longhouse connect: Foreground engine sync (watch + fallback scan)
- longhouse auth: Authenticate with remote Longhouse server
- longhouse doctor: Self-diagnosis for server health, shipper status, config
"""
