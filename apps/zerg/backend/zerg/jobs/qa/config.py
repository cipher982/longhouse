"""QA Agent configuration - thresholds and settings.

All configuration as Python constants for simplicity.
Environment variables can override via os.getenv().
"""

from __future__ import annotations

import os

# API endpoints
API_URL = os.getenv("QA_API_URL", "https://api.swarmlet.com")
API_URL_INTERNAL = os.getenv("QA_API_URL_INTERNAL", "http://localhost:47300")

# Discord webhook (uses main Discord config)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Thresholds for anomaly detection
THRESHOLDS = {
    # Error rates (as decimal, e.g., 0.05 = 5%)
    "error_rate_warn": 0.05,
    "error_rate_critical": 0.10,
    # Latency (milliseconds)
    "p95_latency_warn_ms": 5000,
    "p95_latency_critical_ms": 10000,
    # Failed runs per hour
    "failed_runs_1h_warn": 5,
    "failed_runs_1h_critical": 10,
    # Stuck workers
    "stuck_workers_warn": 2,
    "stuck_workers_critical": 5,
}

# Alert behavior
ALERT_COOLDOWN_MINUTES = 60  # Minimum time between alerts for same issue
CHRONIC_THRESHOLD = 3  # Consecutive occurrences to mark as chronic
RESOLVE_THRESHOLD = 3  # Consecutive clean runs to resolve an issue

# Run settings
RUN_DIR = "/tmp/qa-run"
STATE_VERSION = 1

# Synthetic health check
ENABLE_SYNTHETIC = os.getenv("QA_ENABLE_SYNTHETIC", "true").lower() == "true"
SYNTHETIC_MESSAGE = "ping"
SYNTHETIC_TIMEOUT_MS = 30000

# Agent settings
AGENT_TIMEOUT_SECONDS = 480  # 8 minutes for Claude analysis

# z.ai API settings (for Claude Code CLI with z.ai backend)
# Uses full Claude Code agent with agentic capabilities via z.ai's
# Anthropic-compatible API. Key insight: must use ANTHROPIC_AUTH_TOKEN
# (not ANTHROPIC_API_KEY) and unset CLAUDE_CODE_USE_BEDROCK.
ZAI_API_KEY = os.getenv("ZAI_API_KEY")
ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/anthropic")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-4.7")
