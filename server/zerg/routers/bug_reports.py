"""Phone bug-report upload and Machine Agent bundle fetch routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.auth.caller import Caller
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.browser_route_auth import get_current_browser_route_caller
from zerg.dependencies.form_post_origin import reject_cross_origin_form_post
from zerg.dependencies.request_db import no_request_db
from zerg.models.device_token import DeviceToken
from zerg.services.bug_reports import MAX_REPORT_FILE_BYTES
from zerg.services.bug_reports import MAX_REPORT_FILES
from zerg.services.bug_reports import BugReportUpload
from zerg.services.bug_reports import create_bug_report
from zerg.services.bug_reports import read_manifest
from zerg.services.bug_reports import read_report_file
from zerg.services.session_chat_impl import _load_session_for_continuation

router = APIRouter(prefix="/reports", tags=["reports"])
agents_router = APIRouter(prefix="/agents/reports", tags=["agents"])


class BugReportResponse(BaseModel):
    report_id: str
    created_at: str
    source_session_id: str | None
    files: list[dict]


def _owner_id(identity: object) -> int:
    try:
        return int(getattr(identity, "owner_id"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc


def _response_from_bundle(bundle) -> BugReportResponse:
    return BugReportResponse(
        report_id=bundle.report_id,
        created_at=bundle.created_at,
        source_session_id=bundle.source_session_id,
        files=[
            {
                "name": item.name,
                "mime_type": item.mime_type,
                "byte_size": item.byte_size,
                "sha256": item.sha256,
                "kind": item.kind,
            }
            for item in bundle.files
        ],
    )


@router.post("", response_model=BugReportResponse)
async def upload_bug_report(
    request: Request,
    description: str = Form(...),
    context_json: str = Form("{}"),
    source_session_id: str | None = Form(None),
    client_report_id: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    caller: Caller = Depends(get_current_browser_route_caller),
    db: Session | None = Depends(no_request_db),
) -> BugReportResponse:
    """Publish the reviewed phone report before any Console execution starts."""

    reject_cross_origin_form_post(request)
    owner_id = int(caller.owner_id)
    normalized_source_session_id = None
    if source_session_id and source_session_id.strip():
        try:
            normalized_source_session_id = str(UUID(source_session_id.strip()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid source_session_id") from exc
        _load_session_for_continuation(db, normalized_source_session_id, owner_id=owner_id)

    if len(files) > MAX_REPORT_FILES:
        raise HTTPException(
            status_code=413,
            detail={"code": "report_too_many_files", "message": f"Too many report images (maximum {MAX_REPORT_FILES})."},
        )
    uploads: list[BugReportUpload] = []
    for upload in files:
        data = await upload.read(MAX_REPORT_FILE_BYTES + 1)
        if len(data) > MAX_REPORT_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"code": "report_image_too_large", "message": "An attached image is too large."},
            )
        uploads.append(
            BugReportUpload(
                filename=upload.filename or "image",
                mime_type=upload.content_type or "application/octet-stream",
                data=data,
            )
        )
    bundle = create_bug_report(
        owner_id=owner_id,
        description=description,
        context_json=context_json,
        source_session_id=normalized_source_session_id,
        uploads=uploads,
        client_report_id=client_report_id,
    )
    return _response_from_bundle(bundle)


@agents_router.get("/{report_id}/manifest")
def get_bug_report_manifest(
    report_id: str,
    identity: DeviceToken = Depends(verify_agents_caller),
    _tenant=Depends(require_single_tenant),
) -> dict:
    try:
        return read_manifest(report_id, owner_id=_owner_id(identity))
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="report not found") from None


@agents_router.get("/{report_id}/files/{filename}")
def get_bug_report_file(
    report_id: str,
    filename: str,
    identity: DeviceToken = Depends(verify_agents_caller),
    _tenant=Depends(require_single_tenant),
):
    try:
        path, entry = read_report_file(report_id, owner_id=_owner_id(identity), filename=filename)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="report file not found") from None
    return FileResponse(path, media_type=str(entry.get("mime_type") or "application/octet-stream"), filename=filename)
