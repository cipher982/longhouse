"""Command-line interface for hatch."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Sequence

from hatch import __version__
from hatch.backends import Backend
from hatch.backends import get_config
from hatch.context import detect_context
from hatch.runner import AgentResult
from hatch.runner import run_sync

# Exit codes
EXIT_SUCCESS = 0
EXIT_AGENT_ERROR = 1
EXIT_TIMEOUT = 2
EXIT_NOT_FOUND = 3
EXIT_CONFIG_ERROR = 4


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="hatch",
        description="Run AI coding agents headlessly (Claude Code, Codex, Gemini)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  hatch "What is 2+2?"
  hatch -b codex "Write unit tests"
  hatch -b bedrock --cwd /path/to/project "Fix the bug"
  hatch --json "Analyze this" | jq .output

Backends:
  zai      Claude Code CLI with z.ai/GLM-4.7 (default)
  bedrock  Claude Code CLI with AWS Bedrock
  codex    OpenAI Codex CLI
  gemini   Google Gemini CLI

Environment Variables:
  ZAI_API_KEY     API key for zai backend
  OPENAI_API_KEY  API key for codex backend
  AWS_PROFILE     AWS profile for bedrock backend (default: zh-qa-engineer)
  AWS_REGION      AWS region for bedrock backend (default: us-east-1)
""",
    )

    parser.add_argument(
        "prompt",
        nargs="?",
        help="Prompt to send to the agent (reads from stdin if '-' or omitted)",
    )

    parser.add_argument(
        "-b",
        "--backend",
        choices=["zai", "bedrock", "codex", "gemini"],
        default="zai",
        help="Backend to use (default: zai)",
    )

    parser.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Timeout in seconds (default: 300)",
    )

    parser.add_argument(
        "-C",
        "--cwd",
        metavar="DIR",
        help="Working directory for the agent (default: current directory)",
    )

    parser.add_argument(
        "--model",
        metavar="MODEL",
        help="Model override (backend-specific)",
    )

    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh"],
        help="Codex reasoning effort level (codex backend only)",
    )

    parser.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default="text",
        help="Claude output format (Claude backends only)",
    )

    parser.add_argument(
        "--include-partial-messages",
        action="store_true",
        help="Include partial messages in Claude output (Claude backends only)",
    )

    parser.add_argument(
        "--api-key",
        metavar="KEY",
        help="API key override (otherwise from environment)",
    )

    parser.add_argument(
        "-r",
        "--resume",
        metavar="SESSION_ID",
        help="Resume a previous Claude Code session by ID (Claude backends only)",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output JSON result instead of plain text",
    )

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser


def get_prompt(args: argparse.Namespace) -> str:
    """Get prompt from args or stdin."""
    if args.prompt is None or args.prompt == "-":
        # Read from stdin
        if sys.stdin.isatty():
            print("Reading prompt from stdin (Ctrl+D to end):", file=sys.stderr)
        prompt = sys.stdin.read()
        if not prompt.strip():
            print("Error: Empty prompt", file=sys.stderr)
            sys.exit(EXIT_CONFIG_ERROR)
        return prompt
    return args.prompt


