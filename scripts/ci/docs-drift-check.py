#!/usr/bin/env python3
"""
Docs drift detector — LLM-powered CI check for documentation accuracy.

Sends the full PR diff + all doc pages to Grok 4.3 via OpenRouter.
The model decides what's drifted. ~$0.01 per run, ~$1/month at 80 PRs.

Non-blocking: posts a PR comment with findings. Never fails the build
for drift — but DOES fail for broken configuration (missing API key, etc).

Usage:
  python scripts/ci/docs-drift-check.py                    # CI mode
  python scripts/ci/docs-drift-check.py --dry-run           # local testing
  python scripts/ci/docs-drift-check.py --dry-run --base-ref HEAD~5
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "web" / "src" / "pages" / "docs"

MODEL = "x-ai/grok-4.3"

SYSTEM_PROMPT = """\
You are a documentation accuracy reviewer for an open-source project called Longhouse.

You will receive:
1. A git diff showing code changes in a pull request
2. The project's full documentation pages (React TSX files containing user-facing text)

Your job: identify specific claims in the documentation that are now INCORRECT or \
INCOMPLETE because of the code changes. Only flag real problems — not style preferences, \
not "could be improved", not speculative issues.

Rules:
- Only flag claims that are CONTRADICTED by the diff, not things that are merely not mentioned
- If the diff adds a new command, flag, endpoint, or config option that a reference page \
  claims to cover completely but now misses — that's incomplete
- Be specific: quote the exact doc text that is wrong and explain what changed
- If nothing is wrong, say so clearly
- Never suggest rewrites — just identify the problems

Respond in JSON:
{
  "findings": [
    {
      "doc_page": "filename.tsx",
      "claim": "exact text from the doc that is now wrong or incomplete",
      "issue": "what changed and why this claim is incorrect",
      "confidence": "high" | "medium"
    }
  ],
  "summary": "one-line summary: how many issues found, or 'no drift detected'"
}

