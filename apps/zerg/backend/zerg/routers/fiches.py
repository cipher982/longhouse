import logging
import time
from time import perf_counter
from typing import Any
from typing import List
from typing import Optional

from dotenv import load_dotenv

# FastAPI imports
from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response
from fastapi import status

# Instantiate OpenAI client with API key from central settings
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import get_db
from zerg.events import EventType
from zerg.events.decorators import publish_event
from zerg.events.event_bus import event_bus
from zerg.metrics import dashboard_snapshot_courses_returned
from zerg.metrics import dashboard_snapshot_fiches_returned
from zerg.metrics import dashboard_snapshot_latency_seconds
from zerg.metrics import dashboard_snapshot_requests_total
from zerg.schemas.schemas import CourseBundle
from zerg.schemas.schemas import DashboardSnapshot
from zerg.schemas.schemas import Fiche
from zerg.schemas.schemas import FicheCreate
from zerg.schemas.schemas import FicheDetails
from zerg.schemas.schemas import FicheUpdate
from zerg.schemas.schemas import MessageCreate
from zerg.schemas.schemas import MessageResponse
from zerg.utils.time import utc_now_naive

# Use override=True to ensure proper quote stripping even if vars are inherited from parent process
load_dotenv(override=True)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Helper validation
# ------------------------------------------------------------


def _validate_model_or_400(model_id: str) -> None:
    """Raise 400 if *model_id* not in registry. Test models are always allowed (with warning)."""
    from zerg.models_config import MODELS_BY_ID
    from zerg.testing.test_models import is_test_model
    from zerg.testing.test_models import warn_if_test_model

    if not model_id or model_id.strip() == "":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'model' must be a non-empty string")

    # Allow test models (logs warning but doesn't block)
    if is_test_model(model_id):
        warn_if_test_model(model_id)
        return  # Test model is valid

    # Production models must be in the registry
    if model_id not in MODELS_BY_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported model '{model_id}'. Call /api/models for valid IDs.",
        )


def _enforce_model_allowlist_or_422(model_id: str, current_user) -> str:
    """Return a permitted model_id for the user or raise 422.

    Non-admin users are restricted to the comma-separated allowlist
    from settings.ALLOWED_MODELS_NON_ADMIN when provided. Admins are
    unrestricted. If the requested model is disallowed and a default
    is configured, we still reject (explicit) to avoid silent changes.
    """
    role = getattr(current_user, "role", "USER")
    if role == "ADMIN":
        return model_id

    settings = get_settings()
    raw = settings.allowed_models_non_admin or ""
    allow = [m.strip() for m in raw.split(",") if m.strip()]
    if not allow:
        # No allowlist configured – permit any registered model
        return model_id

    if model_id in allow:
        return model_id

    allowed_str = ",".join(allow)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Model '{model_id}' is not allowed for non-admin users. Allowed: {allowed_str}",
    )


# ---------------------------------------------------------------------------
# Router & deps
# ---------------------------------------------------------------------------

from zerg.dependencies.auth import get_current_user  # noqa: E402

router = APIRouter(tags=["fiches"], dependencies=[Depends(get_current_user)])

# Simple in-memory idempotency cache
# Maps (idempotency_key, user_id) -> (fiche_id, created_at)
# For production, use Redis or database table
IDEMPOTENCY_TTL_SECS = 600
IDEMPOTENCY_MAX_SIZE = 1000
IDEMPOTENCY_CACHE: dict[tuple[str, int], tuple[int, float]] = {}


def _now() -> float:
    return time.time()


def _cleanup_idempotency_cache() -> None:
    """Remove expired entries and enforce size limit (called before storing new entry)."""
    now = _now()
    # Remove expired entries
    expired = [k for k, (_, ts) in IDEMPOTENCY_CACHE.items() if now - ts > IDEMPOTENCY_TTL_SECS]
    for k in expired:
        IDEMPOTENCY_CACHE.pop(k, None)

    # If at or over size limit, remove oldest entries to make room for new entry
    if len(IDEMPOTENCY_CACHE) >= IDEMPOTENCY_MAX_SIZE:
        sorted_keys = sorted(IDEMPOTENCY_CACHE.keys(), key=lambda k: IDEMPOTENCY_CACHE[k][1])
        to_remove = len(IDEMPOTENCY_CACHE) - IDEMPOTENCY_MAX_SIZE + 1
        for k in sorted_keys[:to_remove]:
            IDEMPOTENCY_CACHE.pop(k, None)


