#!/usr/bin/env python3
"""Run one managed Claude channel POC under a PTY."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from zerg.qa.managed_claude_live import ManagedClaudeLiveConfig  # noqa: E402
from zerg.qa.managed_claude_live import append_terminal_log  # noqa: E402,F401
from zerg.qa.managed_claude_live import assistant_transcript_contains  # noqa: E402,F401
from zerg.qa.managed_claude_live import build_channel_send_command  # noqa: E402,F401
from zerg.qa.managed_claude_live import channel_send  # noqa: E402,F401
from zerg.qa.managed_claude_live import compact_terminal_text  # noqa: E402,F401
from zerg.qa.managed_claude_live import default_output_root  # noqa: E402
from zerg.qa.managed_claude_live import monotonic_ms  # noqa: E402,F401
from zerg.qa.managed_claude_live import read_json_file  # noqa: E402,F401
from zerg.qa.managed_claude_live import run_id_now  # noqa: E402
from zerg.qa.managed_claude_live import run_managed_claude_live_session  # noqa: E402
from zerg.qa.managed_claude_live import run_probe  # noqa: E402,F401
from zerg.qa.managed_claude_live import set_nonblocking  # noqa: E402,F401
from zerg.qa.managed_claude_live import strip_terminal_controls  # noqa: E402,F401
from zerg.qa.managed_claude_live import text_fragments  # noqa: E402,F401
from zerg.qa.managed_claude_live import transcript_line_counts  # noqa: E402,F401
from zerg.qa.managed_claude_live import transcript_paths  # noqa: E402,F401
from zerg.qa.managed_claude_live import utc_now  # noqa: E402,F401
from zerg.qa.managed_claude_live import wait_for_channel_ready  # noqa: E402,F401

DEFAULT_OUTPUT_ROOT = default_output_root(ROOT)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=ROOT)
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--name", default="Claude propagation POC")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Per-process ANTHROPIC_MODEL override.")
    parser.add_argument("--prompt", default="Please reply with exactly: LONGHOUSE CLAUDE PROFILE READY")
    parser.add_argument("--expected", default="LONGHOUSE CLAUDE PROFILE READY")
    parser.add_argument(
        "--steer-text",
        help="Optional active-turn channel correction to send after the initial prompt.",
    )
    parser.add_argument(
        "--steer-expected",
        help="Assistant text that must appear in a new transcript row after --steer-text is sent.",
    )
    parser.add_argument(
        "--steer-delay-secs",
        type=float,
        default=2.0,
        help="Seconds after prompt send before injecting --steer-text with intent=steer metadata.",
    )
    parser.add_argument("--run-id", default=run_id_now())
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--launch-timeout-secs", type=float, default=45.0)
    parser.add_argument("--response-timeout-secs", type=float, default=60.0)
    parser.add_argument("--post-close-probe-secs", type=float, default=0.0)
    parser.add_argument(
        "--skip-post-close-probe",
        action="store_true",
        help="Do not run the post-close truth probe before returning.",
    )
    parser.add_argument(
        "--skip-live-probe",
        action="store_true",
        help="Do not run the concurrent truth probe while waiting for the managed response.",
    )
    parser.add_argument(
        "--session-id-file",
        type=Path,
        help="Optional path to write the managed session id as soon as Claude prints it.",
    )
    args = parser.parse_args()
    if args.steer_delay_secs < 0:
        parser.error("--steer-delay-secs must be >= 0")
    if args.steer_text and not args.steer_expected:
        parser.error("--steer-expected is required when --steer-text is set")
    return args


def main() -> int:
    args = parse_args()
    config = ManagedClaudeLiveConfig(
        cwd=args.cwd,
        project=args.project,
        name=args.name,
        model=args.model,
        prompt=args.prompt,
        expected=args.expected,
        steer_text=args.steer_text,
        steer_expected=args.steer_expected,
        steer_delay_secs=args.steer_delay_secs,
        run_id=args.run_id,
        output_dir=args.output_dir,
        launch_timeout_secs=args.launch_timeout_secs,
        response_timeout_secs=args.response_timeout_secs,
        post_close_probe_secs=args.post_close_probe_secs,
        skip_post_close_probe=args.skip_post_close_probe,
        skip_live_probe=args.skip_live_probe,
        session_id_file=args.session_id_file,
        repo_root=ROOT,
    )
    summary = run_managed_claude_live_session(config)
    summary_path = Path(str(summary["terminal_log"])).with_name("summary.md")
    print(summary_path)
    return 0 if summary.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
