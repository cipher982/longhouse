"""Unit coverage for scripts/ops/backfill-inline-media.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "backfill-inline-media.py"


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("inline_media_backfill_runner", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


runner = _load_runner_module()


def _args(tmp_path: Path, **overrides):
    values = {
        "api_url": "https://david010.longhouse.ai",
        "out": tmp_path / "pages.jsonl",
        "summary": tmp_path / "summary.json",
        "after_id": 0,
        "max_rows": 1000,
        "max_bytes": 50 * 1024 * 1024,
        "dry_run": True,
        "max_pages": None,
        "retries": 1,
        "retry_delay_s": 0,
        "compare_summary": None,
        "allow_skipped_budget": False,
        "started_at": "2026-06-27T00:00:00+00:00",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeClient:
    def __init__(self, rows):
        self.rows = list(rows)
        self.after_ids: list[int] = []

    def post_batch(self, *, after_id: int):
        self.after_ids.append(after_id)
        if not self.rows:
            raise AssertionError("unexpected extra batch")
        return dict(self.rows.pop(0))


def test_run_writes_jsonl_summary_and_stops_on_short_page(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {
                "dry_run": True,
                "scanned_source_lines": 1000,
                "candidate_refs": 2,
                "decoded_bytes": 25,
                "stored_objects": 0,
                "refs_upserted": 0,
                "skipped_existing_refs": 0,
                "skipped_budget": 0,
                "skipped_disk_floor": 0,
                "rejected": 1,
                "last_source_line_id": 100,
            },
            {
                "dry_run": True,
                "scanned_source_lines": 5,
                "candidate_refs": 1,
                "decoded_bytes": 10,
                "stored_objects": 0,
                "refs_upserted": 0,
                "skipped_existing_refs": 0,
                "skipped_budget": 0,
                "skipped_disk_floor": 0,
                "rejected": 0,
                "last_source_line_id": 105,
            },
        ]
    )

    summary = runner.run(_args(tmp_path), client)

    assert client.after_ids == [0, 100]
    assert summary["pages"] == 2
    assert summary["scanned_source_lines"] == 1005
    assert summary["candidate_refs"] == 3
    assert summary["decoded_bytes"] == 35
    assert summary["rejected"] == 1
    assert summary["last_source_line_id"] == 105
    assert summary["complete"] is True
    assert len(runner._read_jsonl(tmp_path / "pages.jsonl")) == 2
    assert json.loads((tmp_path / "summary.json").read_text())["complete"] is True


def test_run_resumes_from_existing_jsonl_cursor(tmp_path: Path) -> None:
    out = tmp_path / "pages.jsonl"
    out.write_text(
        json.dumps(
            {
                "scanned_source_lines": 1000,
                "candidate_refs": 0,
                "decoded_bytes": 0,
                "last_source_line_id": 777,
            }
        )
        + "\n"
    )
    client = FakeClient(
        [
            {
                "dry_run": True,
                "scanned_source_lines": 1,
                "candidate_refs": 0,
                "decoded_bytes": 0,
                "stored_objects": 0,
                "refs_upserted": 0,
                "skipped_existing_refs": 0,
                "skipped_budget": 0,
                "skipped_disk_floor": 0,
                "rejected": 0,
                "last_source_line_id": 778,
            }
        ]
    )

    runner.run(_args(tmp_path, out=out), client)

    assert client.after_ids == [777]
    assert len(runner._read_jsonl(out)) == 2


def test_run_resumes_past_trailing_null_cursor_page(tmp_path: Path) -> None:
    out = tmp_path / "pages.jsonl"
    out.write_text(
        json.dumps(
            {
                "scanned_source_lines": 1000,
                "candidate_refs": 1,
                "decoded_bytes": 5,
                "last_source_line_id": 777,
            }
        )
        + "\n"
        + json.dumps(
            {
                "scanned_source_lines": 0,
                "candidate_refs": 0,
                "decoded_bytes": 0,
                "last_source_line_id": None,
            }
        )
        + "\n"
    )
    client = FakeClient(
        [
            {
                "dry_run": True,
                "scanned_source_lines": 0,
                "candidate_refs": 0,
                "decoded_bytes": 0,
                "stored_objects": 0,
                "refs_upserted": 0,
                "skipped_existing_refs": 0,
                "skipped_budget": 0,
                "skipped_disk_floor": 0,
                "rejected": 0,
                "last_source_line_id": None,
            }
        ]
    )

    summary = runner.run(_args(tmp_path, out=out, max_pages=1), client)

    assert client.after_ids == [777]
    assert summary["last_source_line_id"] == 777
    assert summary["complete"] is True


def test_compare_summary_accepts_missing_optional_zero_counters(tmp_path: Path) -> None:
    expected = tmp_path / "expected.json"
    expected.write_text(
        json.dumps(
            {
                "dry_run": True,
                "max_rows": 1000,
                "max_bytes": 50 * 1024 * 1024,
                "pages": 1,
                "scanned_source_lines": 10,
                "candidate_refs": 1,
                "decoded_bytes": 5,
                "skipped_budget": 0,
                "rejected": 0,
                "last_source_line_id": 10,
                "final_page_scanned_source_lines": 10,
                "complete": True,
            }
        )
    )
    actual = {
        "dry_run": True,
        "max_rows": 1000,
        "max_bytes": 50 * 1024 * 1024,
        "pages": 1,
        "scanned_source_lines": 10,
        "candidate_refs": 1,
        "decoded_bytes": 5,
        "stored_objects": 0,
        "refs_upserted": 0,
        "skipped_existing_refs": 0,
        "skipped_budget": 0,
        "skipped_disk_floor": 0,
        "rejected": 0,
        "last_source_line_id": 10,
        "final_page_scanned_source_lines": 10,
        "complete": True,
    }

    assert runner.compare_summary(actual, expected) == []


def test_compare_summary_rejects_completion_mismatch(tmp_path: Path) -> None:
    expected = tmp_path / "expected.json"
    expected.write_text(
        json.dumps(
            {
                "dry_run": True,
                "max_rows": 1000,
                "max_bytes": 50 * 1024 * 1024,
                "pages": 1,
                "scanned_source_lines": 1000,
                "candidate_refs": 0,
                "decoded_bytes": 0,
                "stored_objects": 0,
                "refs_upserted": 0,
                "skipped_existing_refs": 0,
                "skipped_budget": 0,
                "skipped_disk_floor": 0,
                "rejected": 0,
                "last_source_line_id": 1000,
                "final_page_scanned_source_lines": 1000,
                "complete": True,
            }
        )
    )
    actual = json.loads(expected.read_text())
    actual["complete"] = False

    assert runner.compare_summary(actual, expected) == ["complete: actual=False expected=True"]


def test_run_fails_loudly_on_skipped_budget_without_ack(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {
                "dry_run": True,
                "scanned_source_lines": 1,
                "candidate_refs": 1,
                "decoded_bytes": 0,
                "stored_objects": 0,
                "refs_upserted": 0,
                "skipped_existing_refs": 0,
                "skipped_budget": 1,
                "skipped_disk_floor": 0,
                "rejected": 0,
                "last_source_line_id": 1,
            }
        ]
    )

    with pytest.raises(SystemExit) as exc:
        runner.run(_args(tmp_path), client)

    assert exc.value.code == 4


def test_validate_args_requires_apply_confirmation_and_baseline(tmp_path: Path) -> None:
    parser = runner.build_parser()
    args = parser.parse_args(["--apply", "--out", str(tmp_path / "apply.jsonl")])

    with pytest.raises(ValueError, match="--confirm-apply"):
        runner.validate_args(args)

    args.confirm_apply = True
    with pytest.raises(ValueError, match="--confirmed-backup"):
        runner.validate_args(args)

    args.confirmed_backup = True
    with pytest.raises(ValueError, match="--baseline-summary"):
        runner.validate_args(args)

    args.accept_new_baseline = True
    runner.validate_args(args)


def test_call_with_retries_retries_transient_failure(monkeypatch) -> None:
    calls = 0

    class FlakyClient:
        def post_batch(self, *, after_id: int):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary")
            return {"last_source_line_id": after_id + 1}

    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    row = runner._call_with_retries(FlakyClient(), after_id=10, attempts=2, retry_delay_s=0)

    assert row["last_source_line_id"] == 11
    assert calls == 2
