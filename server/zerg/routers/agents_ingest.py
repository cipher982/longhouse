"""Agents API — session ingest endpoint."""

import gzip
import logging

import zstandard
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.device_token import DeviceToken
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import SessionIngest
from zerg.services.session_views import IngestResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


async def decompress_if_gzipped(request: Request) -> bytes:
    """Decompress request body if gzip or zstd encoded.

    Returns:
        Decompressed request body as bytes
    """
    body = await request.body()
    content_encoding = request.headers.get("Content-Encoding", "").lower()

    if content_encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except gzip.BadGzipFile as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid gzip content: {e}",
            )
    elif content_encoding == "zstd":
        try:
            dctx = zstandard.ZstdDecompressor()
            chunks = []
            with dctx.stream_reader(body) as reader:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
            body = b"".join(chunks)
        except zstandard.ZstdError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid zstd content: {e}",
            )

    return body


@router.post("/ingest", response_model=IngestResponse)
async def ingest_session(
    request: Request,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> IngestResponse:
    """Ingest a session with events.

    Creates or updates a session and inserts events, handling deduplication
    automatically via event hashing.

    This endpoint is called by the shipper to sync local session files
    (e.g., ~/.claude/projects/...) to Zerg.

    Features:
    - Accepts gzip-compressed payloads (Content-Encoding: gzip)
    - Triggers async background summary/embedding/turn-loop work after successful ingest
    """
    try:
        body = await decompress_if_gzipped(request)

        import json

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid JSON: {e}",
            )

        try:
            data = SessionIngest(**payload)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid payload: {e}",
            )

        if device_token:
            if data.device_id and data.device_id != device_token.device_id:
                logger.debug(
                    "Device ID mismatch: payload %s != token %s, using token device_id",
                    data.device_id,
                    device_token.device_id,
                )
            data.device_id = device_token.device_id

        from zerg.services.write_serializer import get_write_serializer

        ws = get_write_serializer()

        def _do_ingest(write_db):
            store = AgentsStore(write_db)
            return store.ingest_session(data)

        result = await ws.execute_or_direct(_do_ingest, db, label="ingest")

        return IngestResponse(
            session_id=str(result.session_id),
            events_inserted=result.events_inserted,
            events_skipped=result.events_skipped,
            session_created=result.session_created,
        )

    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to ingest session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to ingest session",
        )
