import logging
from typing import Any
from typing import List
from typing import Optional

from dotenv import load_dotenv

# FastAPI imports
from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
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
from zerg.schemas.schemas import Agent
from zerg.schemas.schemas import AgentCreate
from zerg.schemas.schemas import AgentDetails
from zerg.schemas.schemas import AgentUpdate
from zerg.schemas.schemas import MessageCreate
from zerg.schemas.schemas import MessageResponse

load_dotenv()
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Helper validation
# ------------------------------------------------------------


def _validate_model_or_400(model_id: str) -> None:
    """Raise 400 if *model_id* not in registry."""

    from zerg.models_config import MODELS_BY_ID

    if not model_id or model_id.strip() == "":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'model' must be a non-empty string")

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
    if scope == "my":
        return crud.get_agents(db, skip=skip, limit=limit, owner_id=current_user.id)

    from zerg.dependencies.auth import AUTH_DISABLED  # local import to avoid cycle

    if AUTH_DISABLED:
        # Dev/test mode – return entire list regardless of role so the SPA
        # dashboard (which always requests *scope=all*) continues to work
        # without requiring admin privileges or a bearer token.
        return crud.get_agents(db, skip=skip, limit=limit)

    if getattr(current_user, "role", "USER") != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required for scope=all")
    return crud.get_agents(db, skip=skip, limit=limit)


@router.post("/", response_model=Agent, status_code=status.HTTP_201_CREATED)
@router.post("", response_model=Agent, status_code=status.HTTP_201_CREATED)
@publish_event(EventType.AGENT_CREATED)
async def create_agent(
    agent: AgentCreate = Body(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _validate_model_or_400(agent.model)
    # Enforce role-based allowlist for non-admin users
    model_to_use = _enforce_model_allowlist_or_422(agent.model, current_user)

    try:
        return crud.create_agent(
            db=db,
            owner_id=current_user.id,
            name=agent.name,
            system_instructions=agent.system_instructions,
            task_instructions=agent.task_instructions,
            model=model_to_use,
            schedule=agent.schedule,
            config=agent.config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/{agent_id}", response_model=Agent)
def read_agent(agent_id: int, db: Session = Depends(get_db)):
    row = crud.get_agent(db, agent_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
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
):
    row = crud.get_agent(db, agent_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

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
async def delete_agent(agent_id: int, db: Session = Depends(get_db)):
    row = crud.get_agent(db, agent_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if not crud.delete_agent(db, agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    payload = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    payload.pop("_sa_instance_state", None)
    await event_bus.publish(EventType.AGENT_DELETED, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{agent_id}/messages", response_model=List[MessageResponse])
def read_agent_messages(agent_id: int, skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    if not crud.get_agent(db, agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return crud.get_agent_messages(db, agent_id=agent_id, skip=skip, limit=limit) or []


@router.post("/{agent_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
def create_agent_message(agent_id: int, message: MessageCreate, db: Session = Depends(get_db)):
    if not crud.get_agent(db, agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
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