Only include findings with high or medium confidence. Omit low-confidence guesses.\
"""


def get_diff(base_ref: str) -> str:
    """Get the PR diff."""
    result = subprocess.run(
        ["git", "diff", base_ref, "HEAD"],
        capture_output=True, text=True, check=True, cwd=REPO_ROOT,
    )
    return result.stdout


def read_all_doc_pages() -> dict[str, str]:
    """Read every doc page."""
    pages = {}
    for path in sorted(DOCS_DIR.glob("*.tsx")):
        # Skip layout and utility components — only content pages
        if path.name in ("DocsLayout.tsx", "CodeBlock.tsx"):
            continue
        pages[path.name] = path.read_text()
    return pages


def call_llm(diff: str, doc_pages: dict[str, str], api_key: str) -> dict:
    """Call Grok 4.3 via OpenRouter. Raises on failure."""
    doc_sections = [f"--- {name} ---\n{content}" for name, content in doc_pages.items()]
    docs_text = "\n\n".join(doc_sections)

    user_message = f"## Git Diff\n\n```diff\n{diff}\n```\n\n## Documentation Pages\n\n{docs_text}"

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

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

    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cost = prompt_tokens * 0.0000002 + completion_tokens * 0.0000005
    print(f"  Tokens: {prompt_tokens:,} in / {completion_tokens:,} out — ${cost:.4f}")

    return json.loads(content)


def format_pr_comment(result: dict, doc_count: int) -> str:
    """Format LLM findings as a GitHub PR comment."""
    findings = result.get("findings", [])
    summary = result.get("summary", "")

    if not findings:
        return (
            "### Docs Drift Check\n\n"
            f"**{summary or 'No documentation drift detected.'}**\n\n"
            f"Checked {doc_count} doc page(s) against this PR's changes."
        )

    lines = [
        "### Docs Drift Check\n",
        f"Found **{len(findings)} potential issue(s)** in documentation:\n",
    ]

    for f in findings:
        badge = "HIGH" if f["confidence"] == "high" else "MEDIUM"
        lines.append(
            f"**[{badge}] {f['doc_page']}**\n"
            f"> {f['claim']}\n\n"
            f"{f['issue']}\n"
        )

    lines.append(
        "\n---\n"
        f"*Checked {doc_count} doc page(s). "
        "This is informational — not a merge blocker.*"
    )
    return "\n".join(lines)


def post_pr_comment(body: str) -> None:
    """Post or update a PR comment via gh CLI."""
    pr_number = os.environ.get("PR_NUMBER")
    if not pr_number:
        print("  No PR_NUMBER set (local run?), skipping comment.")
        return

    # Find existing comment to update (avoid spam)
    result = subprocess.run(
        [
            "gh", "pr", "view", pr_number,
            "--json", "comments",
            "--jq", '.comments[] | select(.body | startswith("### Docs Drift Check")) | .id',
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    existing_ids = [cid.strip() for cid in result.stdout.strip().split("\n") if cid.strip()]

    if existing_ids:
        update = subprocess.run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/issues/comments/{existing_ids[0]}",
             "--method", "PATCH", "-f", f"body={body}"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        if update.returncode == 0:
            print(f"  Updated existing PR comment {existing_ids[0]}")
            return
        detail = (update.stderr or update.stdout).strip()
        print(f"  Warning: failed to update docs drift comment {existing_ids[0]}: {detail}", file=sys.stderr)

    create = subprocess.run(
        ["gh", "pr", "comment", pr_number, "--body", body],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    if create.returncode == 0:
        print("  Posted new PR comment")
        return
    detail = (create.stderr or create.stdout).strip()
    print(f"  Warning: failed to post docs drift comment: {detail}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Docs drift detector")
    parser.add_argument("--dry-run", action="store_true", help="Print findings, don't post comment")
    parser.add_argument("--base-ref", default="origin/main", help="Git ref to diff against")
    args = parser.parse_args()

    # --- Validate configuration (fail hard) ---
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is not set. This is a configuration error.", file=sys.stderr)
        sys.exit(1)

    print(f"Docs drift check: diffing against {args.base_ref}")

    # --- Get the diff ---
    diff = get_diff(args.base_ref)
    if not diff:
        print("  Empty diff, nothing to check.")
        sys.exit(0)
    print(f"  Diff: {len(diff):,} chars (~{len(diff) // 4:,} tokens)")

    # --- Read all doc pages ---
    doc_pages = read_all_doc_pages()
    if not doc_pages:
        print("  No doc pages found. Skipping.")
        sys.exit(0)
    total_doc_chars = sum(len(c) for c in doc_pages.values())
    print(f"  Docs: {len(doc_pages)} pages, {total_doc_chars:,} chars (~{total_doc_chars // 4:,} tokens)")

    # --- LLM analysis ---
    print(f"  Model: {MODEL}")
    try:
        result = call_llm(diff, doc_pages, api_key)
    except Exception as e:
        # LLM failures are real errors — log them loudly but don't block merges
        msg = f"Docs drift check failed: {e}"
        print(f"  ERROR: {msg}", file=sys.stderr)
        comment = (
            "### Docs Drift Check\n\n"
            f"**Error:** LLM analysis failed — `{e}`\n\n"
            "This is a CI infrastructure issue, not a code problem. "
            "The docs drift check could not run."
        )
        if args.dry_run:
            print("\n--- PR Comment (dry run) ---")
            print(comment)
        else:
            post_pr_comment(comment)
        sys.exit(1)

    # --- Output ---
    findings = result.get("findings", [])
    summary = result.get("summary", "no summary")
    print(f"  Result: {summary}")

    for f in findings:
        print(f"  [{f.get('confidence', '?')}] {f['doc_page']}: {f['issue']}")

    comment = format_pr_comment(result, len(doc_pages))

    if args.dry_run:
        print("\n--- PR Comment (dry run) ---")
        print(comment)
    else:
        post_pr_comment(comment)


if __name__ == "__main__":
    main()
