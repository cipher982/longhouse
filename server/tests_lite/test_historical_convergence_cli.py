"""A cohort check must not confuse an empty or incomplete fence with convergence."""

import json
import sqlite3
import time
from uuid import uuid4

import pytest
import typer
from typer.testing import CliRunner

from zerg.cli.historical_convergence import PROJECTORS
from zerg.cli.historical_convergence import _snapshot
from zerg.cli.historical_convergence import capture_targets
from zerg.cli.historical_convergence import historical_convergence
from zerg.cli.historical_convergence import observe_targets
from zerg.embedding_space import EMBEDDING_PROJECTOR_ID


@pytest.fixture
def cohort_store(tmp_path):
    catalog, search, cohort = (tmp_path / name for name in ("catalog.db", "search.db", "cohort.json"))
    session_id, generation_id = str(uuid4()), str(uuid4())
    with sqlite3.connect(catalog) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, current_render_generation TEXT, render_state TEXT, user_state TEXT);
            CREATE TABLE session_tombstones (session_id TEXT PRIMARY KEY, deletion_revision INTEGER);
            CREATE TABLE render_generations (generation_id TEXT PRIMARY KEY, session_id TEXT);
            CREATE TABLE render_objects (object_id TEXT PRIMARY KEY, session_id TEXT, generation_id TEXT, event_count INTEGER,
                                         commit_seq INTEGER, retired_at TEXT, retirement_revision INTEGER);
            CREATE INDEX render_session ON render_objects(session_id);
            CREATE TABLE projector_state (projector TEXT, session_id TEXT, desired_revision INTEGER, completed_revision INTEGER,
                                          claimed_revision INTEGER, status TEXT, failure_count INTEGER, last_error_code TEXT,
                                          retry_at TEXT, last_error_message TEXT, PRIMARY KEY(projector, session_id));
            CREATE TABLE projector_store_bindings (projector TEXT PRIMARY KEY, store_id TEXT, schema_generation TEXT);
            """
        )
        connection.execute("INSERT INTO sessions VALUES (?, ?, 'ready', 'active')", (session_id, generation_id))
        connection.execute("INSERT INTO render_generations VALUES (?, ?)", (generation_id, session_id))
        connection.execute("INSERT INTO render_objects VALUES ('object-a', ?, ?, 2, 10, NULL, NULL)", (session_id, generation_id))
        for projector in PROJECTORS:
            connection.execute(
                "INSERT INTO projector_state VALUES (?, ?, 10, 10, NULL, 'idle', 0, NULL, NULL, NULL)", (projector, session_id)
            )
            connection.execute("INSERT INTO projector_store_bindings VALUES (?, 'store', 'schema')", (projector,))
    with sqlite3.connect(search) as connection:
        connection.executescript(
            """
            CREATE TABLE search_meta (singleton INTEGER PRIMARY KEY, store_id TEXT, schema_generation TEXT);
            INSERT INTO search_meta VALUES (1, 'store', 'schema');
            CREATE TABLE session_index (session_id TEXT PRIMARY KEY, generation_id TEXT, desired_revision INTEGER,
                                        indexed_through INTEGER, object_count INTEGER, event_count INTEGER, tombstoned INTEGER);
            """
        )
        connection.execute("INSERT INTO session_index VALUES (?, ?, 10, 10, 1, 2, 0)", (session_id, generation_id))
    cohort.write_text(json.dumps({"sessions": [{"session_id": session_id}]}))
    return catalog, search, cohort, session_id


def _invoke(cohort_store, output, *extra):
    catalog, search, cohort, _ = cohort_store
    app = typer.Typer()
    app.command()(historical_convergence)
    result = CliRunner().invoke(
        app,
        ["--catalog-db", str(catalog), "--search-db", str(search), "--cohort", str(cohort), "--output", str(output), *extra],
    )
    return result, json.loads(output.read_text())


@pytest.mark.parametrize("gap", ["failed_embedding", "missing_embedding", "missing_publication"])
def test_diagnostic_pending_is_not_a_successful_wait(cohort_store, tmp_path, gap):
    catalog, search, _, _ = cohort_store
    if gap == "failed_embedding":
        with sqlite3.connect(catalog) as connection:
            connection.execute(
                """UPDATE projector_state SET completed_revision = 0, status = 'failed', failure_count = 1,
                   last_error_code = 'embedding_projection_failed', retry_at = '2099-01-01T00:00:00Z',
                   last_error_message = 'PRIVATE TRANSCRIPT AND CREDENTIAL' WHERE projector = ?""",
                (EMBEDDING_PROJECTOR_ID,),
            )
    elif gap == "missing_embedding":
        with sqlite3.connect(catalog) as connection:
            connection.execute("DELETE FROM projector_state WHERE projector = ?", (EMBEDDING_PROJECTOR_ID,))
    else:
        with sqlite3.connect(search) as connection:
            connection.execute("DELETE FROM session_index")
    diagnostic, snapshot = _invoke(cohort_store, tmp_path / "snapshot.json")
    assert diagnostic.exit_code == 0
    assert snapshot["status"] == "pending"
    result, qualification = _invoke(cohort_store, tmp_path / "wait.json", "--wait", "--timeout", "1", "--poll-interval", "0.05")
    assert result.exit_code == 1
    assert qualification["status"] == "timeout"
    assert qualification["summary"]["target_complete_sessions"] == 0
    if gap == "missing_publication":
        assert qualification["summary"]["publication"] == {"missing": 1}
    else:
        category = "failed_awaiting_retry" if gap == "failed_embedding" else "missing"
        assert qualification["summary"]["projectors"][EMBEDDING_PROJECTOR_ID][category] == 1
    assert "PRIVATE TRANSCRIPT AND CREDENTIAL" not in (tmp_path / "wait.json").read_text()


def test_nonempty_history_with_empty_target_fence_fails_even_diagnostic(cohort_store, tmp_path):
    catalog, search, _, _ = cohort_store
    # The original defect advanced immutable object commit_seq without advancing
    # the projector's desired fence. Both ledgers then certified an empty render.
    with sqlite3.connect(catalog) as connection:
        connection.execute("UPDATE render_objects SET commit_seq = 20")
    with sqlite3.connect(search) as connection:
        connection.execute("UPDATE session_index SET object_count = 0, event_count = 0")
    before = (catalog.read_bytes(), search.read_bytes())
    result, report = _invoke(cohort_store, tmp_path / "empty-fence.json")
    assert result.exit_code == 1
    assert report["status"] == "invariant_failure"
    assert report["summary"]["invariant_failures"] == {"nonempty_render_empty_target_fence": 2}
    assert (catalog.read_bytes(), search.read_bytes()) == before


def test_newer_publication_uses_its_own_fence_without_moving_targets(cohort_store):
    catalog, search, _, session_id = cohort_store
    deadline = time.monotonic() + 30
    with _snapshot(catalog, deadline=deadline, lock_timeout=1) as connection:
        targets = capture_targets(connection, [session_id], deadline=deadline)
    replacement = str(uuid4())
    with sqlite3.connect(catalog) as connection:
        # A superseded object still belongs to the older immutable fence, even
        # though it no longer contributes to the current generation.
        connection.execute("UPDATE render_objects SET retired_at = 'now', retirement_revision = 20")
        connection.execute("INSERT INTO render_generations VALUES (?, ?)", (replacement, session_id))
        connection.execute("INSERT INTO render_objects VALUES ('object-b', ?, ?, 3, 20, NULL, NULL)", (session_id, replacement))
        connection.execute("UPDATE sessions SET current_render_generation = ?", (replacement,))
        connection.execute("UPDATE projector_state SET desired_revision = 30")
    with _snapshot(search, deadline=deadline, lock_timeout=1) as s:
        with _snapshot(catalog, deadline=deadline, lock_timeout=1) as c:
            older = observe_targets(c, s, targets, deadline=deadline)
    assert older["summary"]["target_complete_sessions"] == 1
    assert older["summary"]["invariant_failures"] == {}
    with sqlite3.connect(catalog) as connection:
        connection.execute("UPDATE projector_state SET completed_revision = 20")
    with sqlite3.connect(search) as connection:
        connection.execute(
            "UPDATE session_index SET generation_id = ?, desired_revision = 20, indexed_through = 20, event_count = 3", (replacement,)
        )
    with _snapshot(search, deadline=deadline, lock_timeout=1) as s:
        with _snapshot(catalog, deadline=deadline, lock_timeout=1) as c:
            newer = observe_targets(c, s, targets, deadline=deadline)
    assert newer["summary"]["target_complete_sessions"] == 1
    assert newer["summary"]["invariant_failures"] == {}
    assert newer["sessions"][0]["publication"]["published"]["catalog_fence"] == {"object_count": 1, "event_count": 3}
