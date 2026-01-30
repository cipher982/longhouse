#!/usr/bin/env python3
"""
Patrol runner - orchestrates autonomous code patrol.

Usage:
    python patrol.py                    # Single run, auto-select target
    python patrol.py --target FILE      # Patrol specific file
    python patrol.py --loop             # Continuous loop with sleep
    python patrol.py --dry-run          # Show what would run, don't execute
"""

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent))
from registry import record_scan, was_recently_scanned
from validate_finding import validate_finding

REPO_ROOT = Path(__file__).parent.parent.parent
REPORTS_DIR = Path(__file__).parent.parent / "reports"
BACKEND_ROOT = REPO_ROOT / "apps/zerg/backend/zerg"

# Hotspot files - high leverage, complex logic
HOTSPOTS = [
    "services/oikos_react_engine.py",
    "services/commis_runner.py",
    "tools/builtin/oikos_tools.py",
    "tools/tool_search.py",
    "managers/fiche_runner.py",
    "services/oikos_service.py",
    "routes/oikos.py",
    "jobs/processor.py",
]

# Patrol prompts - each has different focus
PROMPTS = {
    "doc_mismatch": """
TARGET: {target}

Task: Compare docstrings/comments to actual code behavior in this file.

Rules:
1. Cite exact file path + line numbers for each mismatch
2. Provide a specific example of the discrepancy
3. If no clear mismatch with evidence, output exactly: {{"status": "NO_FINDINGS"}}

Output JSON:
{{
  "status": "finding" | "NO_FINDINGS",
  "category": "doc_mismatch",
  "description": "...",
  "evidence": ["file.py:123", "file.py:145"],
  "example": "Docstring says X but code does Y",
  "suggested_fix": "Update docstring to reflect..."
}}
""",
    "edge_case": """
TARGET: {target}

Task: Identify ONE unhandled edge case in this file.

Rules:
1. Must point to the precise line(s) where the edge case falls through
2. Provide a minimal test case (input + expected vs actual outcome)
3. If nothing concrete, output exactly: {{"status": "NO_FINDINGS"}}

Output JSON:
{{
  "status": "finding" | "NO_FINDINGS",
  "category": "edge_case",
  "description": "...",
  "evidence": ["file.py:123"],
  "test_snippet": "def test_edge_case(): ...",
  "suggested_fix": "Add check for..."
}}
""",
    "race_condition": """
TARGET: {target}

Task: Find async code that accesses shared state without proper synchronization.

Rules:
1. Must cite exact lines where the race can occur
2. Explain the specific interleaving that causes the bug
3. If no race condition found, output exactly: {{"status": "NO_FINDINGS"}}

Output JSON:
{{
  "status": "finding" | "NO_FINDINGS",
  "category": "race",
  "description": "...",
  "evidence": ["file.py:123", "file.py:145"],
  "interleaving": "Thread A reads X, Thread B writes X, Thread A uses stale X",
  "suggested_fix": "Add lock around..."
}}
""",
}


def select_target() -> tuple[str, str]:
    """Select next target (file, prompt_id) avoiding recent scans."""
    # Shuffle to add variety
    files = HOTSPOTS.copy()
    prompts = list(PROMPTS.keys())
    random.shuffle(files)
    random.shuffle(prompts)

    for f in files:
        for p in prompts:
            full_path = f"apps/zerg/backend/zerg/{f}"
            if not was_recently_scanned(full_path, p):
                return full_path, p

    # All combinations recently scanned, pick random
    f = random.choice(files)
    p = random.choice(prompts)
    return f"apps/zerg/backend/zerg/{f}", p


