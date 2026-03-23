"""CRUD operations for Triggers."""

from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.models import Trigger


def create_trigger(
    db: Session,
    *,
    fiche_id: int,
    trigger_type: str = "webhook",
    secret: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
):
    """Create a new trigger for an fiche.

    The **secret** is only relevant for webhook triggers.  For future trigger
    types we still persist a secret so external systems can authenticate when
    hitting the generic `/events` endpoint (or skip when not applicable).
    """

    if secret is None:
        secret = uuid4().hex

    db_trigger = Trigger(
        fiche_id=fiche_id,
        type=trigger_type,
        secret=secret,
        config=config,
    )
    db.add(db_trigger)
    db.commit()
    db.refresh(db_trigger)
    return db_trigger


def get_trigger(db: Session, trigger_id: int):
    return db.query(Trigger).filter(Trigger.id == trigger_id).first()


def delete_trigger(db: Session, trigger_id: int):
    trg = get_trigger(db, trigger_id)
    if trg is None:
        return False
    db.delete(trg)
    db.commit()
    return True


def get_triggers(db: Session, fiche_id: Optional[int] = None) -> List[Trigger]:
    """
    Retrieve triggers, optionally filtered by fiche_id.
    """
    query = db.query(Trigger)
    if fiche_id is not None:
        query = query.filter(Trigger.fiche_id == fiche_id)
    return query.order_by(Trigger.id).all()
