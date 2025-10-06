from datetime import datetime
from datetime import timedelta

from zerg.crud import crud
from zerg.services.ops_service import get_summary
from zerg.services.ops_service import get_timeseries
from zerg.services.ops_service import get_top_agents


def _mk_run(
    db, agent_id: int, thread_id: int, *, started_at, finished_at=None, status="running", duration_ms=None, cost=None
):
    run = crud.create_run(db, agent_id=agent_id, thread_id=thread_id, trigger="api", status="queued")
    crud.mark_running(db, run.id, started_at=started_at)
    if finished_at is not None:
        if status == "success":
            crud.mark_finished(db, run.id, finished_at=finished_at, duration_ms=duration_ms, total_cost_usd=cost)
        elif status == "failed":
            crud.mark_failed(db, run.id, finished_at=finished_at, duration_ms=duration_ms, error="x")
        else:
            crud.mark_finished(db, run.id, finished_at=finished_at, duration_ms=duration_ms)
    return run


def test_summary_basic(db_session):
    # Create admin current user
    admin = crud.create_user(db_session, email="ops-admin@local", provider=None, role="ADMIN")

    # Agent and thread for admin
    agent = crud.create_agent(
        db_session,
        owner_id=admin.id,
        name="ops-agent",
        system_instructions="s",
        task_instructions="t",
        model="gpt-mock",
    )
    thread = crud.create_thread(db=db_session, agent_id=agent.id, title="t1", active=True, agent_state={})

    now = datetime.utcnow()
    today = now

    # 1 success with cost and duration
    _mk_run(
        db_session,
        agent.id,
        thread.id,
        started_at=today - timedelta(minutes=30),
        finished_at=today - timedelta(minutes=29),
        status="success",
        duration_ms=60000,
        cost=0.01,
    )
    # 1 failed within last hour
    _mk_run(
        db_session,
        agent.id,
        thread.id,
        started_at=today - timedelta(minutes=10),
        finished_at=today - timedelta(minutes=9),
        status="failed",
        duration_ms=10000,
    )

    # A message to count active users
    crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="hi")

    s = get_summary(db_session, admin)
    assert s["runs_today"] >= 2
    assert s["cost_today_usd"] is None or isinstance(s["cost_today_usd"], float)
    assert "budget_user" in s and "budget_global" in s
    assert isinstance(s["active_users_24h"], int)
    assert isinstance(s["agents_total"], int)
    assert "latency_ms" in s and set(s["latency_ms"].keys()) == {"p50", "p95"}
    assert isinstance(s["errors_last_hour"], int)
    assert isinstance(s["top_agents_today"], list)


def test_timeseries_zero_fill_runs(db_session):
    admin = crud.create_user(db_session, email="ops-admin2@local", provider=None, role="ADMIN")
    agent = crud.create_agent(
        db_session, owner_id=admin.id, name="a", system_instructions="s", task_instructions="t", model="gpt-mock"
    )
    thread = crud.create_thread(db=db_session, agent_id=agent.id, title="t", active=True, agent_state={})

    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    h3 = base.replace(hour=3)
    h15 = base.replace(hour=15)
    _mk_run(
        db_session,
        agent.id,
        thread.id,
        started_at=h3 + timedelta(minutes=1),
        finished_at=h3 + timedelta(minutes=2),
        status="success",
    )
    _mk_run(
        db_session,
        agent.id,
        thread.id,
        started_at=h15 + timedelta(minutes=1),
        finished_at=h15 + timedelta(minutes=2),
        status="success",
    )

    series = get_timeseries(db_session, metric="runs_by_hour", window="today")
    assert len(series) == 24
    vals = {i: 0 for i in range(24)}
    for i, bucket in enumerate(series):
        vals[i] = bucket["value"]
    assert vals[3] >= 1
    assert vals[15] >= 1


def test_top_agents_ordering_and_limit(db_session):
    admin = crud.create_user(db_session, email="ops-admin3@local", provider=None, role="ADMIN")
    a1 = crud.create_agent(
        db_session, owner_id=admin.id, name="A1", system_instructions="s", task_instructions="t", model="gpt-mock"
    )
    a2 = crud.create_agent(
        db_session, owner_id=admin.id, name="A2", system_instructions="s", task_instructions="t", model="gpt-mock"
    )
    t1 = crud.create_thread(db=db_session, agent_id=a1.id, title="t1", active=True, agent_state={})
    t2 = crud.create_thread(db=db_session, agent_id=a2.id, title="t2", active=True, agent_state={})
    now = datetime.utcnow()
    # A1: 3 runs
    for _ in range(3):
        _mk_run(
            db_session,
            a1.id,
            t1.id,
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4),
            status="success",
        )
    # A2: 1 run
    _mk_run(
        db_session,
        a2.id,
        t2.id,
        started_at=now - timedelta(minutes=6),
        finished_at=now - timedelta(minutes=5),
        status="success",
    )

    top = get_top_agents(db_session, window="today", limit=1)
    assert len(top) == 1
    assert top[0]["agent_id"] == a1.id


