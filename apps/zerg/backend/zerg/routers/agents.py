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
from zerg.metrics import dashboard_snapshot_agents_returned
from zerg.metrics import dashboard_snapshot_latency_seconds
from zerg.metrics import dashboard_snapshot_requests_total
from zerg.metrics import dashboard_snapshot_runs_returned
from zerg.schemas.schemas import Agent
from zerg.schemas.schemas import AgentCreate
from zerg.schemas.schemas import AgentDetails
from zerg.schemas.schemas import AgentRunsBundle
from zerg.schemas.schemas import AgentUpdate
from zerg.schemas.schemas import DashboardSnapshot
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

router = APIRouter(tags=["agents"], dependencies=[Depends(get_current_user)])

# Simple in-memory idempotency cache
# Maps (idempotency_key, user_id) -> (agent_id, created_at)
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


def _get_agents_for_scope(
    db: Session,
    current_user,
    scope: str,
    *,
    skip: int = 0,
    limit: int = 100,
):
    if scope == "my":
        return crud.get_agents(db, skip=skip, limit=limit, owner_id=current_user.id)

    from zerg.dependencies.auth import AUTH_DISABLED  # local import to avoid cycle

    if AUTH_DISABLED:
        return crud.get_agents(db, skip=skip, limit=limit)

    if getattr(current_user, "role", "USER") != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required for scope=all")
    return crud.get_agents(db, skip=skip, limit=limit)


def _check_idempotency_cache(key: str, user_id: int, db: Session) -> Optional[Agent]:
    """Check if this request was already processed."""
    cache_key = (key, user_id)
    entry = IDEMPOTENCY_CACHE.get(cache_key)
    if entry:
        agent_id, created_at = entry
        if _now() - created_at > IDEMPOTENCY_TTL_SECS:
            IDEMPOTENCY_CACHE.pop(cache_key, None)
            return None
        agent = crud.get_agent(db, agent_id)
        if agent:
            logger.info(f"Idempotency: Returning existing agent {agent_id} for key {key}")
            return agent
    return None


def _store_idempotency_cache(key: str, user_id: int, agent_id: int) -> None:
    """Store successful agent creation in cache."""
    _cleanup_idempotency_cache()
    cache_key = (key, user_id)
    IDEMPOTENCY_CACHE[cache_key] = (agent_id, _now())


# ---------------------------------------------------------------------------
# List / create
# ---------------------------------------------------------------------------


