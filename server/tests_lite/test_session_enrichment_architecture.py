"""Architecture guardrails for session enrichment."""

from __future__ import annotations

from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
PROD_ROOT = SERVER_ROOT / "zerg"


def _production_python_files() -> list[Path]:
    return sorted(path for path in PROD_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def test_summary_embedding_task_queue_runtime_paths_do_not_return():
    forbidden = (
        "ingest_task_queue",
        "COLD_INGEST_WORKER_CONCURRENCY",
        'task_type == "summary"',
        "task_type == 'summary'",
        'task_type == "embedding"',
        "task_type == 'embedding'",
    )
    offenders: list[str] = []
    for path in _production_python_files():
        if path.relative_to(SERVER_ROOT).as_posix() == "zerg/models/agents.py":
            continue
        text = path.read_text()
        for pattern in forbidden:
            if pattern in text:
                offenders.append(f"{path.relative_to(SERVER_ROOT)} contains {pattern!r}")

    assert offenders == []
