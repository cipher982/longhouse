#!/usr/bin/env python3
"""
Docs drift detector — LLM-powered CI check for documentation accuracy.

Runs on PRs: maps changed files to relevant doc pages, then asks an LLM
whether any doc claims are now incorrect or incomplete given the diff.

Non-blocking: posts a PR comment with findings. Never fails the build.

Usage:
  python scripts/ci/docs-drift-check.py          # auto-detects from git diff
  python scripts/ci/docs-drift-check.py --dry-run # print findings, don't comment
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "web" / "src" / "pages" / "docs"

# ---------------------------------------------------------------------------
# Stage 1: Static triage — map changed code paths to relevant doc pages
# ---------------------------------------------------------------------------

# Each entry: (glob-ish prefix, list of doc page filenames)
TRIAGE_MAP: list[tuple[str, list[str]]] = [
    # CLI entry points
    ("server/zerg/cli/", ["CLIReferencePage.tsx", "QuickStartPage.tsx"]),
    ("engine/src/cli", ["CLIReferencePage.tsx"]),
    # API routers
    ("server/zerg/routers/", ["MachineAPIPage.tsx", "SearchPage.tsx"]),
    ("server/zerg/routers/agents", ["MachineAPIPage.tsx"]),
    ("server/zerg/routers/auth", ["ConfigurationPage.tsx"]),
    # Session / ingest
    ("server/zerg/services/agents_store", ["IntegrationsPage.tsx", "SearchPage.tsx"]),
    ("server/zerg/services/search", ["SearchPage.tsx"]),
    # Engine pipeline
    ("engine/src/pipeline/", ["IntegrationsPage.tsx"]),
    ("engine/src/codex_bridge", ["IntegrationsPage.tsx"]),
    ("engine/src/gemini", ["IntegrationsPage.tsx"]),
    # MCP server
    ("server/zerg/mcp_server/", ["IntegrationsPage.tsx", "MachineAPIPage.tsx"]),
    # Configuration / auth
    ("server/zerg/config", ["ConfigurationPage.tsx"]),
    ("server/zerg/auth/", ["ConfigurationPage.tsx"]),
    ("server/zerg/database.py", ["ConfigurationPage.tsx"]),
    # Remote control / coordination
    ("server/zerg/services/session_chat", ["RemoteControlPage.tsx"]),
    ("server/zerg/services/continuation", ["RemoteControlPage.tsx"]),
    ("server/zerg/routers/runner", ["RemoteControlPage.tsx"]),
    # Docs themselves (flag if docs change without code — might be intentional)
    ("web/src/pages/docs/", []),
    # Install / onboarding
    ("scripts/install", ["QuickStartPage.tsx"]),
    ("scripts/onboarding", ["QuickStartPage.tsx"]),
]


def get_changed_files(base_ref: str = "origin/main") -> list[str]:
    """Get files changed in this PR relative to base."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_ref, "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except Exception:
        return []


def triage(changed_files: list[str]) -> dict[str, list[str]]:
    """Map changed files to doc pages that might need updating.

    Returns: {doc_page_filename: [changed_files_that_triggered_it]}
    """
    hits: dict[str, list[str]] = {}
    for changed in changed_files:
        for prefix, doc_pages in TRIAGE_MAP:
            if changed.startswith(prefix):
                for page in doc_pages:
                    hits.setdefault(page, []).append(changed)
    return hits


# ---------------------------------------------------------------------------
# Stage 2: LLM analysis — check if docs are still accurate
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a documentation accuracy reviewer for an open-source project called Longhouse.

You will receive:
1. A git diff showing code changes in a pull request
2. One or more documentation pages (React TSX files containing user-facing text)

Your job: identify specific claims in the documentation that are now INCORRECT or \
INCOMPLETE because of the code changes. Only flag real problems — not style preferences, \
not "could be improved", not speculative issues.

Rules:
- Only flag claims that are CONTRADICTED by the diff, not things that are merely not mentioned
- If the diff adds a new feature that docs don't cover yet, flag it only if the docs claim \
  to be a complete reference (e.g., a CLI reference page missing a new command)
- Be specific: quote the exact doc text that is wrong and explain what changed
- If nothing is wrong, say so clearly
- Never suggest rewrites — just identify the problems

Respond in JSON:
{
  "findings": [
    {
      "doc_page": "filename.tsx",
      "claim": "exact text from the doc that is now wrong",
      "issue": "what changed and why this claim is incorrect",
      "confidence": "high" | "medium"
    }
  ],
  "summary": "one-line summary: how many issues found, or 'no drift detected'"
}

