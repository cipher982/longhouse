"""CatalogStore: queued session input, input receipts and attachments, and directed peer input."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import delete
from sqlalchemy import insert
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import _console_turns_with_reporting_run
from zerg.catalogd.store import _directed_input_dto
from zerg.catalogd.store import _input_attachment_dto
from zerg.catalogd.store import _input_attachment_summaries_by_receipt
from zerg.catalogd.store import _input_receipt_dto
from zerg.catalogd.store import _input_receipt_rows
from zerg.catalogd.store import _live_control_session_dto
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _write_transaction
from zerg.models.live_store import LiveConsoleTurn
from zerg.models.live_store import LiveDirectedInput
from zerg.models.live_store import LiveSessionInputAttachment
from zerg.models.live_store import LiveSessionInputReceipt


class InputsMixin:
    def list_queued_input_sessions(self, *, limit: int) -> dict[str, Any]:
        """Return a bounded set of sessions with queued hot input."""

        from zerg.services.live_session_inputs import list_session_ids_with_queued_live_receipts

        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                session_ids = list_session_ids_with_queued_live_receipts(orm, limit=limit)
            finally:
                orm.close()
            return {
                "session_ids": [str(session_id) for session_id in session_ids],
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def claim_queued_input(
        self,
        *,
        session_id: str,
        delivery_request_id: str,
    ) -> dict[str, Any]:
        """Check drainability and claim exactly one queued input receipt."""

        from zerg.services.live_control_catalog import load_live_control_session
        from zerg.services.live_session_inputs import MAX_DELIVERY_AGE
        from zerg.services.live_session_inputs import _snapshot
        from zerg.services.live_session_inputs import claim_next_live_queued_receipt
        from zerg.services.live_session_inputs import expire_stale_live_receipts
        from zerg.services.session_state_contract import SEND_DISPATCHABLE_ACTIVITY_STATES
        from zerg.services.session_state_contract import input_activity_state

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                expire_stale_live_receipts(orm, session_id=session_id, now=observed_at)
                replay_receipt = (
                    orm.query(LiveSessionInputReceipt.id)
                    .filter(
                        LiveSessionInputReceipt.session_id == session_id,
                        LiveSessionInputReceipt.delivery_request_id == delivery_request_id,
                        LiveSessionInputReceipt.status == "delivering",
                    )
                    .first()
                )
                if replay_receipt is None:
                    fresh_receipt = (
                        orm.query(LiveSessionInputReceipt.id)
                        .filter(
                            LiveSessionInputReceipt.session_id == session_id,
                            LiveSessionInputReceipt.status == "queued",
                            LiveSessionInputReceipt.created_at >= observed_at - MAX_DELIVERY_AGE,
                        )
                        .first()
                    )
                    if fresh_receipt is None:
                        orm.rollback()
                        return {
                            "claimed": False,
                            "reason": "queue_empty",
                            "commit_seq": str(_current_commit_seq(connection)),
                        }
                session = load_live_control_session(orm, session_id)
                if session is None:
                    orm.rollback()
                    return {
                        "claimed": False,
                        "reason": "session_not_found",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                projection = self._served_session_projection(connection, session, observed_at=observed_at)
                if projection is None or input_activity_state(projection) not in SEND_DISPATCHABLE_ACTIVITY_STATES:
                    orm.rollback()
                    return {
                        "claimed": False,
                        "reason": "activity_not_drainable",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if projection.control is None or projection.control.actions.send_input.state != "available":
                    orm.rollback()
                    return {
                        "claimed": False,
                        "reason": "control_unavailable",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                replay_row = (
                    orm.query(LiveSessionInputReceipt)
                    .filter(
                        LiveSessionInputReceipt.session_id == session_id,
                        LiveSessionInputReceipt.delivery_request_id == delivery_request_id,
                        LiveSessionInputReceipt.status == "delivering",
                    )
                    .first()
                )
                if replay_row is not None:
                    snapshot = _snapshot(replay_row)
                    orm.rollback()
                    return {
                        "claimed": True,
                        "exact_replay": True,
                        "session": _live_control_session_dto(session),
                        "receipt": _input_receipt_dto(snapshot),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                receipt = claim_next_live_queued_receipt(
                    orm,
                    session_id=session_id,
                    delivery_request_id=delivery_request_id,
                )
                if receipt is None:
                    orm.rollback()
                    return {
                        "claimed": False,
                        "reason": "queue_empty",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "claimed": True,
                "exact_replay": False,
                "session": _live_control_session_dto(session),
                "receipt": _input_receipt_dto(receipt),
                "commit_seq": str(commit_seq),
            }

    def finish_queued_input(
        self,
        *,
        receipt_id: str,
        delivery_request_id: str,
        status: str,
        error: str | None,
    ) -> dict[str, Any]:
        """Apply the terminal delivery result and archive projection atomically."""

        from zerg.services.live_session_inputs import _snapshot
        from zerg.services.live_session_inputs import mark_live_receipt_delivered_with_projection
        from zerg.services.live_session_inputs import mark_live_receipt_failed
        from zerg.services.live_session_inputs import requeue_live_receipt

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                row = orm.get(LiveSessionInputReceipt, receipt_id)
                if row is None or str(row.delivery_request_id or "") != delivery_request_id:
                    orm.rollback()
                    return {
                        "found": False,
                        "changed": False,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if str(row.status) == status:
                    snapshot = _snapshot(row)
                    orm.rollback()
                    return {
                        "found": True,
                        "changed": False,
                        "receipt": _input_receipt_dto(snapshot),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if str(row.status) != "delivering":
                    orm.rollback()
                    return {
                        "found": True,
                        "changed": False,
                        "reason": "receipt_not_delivering",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if status == "delivered":
                    snapshot = mark_live_receipt_delivered_with_projection(
                        orm,
                        receipt_id=receipt_id,
                        delivery_request_id=delivery_request_id,
                    )
                elif status == "queued":
                    # Transient transport failure: the input is late, not lost.
                    # Returning it to the queue is what makes a disconnected
                    # machine delay delivery instead of dropping it.
                    # requeue_live_receipt fails the receipt itself once
                    # attempts are exhausted, so one undeliverable input cannot
                    # block every later one behind it.
                    snapshot, _requeued = requeue_live_receipt(
                        orm,
                        receipt_id=receipt_id,
                        error=error or "session input delivery deferred",
                    )
                else:
                    snapshot = mark_live_receipt_failed(
                        orm,
                        receipt_id=receipt_id,
                        error=error or "session input delivery failed",
                    )
                if snapshot is None:
                    raise RuntimeError("claimed input receipt disappeared during finish")
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "found": True,
                "changed": True,
                "receipt": _input_receipt_dto(snapshot),
                "commit_seq": str(commit_seq),
            }

    def upsert_input_receipt(self, *, receipt: dict[str, Any]) -> dict[str, Any]:
        """Persist one idempotent live input receipt and optional archive projection."""

        from zerg.services.live_session_inputs import _record_live_input_receipt
        from zerg.services.live_session_inputs import load_live_input_receipt_by_id

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                receipt_id = _record_live_input_receipt(
                    orm,
                    owner_id=receipt["owner_id"],
                    session_id=receipt["session_id"],
                    provider=receipt["provider"],
                    text=receipt["text"],
                    intent=receipt["intent"],
                    status=receipt["status"],
                    client_request_id=receipt.get("client_request_id"),
                    payload_digest=receipt.get("payload_digest"),
                    device_id=receipt.get("device_id"),
                    thread_id=receipt.get("thread_id"),
                    archive_session_input_id=receipt.get("archive_session_input_id"),
                    control_command_id=receipt.get("control_command_id"),
                    delivery_request_id=receipt.get("delivery_request_id"),
                    enqueue_archive_projection=receipt["enqueue_archive_projection"],
                    error=receipt.get("error"),
                    expires_at=receipt.get("expires_at"),
                )
                orm.commit()
                snapshot = load_live_input_receipt_by_id(orm, receipt_id=receipt_id)
                if snapshot is None:
                    raise RuntimeError("input receipt disappeared after upsert")
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "receipt": _input_receipt_dto(snapshot),
                "commit_seq": str(commit_seq),
            }

    def create_input_attachment(
        self,
        *,
        attachment: dict[str, Any],
        allow_unbound: bool = False,
    ) -> dict[str, Any]:
        """Create bounded attachment metadata for an input receipt.

        Helm attachments must name an existing owner/session-scoped receipt.
        Console uploads explicitly set ``allow_unbound`` because their UUID
        group is created before the live Console receipt in the same request.
        """
        observed_at = datetime.now(UTC)
        table = LiveSessionInputAttachment.__table__
        with _write_transaction(self.engine) as connection:
            pruned_blob_paths = list(connection.execute(select(table.c.blob_path).where(table.c.expires_at <= observed_at)).scalars())
            connection.execute(delete(table).where(table.c.expires_at <= observed_at))
            if not allow_unbound:
                receipt = connection.execute(
                    select(LiveSessionInputReceipt.__table__.c.id).where(
                        LiveSessionInputReceipt.__table__.c.id == attachment["input_receipt_id"],
                        LiveSessionInputReceipt.__table__.c.owner_id == attachment["owner_id"],
                        LiveSessionInputReceipt.__table__.c.session_id == attachment["session_id"],
                    )
                ).first()
                if receipt is None:
                    return {
                        "created": False,
                        "attachment": None,
                        "reason": "input_receipt_not_found",
                        "pruned_blob_paths": pruned_blob_paths,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
            existing = connection.execute(select(table).where(table.c.id == attachment["id"])).mappings().first()
            values = {**attachment, "created_at": observed_at}
            if existing is not None:
                comparable = {key: existing[key] for key in attachment}
                if comparable != attachment:
                    return {
                        "created": False,
                        "reason": "idempotency_conflict",
                        "pruned_blob_paths": pruned_blob_paths,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                return {
                    "created": False,
                    "attachment": _input_attachment_dto(SimpleNamespace(**existing)),
                    "pruned_blob_paths": pruned_blob_paths,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            connection.execute(insert(table).values(**values))
            row = connection.execute(select(table).where(table.c.id == attachment["id"])).mappings().one()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "created": True,
                "attachment": _input_attachment_dto(SimpleNamespace(**row)),
                "pruned_blob_paths": pruned_blob_paths,
                "commit_seq": str(commit_seq),
            }

    def delete_input_attachments(
        self,
        *,
        owner_id: int,
        session_id: str,
        input_receipt_id: str,
    ) -> dict[str, Any]:
        """Delete one upload group and return its delivery-blob paths."""

        observed_at = datetime.now(UTC)
        table = LiveSessionInputAttachment.__table__
        with _write_transaction(self.engine) as connection:
            rows = (
                connection.execute(
                    select(table.c.blob_path).where(
                        table.c.owner_id == owner_id,
                        table.c.session_id == session_id,
                        table.c.input_receipt_id == input_receipt_id,
                    )
                )
                .scalars()
                .all()
            )
            deleted = (
                connection.execute(
                    delete(table).where(
                        table.c.owner_id == owner_id,
                        table.c.session_id == session_id,
                        table.c.input_receipt_id == input_receipt_id,
                    )
                ).rowcount
                or 0
            )
            commit_seq = _advance_commit_seq(connection, observed_at) if deleted else _current_commit_seq(connection)
            return {
                "deleted": int(deleted),
                "blob_paths": list(rows),
                "commit_seq": str(commit_seq),
            }

    def list_input_attachments(
        self,
        *,
        owner_id: int,
        session_id: str,
        input_receipt_id: str,
    ) -> dict[str, Any]:
        """Every unexpired attachment of one receipt, in upload order.

        The queue drain replays a parked SEND with its images, so a mid-turn
        image send waits durably like any SEND instead of in provider memory.
        """

        table = LiveSessionInputAttachment.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            rows = (
                connection.execute(
                    select(table)
                    .where(
                        table.c.input_receipt_id == input_receipt_id,
                        table.c.owner_id == owner_id,
                        table.c.session_id == session_id,
                        table.c.expires_at > observed_at,
                    )
                    .order_by(table.c.created_at.asc(), table.c.id.asc())
                )
                .mappings()
                .all()
            )
            return {
                "attachments": [_input_attachment_dto(SimpleNamespace(**row)) for row in rows],
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def read_input_attachment(
        self,
        *,
        owner_id: int,
        session_id: str,
        input_receipt_id: str,
        attachment_id: str,
    ) -> dict[str, Any]:
        """Read one unexpired attachment through its full ownership boundary."""

        table = LiveSessionInputAttachment.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            row = (
                connection.execute(
                    select(table).where(
                        table.c.id == attachment_id,
                        table.c.input_receipt_id == input_receipt_id,
                        table.c.owner_id == owner_id,
                        table.c.session_id == session_id,
                        table.c.expires_at > observed_at,
                    )
                )
                .mappings()
                .first()
            )
            return {
                "found": row is not None,
                "attachment": _input_attachment_dto(SimpleNamespace(**row)) if row is not None else None,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def read_input_receipt(
        self,
        *,
        owner_id: int,
        session_id: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        """Read one public input idempotency receipt."""

        from zerg.services.live_session_inputs import get_live_input_receipt_by_client_request

        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                receipt = get_live_input_receipt_by_client_request(
                    orm,
                    owner_id=owner_id,
                    session_id=session_id,
                    client_request_id=client_request_id,
                )
                turn = (
                    orm.query(LiveConsoleTurn).filter(LiveConsoleTurn.receipt_id == receipt.id).one_or_none()
                    if receipt is not None
                    else None
                )
                reporting_turn_ids = _console_turns_with_reporting_run(
                    orm, [turn] if turn is not None else [], observed_at=datetime.now(UTC)
                )
                attachments_by_receipt = _input_attachment_summaries_by_receipt(
                    orm,
                    session_id=session_id,
                    receipts=[receipt] if receipt is not None else [],
                )
            finally:
                orm.close()
            return {
                "found": receipt is not None,
                "receipt": (
                    _input_receipt_dto(
                        receipt,
                        turn=turn,
                        attachments=attachments_by_receipt.get(str(receipt.id), []),
                        reporting_turn_ids=reporting_turn_ids,
                    )
                    if receipt is not None
                    else None
                ),
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def list_session_input_receipts(self, *, session_id: str, limit: int = 50) -> dict[str, Any]:
        """The newest receipts for a session regardless of status, for provenance."""
        with _read_snapshot(self.engine) as connection:
            receipts = _input_receipt_rows(connection, session_id=session_id, limit=limit)
            return {"receipts": receipts, "commit_seq": str(_current_commit_seq(connection))}

    def link_input_receipts_to_events(
        self,
        *,
        session_id: str,
        candidates: list[dict[str, Any]],
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Link delivered sends to the durable user events they became.

        Without a Longhouse identity on the provider event, text/time is a
        lossy fallback. Link only when both the receipt and event each have
        exactly one eligible counterpart. If duplicate text leaves either side
        ambiguous, preserve the receipt unlinked; never break ties by ordering.
        """
        from zerg.services.session_input_links import normalize_input_text

        table = LiveSessionInputReceipt.__table__
        linked: list[dict[str, str]] = []
        with _write_transaction(self.engine) as connection:
            receipts = (
                connection.execute(
                    select(table)
                    .where(
                        table.c.session_id == session_id,
                        table.c.durable_event_id.is_(None),
                        table.c.status.in_(("delivered", "delivering")),
                    )
                    .order_by(table.c.created_at.asc(), table.c.id.asc())
                )
                .mappings()
                .all()
            )
            if not receipts:
                return {"linked": linked, "commit_seq": str(_current_commit_seq(connection))}
            # Matching only on text and time is inherently lossy. Link only
            # when each receipt has one eligible event and each event has one
            # eligible receipt; ties on either side remain unlinked rather
            # than being guessed by chronology.
            ordered_candidates = sorted(candidates, key=lambda item: (item["timestamp"], item["event_id"]))
            eligible_by_receipt: dict[str, list[dict[str, Any]]] = {}
            for receipt in receipts:
                receipt_text = normalize_input_text(receipt["text"])
                created_at = receipt["created_at"]
                if created_at is not None and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                eligible_by_receipt[str(receipt["id"])] = [
                    candidate
                    for candidate in ordered_candidates
                    if normalize_input_text(candidate["text"]) == receipt_text
                    and (
                        created_at is None
                        or (candidate["timestamp"].replace(tzinfo=UTC) if candidate["timestamp"].tzinfo is None else candidate["timestamp"])
                        >= created_at - timedelta(seconds=5)
                    )
                ]

            remaining_receipts = set(eligible_by_receipt)
            while remaining_receipts:
                candidate_receipts: dict[str, list[str]] = {}
                for receipt_id in remaining_receipts:
                    for candidate in eligible_by_receipt[receipt_id]:
                        event_id = str(candidate["event_id"])
                        candidate_receipts.setdefault(event_id, []).append(receipt_id)
                unambiguous: list[tuple[str, dict[str, Any]]] = []
                for receipt_id in sorted(remaining_receipts):
                    options = [
                        candidate
                        for candidate in eligible_by_receipt[receipt_id]
                        if str(candidate["event_id"]) in candidate_receipts and len(candidate_receipts[str(candidate["event_id"])]) == 1
                    ]
                    if len(options) == 1:
                        candidate = options[0]
                        if (
                            sum(
                                1
                                for other_id in remaining_receipts
                                if any(str(item["event_id"]) == str(candidate["event_id"]) for item in eligible_by_receipt[other_id])
                            )
                            == 1
                        ):
                            unambiguous.append((receipt_id, candidate))
                if not unambiguous:
                    break
                for receipt_id, candidate in unambiguous:
                    connection.execute(update(table).where(table.c.id == receipt_id).values(durable_event_id=candidate["event_id"]))
                    receipt = next(row for row in receipts if str(row["id"]) == receipt_id)
                    linked.append(
                        {
                            "receipt_id": receipt_id,
                            "client_request_id": receipt["client_request_id"],
                            "durable_event_id": candidate["event_id"],
                        }
                    )
                    remaining_receipts.remove(receipt_id)
                # A candidate is removed implicitly by remaining_receipts; the
                # next iteration recomputes competing pairs.
            commit_seq = _advance_commit_seq(connection, observed_at) if linked else _current_commit_seq(connection)
            return {"linked": linked, "commit_seq": str(commit_seq)}

    def list_recent_input_receipts(self, *, session_id: str) -> dict[str, Any]:
        """Return queued/delivering and bounded recent terminal receipts."""

        from zerg.services.live_session_inputs import count_live_queued_receipts
        from zerg.services.live_session_inputs import list_recent_live_input_receipts

        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                receipts = list_recent_live_input_receipts(orm, session_id=session_id)
                turns_by_receipt = {}
                if receipts:
                    turns = orm.query(LiveConsoleTurn).filter(LiveConsoleTurn.receipt_id.in_([receipt.id for receipt in receipts])).all()
                    turns_by_receipt = {str(turn.receipt_id): turn for turn in turns}
                reporting_turn_ids = _console_turns_with_reporting_run(orm, turns_by_receipt.values(), observed_at=datetime.now(UTC))
                attachments_by_receipt = _input_attachment_summaries_by_receipt(
                    orm,
                    session_id=session_id,
                    receipts=receipts,
                )
                queued_count = count_live_queued_receipts(orm, session_id=session_id)
            finally:
                orm.close()
            return {
                "receipts": [
                    _input_receipt_dto(
                        receipt,
                        turn=turns_by_receipt.get(str(receipt.id)),
                        attachments=attachments_by_receipt.get(str(receipt.id), []),
                        reporting_turn_ids=reporting_turn_ids,
                    )
                    for receipt in receipts
                ],
                "queued_count": queued_count,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def cancel_input_receipt(self, *, session_id: str, receipt_id: str) -> dict[str, Any]:
        """Cancel one queued receipt, atomically with its Console turn."""

        from zerg.services.live_session_inputs import _snapshot

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                receipt_row = (
                    orm.query(LiveSessionInputReceipt)
                    .filter(
                        LiveSessionInputReceipt.session_id == session_id,
                        LiveSessionInputReceipt.id == receipt_id,
                        LiveSessionInputReceipt.status == "queued",
                    )
                    .one_or_none()
                )
                if receipt_row is None:
                    orm.rollback()
                    return {
                        "cancelled": False,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                turn = orm.query(LiveConsoleTurn).filter(LiveConsoleTurn.receipt_id == receipt_id).one_or_none()
                if turn is not None and turn.state != "queued":
                    orm.rollback()
                    return {
                        "cancelled": False,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                receipt_row.status = "cancelled"
                receipt_row.updated_at = observed_at
                if turn is not None:
                    turn.state = "cancelled"
                    turn.error = "cancelled by user"
                    turn.updated_at = observed_at
                    turn.terminal_at = observed_at
                    receipt_row.error_json = json.dumps(
                        {"code": "cancelled", "message": "cancelled by user"},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                orm.commit()
                receipt = _snapshot(receipt_row)
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "cancelled": True,
                "receipt": _input_receipt_dto(receipt, turn=turn),
                "commit_seq": str(commit_seq),
            }

    def create_directed_input(
        self,
        *,
        owner_id: int,
        source_session_id: str,
        target_session_id: str,
        text: str,
        reply_to_id: int | None,
        client_request_id: str,
        created_at: datetime,
    ) -> dict[str, Any]:
        table = LiveDirectedInput.__table__
        with _write_transaction(self.engine) as connection:
            existing = (
                connection.execute(
                    select(table).where(
                        table.c.owner_id == owner_id,
                        table.c.source_session_id == source_session_id,
                        table.c.client_request_id == client_request_id,
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                exact = (
                    str(existing["target_session_id"]) == target_session_id
                    and str(existing["body"]) == text
                    and existing["reply_to_id"] == reply_to_id
                )
                receipt = None
                if exact and existing["input_receipt_id"] is not None:
                    receipt = (
                        connection.execute(
                            select(LiveSessionInputReceipt.__table__).where(LiveSessionInputReceipt.id == existing["input_receipt_id"])
                        )
                        .mappings()
                        .first()
                    )
                return {
                    "created": False,
                    "idempotency_conflict": not exact,
                    "directed_input": (
                        _directed_input_dto(
                            SimpleNamespace(**existing),
                            SimpleNamespace(**receipt) if receipt is not None else None,
                        )
                        if exact
                        else None
                    ),
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            if source_session_id == target_session_id:
                return {"invalid": "same_session", "commit_seq": str(_current_commit_seq(connection))}
            if not self._session_explicitly_belongs_to_owner(connection, session_id=source_session_id, owner_id=owner_id):
                return {"not_found": "source", "commit_seq": str(_current_commit_seq(connection))}
            if not self._session_explicitly_belongs_to_owner(connection, session_id=target_session_id, owner_id=owner_id):
                return {"not_found": "target", "commit_seq": str(_current_commit_seq(connection))}
            if reply_to_id is not None:
                parent = connection.execute(select(table).where(table.c.id == reply_to_id, table.c.owner_id == owner_id)).mappings().first()
                if parent is None:
                    return {"not_found": "reply", "commit_seq": str(_current_commit_seq(connection))}
                if str(parent["target_session_id"]) != source_session_id or str(parent["source_session_id"]) != target_session_id:
                    return {"invalid": "reply_direction", "commit_seq": str(_current_commit_seq(connection))}
            directed_input_id = int(
                connection.execute(
                    insert(table)
                    .values(
                        owner_id=owner_id,
                        source_session_id=source_session_id,
                        target_session_id=target_session_id,
                        body=text,
                        reply_to_id=reply_to_id,
                        client_request_id=client_request_id,
                        created_at=created_at,
                    )
                    .returning(table.c.id)
                ).scalar_one()
            )
            commit_seq = _advance_commit_seq(connection, created_at)
            row = connection.execute(select(table).where(table.c.id == directed_input_id)).mappings().one()
            return {
                "created": True,
                "idempotency_conflict": False,
                "directed_input": _directed_input_dto(SimpleNamespace(**row)),
                "commit_seq": str(commit_seq),
            }

    def link_directed_input_receipt(
        self,
        *,
        owner_id: int,
        directed_input_id: int,
        input_receipt_id: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        table = LiveDirectedInput.__table__
        receipt_table = LiveSessionInputReceipt.__table__
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table).where(table.c.id == directed_input_id, table.c.owner_id == owner_id)).mappings().first()
            if row is None:
                return {"not_found": "directed_input", "commit_seq": str(_current_commit_seq(connection))}
            receipt = (
                connection.execute(
                    select(receipt_table).where(
                        receipt_table.c.id == input_receipt_id,
                        receipt_table.c.owner_id == owner_id,
                        receipt_table.c.session_id == row["target_session_id"],
                    )
                )
                .mappings()
                .first()
            )
            if receipt is None:
                return {"not_found": "receipt", "commit_seq": str(_current_commit_seq(connection))}
            existing_receipt_id = row["input_receipt_id"]
            if existing_receipt_id is not None and str(existing_receipt_id) != input_receipt_id:
                return {"conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            if existing_receipt_id is None:
                connection.execute(update(table).where(table.c.id == directed_input_id).values(input_receipt_id=input_receipt_id))
                commit_seq = _advance_commit_seq(connection, observed_at)
                row = connection.execute(select(table).where(table.c.id == directed_input_id)).mappings().one()
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "directed_input": _directed_input_dto(
                    SimpleNamespace(**row),
                    SimpleNamespace(**receipt),
                ),
                "commit_seq": str(commit_seq),
            }

    def list_directed_inputs(
        self,
        *,
        owner_id: int,
        session_id: str,
        direction: str,
        after_id: int,
        limit: int,
    ) -> dict[str, Any]:
        table = LiveDirectedInput.__table__
        receipt_table = LiveSessionInputReceipt.__table__
        with _read_snapshot(self.engine) as connection:
            if not self._session_explicitly_belongs_to_owner(connection, session_id=session_id, owner_id=owner_id):
                return {"found": False, "directed_inputs": [], "commit_seq": str(_current_commit_seq(connection))}
            query = select(table).where(table.c.owner_id == owner_id, table.c.id > after_id)
            if direction == "inbound":
                query = query.where(table.c.target_session_id == session_id)
            elif direction == "outbound":
                query = query.where(table.c.source_session_id == session_id)
            else:
                query = query.where(or_(table.c.target_session_id == session_id, table.c.source_session_id == session_id))
            rows = connection.execute(query.order_by(table.c.id.asc()).limit(limit)).mappings().all()
            receipt_ids = [str(row["input_receipt_id"]) for row in rows if row["input_receipt_id"] is not None]
            receipts = {
                str(receipt["id"]): receipt
                for receipt in (
                    connection.execute(select(receipt_table).where(receipt_table.c.id.in_(receipt_ids))).mappings().all()
                    if receipt_ids
                    else []
                )
            }
            directed_inputs = [
                _directed_input_dto(
                    SimpleNamespace(**row),
                    SimpleNamespace(**receipts[str(row["input_receipt_id"])])
                    if row["input_receipt_id"] is not None and str(row["input_receipt_id"]) in receipts
                    else None,
                )
                for row in rows
            ]
            return {
                "found": True,
                "directed_inputs": directed_inputs,
                "next_cursor": directed_inputs[-1]["id"] if directed_inputs else after_id,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def read_directed_input(self, *, owner_id: int, directed_input_id: int) -> dict[str, Any]:
        table = LiveDirectedInput.__table__
        receipt_table = LiveSessionInputReceipt.__table__
        with _read_snapshot(self.engine) as connection:
            row = connection.execute(select(table).where(table.c.id == directed_input_id, table.c.owner_id == owner_id)).mappings().first()
            if row is None:
                return {"found": False, "commit_seq": str(_current_commit_seq(connection))}
            receipt = None
            if row["input_receipt_id"] is not None:
                receipt = connection.execute(select(receipt_table).where(receipt_table.c.id == row["input_receipt_id"])).mappings().first()
            return {
                "found": True,
                "directed_input": _directed_input_dto(
                    SimpleNamespace(**row),
                    SimpleNamespace(**receipt) if receipt is not None else None,
                ),
                "commit_seq": str(_current_commit_seq(connection)),
            }