@router.get("/", response_model=List[Agent])
@router.get("", response_model=List[Agent])
def read_agents(
    *,
    scope: str = Query("my", pattern="^(my|all)$"),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _get_agents_for_scope(db, current_user, scope, skip=skip, limit=limit)


@router.get("/dashboard", response_model=DashboardSnapshot)
def read_dashboard_snapshot(
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
    agents: List[Agent] = []
    bundles: List[AgentRunsBundle] = []
    total_runs = 0

    try:
        agents = _get_agents_for_scope(db, current_user, scope, skip=skip, limit=limit)

        if runs_limit > 0:
            for agent in agents:
                runs = crud.list_runs(db, agent.id, limit=runs_limit)
                bundles.append(AgentRunsBundle(agent_id=agent.id, runs=runs))
        else:
            bundles = [AgentRunsBundle(agent_id=agent.id, runs=[]) for agent in agents]

        total_runs = sum(len(bundle.runs) for bundle in bundles)

        logger.info(
            "Dashboard snapshot fetched (scope=%s, runs_limit=%s, agents=%s, total_runs=%s)",
            scope,
            runs_limit,
            len(agents),
            total_runs,
        )

        return DashboardSnapshot(
            scope=scope,
            fetched_at=utc_now_naive(),
            runs_limit=runs_limit,
            agents=agents,
            runs=bundles,
        )
    except Exception:
        status_label = "error"
        raise
    finally:
        duration = perf_counter() - start
        dashboard_snapshot_requests_total.labels(scope=scope, status=status_label).inc()
        dashboard_snapshot_latency_seconds.observe(duration)
        dashboard_snapshot_agents_returned.observe(float(len(agents)))
        dashboard_snapshot_runs_returned.observe(float(total_runs))


@router.post("/", response_model=Agent, status_code=status.HTTP_201_CREATED)
@router.post("", response_model=Agent, status_code=status.HTTP_201_CREATED)
@publish_event(EventType.AGENT_CREATED)
async def create_agent(
    agent: AgentCreate = Body(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    _validate_model_or_400(agent.model)
    # Enforce role-based allowlist for non-admin users
    model_to_use = _enforce_model_allowlist_or_422(agent.model, current_user)

    # Check idempotency cache to prevent double-creation
    if idempotency_key:
        cached_agent = _check_idempotency_cache(idempotency_key, current_user.id, db)
        if cached_agent:
            return cached_agent

    try:
        created_agent = crud.create_agent(
            db=db,
            owner_id=current_user.id,
            # name removed - backend auto-generates
            system_instructions=agent.system_instructions,
            task_instructions=agent.task_instructions,
            model=model_to_use,
            schedule=agent.schedule,
            config=agent.config,
        )

        # Store in idempotency cache
        if idempotency_key:
            _store_idempotency_cache(idempotency_key, current_user.id, created_agent.id)

        return created_agent
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/{agent_id}", response_model=Agent)
def read_agent(agent_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = crud.get_agent(db, agent_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not agent owner")
    return row


@router.put("/{agent_id}", response_model=Agent)
@publish_event(EventType.AGENT_UPDATED)
async def update_agent(
    agent_id: int,
    agent: AgentUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if agent.model is not None:
        _validate_model_or_400(agent.model)
        # Enforce role-based allowlist for non-admin users when updating model
        agent_model_validated = _enforce_model_allowlist_or_422(agent.model, current_user)
    else:
        agent_model_validated = None

    # Authorization: only owner or admin may update an agent
    existing = crud.get_agent(db, agent_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and existing.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not agent owner")

    try:
        row = crud.update_agent(
            db=db,
            agent_id=agent_id,
            name=agent.name,
            system_instructions=agent.system_instructions,
            task_instructions=agent.task_instructions,
            model=agent_model_validated,
            status=agent.status.value if agent.status else None,
            schedule=agent.schedule,
            config=agent.config,
            allowed_tools=agent.allowed_tools,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return row


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------


# Optional import for type hints
@router.get("/{agent_id}/details", response_model=AgentDetails, response_model_exclude_none=True)
def read_agent_details(
    agent_id: int,
    include: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    row = crud.get_agent(db, agent_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not agent owner")

    include_set: set[str] = set(p.strip().lower() for p in include.split(",")) if include else set()
    payload: dict[str, Any] = {"agent": row}
    if "threads" in include_set:
        payload["threads"] = []
    if "runs" in include_set:
        payload["runs"] = crud.list_runs(db, agent_id)  # type: ignore[assignment]
    if "stats" in include_set:
        payload["stats"] = {}
    return payload


# ---------------------------------------------------------------------------
# Delete & aux
# ---------------------------------------------------------------------------


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = crud.get_agent(db, agent_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not agent owner")

    if not crud.delete_agent(db, agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    payload = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    payload.pop("_sa_instance_state", None)
    await event_bus.publish(EventType.AGENT_DELETED, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{agent_id}/messages", response_model=List[MessageResponse])
def read_agent_messages(
    agent_id: int,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    agent = crud.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not agent owner")
    return crud.get_agent_messages(db, agent_id=agent_id, skip=skip, limit=limit) or []


@router.post("/{agent_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
def create_agent_message(
    agent_id: int,
    message: MessageCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    agent = crud.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not agent owner")
    return crud.create_agent_message(db=db, agent_id=agent_id, role=message.role, content=message.content)


@router.post("/{agent_id}/task", status_code=status.HTTP_202_ACCEPTED)
async def run_agent_task(agent_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    agent = crud.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Authorization: only owner or admin may run an agent's task
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not agent owner")

    from zerg.services.task_runner import execute_agent_task

    try:
        thread = await execute_agent_task(db, agent, thread_type="manual")
    except ValueError as exc:
        if "already running" in str(exc).lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent already running") from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"thread_id": thread.id}