Only include findings with high or medium confidence. Omit low-confidence guesses.\
"""


def read_doc_page(filename: str) -> str:
    """Read a doc page and extract the user-facing text content."""
    path = DOCS_DIR / filename
    if not path.exists():
        return ""
    return path.read_text()


def get_diff(base_ref: str = "origin/main") -> str:
    """Get the full diff for LLM analysis."""
    try:
        result = subprocess.run(
            ["git", "diff", base_ref, "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        diff = result.stdout
        # Truncate large diffs to stay within token budget
        max_chars = 80_000  # ~20k tokens
        if len(diff) > max_chars:
            diff = diff[:max_chars] + "\n\n[... diff truncated for token budget ...]"
        return diff
    except Exception:
        return ""


def call_llm(diff: str, doc_pages: dict[str, str]) -> dict | None:
    """Call the LLM via OpenRouter to analyze docs against the diff."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("⚠️  OPENROUTER_API_KEY not set, skipping LLM analysis")
        return None

    # Build the user message
    doc_sections = []
    for filename, content in doc_pages.items():
        doc_sections.append(f"--- {filename} ---\n{content}")
    docs_text = "\n\n".join(doc_sections)

    user_message = f"## Git Diff\n\n```diff\n{diff}\n```\n\n## Documentation Pages\n\n{docs_text}"

    payload = {
        "model": "x-ai/grok-4-fast",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    import urllib.request

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/cipher982/longhouse",
            "X-Title": "Longhouse Docs Drift CI",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as e:
        print(f"⚠️  LLM call failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Stage 3: Output — format findings for PR comment or stdout
# ---------------------------------------------------------------------------


def format_pr_comment(result: dict, triage_hits: dict[str, list[str]]) -> str:
    """Format LLM findings as a GitHub PR comment."""
    findings = result.get("findings", [])
    summary = result.get("summary", "")

    if not findings:
        return (
            "### 📄 Docs Drift Check\n\n"
            f"**{summary or 'No documentation drift detected.'}**\n\n"
            f"Checked {len(triage_hits)} doc page(s) against this PR's changes."
        )

    lines = [
        "### 📄 Docs Drift Check\n",
        f"Found **{len(findings)} potential issue(s)** in documentation:\n",
    ]

    for f in findings:
        confidence_badge = "🔴" if f["confidence"] == "high" else "🟡"
        lines.append(
            f"{confidence_badge} **{f['doc_page']}**\n"
            f"> {f['claim']}\n\n"
            f"  ↳ {f['issue']}\n"
        )

    lines.append(
        "\n---\n"
        f"*Checked {len(triage_hits)} doc page(s). "
        "This is informational — not a merge blocker.*"
    )
    return "\n".join(lines)


def post_pr_comment(body: str) -> bool:
    """Post or update a PR comment via gh CLI."""
    pr_number = os.environ.get("PR_NUMBER")
    if not pr_number:
        print("⚠️  PR_NUMBER not set, skipping comment")
        return False

    # Check for existing comment to update (avoid spam)
    marker = "### 📄 Docs Drift Check"
    try:
        result = subprocess.run(
            [
                "gh", "pr", "view", pr_number,
                "--json", "comments",
                "--jq", '.comments[] | select(.body | startswith("### 📄 Docs Drift Check")) | .id',
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        existing_ids = [cid.strip() for cid in result.stdout.strip().split("\n") if cid.strip()]

        if existing_ids:
            # Update existing comment
            subprocess.run(
                ["gh", "api", f"repos/{{owner}}/{{repo}}/issues/comments/{existing_ids[0]}",
                 "--method", "PATCH", "-f", f"body={body}"],
                capture_output=True,
                cwd=REPO_ROOT,
            )
            print(f"✅ Updated existing PR comment {existing_ids[0]}")
        else:
            # Create new comment
            subprocess.run(
                ["gh", "pr", "comment", pr_number, "--body", body],
                capture_output=True,
                cwd=REPO_ROOT,
            )
            print("✅ Posted new PR comment")
        return True
    except Exception as e:
        print(f"⚠️  Failed to post PR comment: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Docs drift detector")
    parser.add_argument("--dry-run", action="store_true", help="Print findings without posting PR comment")
    parser.add_argument("--base-ref", default="origin/main", help="Git ref to diff against")
    args = parser.parse_args()

    print("📄 Docs drift check starting...")

    # Stage 1: Triage
    changed_files = get_changed_files(args.base_ref)
    if not changed_files:
        print("  No changed files detected, nothing to check.")
        sys.exit(0)

    print(f"  {len(changed_files)} files changed")

    triage_hits = triage(changed_files)
    if not triage_hits:
        print("  No doc-relevant code changes detected, skipping LLM check.")
        sys.exit(0)

    print(f"  Triage: {len(triage_hits)} doc page(s) may need review:")
    for page, triggers in triage_hits.items():
        print(f"    {page} ← {', '.join(triggers[:3])}{'...' if len(triggers) > 3 else ''}")

    # Stage 2: LLM analysis
    diff = get_diff(args.base_ref)
    if not diff:
        print("  Could not get diff, skipping LLM check.")
        sys.exit(0)

    doc_pages = {}
    for page_filename in triage_hits:
        content = read_doc_page(page_filename)
        if content:
            doc_pages[page_filename] = content

    if not doc_pages:
        print("  No doc pages found to check, skipping.")
        sys.exit(0)

    print(f"  Sending diff ({len(diff)} chars) + {len(doc_pages)} doc pages to LLM...")
    result = call_llm(diff, doc_pages)

    if result is None:
        print("  LLM analysis skipped or failed.")
        sys.exit(0)

    # Stage 3: Output
    findings = result.get("findings", [])
    summary = result.get("summary", "no summary")
    print(f"  Result: {summary}")

    if findings:
        for f in findings:
            conf = f.get("confidence", "?")
            print(f"  [{conf}] {f['doc_page']}: {f['issue']}")

    comment = format_pr_comment(result, triage_hits)

    if args.dry_run:
        print("\n--- PR Comment (dry run) ---")
        print(comment)
    else:
        post_pr_comment(comment)

    print("📄 Docs drift check complete.")


if __name__ == "__main__":
    main()
