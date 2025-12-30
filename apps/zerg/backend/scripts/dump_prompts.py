#!/usr/bin/env python3
"""Dump full assembled prompts for supervisor and worker agents.

This script shows the complete prompt that gets sent to the LLM, including:
- Connector protocols (static)
- Base prompt templates
- Dynamic user context injection
- Server information
- Integration status

Usage:
    uv run scripts/dump_prompts.py --role supervisor
    uv run scripts/dump_prompts.py --role worker
    uv run scripts/dump_prompts.py --role all  # Default: both
    uv run scripts/dump_prompts.py --role supervisor --format json
"""

import argparse
import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.database import get_db
from zerg.prompts.composer import build_supervisor_prompt, build_worker_prompt
from zerg.prompts.connector_protocols import get_connector_protocols


def format_markdown(role: str, prompt: str, protocols: str) -> str:
    """Format prompt as readable markdown."""
    lines = [
        f"# {role.upper()} PROMPT",
        "",
        "## Connector Protocols (Static)",
        "",
        "```",
        protocols,
        "```",
        "",
        f"## {role.capitalize()} System Prompt",
        "",
        "```",
        prompt,
        "```",
        "",
        "## Metrics",
        "",
        f"- **Total length**: {len(protocols) + len(prompt):,} characters",
        f"- **Protocols length**: {len(protocols):,} characters",
        f"- **Role-specific length**: {len(prompt):,} characters",
        f"- **Estimated tokens**: ~{(len(protocols) + len(prompt)) // 4:,} tokens",
    ]
    return "\n".join(lines)


def format_json_output(role: str, prompt: str, protocols: str) -> dict:
    """Format prompt as structured JSON."""
    return {
        "role": role,
        "protocols": protocols,
        "prompt": prompt,
        "metrics": {
            "total_chars": len(protocols) + len(prompt),
            "protocols_chars": len(protocols),
            "prompt_chars": len(prompt),
            "estimated_tokens": (len(protocols) + len(prompt)) // 4,
        },
    }


def dump_prompt(role: str, output_format: str = "markdown") -> None:
    """Dump the assembled prompt for a role.

    Args:
        role: 'supervisor' or 'worker'
        output_format: 'markdown' or 'json'
    """
    db = next(get_db())
    try:
        # Get first user (or create one for demo)
        from zerg.models.models import User

        user = db.query(User).first()
        if not user:
            print("ERROR: No users found. Create a user first.", file=sys.stderr)
            sys.exit(1)

        # Get connector protocols (static part)
        protocols = get_connector_protocols()

        # Build role-specific prompt
        if role == "supervisor":
            prompt = build_supervisor_prompt(user)
        elif role == "worker":
            prompt = build_worker_prompt(user)
        else:
            raise ValueError(f"Unknown role: {role}")

        # Format and output
        if output_format == "json":
            output = format_json_output(role, prompt, protocols)
            print(json.dumps(output, indent=2))
        else:
            output = format_markdown(role, prompt, protocols)
            print(output)

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(
        description="Dump assembled prompts for debugging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Show supervisor prompt
  uv run scripts/dump_prompts.py --role supervisor

  # Show worker prompt
  uv run scripts/dump_prompts.py --role worker

  # Show both (default)
  uv run scripts/dump_prompts.py

  # JSON output for programmatic parsing
  uv run scripts/dump_prompts.py --role supervisor --format json
        """,
    )
    parser.add_argument(
        "--role",
        choices=["supervisor", "worker", "all"],
        default="all",
        help="Which agent role to dump (default: all)",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format (default: markdown)",
    )

    args = parser.parse_args()

    if args.role == "all":
        roles = ["supervisor", "worker"]
    else:
        roles = [args.role]

    for i, role in enumerate(roles):
        if i > 0 and args.format == "markdown":
            print("\n" + "=" * 80 + "\n")
        dump_prompt(role, args.format)


if __name__ == "__main__":
    main()
