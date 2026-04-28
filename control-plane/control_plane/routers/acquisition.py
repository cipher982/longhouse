from __future__ import annotations

from collections import Counter
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import hashlib
import json
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import AcquisitionEvent
from control_plane.routers.instances import require_admin

router = APIRouter(prefix="/api/acquisition", tags=["acquisition"])
install_router = APIRouter(tags=["acquisition"])

INSTALLER_REDIRECT_URL = "https://raw.githubusercontent.com/cipher982/longhouse/main/scripts/install.sh"


class AcquisitionEventIn(BaseModel):
    event_name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_.:-]+$")
    install_id: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=80)
    version: str | None = Field(default=None, max_length=80)
    os_name: str | None = Field(default=None, max_length=64)
    arch: str | None = Field(default=None, max_length=64)
    command: str | None = Field(default=None, max_length=80)
    install_method: str | None = Field(default=None, max_length=80)
    install_source: str | None = Field(default=None, max_length=80)
    channel: str | None = Field(default=None, max_length=80)
    topology: str | None = Field(default=None, max_length=80)
    ci: bool = False
    props: dict[str, Any] = Field(default_factory=dict)


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    if request.client:
        return request.client.host
    return None


def _daily_ip_hash(request: Request) -> str | None:
    ip = _client_ip(request)
    if not ip:
        return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{settings.jwt_secret}:{day}:{ip}".encode()).hexdigest()[:32]


def _clean_props(props: dict[str, Any]) -> str | None:
    cleaned: dict[str, str | int | float | bool | None] = {}
    for key, value in props.items():
        if not isinstance(key, str) or len(key) > 80:
            continue
        if value is None or isinstance(value, str | int | float | bool):
            cleaned[key] = value
    if not cleaned:
        return None
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))[:4096]


def _record_event(db: Session, request: Request, payload: AcquisitionEventIn) -> AcquisitionEvent:
    event = AcquisitionEvent(
        event_name=payload.event_name,
        install_id=payload.install_id,
        source=payload.source,
        version=payload.version,
        os_name=payload.os_name,
        arch=payload.arch,
        command=payload.command,
        install_method=payload.install_method,
        install_source=payload.install_source,
        channel=payload.channel,
        topology=payload.topology,
        ci=payload.ci,
        country=request.headers.get("cf-ipcountry"),
        ip_hash=_daily_ip_hash(request),
        user_agent=request.headers.get("user-agent"),
        referrer=request.headers.get("referer"),
        path=str(request.url.path),
        props_json=_clean_props(payload.props),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.post("/events", status_code=202)
def create_acquisition_event(payload: AcquisitionEventIn, request: Request, db: Session = Depends(get_db)):
    _record_event(db, request, payload)
    return {"ok": True}


@router.get("/summary", dependencies=[Depends(require_admin)])
def acquisition_summary(days: int = 30, db: Session = Depends(get_db)):
    days = max(1, min(days, 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events = (
        db.query(AcquisitionEvent)
        .filter(AcquisitionEvent.received_at >= cutoff)
        .order_by(AcquisitionEvent.received_at.asc())
        .all()
    )

    by_event = Counter(event.event_name for event in events)
    unique_installs = len({event.install_id for event in events if event.install_id})
    by_day: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        received_at = event.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        by_day[received_at.date().isoformat()][event.event_name] += 1

    return {
        "days": days,
        "total_events": len(events),
        "unique_install_ids": unique_installs,
        "by_event": dict(sorted(by_event.items())),
        "by_day": {day: dict(counts) for day, counts in sorted(by_day.items())},
    }


@install_router.get("/install.sh", include_in_schema=False)
def tracked_installer(request: Request, db: Session = Depends(get_db)):
    _record_event(
        db,
        request,
        AcquisitionEventIn(
            event_name="installer_fetch",
            source="control_plane",
            command="install_sh",
            props={"redirect": "github_raw"},
        ),
    )
    return RedirectResponse(INSTALLER_REDIRECT_URL, status_code=302)
