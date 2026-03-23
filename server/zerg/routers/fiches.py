import logging
import os
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
from zerg.crud import create_fiche as create_fiche_record
from zerg.crud import create_fiche_message as create_fiche_message_record
from zerg.crud import delete_fiche as delete_fiche_record
from zerg.crud import get_fiche
from zerg.crud import get_fiche_messages
from zerg.crud import get_fiches
from zerg.crud import list_runs as list_fiche_runs
from zerg.crud import update_fiche as update_fiche_record
from zerg.database import get_db
from zerg.events import EventType
from zerg.events.decorators import publish_event
from zerg.events.event_bus import event_bus
from zerg.metrics import dashboard_snapshot_fiches_returned
from zerg.metrics import dashboard_snapshot_latency_seconds
from zerg.metrics import dashboard_snapshot_requests_total
from zerg.metrics import dashboard_snapshot_runs_returned
from zerg.schemas.schemas import Automation
from zerg.schemas.schemas import AutomationCreate
from zerg.schemas.schemas import AutomationDetails
from zerg.schemas.schemas import AutomationUpdate
from zerg.schemas.schemas import DashboardSnapshot
from zerg.schemas.schemas import MessageCreate
from zerg.schemas.schemas import MessageResponse
from zerg.schemas.schemas import RunBundle
from zerg.utils.time import utc_now_naive

# Use override=True to ensure proper quote stripping even if vars are inherited from parent process.
# In test/E2E mode, do not override explicit env vars like ENVIRONMENT.
_override_env = os.getenv("TESTING", "").strip().lower() not in {"1", "true", "yes", "on"}
load_dotenv(override=_override_env)
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

router = APIRouter(tags=["automations"], dependencies=[Depends(get_current_user)])

# Simple in-memory idempotency cache
# Maps (idempotency_key, user_id) -> (automation_id, created_at)
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
        return get_fiches(db, skip=skip, limit=limit, owner_id=current_user.id)

    from zerg.dependencies.auth import AUTH_DISABLED  # local import to avoid cycle

    if AUTH_DISABLED:
        return get_fiches(db, skip=skip, limit=limit)

    if getattr(current_user, "role", "USER") != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required for scope=all")
    return get_fiches(db, skip=skip, limit=limit)


def _check_idempotency_cache(key: str, user_id: int, db: Session) -> Optional[Automation]:
    """Check if this request was already processed."""
    cache_key = (key, user_id)
    entry = IDEMPOTENCY_CACHE.get(cache_key)
    if entry:
        fiche_id, created_at = entry
        if _now() - created_at > IDEMPOTENCY_TTL_SECS:
            IDEMPOTENCY_CACHE.pop(cache_key, None)
            return None
        fiche = get_fiche(db, fiche_id)
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