def _get_fiches_for_scope(
    db: Session,
    current_user,
    scope: str,
    *,
    skip: int = 0,
    limit: int = 100,
):
    if scope == "my":
        return crud.get_fiches(db, skip=skip, limit=limit, owner_id=current_user.id)

    from zerg.dependencies.auth import AUTH_DISABLED  # local import to avoid cycle

    if AUTH_DISABLED:
        return crud.get_fiches(db, skip=skip, limit=limit)

    if getattr(current_user, "role", "USER") != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required for scope=all")
    return crud.get_fiches(db, skip=skip, limit=limit)


def _check_idempotency_cache(key: str, user_id: int, db: Session) -> Optional[Fiche]:
    """Check if this request was already processed."""
    cache_key = (key, user_id)
    entry = IDEMPOTENCY_CACHE.get(cache_key)
    if entry:
        fiche_id, created_at = entry
        if _now() - created_at > IDEMPOTENCY_TTL_SECS:
            IDEMPOTENCY_CACHE.pop(cache_key, None)
            return None
        fiche = crud.get_fiche(db, fiche_id)
        if fiche:
            logger.info(f"Idempotency: Returning existing fiche {fiche_id} for key {key}")
            return fiche
    return None


def _store_idempotency_cache(key: str, user_id: int, fiche_id: int) -> None:
    """Store successful fiche creation in cache."""
    _cleanup_idempotency_cache()
    cache_key = (key, user_id)
    IDEMPOTENCY_CACHE[cache_key] = (fiche_id, _now())


# ---------------------------------------------------------------------------
# List / create
# ---------------------------------------------------------------------------


