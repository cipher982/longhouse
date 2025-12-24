"""Test-only tool stubbing mechanism for deterministic E2E tests.

This module provides a way to stub tool outputs (like ssh_exec, runner_exec)
in tests without requiring real servers or external dependencies.

Usage:
    1. Set env var: ZERG_TOOL_STUBS_PATH=/path/to/stubs.json
    2. Create stubs file with format:
        {
            "ssh_exec": [
                {
                    "match": {"host": "cube", "command_contains": "df"},
                    "result": {"ok": true, "data": {"stdout": "...", "exit_code": 0}}
                }
            ]
        }
    3. Run tests - matching tool calls return stubbed results

IMPORTANT: This is TEST-ONLY infrastructure. Production code should never
enable stubbing (env var must not be set in prod).
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Global cache for loaded stubs
_stubs_cache: dict[str, Any] | None = None
_stubs_loaded: bool = False


def get_tool_stubs() -> dict[str, Any] | None:
    """Load tool stubs from the env var path if configured.

    Returns None if ZERG_TOOL_STUBS_PATH is not set or file doesn't exist.
    Caches the result for efficiency.
    """
    global _stubs_cache, _stubs_loaded

    if _stubs_loaded:
        return _stubs_cache

    _stubs_loaded = True
    stubs_path = os.environ.get("ZERG_TOOL_STUBS_PATH")

    if not stubs_path:
        return None

    path = Path(stubs_path)
    if not path.exists():
        logger.warning(f"ZERG_TOOL_STUBS_PATH set but file not found: {stubs_path}")
        return None

    try:
        with open(path) as f:
            _stubs_cache = json.load(f)
            logger.info(f"Loaded tool stubs from {stubs_path}: {list(_stubs_cache.keys())}")
            return _stubs_cache
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in tool stubs file: {e}")
        return None


def clear_stubs_cache():
    """Clear the stubs cache (useful for tests that change stubs)."""
    global _stubs_cache, _stubs_loaded
    _stubs_cache = None
    _stubs_loaded = False


def match_stub(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Find a matching stub for a tool call.

    Args:
        tool_name: Name of the tool being called (e.g., "ssh_exec", "runner_exec")
        args: Arguments passed to the tool

    Returns:
        The stub result dict if a match is found, None otherwise.

    Match rules:
        - "host": exact match on args["host"]
        - "command_contains": substring match on args["command"]
        - "match_all": always matches (useful for catch-all stubs)
    """
    stubs = get_tool_stubs()
    if not stubs:
        return None

    tool_stubs = stubs.get(tool_name, [])
    if not tool_stubs:
        return None

    for stub in tool_stubs:
        match_rules = stub.get("match", {})

        # Check if this stub matches
        if _matches(match_rules, args):
            logger.info(f"Stub matched for {tool_name}: {match_rules}")
            return stub.get("result")

    return None


def _matches(rules: dict[str, Any], args: dict[str, Any]) -> bool:
    """Check if match rules apply to the given args."""
    # Empty rules or "match_all" = always match
    if not rules or rules.get("match_all"):
        return True

    # Check each rule
    for rule_key, rule_value in rules.items():
        if rule_key == "match_all":
            continue

        if rule_key == "host":
            if args.get("host") != rule_value:
                return False

        elif rule_key == "command_contains":
            command = args.get("command", "")
            if rule_value not in command:
                return False

        elif rule_key == "tool_name_contains":
            # For runner_exec - match on tool_name arg
            tool_name_arg = args.get("tool_name", "")
            if rule_value not in tool_name_arg:
                return False

        elif rule_key == "query_contains":
            # For web_search or similar tools
            query = args.get("query", "") or args.get("input", "")
            if rule_value not in query:
                return False

        elif rule_key.endswith("_exact"):
            # Exact match on any arg: e.g., "device_id_exact": "1"
            arg_name = rule_key[:-6]  # Strip "_exact"
            if args.get(arg_name) != rule_value:
                return False

        elif rule_key.endswith("_contains"):
            # Substring match on any arg: e.g., "path_contains": "/home"
            arg_name = rule_key[:-9]  # Strip "_contains"
            if rule_value not in str(args.get(arg_name, "")):
                return False

    return True