@router.get("/", response_model=List[Automation])
@router.get("", response_model=List[Automation])
def list_automations(
    *,
    scope: str = Query("my", pattern="^(my|all)$"),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _get_fiches_for_scope(db, current_user, scope, skip=skip, limit=limit)


@router.get("/dashboard", response_model=DashboardSnapshot)
def read_automation_overview(
    *,
    scope: str = Query("my", pattern="^(my|all)$"),
    runs_limit: int = Query(50, ge=0, le=500),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    start = perf_counter()
    status_label = "success"
    automations: List[Automation] = []
    bundles: List[RunBundle] = []
    total_runs = 0

    try:
        automations = _get_fiches_for_scope(db, current_user, scope, skip=skip, limit=limit)

        if runs_limit > 0:
            for automation in automations:
                runs = list_fiche_runs(db, automation.id, limit=runs_limit)
                bundles.append(RunBundle(automation_id=automation.id, runs=runs))
        else:
            bundles = [RunBundle(automation_id=automation.id, runs=[]) for automation in automations]

        total_runs = sum(len(bundle.runs) for bundle in bundles)

        logger.info(
            "Dashboard snapshot fetched (scope=%s, runs_limit=%s, fiches=%s, total_runs=%s)",
            scope,
            runs_limit,
            len(automations),
            total_runs,
        )

        return DashboardSnapshot(
            scope=scope,
            fetched_at=utc_now_naive(),
            runs_limit=runs_limit,
            automations=automations,
            runs=bundles,
        )
    except Exception:
        status_label = "error"
        raise
    finally:
        duration = perf_counter() - start
        dashboard_snapshot_requests_total.labels(scope=scope, status=status_label).inc()
        dashboard_snapshot_latency_seconds.observe(duration)
        dashboard_snapshot_fiches_returned.observe(float(len(automations)))
        dashboard_snapshot_runs_returned.observe(float(total_runs))


@router.post("/", response_model=Automation, status_code=status.HTTP_201_CREATED)
@router.post("", response_model=Automation, status_code=status.HTTP_201_CREATED)
@publish_event(EventType.AUTOMATION_CREATED)
async def create_automation(
    automation: AutomationCreate = Body(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    settings = get_settings()
    model_override = None
    if settings.testing and (settings.environment or "").lower() == "test:e2e":
        override = os.getenv("E2E_DEFAULT_MODEL", "").strip()
        if override:
            model_override = override

    model_id = model_override or automation.model
    _validate_model_or_400(model_id)
    # Enforce role-based allowlist for non-admin users
    model_to_use = _enforce_model_allowlist_or_422(model_id, current_user)

    # Check idempotency cache to prevent double-creation
    if idempotency_key:
        cached_automation = _check_idempotency_cache(idempotency_key, current_user.id, db)
        if cached_automation:
            return cached_automation

    try:
        created_automation = create_fiche_record(
            db=db,
            owner_id=current_user.id,
            # name removed - backend auto-generates
            system_instructions=automation.system_instructions,
            task_instructions=automation.task_instructions,
            model=model_to_use,
            schedule=automation.schedule,
            config=automation.config,
        )

        # Store in idempotency cache
        if idempotency_key:
            _store_idempotency_cache(idempotency_key, current_user.id, created_automation.id)

        return created_automation
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/{automation_id}", response_model=Automation)
def read_automation(automation_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = get_fiche(db, automation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not automation owner")
    return row


@router.put("/{automation_id}", response_model=Automation)
@publish_event(EventType.AUTOMATION_UPDATED)
async def update_automation(
    automation_id: int,
    automation: AutomationUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if automation.model is not None:
        _validate_model_or_400(automation.model)
        # Enforce role-based allowlist for non-admin users when updating model
        fiche_model_validated = _enforce_model_allowlist_or_422(automation.model, current_user)
    else:
        fiche_model_validated = None

    # Authorization: only owner or admin may update an automation
    existing = get_fiche(db, automation_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and existing.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not automation owner")

    try:
        row = update_fiche_record(
            db=db,
            fiche_id=automation_id,
            name=automation.name,
            system_instructions=automation.system_instructions,
            task_instructions=automation.task_instructions,
            model=fiche_model_validated,
            status=automation.status.value if automation.status else None,
            schedule=automation.schedule,
            config=automation.config,
            allowed_tools=automation.allowed_tools,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")
    return row


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------


# Optional import for type hints
@router.get("/{automation_id}/details", response_model=AutomationDetails, response_model_exclude_none=True)
def read_automation_details(
    automation_id: int,
    include: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    row = get_fiche(db, automation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not automation owner")

    include_set: set[str] = set(p.strip().lower() for p in include.split(",")) if include else set()
    payload: dict[str, Any] = {"automation": row}
    if "threads" in include_set:
        payload["threads"] = []
    if "runs" in include_set:
        payload["runs"] = list_fiche_runs(db, automation_id)  # type: ignore[assignment]
    if "stats" in include_set:
        payload["stats"] = {}
    return payload


# ---------------------------------------------------------------------------
# Delete & aux
# ---------------------------------------------------------------------------


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(automation_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = get_fiche(db, automation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not automation owner")

    if not delete_fiche_record(db, automation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")

    payload = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    payload.pop("_sa_instance_state", None)
    payload["event_type"] = EventType.AUTOMATION_DELETED
    await event_bus.publish(EventType.AUTOMATION_DELETED, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{automation_id}/messages", response_model=List[MessageResponse])
def read_automation_messages(
    automation_id: int,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    automation = get_fiche(db, automation_id)
    if automation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and automation.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not automation owner")
    return get_fiche_messages(db, fiche_id=automation_id, skip=skip, limit=limit) or []


@router.post("/{automation_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
def create_automation_message(
    automation_id: int,
    message: MessageCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    automation = get_fiche(db, automation_id)
    if automation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and automation.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not automation owner")
    return create_fiche_message_record(db=db, fiche_id=automation_id, role=message.role, content=message.content)


@router.post("/{automation_id}/task", status_code=status.HTTP_202_ACCEPTED)
async def run_automation_task(
    automation_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    automation = get_fiche(db, automation_id)
    if automation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")

    # Authorization: only owner or admin may start an automation run
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and automation.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not automation owner")

    from zerg.services.task_runner import execute_fiche_task

    try:
        thread = await execute_fiche_task(db, automation, thread_type="manual")
    except ValueError as exc:
        if "already running" in str(exc).lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Automation already running") from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"thread_id": thread.id}
