#!/usr/bin/env python3
"""Resumable operator runner for legacy inline media backfill.

This script drives the hosted API endpoint one bounded page at a time. It is
intentionally thin: the server owns scan/decode/write semantics, while this
runner owns cursor resume, retries, JSONL evidence, and apply gates.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen


DEFAULT_API_URL = "https://david010.longhouse.ai"
DEFAULT_TOKEN_FILE = Path("~/.longhouse/machine/device-token").expanduser()
DEFAULT_MAX_ROWS = 1000
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
SUMMARY_KEYS = (
    "dry_run",
    "max_rows",
    "max_bytes",
    "pages",
    "scanned_source_lines",
    "candidate_refs",
    "decoded_bytes",
    "stored_objects",
    "refs_upserted",
    "skipped_existing_refs",
    "skipped_budget",
    "skipped_disk_floor",
    "rejected",
    "last_source_line_id",
    "final_page_scanned_source_lines",
    "complete",
)
APPLY_BASELINE_KEYS = (
    "max_rows",
    "max_bytes",
    "pages",
    "scanned_source_lines",
    "candidate_refs",
    "decoded_bytes",
    "skipped_budget",
    "skipped_disk_floor",
    "rejected",
    "last_source_line_id",
    "final_page_scanned_source_lines",
)
OPTIONAL_ZERO_KEYS = frozenset(
    {
        "stored_objects",
        "refs_upserted",
        "skipped_existing_refs",
        "skipped_disk_floor",
    }
)


@dataclass(frozen=True)
class BackfillConfig:
    api_url: str
    token: str
    dry_run: bool
    max_rows: int
    max_bytes: int
    disk_floor_bytes: int
    confirmed_backup_gate: bool
    timeout_s: int


class BackfillClient:
    def __init__(self, config: BackfillConfig) -> None:
        self.config = config

    def post_batch(self, *, after_id: int) -> dict[str, Any]:
        params = {
            "dry_run": str(self.config.dry_run).lower(),
            "max_rows": self.config.max_rows,
            "max_bytes": self.config.max_bytes,
            "after_id": after_id,
            "confirmed_backup_gate": str(self.config.confirmed_backup_gate).lower(),
            "disk_floor_bytes": self.config.disk_floor_bytes,
        }
        url = (
            self.config.api_url.rstrip("/")
            + "/api/agents/media/backfill-inline-data-urls?"
            + urlencode(params)
        )
        request = Request(
            url,
            method="POST",
            headers={
                "Accept": "application/json",
                "User-Agent": "longhouse-inline-media-backfill/1",
                "X-Agents-Token": self.config.token,
            },
        )
        try:
            with urlopen(request, timeout=self.config.timeout_s) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if 400 <= exc.code < 500 and exc.code != 429:
                raise NonRetryableError(f"HTTP {exc.code}: {detail}") from exc
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON response: {raw[:200]}") from exc


class NonRetryableError(RuntimeError):
    """Failure that should not be retried by the operator loop."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_token(path: Path) -> str:
    token = path.read_text().strip()
    if not token:
        raise ValueError(f"empty token file: {path}")
    return token


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSONL row") from exc
    return rows


def _last_cursor(rows: list[dict[str, Any]], fallback: int | None = 0) -> int | None:
    for row in reversed(rows):
        raw = row.get("last_source_line_id")
        if raw is not None:
            return int(raw)
    return fallback


