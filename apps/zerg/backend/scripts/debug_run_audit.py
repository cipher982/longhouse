#!/usr/bin/env python3
"""Debug a run by showing its LLM audit trail.

Usage:
    uv run python scripts/debug_run_audit.py --run-id 82
    uv run python scripts/debug_run_audit.py --run-id 82 --show-messages
"""

import argparse
import sys
from zerg.database import get_session_factory
from zerg.services.llm_audit import get_run_llm_history

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--show-messages", action="store_true")
    args = parser.parse_args()

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        history = get_run_llm_history(db, args.run_id)

        print(f"LLM Audit Trail for Run {args.run_id}")
        print("=" * 80)

        if not history:
            print("No audit logs found for this run.")
            return

        for i, entry in enumerate(history):
            print(f"\n[{i+1}] {entry['phase']} ({entry['model']})")
            print(f"    Messages: {len(entry['messages'])} | Duration: {entry['duration_ms']}ms")

            # Safe access to nested dicts
            tokens = entry.get('tokens', {})
            print(f"    Tokens: in={tokens.get('input')} out={tokens.get('output')} reasoning={tokens.get('reasoning')}")

            response = entry.get('response', {})
            if response.get('tool_calls'):
                for tc in response['tool_calls']:
                    print(f"    -> Tool: {tc.get('name')}({tc.get('args')})")
            elif response.get('content'):
                content = str(response['content'])
                preview = content[:100].replace('\n', ' ')
                print(f"    -> Response: {preview}...")

            if entry.get('error'):
                print(f"    ERROR: {entry['error']}")

            if args.show_messages:
                print(f"\n    --- Messages ---")
                for msg in entry.get('messages', []):
                    content = str(msg.get('content', ''))
                    print(f"    [{msg.get('type')}] {content[:200].replace('\n', ' ')}")
                    if len(content) > 200:
                        print(f"        ... (len={len(content)})")
                    if msg.get('tool_calls'):
                        print(f"        Tool Calls: {msg['tool_calls']}")
    finally:
        db.close()

if __name__ == "__main__":
    main()
