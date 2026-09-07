"""Read-only cohort diagnosis and finite qualification of historical publication.

The target is the desired revision captured once, not the continually advancing
head. Search counts are checked against catalog manifests at the publication's
own generation/revision fence. Embedding completion is the active projector's
ledger certificate, not an assertion that every event should produce a vector.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import typer

from zerg.embedding_space import EMBEDDING_PROJECTOR_ID

PROJECTORS = ("search-v2", EMBEDDING_PROJECTOR_ID)
MAX_COHORT_SESSIONS = 50_000
MAX_COHORT_BYTES = 16 * 1024 * 1024
PROJECTOR_CATEGORIES = ("complete", "pending", "failed_awaiting_retry", "failed_without_retry", "missing")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _cohort(path: Path) -> list[str]:
    with path.open("rb") as source:
        payload = source.read(MAX_COHORT_BYTES + 1)
    if len(payload) > MAX_COHORT_BYTES:
        raise ValueError("cohort exceeds the 16 MiB input bound")
    value = json.loads(payload)
    if isinstance(value, dict):
        value = value.get("sessions")
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_COHORT_SESSIONS:
        raise ValueError("cohort must be a nonempty array or a sessions manifest, with at most 50000 entries")
    sessions: dict[str, None] = {}
    for entry in value:
        session_id = entry.get("session_id") if isinstance(entry, dict) else entry
        if not isinstance(session_id, str):
            raise ValueError("each cohort entry must be a UUID string or an object with session_id")
        try:
            normalized = str(UUID(session_id))
        except ValueError:
            raise ValueError("cohort contains an invalid session UUID") from None
        sessions[normalized] = None
    return list(sessions)


@contextmanager
def _snapshot(path: Path, *, deadline: float, lock_timeout: float):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("read deadline exceeded")
    # Do not use immutable=1: it ignores live WAL contents. URI escaping also
    # prevents a filename containing '?' from changing the read-only contract.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=min(lock_timeout, remaining))
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        # BEGIN is deferred. Materialize the snapshot before opening the other
        # database so a search publication never precedes its catalog objects.
        connection.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
        yield connection
    finally:
        connection.close()


def _one(connection: sqlite3.Connection, query: str, parameters: tuple = ()) -> dict[str, Any] | None:
    row = connection.execute(query, parameters).fetchone()
    return dict(row) if row is not None else None


def _session(connection: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    session = _one(
        connection,
        "SELECT current_render_generation, render_state, user_state FROM sessions WHERE session_id = ?",
        (session_id,),
    )
    tombstone = _one(connection, "SELECT deletion_revision FROM session_tombstones WHERE session_id = ?", (session_id,))
    lifecycle = (
        "tombstoned"
        if tombstone is not None or (session is not None and session["user_state"] == "deleted")
        else "missing"
        if session is None
        else "retired"
        if session["render_state"] == "retired"
        else "active"
    )
    generation_id = session["current_render_generation"] if session else None
    generation = _one(
        connection,
        "SELECT generation_id FROM render_generations WHERE session_id = ? AND generation_id = ?",
        (session_id, generation_id),
    )
    return {"lifecycle": lifecycle, "generation_id": generation_id, "generation_exists": generation is not None}


def _states(connection: sqlite3.Connection, session_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """SELECT projector, desired_revision, completed_revision, claimed_revision,
                  status, failure_count, last_error_code, retry_at
           FROM projector_state WHERE session_id = ? AND projector IN (?, ?)""",
        (session_id, *PROJECTORS),
    )
    return {row["projector"]: dict(row) for row in rows}


def _counts(connection: sqlite3.Connection, session_id: str, generation_id: str | None, revision: int | None) -> dict[str, int]:
    # This is storage.session.render_objects.list.v2's immutable membership
    # predicate, not retired_at IS NULL (which would erase historical fences).
    fence = "retired_at IS NULL" if revision is None else "commit_seq <= ? AND (retirement_revision IS NULL OR retirement_revision > ?)"
    parameters = (session_id, generation_id) if revision is None else (session_id, generation_id, revision, revision)
    row = connection.execute(
        f"SELECT COUNT(*), COALESCE(SUM(event_count), 0) FROM render_objects WHERE session_id = ? AND generation_id = ? AND {fence}",
        parameters,
    ).fetchone()
    return {"object_count": int(row[0]), "event_count": int(row[1])}


def capture_targets(connection: sqlite3.Connection, session_ids: list[str], *, deadline: float) -> list[dict[str, Any]]:
    targets = []
    for session_id in session_ids:
        if time.monotonic() >= deadline:
            raise TimeoutError("target capture deadline exceeded")
        session = _session(connection, session_id)
        states = _states(connection, session_id)
        revisions = {projector: states[projector]["desired_revision"] if projector in states else None for projector in PROJECTORS}
        current = _counts(connection, session_id, session["generation_id"], None)
        fences = {
            revision: _counts(connection, session_id, session["generation_id"], revision)
            for revision in set(revisions.values())
            if revision is not None
        }
        targets.append(
            {
                "session_id": session_id,
                **session,
                "desired_revisions": revisions,
                "current_render": current,
                "fences": {projector: fences.get(revision) for projector, revision in revisions.items()},
            }
        )
    return targets


def _projector(state: dict[str, Any] | None, revision: int | None) -> dict[str, Any]:
    if state is None:
        return {"category": "missing", "target_revision": revision, "target_complete": False, "row_missing": True}
    complete = revision is not None and state["completed_revision"] >= revision
    category = (
        "missing"
        if revision is None
        else "complete"
        if complete
        else "failed_awaiting_retry"
        if state["status"] == "failed" and state["retry_at"] is not None
        else "failed_without_retry"
        if state["status"] in {"failed", "quarantined"}
        else "pending"
    )
    # Never copy last_error_message: provider exceptions can contain source text
    # or credentials. Error codes are a bounded machine vocabulary only.
    error_code = state["last_error_code"]
    if error_code is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None:
        error_code = "unrecognized_error_code"
    return {
        "category": category,
        "target_revision": revision,
        "target_complete": complete,
        "captured_target_missing": revision is None,
        "desired_revision": state["desired_revision"],
        "completed_revision": state["completed_revision"],
        "claimed_revision": state["claimed_revision"],
        "status": state["status"],
        "failure_count": state["failure_count"],
        "last_error_code": error_code,
        "retry_at": state["retry_at"],
    }


def observe_targets(
    catalog: sqlite3.Connection, search: sqlite3.Connection, targets: list[dict[str, Any]], *, deadline: float
) -> dict[str, Any]:
    store = _one(search, "SELECT store_id, schema_generation FROM search_meta WHERE singleton = 1")
    bindings = {}
    for projector in PROJECTORS:
        binding = _one(catalog, "SELECT store_id, schema_generation FROM projector_store_bindings WHERE projector = ?", (projector,))
        bindings[projector] = {
            "category": "missing" if binding is None or store is None else "matched" if binding == store else "mismatch",
            "catalog_binding": binding,
            "search_store": store,
        }
    rows = []
    for target in targets:
        if time.monotonic() >= deadline:
            raise TimeoutError("observation deadline exceeded")
        session_id = target["session_id"]
        current = _session(catalog, session_id)
        states = _states(catalog, session_id)
        projectors = {name: _projector(states.get(name), target["desired_revisions"][name]) for name in PROJECTORS}
        failures = []
        for name, fence in target["fences"].items():
            if (
                target["lifecycle"] == "active"
                and target["current_render"]["event_count"] > 0
                and fence is not None
                and fence["event_count"] == 0
            ):
                failures.append({"category": "nonempty_render_empty_target_fence", "projector": name})
        publication = _one(
            search,
            """SELECT generation_id, desired_revision, indexed_through, object_count, event_count, tombstoned
               FROM session_index WHERE session_id = ?""",
            (session_id,),
        )
        removed = current["lifecycle"] in {"tombstoned", "retired"}
        publication_complete = False
        if publication is None or publication["tombstoned"]:
            publication_category = current["lifecycle"] if removed else "missing" if publication is None else "tombstoned"
            publication_complete = removed
        elif removed:
            publication_category = "pending_removal"
        else:
            generation = _one(
                catalog,
                "SELECT generation_id FROM render_generations WHERE session_id = ? AND generation_id = ?",
                (session_id, publication["generation_id"]),
            )
            published_fence = _counts(catalog, session_id, publication["generation_id"], publication["indexed_through"])
            publication["catalog_fence"] = published_fence
            mismatches = []
            if generation is None:
                mismatches.append("generation_missing")
            if publication["desired_revision"] != publication["indexed_through"]:
                mismatches.append("publication_revision_mismatch")
            for count in ("object_count", "event_count"):
                if publication[count] != published_fence[count]:
                    mismatches.append(f"published_{count}_mismatch")
            search_revision = target["desired_revisions"]["search-v2"]
            if publication["indexed_through"] == search_revision and publication["generation_id"] != target["generation_id"]:
                mismatches.append("target_generation_mismatch")
            failures.extend({"category": category} for category in mismatches)
            captured_revisions = list(target["desired_revisions"].values())
            publication_complete = not mismatches and all(
                revision is not None and publication["indexed_through"] >= revision for revision in captured_revisions
            )
            publication_category = "invariant_failure" if mismatches else "complete" if publication_complete else "pending"
        complete = (
            not failures
            and target["lifecycle"] != "missing"
            and (target["lifecycle"] != "active" or target["generation_exists"])
            and current["lifecycle"] != "missing"
            and (removed or target["generation_exists"])
            and publication_complete
            and all(value["target_complete"] for value in projectors.values())
            and all(value["category"] == "matched" for value in bindings.values())
        )
        rows.append(
            {
                "session_id": session_id,
                "lifecycle": current["lifecycle"],
                "target_complete": complete,
                "projectors": projectors,
                "publication": {"category": publication_category, "target_complete": publication_complete, "published": publication},
                "invariant_failures": failures,
            }
        )
    return {
        "observed_at": _now(),
        "bindings": bindings,
        "sessions": rows,
        "summary": {
            "cohort_sessions": len(rows),
            "target_complete_sessions": sum(row["target_complete"] for row in rows),
            "lifecycle": dict(Counter(row["lifecycle"] for row in rows)),
            "projectors": {
                name: {category: sum(row["projectors"][name]["category"] == category for row in rows) for category in PROJECTOR_CATEGORIES}
                for name in PROJECTORS
            },
            "publication": dict(Counter(row["publication"]["category"] for row in rows)),
            "invariant_failures": dict(Counter(failure["category"] for row in rows for failure in row["invariant_failures"])),
        },
    }


def _retain(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def check_convergence(
    *,
    catalog_db: Path,
    search_db: Path,
    cohort: Path,
    output: Path,
    wait: bool = False,
    timeout: float = 600,
    read_timeout: float = 120,
    lock_timeout: float = 5,
    poll_interval: float = 10,
) -> dict[str, Any]:
    for value in (timeout, read_timeout, lock_timeout, poll_interval):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeouts and polling interval must be positive finite seconds")
    protected = {path.resolve() for path in (catalog_db, search_db, cohort)}
    protected.update(Path(str(path.resolve()) + suffix) for path in (catalog_db, search_db) for suffix in ("-wal", "-shm", "-journal"))
    if output.resolve() in protected or any(output.exists() and path.exists() and output.samefile(path) for path in protected):
        raise ValueError("output must not overwrite an input database, sidecar, or cohort")
    started = time.monotonic()
    deadline = started + timeout
    report: dict[str, Any] = {
        "status": "pending",
        "mode": "wait" if wait else "snapshot",
        "started_at": _now(),
        "catalog_db": str(catalog_db),
        "search_db": str(search_db),
        "cohort": str(cohort),
        "active_embedding_projector": EMBEDDING_PROJECTOR_ID,
        "completion_contract": "captured desired revisions; exact published search fence and active embedding ledger; removed sessions reported separately",
        "timeout_seconds": timeout,
        "observations": 0,
    }
    try:
        phase = "cohort"
        session_ids = _cohort(cohort)
        phase = "capture"
        read_deadline = min(deadline, time.monotonic() + read_timeout)
        with _snapshot(catalog_db, deadline=read_deadline, lock_timeout=lock_timeout) as catalog:
            report["targets"] = capture_targets(catalog, session_ids, deadline=read_deadline)
        report["captured_at"] = _now()
        _retain(output, report)
        while True:
            phase = "observation"
            read_deadline = min(deadline, time.monotonic() + read_timeout)
            # Search first, catalog second: a newer catalog can reconstruct an
            # older immutable publication. The opposite order can falsely label
            # a publication committed between reads as an absent manifest.
            with _snapshot(search_db, deadline=read_deadline, lock_timeout=lock_timeout) as search:
                with _snapshot(catalog_db, deadline=read_deadline, lock_timeout=lock_timeout) as catalog:
                    observation = observe_targets(catalog, search, report["targets"], deadline=read_deadline)
            report.update(observation)
            report["observations"] += 1
            report.setdefault("initial_summary", observation["summary"])
            summary = observation["summary"]
            if summary["invariant_failures"] or any(binding["category"] == "mismatch" for binding in observation["bindings"].values()):
                report["status"] = "invariant_failure"
            elif summary["target_complete_sessions"] == len(session_ids):
                report["status"] = "complete"
            elif time.monotonic() >= deadline:
                report["status"] = "timeout"
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            _retain(output, report)
            if report["status"] != "pending" or not wait:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report["status"] = "timeout"
                break
            time.sleep(min(poll_interval, remaining))
    except (OSError, sqlite3.Error, ValueError, TimeoutError) as exc:
        report["status"] = "timeout" if time.monotonic() >= deadline else "inspection_error"
        report["error"] = {
            "category": getattr(exc, "sqlite_errorname", type(exc).__name__),
            "phase": phase,
            "action": "Check explicit paths, cohort UUID format, current catalog/search schema, permissions, and read/lock deadlines; no database was modified.",
        }
    report["finished_at"] = _now()
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    _retain(output, report)
    return report


def historical_convergence(
    catalog_db: Path = typer.Option(..., "--catalog-db", help="Authoritative catalog SQLite file; opened read-only."),
    search_db: Path = typer.Option(..., "--search-db", help="Search SQLite file; opened read-only with live WAL visibility."),
    cohort: Path = typer.Option(
        ..., "--cohort", help="JSON array of session UUIDs/objects, or prior {sessions: [...]} candidate manifest."
    ),
    output: Path = typer.Option(
        ..., "--output", help="Retained private JSON summary; never contains transcript text or provider error messages."
    ),
    wait: bool = typer.Option(
        False, "--wait", help="Require all captured targets to complete; incomplete/timeout exits nonzero. Default is diagnostic only."
    ),
    timeout: float = typer.Option(600, "--timeout", min=0.01, help="Total finite deadline in seconds, including capture and reads."),
    read_timeout: float = typer.Option(
        120, "--read-timeout", min=0.01, help="Maximum seconds per read snapshot, also capped by total deadline."
    ),
    lock_timeout: float = typer.Option(
        5, "--lock-timeout", min=0.01, help="Maximum SQLite lock wait in seconds, capped by remaining read deadline."
    ),
    poll_interval: float = typer.Option(
        10, "--poll-interval", min=0.01, help="Seconds between observations; no database transaction held while sleeping."
    ),
) -> None:
    """Diagnose historical convergence, or qualify a fixed cohort with --wait.

    Snapshot mode may exit zero with status=pending; it is NOT convergence
    qualification. Invariant/read failures always fail. --wait exits zero only
    on complete, including both projector ledgers and exact search publication.
    """
    try:
        report = check_convergence(
            catalog_db=catalog_db,
            search_db=search_db,
            cohort=cohort,
            output=output,
            wait=wait,
            timeout=timeout,
            read_timeout=read_timeout,
            lock_timeout=lock_timeout,
            poll_interval=poll_interval,
        )
    except (OSError, ValueError):
        typer.echo("Invalid deadline/output path, or unable to retain the JSON summary.", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        json.dumps(
            {key: report[key] for key in ("status", "mode", "observations", "elapsed_seconds")}
            | {"output": str(output), "summary": report.get("summary")}
        )
    )
    if report["status"] != "complete" and (wait or report["status"] != "pending"):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    typer.run(historical_convergence)