def result_to_exit_code(result: AgentResult) -> int:
    """Convert AgentResult to exit code."""
    if result.ok:
        return EXIT_SUCCESS
    if result.exit_code == -1:  # timeout
        return EXIT_TIMEOUT
    if result.exit_code == -2:  # CLI not found
        return EXIT_NOT_FOUND
    return EXIT_AGENT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    """Main entry point.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    parser = create_parser()
    args = parser.parse_args(argv)

    # Get prompt
    prompt = get_prompt(args)

    # Validate timeout
    if args.timeout <= 0:
        msg = "timeout must be > 0"
        if args.json_output:
            error_result = {
                "ok": False,
                "status": "config_error",
                "output": "",
                "exit_code": EXIT_CONFIG_ERROR,
                "duration_ms": 0,
                "error": msg,
                "stderr": None,
            }
            print(json.dumps(error_result))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # Validate cwd
    if args.cwd:
        cwd_path = Path(args.cwd)
        if not cwd_path.exists():
            msg = f"cwd does not exist: {args.cwd}"
        elif not cwd_path.is_dir():
            msg = f"cwd is not a directory: {args.cwd}"
        else:
            msg = ""
        if msg:
            if args.json_output:
                error_result = {
                    "ok": False,
                    "status": "config_error",
                    "output": "",
                    "exit_code": EXIT_CONFIG_ERROR,
                    "duration_ms": 0,
                    "error": msg,
                    "stderr": None,
                }
                print(json.dumps(error_result))
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return EXIT_CONFIG_ERROR

    # Parse backend
    try:
        backend = Backend(args.backend)
    except ValueError:
        print(f"Error: Invalid backend '{args.backend}'", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # Build backend kwargs
    backend_kwargs: dict = {}
    if args.model:
        backend_kwargs["model"] = args.model
    if args.api_key:
        backend_kwargs["api_key"] = args.api_key
    if args.reasoning_effort:
        backend_kwargs["reasoning_effort"] = args.reasoning_effort
    if args.resume:
        backend_kwargs["resume"] = args.resume
    if args.output_format:
        backend_kwargs["output_format"] = args.output_format
    if args.include_partial_messages:
        backend_kwargs["include_partial_messages"] = True

    # Get config (may raise ValueError for missing API key)
    try:
        ctx = detect_context()
        config = get_config(backend, prompt, ctx, **backend_kwargs)
    except ValueError as e:
        if args.json_output:
            error_result = {
                "ok": False,
                "status": "config_error",
                "output": "",
                "exit_code": EXIT_CONFIG_ERROR,
                "duration_ms": 0,
                "error": str(e),
                "stderr": None,
            }
            print(json.dumps(error_result))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # Build environment
    env = config.build_env()
    cwd = args.cwd

    # Run the agent
    start = time.monotonic()

    try:
        stdout, stderr, return_code, timed_out = run_sync(
            config.cmd,
            config.stdin_data,
            env,
            cwd,
            args.timeout,
        )
    except FileNotFoundError as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        result = AgentResult(
            ok=False,
            output="",
            exit_code=-2,
            duration_ms=duration_ms,
            error=f"CLI not found: {e}",
        )
        if args.json_output:
            print(json.dumps(result.to_dict()))
        else:
            print(f"Error: {result.error}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        result = AgentResult(
            ok=False,
            output="",
            exit_code=-3,
            duration_ms=duration_ms,
            error=str(e),
        )
        if args.json_output:
            print(json.dumps(result.to_dict()))
        else:
            print(f"Error: {result.error}", file=sys.stderr)
        return EXIT_AGENT_ERROR

    duration_ms = int((time.monotonic() - start) * 1000)

    # Build result
    if timed_out:
        result = AgentResult(
            ok=False,
            output="",
            exit_code=-1,
            duration_ms=duration_ms,
            error=f"Agent timed out after {args.timeout}s",
        )
    elif return_code != 0:
        result = AgentResult(
            ok=False,
            output=stdout,
            exit_code=return_code,
            duration_ms=duration_ms,
            error=stderr or f"Exit code {return_code}",
            stderr=stderr,
        )
    elif not stdout.strip():
        result = AgentResult(
            ok=False,
            output="",
            exit_code=0,
            duration_ms=duration_ms,
            error="Empty output from agent",
            stderr=stderr,
        )
    else:
        result = AgentResult(
            ok=True,
            output=stdout,
            exit_code=0,
            duration_ms=duration_ms,
            stderr=stderr,
        )

    # Output
    if args.json_output:
        print(json.dumps(result.to_dict()))
    else:
        if result.ok:
            # Strip trailing whitespace for cleaner output
            print(result.output.rstrip())
        else:
            print(f"Error: {result.error}", file=sys.stderr)
            if result.output:
                print(result.output, file=sys.stderr)

    return result_to_exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
