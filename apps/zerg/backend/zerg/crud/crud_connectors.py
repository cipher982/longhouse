"""CRUD operations for Connectors."""

from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from sqlalchemy.orm import Session

from zerg.models.models import Connector


def create_connector(
    db: Session,
    *,
    owner_id: int,
    type: str,
    provider: str,
    config: Optional[Dict[str, Any]] = None,
) -> Connector:
    connector = Connector(owner_id=owner_id, type=type, provider=provider, config=config or {})
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def get_connector(db: Session, connector_id: int) -> Optional[Connector]:
    return db.query(Connector).filter(Connector.id == connector_id).first()


def get_connectors(
    db: Session,
    *,
    owner_id: Optional[int] = None,
    type: Optional[str] = None,
    provider: Optional[str] = None,
) -> List[Connector]:
    q = db.query(Connector)
    if owner_id is not None:
        q = q.filter(Connector.owner_id == owner_id)
    if type is not None:
        q = q.filter(Connector.type == type)
    if provider is not None:
        q = q.filter(Connector.provider == provider)
    return q.order_by(Connector.id).all()


def update_connector(
    db: Session,
    connector_id: int,
    *,
    config: Optional[Dict[str, Any]] = None,
    type: Optional[str] = None,
    provider: Optional[str] = None,
) -> Optional[Connector]:
    conn = get_connector(db, connector_id)
    if not conn:
        return None
    if type is not None:
        conn.type = type
    if provider is not None:
        conn.provider = provider
    if config is not None:
        conn.config = config  # type: ignore[assignment]
    db.commit()
    db.refresh(conn)
    return conn


def delete_connector(db: Session, connector_id: int) -> bool:
    conn = get_connector(db, connector_id)
    if not conn:
        return False
    db.delete(conn)
    db.commit()
    return True