@router.get("/", response_model=List[Fiche])
@router.get("", response_model=List[Fiche])
def read_fiches(
    *,
    scope: str = Query("my", pattern="^(my|all)$"),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _get_fiches_for_scope(db, current_user, scope, skip=skip, limit=limit)


@router.get("/dashboard", response_model=DashboardSnapshot)
def read_dashboard_snapshot(
    *,
    scope: str = Query("my", pattern="^(my|all)$"),
    courses_limit: int = Query(50, ge=0, le=500),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    start = perf_counter()
    status_label = "success"
    fiches: List[Fiche] = []
    bundles: List[CourseBundle] = []
    total_courses = 0

    try:
        fiches = _get_fiches_for_scope(db, current_user, scope, skip=skip, limit=limit)

        if courses_limit > 0:
            for fiche in fiches:
                courses = crud.list_courses(db, fiche.id, limit=courses_limit)
                bundles.append(CourseBundle(fiche_id=fiche.id, courses=courses))
        else:
            bundles = [CourseBundle(fiche_id=fiche.id, courses=[]) for fiche in fiches]

        total_courses = sum(len(bundle.courses) for bundle in bundles)

        logger.info(
            "Dashboard snapshot fetched (scope=%s, courses_limit=%s, fiches=%s, total_courses=%s)",
            scope,
            courses_limit,
            len(fiches),
            total_courses,
        )

        return DashboardSnapshot(
            scope=scope,
            fetched_at=utc_now_naive(),
            courses_limit=courses_limit,
            fiches=fiches,
            courses=bundles,
        )
    except Exception:
        status_label = "error"
        raise
    finally:
        duration = perf_counter() - start
        dashboard_snapshot_requests_total.labels(scope=scope, status=status_label).inc()
        dashboard_snapshot_latency_seconds.observe(duration)
        dashboard_snapshot_fiches_returned.observe(float(len(fiches)))
        dashboard_snapshot_courses_returned.observe(float(total_courses))


@router.post("/", response_model=Fiche, status_code=status.HTTP_201_CREATED)
@router.post("", response_model=Fiche, status_code=status.HTTP_201_CREATED)
@publish_event(EventType.FICHE_CREATED)
async def create_fiche(
    fiche: FicheCreate = Body(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    _validate_model_or_400(fiche.model)
    # Enforce role-based allowlist for non-admin users
    model_to_use = _enforce_model_allowlist_or_422(fiche.model, current_user)

    # Check idempotency cache to prevent double-creation
    if idempotency_key:
        cached_fiche = _check_idempotency_cache(idempotency_key, current_user.id, db)
        if cached_fiche:
            return cached_fiche

    try:
        created_fiche = crud.create_fiche(
            db=db,
            owner_id=current_user.id,
            # name removed - backend auto-generates
            system_instructions=fiche.system_instructions,
            task_instructions=fiche.task_instructions,
            model=model_to_use,
            schedule=fiche.schedule,
            config=fiche.config,
        )

        # Store in idempotency cache
        if idempotency_key:
            _store_idempotency_cache(idempotency_key, current_user.id, created_fiche.id)

        return created_fiche
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/{fiche_id}", response_model=Fiche)
def read_fiche(fiche_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = crud.get_fiche(db, fiche_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not fiche owner")
    return row


@router.put("/{fiche_id}", response_model=Fiche)
@publish_event(EventType.FICHE_UPDATED)
async def update_fiche(
    fiche_id: int,
    fiche: FicheUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if fiche.model is not None:
        _validate_model_or_400(fiche.model)
        # Enforce role-based allowlist for non-admin users when updating model
        fiche_model_validated = _enforce_model_allowlist_or_422(fiche.model, current_user)
    else:
        fiche_model_validated = None

    # Authorization: only owner or admin may update a fiche
    existing = crud.get_fiche(db, fiche_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and existing.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not fiche owner")

    try:
        row = crud.update_fiche(
            db=db,
            fiche_id=fiche_id,
            name=fiche.name,
            system_instructions=fiche.system_instructions,
            task_instructions=fiche.task_instructions,
            model=fiche_model_validated,
            status=fiche.status.value if fiche.status else None,
            schedule=fiche.schedule,
            config=fiche.config,
            allowed_tools=fiche.allowed_tools,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
    return row


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------


# Optional import for type hints
@router.get("/{fiche_id}/details", response_model=FicheDetails, response_model_exclude_none=True)
def read_fiche_details(
    fiche_id: int,
    include: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    row = crud.get_fiche(db, fiche_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not fiche owner")

    include_set: set[str] = set(p.strip().lower() for p in include.split(",")) if include else set()
    payload: dict[str, Any] = {"fiche": row}
    if "threads" in include_set:
        payload["threads"] = []
    if "courses" in include_set:
        payload["courses"] = crud.list_courses(db, fiche_id)  # type: ignore[assignment]
    if "stats" in include_set:
        payload["stats"] = {}
    return payload


# ---------------------------------------------------------------------------
# Delete & aux
# ---------------------------------------------------------------------------


@router.delete("/{fiche_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fiche(fiche_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = crud.get_fiche(db, fiche_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not fiche owner")

    if not crud.delete_fiche(db, fiche_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")

    payload = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    payload.pop("_sa_instance_state", None)
    await event_bus.publish(EventType.FICHE_DELETED, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{fiche_id}/messages", response_model=List[MessageResponse])
def read_fiche_messages(
    fiche_id: int,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    fiche = crud.get_fiche(db, fiche_id)
    if fiche is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and fiche.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not fiche owner")
    return crud.get_fiche_messages(db, fiche_id=fiche_id, skip=skip, limit=limit) or []


@router.post("/{fiche_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
def create_fiche_message(
    fiche_id: int,
    message: MessageCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    fiche = crud.get_fiche(db, fiche_id)
    if fiche is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and fiche.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not fiche owner")
    return crud.create_fiche_message(db=db, fiche_id=fiche_id, role=message.role, content=message.content)


@router.post("/{fiche_id}/task", status_code=status.HTTP_202_ACCEPTED)
async def run_fiche_task(fiche_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    fiche = crud.get_fiche(db, fiche_id)
    if fiche is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")

    # Authorization: only owner or admin may start a fiche's task course
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and fiche.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not fiche owner")

    from zerg.services.task_runner import execute_fiche_task

    try:
        thread = await execute_fiche_task(db, fiche, thread_type="manual")
    except ValueError as exc:
        if "already running" in str(exc).lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Fiche already running") from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"thread_id": thread.id}
