"""Ops metrics aggregation service (pure SQLAlchemy + small Python helpers)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.models import Course as CourseModel
from zerg.models.models import Fiche as FicheModel
from zerg.models.models import Thread as ThreadModel
from zerg.models.models import ThreadMessage as ThreadMessageModel
from zerg.models.models import User as UserModel


def _today_date_utc() -> datetime.date:
    return datetime.now(timezone.utc).date()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _percentile(values: List[int], p: float) -> Optional[int]:
    if not values:
        return None
    values_sorted = sorted(values)
    k = (len(values_sorted) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(values_sorted) - 1)
    if f == c:
        return int(values_sorted[int(k)])
    d0 = values_sorted[f] * (c - k)
    d1 = values_sorted[c] * (k - f)
    return int(d0 + d1)


def get_summary(db: Session, current_user: UserModel) -> Dict[str, Any]:
    """Compute the primary KPIs for the Ops summary widget/page."""
    today = _today_date_utc()
    now = _now_utc()

    # Courses today (started)
    courses_today_q = db.query(func.count(CourseModel.id)).filter(
        CourseModel.started_at.isnot(None), func.date(CourseModel.started_at) == today
    )
    courses_today = int(courses_today_q.scalar() or 0)

    # Cost today (finished with known cost)
    cost_sum_q = db.query(func.coalesce(func.sum(CourseModel.total_cost_usd), 0.0)).filter(
        CourseModel.finished_at.isnot(None), func.date(CourseModel.finished_at) == today
    )
    cost_count_q = db.query(func.count(CourseModel.id)).filter(
        CourseModel.finished_at.isnot(None),
        func.date(CourseModel.finished_at) == today,
        CourseModel.total_cost_usd.isnot(None),
    )
    known_cost_count = int(cost_count_q.scalar() or 0)
    cost_today_usd_val = float(cost_sum_q.scalar() or 0.0)
    cost_today_usd: Optional[float] = cost_today_usd_val if known_cost_count > 0 else None

    # Budgets
    settings = get_settings()
    # user budget
    user_budget_cents = int(getattr(settings, "daily_cost_per_user_cents", 0) or 0)
    user_cost_q = (
        db.query(func.coalesce(func.sum(CourseModel.total_cost_usd), 0.0))
        .join(FicheModel, FicheModel.id == CourseModel.fiche_id)
        .filter(
            FicheModel.owner_id == current_user.id,
            CourseModel.finished_at.isnot(None),
            func.date(CourseModel.finished_at) == today,
        )
    )
    user_used_usd = float(user_cost_q.scalar() or 0.0)
    user_percent: Optional[float] = None
    if user_budget_cents > 0:
        user_percent = min(100.0, (user_used_usd / (user_budget_cents / 100.0)) * 100.0) if user_used_usd else 0.0

    # global budget
    global_budget_cents = int(getattr(settings, "daily_cost_global_cents", 0) or 0)
    global_cost_q = db.query(func.coalesce(func.sum(CourseModel.total_cost_usd), 0.0)).filter(
        CourseModel.finished_at.isnot(None), func.date(CourseModel.finished_at) == today
    )
    global_used_usd = float(global_cost_q.scalar() or 0.0)
    global_percent: Optional[float] = None
    if global_budget_cents > 0:
        global_percent = min(100.0, (global_used_usd / (global_budget_cents / 100.0)) * 100.0) if global_used_usd else 0.0

    # Active users (24h): owners of courses started in last 24h or posters of messages in last 24h
    since_24h = now - timedelta(hours=24)
    user_ids_from_courses = (
        db.query(FicheModel.owner_id)
        .join(CourseModel, CourseModel.fiche_id == FicheModel.id)
        .filter(CourseModel.started_at.isnot(None), CourseModel.started_at >= since_24h)
        .distinct()
    )
    user_ids_from_messages = (
        db.query(FicheModel.owner_id)
        .join(ThreadModel, ThreadModel.fiche_id == FicheModel.id)
        .join(ThreadMessageModel, ThreadMessageModel.thread_id == ThreadModel.id)
        .filter(ThreadMessageModel.sent_at >= since_24h)
        .distinct()
    )
    # Execute both and union in Python for cross-DB simplicity
    active_user_ids = {row[0] for row in user_ids_from_courses.all()} | {row[0] for row in user_ids_from_messages.all()}
    active_users_24h = len({uid for uid in active_user_ids if uid is not None})

    # Fiches: total and scheduled (simple: schedule IS NOT NULL)
    fiches_total = int(db.query(func.count(FicheModel.id)).scalar() or 0)
    fiches_scheduled = int(db.query(func.count(FicheModel.id)).filter(FicheModel.schedule.isnot(None)).scalar() or 0)

    # Latency: p50/p95 for successful courses today
    durations_rows = (
        db.query(CourseModel.duration_ms)
        .filter(
            CourseModel.duration_ms.isnot(None),
            CourseModel.started_at.isnot(None),
            func.date(CourseModel.started_at) == today,
            CourseModel.status == "success",
        )
        .all()
    )
    durations = [int(r[0]) for r in durations_rows if r[0] is not None]
    latency_p50 = _percentile(durations, 50) or 0
    latency_p95 = _percentile(durations, 95) or 0

    # Errors in last hour (finished failed)
    since_1h = now - timedelta(hours=1)
    errors_last_hour = int(
        db.query(func.count(CourseModel.id))
        .filter(
            CourseModel.finished_at.isnot(None),
            CourseModel.finished_at >= since_1h,
            CourseModel.status == "failed",
        )
        .scalar()
        or 0
    )

    # Top fiches today: course count, cost sum (nullable), p95 duration
    top_fiches = get_top_fiches(db, window="today", limit=5)

    return {
        "courses_today": courses_today,
        "cost_today_usd": cost_today_usd,
        "budget_user": {
            "limit_cents": user_budget_cents,
            "used_usd": user_used_usd,
            "percent": user_percent,
        },
        "budget_global": {
            "limit_cents": global_budget_cents,
            "used_usd": global_used_usd,
            "percent": global_percent,
        },
        "active_users_24h": active_users_24h,
        "fiches_total": fiches_total,
        "fiches_scheduled": fiches_scheduled,
        "latency_ms": {"p50": latency_p50, "p95": latency_p95},
        "errors_last_hour": errors_last_hour,
        "top_fiches_today": top_fiches,
    }


def get_timeseries(db: Session, metric: str, window: str = "today") -> List[Dict[str, Any]]:
    """Return simple time-series suitable for small sparklines.

    - Hourly series for window=today
    - Daily series for window=7d or 30d
    """
    today = _today_date_utc()

    if window == "today":
        result: Dict[int, float] = {h: 0 for h in range(24)}

        if metric == "courses_by_hour":
            rows = (
                db.query(func.extract("hour", CourseModel.started_at), func.count(CourseModel.id))
                .filter(CourseModel.started_at.isnot(None), func.date(CourseModel.started_at) == today)
                .group_by(func.extract("hour", CourseModel.started_at))
                .all()
            )
            for hour_value, count in rows:
                result[int(hour_value)] = int(count)

        elif metric == "errors_by_hour":
            rows = (
                db.query(func.extract("hour", CourseModel.finished_at), func.count(CourseModel.id))
                .filter(
                    CourseModel.finished_at.isnot(None),
                    func.date(CourseModel.finished_at) == today,
                    CourseModel.status == "failed",
                )
                .group_by(func.extract("hour", CourseModel.finished_at))
                .all()
            )
            for hour_value, count in rows:
                result[int(hour_value)] = int(count)

        elif metric == "cost_by_hour":
            rows = (
                db.query(
                    func.extract("hour", CourseModel.finished_at),
                    func.coalesce(func.sum(CourseModel.total_cost_usd), 0.0),
                )
                .filter(
                    CourseModel.finished_at.isnot(None),
                    func.date(CourseModel.finished_at) == today,
                    CourseModel.total_cost_usd.isnot(None),
                )
                .group_by(func.extract("hour", CourseModel.finished_at))
                .all()
            )
            for hour_value, total in rows:
                result[int(hour_value)] = float(total)
        else:
            raise ValueError("Unsupported metric for window=today")

        return [{"hour_iso": f"{h:02d}:00Z", "value": result[h]} for h in range(24)]

    # Daily windows -------------------------------------------------
    if window not in {"7d", "30d"}:
        raise ValueError("Unsupported window")

    days = 7 if window == "7d" else 30
    start_date = today - timedelta(days=days - 1)

    # Prepare zero-filled map of date -> value
    result_day: Dict[str, float] = {}
    for i in range(days):
        d = start_date + timedelta(days=i)
        result_day[d.isoformat()] = 0.0

    def _group_and_fill(select_date_col, select_value_expr, base_filter):
        rows = db.query(select_date_col, select_value_expr).filter(base_filter).group_by(select_date_col).all()
        for day_value, v in rows:
            key = day_value.isoformat() if hasattr(day_value, "isoformat") else str(day_value)
            if key in result_day:
                result_day[key] = float(v)

    if metric == "courses_by_day":
        date_col = func.date(CourseModel.started_at)
        value = func.count(CourseModel.id)
        filt = CourseModel.started_at.isnot(None) & (func.date(CourseModel.started_at) >= start_date)
        _group_and_fill(date_col, value, filt)

    elif metric == "errors_by_day":
        date_col = func.date(CourseModel.finished_at)
        value = func.count(CourseModel.id)
        filt = CourseModel.finished_at.isnot(None) & (func.date(CourseModel.finished_at) >= start_date) & (CourseModel.status == "failed")
        _group_and_fill(date_col, value, filt)

    elif metric == "cost_by_day":
        date_col = func.date(CourseModel.finished_at)
        value = func.coalesce(func.sum(CourseModel.total_cost_usd), 0.0)
        filt = (
            CourseModel.finished_at.isnot(None)
            & (func.date(CourseModel.finished_at) >= start_date)
            & (CourseModel.total_cost_usd.isnot(None))
        )
        _group_and_fill(date_col, value, filt)
    else:
        raise ValueError("Unsupported metric for daily window")

    # Render as ordered array by day
    out: List[Dict[str, Any]] = []
    for i in range(days):
        d = start_date + timedelta(days=i)
        key = d.isoformat()
        out.append({"hour_iso": key, "value": result_day.get(key, 0.0)})
    return out


def get_top_fiches(db: Session, window: str = "today", limit: int = 5) -> List[Dict[str, Any]]:
    """Compute per-fiche aggregates for the given window.

    Supports "today", "7d", and "30d".
    """
    today = _today_date_utc()
    start_date: Optional[datetime.date] = None
    if window == "7d":
        start_date = today - timedelta(days=6)
    elif window == "30d":
        start_date = today - timedelta(days=29)
    elif window != "today":
        raise ValueError("Unsupported window")

    # Base: courses started today per fiche
    base_courses_q = db.query(CourseModel.fiche_id, func.count(CourseModel.id).label("courses")).filter(CourseModel.started_at.isnot(None))
    if start_date is not None:
        base_courses_q = base_courses_q.filter(func.date(CourseModel.started_at) >= start_date)
    else:
        base_courses_q = base_courses_q.filter(func.date(CourseModel.started_at) == today)
    course_rows = base_courses_q.group_by(CourseModel.fiche_id).all()
    courses_map = {fiche_id: int(courses) for fiche_id, courses in course_rows}

    # Cost sum for finished courses with cost
    base_cost_q = db.query(CourseModel.fiche_id, func.coalesce(func.sum(CourseModel.total_cost_usd), 0.0)).filter(
        CourseModel.finished_at.isnot(None), CourseModel.total_cost_usd.isnot(None)
    )
    if start_date is not None:
        base_cost_q = base_cost_q.filter(func.date(CourseModel.finished_at) >= start_date)
    else:
        base_cost_q = base_cost_q.filter(func.date(CourseModel.finished_at) == today)
    cost_rows = base_cost_q.group_by(CourseModel.fiche_id).all()
    cost_map = {fiche_id: float(total) for fiche_id, total in cost_rows}

    # p95 duration for successful courses per fiche (compute in Python)
    base_dur_q = db.query(CourseModel.fiche_id, CourseModel.duration_ms).filter(
        CourseModel.duration_ms.isnot(None),
        CourseModel.started_at.isnot(None),
        CourseModel.status == "success",
    )
    if start_date is not None:
        base_dur_q = base_dur_q.filter(func.date(CourseModel.started_at) >= start_date)
    else:
        base_dur_q = base_dur_q.filter(func.date(CourseModel.started_at) == today)
    dur_rows = base_dur_q.all()
    durations_by_fiche: Dict[int, List[int]] = defaultdict(list)
    for fiche_id, d in dur_rows:
        if d is not None and fiche_id is not None:
            durations_by_fiche[int(fiche_id)].append(int(d))
    p95_map = {fid: (_percentile(vals, 95) or 0) for fid, vals in durations_by_fiche.items()}

    # Join with fiche + owner info
    fiches_info_rows = (
        db.query(FicheModel.id, FicheModel.name, UserModel.email)
        .join(UserModel, UserModel.id == FicheModel.owner_id)
        .filter(FicheModel.id.in_(courses_map.keys()) if courses_map else False)
        .all()
    )
    info_map = {row[0]: (row[1], row[2]) for row in fiches_info_rows}

    items: List[Tuple[int, int]] = sorted(courses_map.items(), key=lambda x: (-x[1], x[0]))
    top_ids = [fid for fid, _ in items][: limit or 5]

    result: List[Dict[str, Any]] = []
    for fid in top_ids:
        name, owner_email = info_map.get(fid, (None, None))
        result.append(
            {
                "fiche_id": fid,
                "name": name,
                "owner_email": owner_email,
                "courses": courses_map.get(fid, 0),
                "cost_usd": cost_map.get(fid),
                "p95_ms": p95_map.get(fid, 0),
            }
        )

    return result