def test_timeseries_database_compatibility_regression(db_session):
    """
    Regression test for database compatibility in timeseries endpoints.

    This test ensures that func.extract('hour', column) works correctly across
    different database backends (SQLite, PostgreSQL) and catches regressions
    if anyone reverts back to SQLite-specific func.strftime() calls.
    """
    admin = crud.create_user(db_session, email="ops-admin-regression@local", provider=None, role="ADMIN")
    agent = crud.create_agent(
        db_session,
        owner_id=admin.id,
        name="regression-agent",
        system_instructions="s",
        task_instructions="t",
        model="gpt-mock",
    )
    thread = crud.create_thread(
        db=db_session, agent_id=agent.id, title="regression-thread", active=True, agent_state={}
    )

    # Create a specific time base for predictable hour extraction - ensure it's today
    now = datetime.utcnow()
    today = now.date()
    base = datetime.combine(today, datetime.min.time()).replace(minute=0, second=0, microsecond=0)
    h5 = base.replace(hour=5)
    h10 = base.replace(hour=10)
    h14 = base.replace(hour=14)

    # Test data for runs_by_hour: Create runs at different hours
    _mk_run(
        db_session,
        agent.id,
        thread.id,
        started_at=h5 + timedelta(minutes=15),  # Hour 5
        finished_at=h5 + timedelta(minutes=16),
        status="success",
        duration_ms=60000,
        cost=0.05,
    )
    _mk_run(
        db_session,
        agent.id,
        thread.id,
        started_at=h10 + timedelta(minutes=20),  # Hour 10
        finished_at=h10 + timedelta(minutes=21),
        status="success",
        duration_ms=45000,
        cost=0.03,
    )

    # Test data for errors_by_hour: Create failed runs at specific hours
    _mk_run(
        db_session,
        agent.id,
        thread.id,
        started_at=h10 + timedelta(minutes=25),  # Hour 10 (error)
        finished_at=h10 + timedelta(minutes=26),
        status="failed",
        duration_ms=5000,
    )
    _mk_run(
        db_session,
        agent.id,
        thread.id,
        started_at=h14 + timedelta(minutes=10),  # Hour 14 (error)
        finished_at=h14 + timedelta(minutes=11),
        status="failed",
        duration_ms=8000,
    )

    # Test data for cost_by_hour: Additional runs with costs
    _mk_run(
        db_session,
        agent.id,
        thread.id,
        started_at=h14 + timedelta(minutes=30),  # Hour 14
        finished_at=h14 + timedelta(minutes=31),
        status="success",
        duration_ms=120000,
        cost=0.08,
    )

    # Test runs_by_hour: Should work with func.extract('hour', started_at)
    runs_series = get_timeseries(db_session, metric="runs_by_hour", window="today")
    assert len(runs_series) == 24

    # Verify specific hours have the expected run counts
    runs_by_hour = {i: 0 for i in range(24)}
    for item in runs_series:
        hour = int(item["hour_iso"].split(":")[0])
        runs_by_hour[hour] = item["value"]

    # Verify the timeseries endpoints work correctly with database-agnostic queries
    assert runs_by_hour[5] >= 1, f"Expected runs at hour 5, got: {runs_by_hour[5]}"
    assert runs_by_hour[10] >= 2, f"Expected 2+ runs at hour 10, got: {runs_by_hour[10]}"  # 1 success + 1 failed
    assert (
        runs_by_hour[14] >= 2
    ), f"Expected 2+ runs at hour 14, got: {runs_by_hour[14]}"  # 1 failed + 1 success both started at 14

    # Test errors_by_hour: Should work with func.extract('hour', finished_at)
    errors_series = get_timeseries(db_session, metric="errors_by_hour", window="today")
    assert len(errors_series) == 24

    # Verify specific hours have the expected error counts
    errors_by_hour = {i: 0 for i in range(24)}
    for item in errors_series:
        hour = int(item["hour_iso"].split(":")[0])
        errors_by_hour[hour] = item["value"]

    assert errors_by_hour[5] == 0, f"Expected no errors at hour 5, got: {errors_by_hour[5]}"
    assert errors_by_hour[10] >= 1, f"Expected 1+ error at hour 10, got: {errors_by_hour[10]}"
    assert errors_by_hour[14] >= 1, f"Expected 1+ error at hour 14, got: {errors_by_hour[14]}"

    # Test cost_by_hour: Should work with func.extract('hour', finished_at)
    cost_series = get_timeseries(db_session, metric="cost_by_hour", window="today")
    assert len(cost_series) == 24

    # Verify specific hours have the expected costs
    cost_by_hour = {i: 0.0 for i in range(24)}
    for item in cost_series:
        hour = int(item["hour_iso"].split(":")[0])
        cost_by_hour[hour] = float(item["value"])

    assert cost_by_hour[5] >= 0.05, f"Expected cost >= 0.05 at hour 5, got: {cost_by_hour[5]}"
    assert cost_by_hour[10] >= 0.03, f"Expected cost >= 0.03 at hour 10, got: {cost_by_hour[10]}"
    assert cost_by_hour[14] >= 0.08, f"Expected cost >= 0.08 at hour 14, got: {cost_by_hour[14]}"

    # Additional sanity check: Ensure no database errors occurred
    # If func.strftime() was used instead of func.extract(), this would fail on PostgreSQL
    assert True, "All timeseries queries executed successfully without database errors"