def run_patrol(target: str, prompt_id: str, dry_run: bool = False) -> dict | None:
    """Run patrol on target with given prompt."""
    prompt_template = PROMPTS[prompt_id]
    full_path = REPO_ROOT / target

    if not full_path.exists():
        print(f"ERROR: Target not found: {full_path}")
        return None

    prompt = prompt_template.format(target=target)

    # Prepend file content for context
    file_content = full_path.read_text()
    full_prompt = f"""
Read this file and analyze it:

```python
# {target}
{file_content}
```

{prompt}

IMPORTANT: Output ONLY valid JSON. No markdown, no explanation, just the JSON object.
"""

    if dry_run:
        print(f"DRY RUN - Would execute:")
        print(f"  Target: {target}")
        print(f"  Prompt: {prompt_id}")
        print(f"  File size: {len(file_content)} chars")
        return None

    print(f"Running patrol: {prompt_id} on {target}")

    # Use hatch for headless z.ai execution
    try:
        result = subprocess.run(
            ["hatch", "-b", "zai", "--json", "-"],
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min timeout
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: Patrol timed out")
        record_scan(target, prompt_id, "invalid")
        return None
    except FileNotFoundError:
        print(
            "ERROR: hatch not found. Install with: uv tool install -e ~/git/zerg/packages/hatch-agent"
        )
        return None

    # Parse output - hatch --json returns {"output": "...", "ok": true/false}
    try:
        hatch_output = json.loads(result.stdout)
        if not hatch_output.get("ok"):
            print(f"ERROR: hatch failed: {hatch_output.get('error')}")
            record_scan(target, prompt_id, "invalid")
            return None
        response = hatch_output.get("output", result.stdout)
    except json.JSONDecodeError:
        response = result.stdout

    # Extract JSON from response (might have markdown wrapping or whitespace)
    response = response.strip()
    json_match = None
    if "```json" in response:
        start = response.find("```json") + 7
        end = response.find("```", start)
        json_match = response[start:end].strip()
    elif "```" in response:
        start = response.find("```") + 3
        end = response.find("```", start)
        json_match = response[start:end].strip()
    elif response.startswith("{"):
        # Find matching closing brace
        brace_count = 0
        end_idx = 0
        for i, c in enumerate(response):
            if c == "{":
                brace_count += 1
            elif c == "}":
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i + 1
                    break
        json_match = response[:end_idx].strip() if end_idx > 0 else response

    if not json_match:
        print(f"ERROR: Could not extract JSON from response")
        print(f"Raw output: {response[:500]}")
        record_scan(target, prompt_id, "invalid")
        return None

    try:
        finding = json.loads(json_match)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}")
        print(f"Attempted to parse: {json_match[:500]}")
        record_scan(target, prompt_id, "invalid")
        return None

    return finding


def process_finding(target: str, prompt_id: str, finding: dict) -> bool:
    """Validate and record finding. Returns True if valid finding."""
    # Check for NO_FINDINGS
    if finding.get("status") == "NO_FINDINGS":
        print(f"  Result: NO_FINDINGS")
        record_scan(target, prompt_id, "no_findings")
        return False

    # Validate the finding
    valid, errors = validate_finding(finding)

    if not valid:
        print(f"  INVALID finding:")
        for e in errors:
            print(f"    - {e}")
        record_scan(target, prompt_id, "invalid")
        return False

    # Valid finding - write report
    print(f"  VALID finding: {finding.get('category')}")
    write_report(target, prompt_id, finding)
    record_scan(target, prompt_id, "finding", evidence_hash=str(hash(str(finding.get("evidence")))))
    return True


def write_report(target: str, prompt_id: str, finding: dict):
    """Write finding to markdown report."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = f"{timestamp}-{prompt_id}-{Path(target).stem}.md"
    filepath = REPORTS_DIR / filename

    report = f"""# Patrol Finding: {finding.get('category', 'unknown')}

**Date:** {datetime.now().isoformat()}
**Target:** {target}
**Prompt:** {prompt_id}

## Description

{finding.get('description', 'No description')}

## Evidence

"""
    for e in finding.get("evidence", []):
        report += f"- `{e}`\n"

    if finding.get("example"):
        report += f"\n## Example\n\n{finding['example']}\n"

    if finding.get("test_snippet"):
        report += f"\n## Test\n\n```python\n{finding['test_snippet']}\n```\n"

    if finding.get("suggested_fix"):
        report += f"\n## Suggested Fix\n\n{finding['suggested_fix']}\n"

    report += f"\n---\n*Generated by patrol/{prompt_id}*\n"

    filepath.write_text(report)
    print(f"  Report written: {filepath}")


def main():
    parser = argparse.ArgumentParser(description="Autonomous code patrol")
    parser.add_argument("--target", help="Specific file to patrol")
    parser.add_argument("--prompt", choices=list(PROMPTS.keys()), help="Specific prompt to use")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--sleep", type=int, default=600, help="Sleep between runs (seconds)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would run")
    args = parser.parse_args()

    import time

    while True:
        # Select target
        if args.target and args.prompt:
            target, prompt_id = args.target, args.prompt
        elif args.target:
            target = args.target
            prompt_id = random.choice(list(PROMPTS.keys()))
        else:
            target, prompt_id = select_target()

        # Run patrol
        finding = run_patrol(target, prompt_id, dry_run=args.dry_run)

        if finding and not args.dry_run:
            process_finding(target, prompt_id, finding)

        if not args.loop:
            break

        print(f"\nSleeping {args.sleep}s before next run...")
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