def summarize_rows(
    rows: list[dict[str, Any]],
    *,
    api_url: str,
    dry_run: bool,
    max_rows: int,
    max_bytes: int,
    out_path: Path,
) -> dict[str, Any]:
    def total(key: str) -> int:
        return sum(int(row.get(key) or 0) for row in rows)

    final = rows[-1] if rows else {}
    decoded_bytes = total("decoded_bytes")
    return {
        "api_url": api_url,
        "dry_run": dry_run,
        "max_rows": max_rows,
        "max_bytes": max_bytes,
        "pages": len(rows),
        "scanned_source_lines": total("scanned_source_lines"),
        "candidate_refs": total("candidate_refs"),
        "decoded_bytes": decoded_bytes,
        "decoded_mib": round(decoded_bytes / 1048576, 2),
        "stored_objects": total("stored_objects"),
        "refs_upserted": total("refs_upserted"),
        "skipped_existing_refs": total("skipped_existing_refs"),
        "skipped_budget": total("skipped_budget"),
        "skipped_disk_floor": total("skipped_disk_floor"),
        "rejected": total("rejected"),
        "last_source_line_id": _last_cursor(rows, fallback=None),
        "final_page_scanned_source_lines": int(final.get("scanned_source_lines") or 0),
        "complete": bool(rows) and int(final.get("scanned_source_lines") or 0) < max_rows,
        "pages_jsonl": str(out_path),
    }


def compare_summary(actual: dict[str, Any], expected_path: Path) -> list[str]:
    expected = json.loads(expected_path.read_text())
    return compare_summary_payload(actual, expected, keys=SUMMARY_KEYS)


