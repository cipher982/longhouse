"""Images that arrived with one user input, before and after they are stored.

The multipart route reads and validates the files, then hands an
``InputUploads`` to the same input path the JSON route uses. That path stores
the bytes against the input's receipt (``store_input_uploads``) before the
receipt can be seen as ``queued``, and passes the stored refs to whatever
delivers the input: a send, a steer, or a Console turn.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from uuid import UUID

from zerg.services.session_input_attachments import attachment_ref_for_engine
from zerg.services.session_input_attachments import delete_catalog_attachment_blobs
from zerg.services.session_input_attachments import list_catalog_attachments
from zerg.services.session_input_attachments import store_catalog_attachment_blob

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InputUpload:
    filename: str | None
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class InputUploads:
    files: tuple[InputUpload, ...]
    # Identity of the whole operation (text, intent, files) for idempotent
    # replay of a Helm receipt with the same client_request_id.
    payload_digest: str
    # Ordered (mime, sha256) digest a Console replay compares without
    # re-reading blobs.
    console_digest: str

    @classmethod
    def build(cls, *, text: str, intent: str, files: list[InputUpload]) -> "InputUploads":
        payload = hashlib.sha256()
        payload.update(b"longhouse-input-payload-v1\0")
        payload.update(text.encode("utf-8"))
        payload.update(b"\0")
        payload.update(intent.encode("utf-8"))
        console = hashlib.sha256()
        console.update(b"longhouse-console-attachments-v1\0")
        for upload in files:
            for component in (upload.filename or "", upload.mime_type, str(len(upload.data))):
                payload.update(component.encode("utf-8"))
                payload.update(b"\0")
            payload.update(upload.data)
            console.update(upload.mime_type.encode("utf-8"))
            console.update(b"\0")
            console.update(hashlib.sha256(upload.data).hexdigest().encode("ascii"))
            console.update(b"\0")
        return cls(files=tuple(files), payload_digest=payload.hexdigest(), console_digest=console.hexdigest())

    @property
    def total_bytes(self) -> int:
        return sum(len(upload.data) for upload in self.files)


async def store_input_uploads(
    uploads: InputUploads,
    *,
    owner_id: int,
    session_id: UUID | str,
    receipt_id: str,
    allow_unbound: bool = False,
) -> list[dict]:
    """Store every file against ``receipt_id`` and return the engine refs, in order.

    A failure part way deletes what this call stored, then raises.
    """

    session_uuid = UUID(str(session_id))
    refs: list[dict] = []
    try:
        for upload in uploads.files:
            stored = await store_catalog_attachment_blob(
                input_receipt_id=receipt_id,
                owner_id=owner_id,
                session_id=session_uuid,
                mime_type=upload.mime_type,
                data=upload.data,
                original_filename=upload.filename,
                original_byte_size=len(upload.data),
                allow_unbound=allow_unbound,
            )
            refs.append(attachment_ref_for_engine(session_id=str(session_uuid), input_id=receipt_id, stored=stored))
    except BaseException:
        await delete_input_uploads(owner_id=owner_id, session_id=session_uuid, receipt_id=receipt_id)
        raise
    return refs


async def stored_input_refs(*, owner_id: int, session_id: UUID | str, receipt_id: str) -> list[dict]:
    """The engine refs of images already stored for a receipt (replay path)."""

    stored = await list_catalog_attachments(owner_id=owner_id, session_id=session_id, input_receipt_id=receipt_id)
    return [attachment_ref_for_engine(session_id=str(session_id), input_id=receipt_id, stored=row) for row in stored]


async def delete_input_uploads(*, owner_id: int, session_id: UUID | str, receipt_id: str) -> None:
    """Best-effort removal of one receipt's stored images."""

    try:
        await delete_catalog_attachment_blobs(
            owner_id=owner_id,
            session_id=UUID(str(session_id)),
            input_receipt_id=receipt_id,
        )
    except Exception:
        logger.exception("attachment cleanup failed for session %s receipt %s", session_id, receipt_id)
