"""Public demo presentation contract: titles and summaries for the demo corpus."""

from __future__ import annotations

# This is the public capture contract. `demo_database` builds
# the corpus from it for `longhouse-server serve --demo` and screenshot captures.
DEMO_PRESENTATION = {
    "demo-claude-01": (
        "Ship semantic search across 12,000 sessions",
        "Added embedding-backed recall with an FTS fallback, migration coverage, and a clean timeline toggle.",
    ),
    "demo-codex-01": (
        "Eliminate duplicate session ingest under load",
        "Traced a check-then-insert race and replaced it with a database-enforced idempotent ingest path.",
    ),
    "demo-antigravity-01": (
        "Fix the empty morning digest",
        "Found a timezone mismatch in the reporting window and verified the next scheduled delivery end to end.",
    ),
    "demo-claude-02": (
        "Add fast watermark detection to the photo pipeline",
        "Built a lightweight OpenCV preflight, friendly validation errors, and representative image tests.",
    ),
    "demo-codex-02": (
        "Polish the timeline search experience",
        "Wired the semantic-search control into the UI and validated responsive layout, types, and lint.",
    ),
    "demo-claude-03": (
        "Recover a production host from a memory spike",
        "Isolated an unbounded embedding backfill, restored headroom without downtime, and made processing stream in batches.",
    ),
    "demo-antigravity-02": (
        "Make session recall actionable",
        "Connected related past work to the active session and kept the recovery path visible while editing.",
    ),
    "demo-claude-04": (
        "Map failure patterns across every project",
        "Compared recent incidents across repositories and distilled recurring causes into practical guardrails.",
    ),
    "demo-codex-03": (
        "Repair the release image build",
        "Diagnosed the missing native dependency in CI and verified the rebuilt runtime image passed.",
    ),
    "demo-claude-05": (
        "Resolve concurrent OAuth refresh races",
        "Tracing a duplicated refresh-token path so multiple browser tabs recover cleanly without invalidating each other.",
    ),
}