def compare_summary_payload(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    keys: tuple[str, ...],
) -> list[str]:
    mismatches: list[str] = []
    for key in keys:
        actual_value = actual.get(key)
        expected_value = expected.get(key)
        if key in OPTIONAL_ZERO_KEYS and expected_value is None:
            expected_value = 0
        if actual_value != expected_value:
            mismatches.append(f"{key}: actual={actual_value!r} expected={expected_value!r}")
    return mismatches


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def _call_with_retries(
    client: BackfillClient,
    *,
    after_id: int,
    attempts: int,
    retry_delay_s: float,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.post_batch(after_id=after_id)
        except NonRetryableError:
            raise
        except Exception as exc:  # noqa: BLE001 - operator script should retry broad transient failures.
            last_error = exc
            if attempt == attempts:
                break
            sleep_s = retry_delay_s * min(8, 2 ** (attempt - 1))
            print(
                f"batch after_id={after_id} failed attempt {attempt}/{attempts}: {exc}; retrying in {sleep_s:.1f}s",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
    raise RuntimeError(f"batch after_id={after_id} failed after {attempts} attempts: {last_error}") from last_error


def run(args: argparse.Namespace, client: BackfillClient) -> dict[str, Any]:
    existing_rows = _read_jsonl(args.out)
    after_id = int(_last_cursor(existing_rows, args.after_id) or 0)
    if existing_rows:
        print(f"resuming from {args.out} after_id={after_id} existing_pages={len(existing_rows)}", file=sys.stderr)

    pages_written = 0
    while True:
        if args.max_pages is not None and pages_written >= args.max_pages:
            break

        row = _call_with_retries(
            client,
            after_id=after_id,
            attempts=args.retries,
            retry_delay_s=args.retry_delay_s,
        )
        row["runner_observed_at"] = _utc_now()
        _append_jsonl(args.out, row)
        pages_written += 1

        scanned = int(row.get("scanned_source_lines") or 0)
        candidates = int(row.get("candidate_refs") or 0)
        decoded = int(row.get("decoded_bytes") or 0)
        rejected = int(row.get("rejected") or 0)
        skipped_budget = int(row.get("skipped_budget") or 0)
        after_id = int(row.get("last_source_line_id") or after_id)
        if candidates or rejected or skipped_budget:
            print(
                "page="
                f"{len(existing_rows) + pages_written} after={after_id} scanned={scanned} "
                f"candidates={candidates} decoded={decoded} rejected={rejected} skipped_budget={skipped_budget}",
                file=sys.stderr,
            )
        if scanned < args.max_rows:
            break

    rows = _read_jsonl(args.out)
    summary = summarize_rows(
        rows,
        api_url=args.api_url,
        dry_run=args.dry_run,
        max_rows=args.max_rows,
        max_bytes=args.max_bytes,
        out_path=args.out,
    )
    summary["started_at"] = args.started_at
    summary["finished_at"] = _utc_now()
    summary["mode"] = "dry_run" if args.dry_run else "apply"
    if args.summary:
        _write_summary(args.summary, summary)

    if summary["skipped_budget"] and not args.allow_skipped_budget:
        print(
            f"skipped_budget={summary['skipped_budget']} requires --allow-skipped-budget",
            file=sys.stderr,
        )
        raise SystemExit(4)

    if args.compare_summary:
        mismatches = compare_summary(summary, args.compare_summary)
        if mismatches:
            for mismatch in mismatches:
                print(f"summary mismatch: {mismatch}", file=sys.stderr)
            raise SystemExit(3)

    if not args.dry_run and args.baseline_summary is not None and not args.accept_new_baseline:
        baseline = json.loads(args.baseline_summary.read_text())
        mismatches = compare_summary_payload(summary, baseline, keys=APPLY_BASELINE_KEYS)
        if mismatches:
            for mismatch in mismatches:
                print(f"apply baseline mismatch: {mismatch}", file=sys.stderr)
            raise SystemExit(3)

    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--out", type=Path, required=True, help="JSONL page log; existing files are resumed")
    parser.add_argument("--summary", type=Path, help="Optional path for final summary JSON")
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--disk-floor-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--timeout-s", type=int, default=90)
    parser.add_argument("--retries", type=int, default=6)
    parser.add_argument("--retry-delay-s", type=float, default=2.0)
    parser.add_argument("--max-pages", type=int, help="Stop after this many newly fetched pages")
    parser.add_argument("--compare-summary", type=Path, help="Fail unless final summary matches this baseline")
    parser.add_argument("--allow-skipped-budget", action="store_true", help="Do not fail when skipped_budget is nonzero")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    mode.add_argument("--apply", dest="dry_run", action="store_false")

    parser.add_argument("--confirm-apply", action="store_true", help="Required with --apply")
    parser.add_argument("--confirmed-backup", action="store_true", help="Required with --apply after backup verification")
    parser.add_argument("--baseline-summary", type=Path, help="Required with --apply unless accepting a new baseline")
    parser.add_argument(
        "--accept-new-baseline",
        action="store_true",
        help="Allow --apply without a baseline summary after manual PM/operator approval",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.max_rows <= 0 or args.max_rows > DEFAULT_MAX_ROWS:
        raise ValueError("--max-rows must be between 1 and 1000")
    if args.max_bytes <= 0 or args.max_bytes > DEFAULT_MAX_BYTES:
        raise ValueError("--max-bytes must be between 1 and 52428800")
    if args.retries <= 0:
        raise ValueError("--retries must be positive")
    if args.retry_delay_s < 0:
        raise ValueError("--retry-delay-s must be non-negative")
    if args.max_pages is not None and args.max_pages <= 0:
        raise ValueError("--max-pages must be positive")
    if not args.dry_run:
        if not args.confirm_apply:
            raise ValueError("--apply requires --confirm-apply")
        if not args.confirmed_backup:
            raise ValueError("--apply requires --confirmed-backup")
        if args.baseline_summary is None and not args.accept_new_baseline:
            raise ValueError("--apply requires --baseline-summary or --accept-new-baseline")
        if args.baseline_summary is not None:
            baseline = json.loads(args.baseline_summary.read_text())
            if baseline.get("dry_run") is not True or baseline.get("complete") is not True:
                raise ValueError("--baseline-summary must be a complete dry-run summary")
            if baseline.get("max_rows") != args.max_rows:
                raise ValueError("--baseline-summary max_rows does not match --max-rows")
            if baseline.get("max_bytes") != args.max_bytes:
                raise ValueError("--baseline-summary max_bytes does not match --max-bytes")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.started_at = _utc_now()
    try:
        validate_args(args)
        token = _read_token(args.token_file)
        client = BackfillClient(
            BackfillConfig(
                api_url=args.api_url,
                token=token,
                dry_run=args.dry_run,
                max_rows=args.max_rows,
                max_bytes=args.max_bytes,
                disk_floor_bytes=args.disk_floor_bytes,
                confirmed_backup_gate=args.confirmed_backup,
                timeout_s=args.timeout_s,
            )
        )
        run(args, client)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - operator script should fail with a clear message.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
