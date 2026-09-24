"""Browser/iOS routes for relaying a provider sign-in to a user's machine."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import Field

from zerg.dependencies.browser_auth import get_current_browser_caller
from zerg.services.provider_sign_in import ProviderSignInError
from zerg.services.provider_sign_in import cancel_provider_sign_in
from zerg.services.provider_sign_in import start_provider_sign_in
from zerg.services.provider_sign_in import submit_provider_sign_in_code
from zerg.utils.time import UTCBaseModel

router = APIRouter(prefix="/timeline/machines", tags=["machines"])


class ProviderSignInStartResponse(UTCBaseModel):
    attempt_id: str
    provider: str
    flow: Literal["device_code", "paste_code"]
    verification_url: str = Field(..., description="Open this to sign in with the provider.")
    user_code: str | None = Field(default=None, description="Device code to enter on the verification page (device_code flow).")
    prerequisite: str | None = Field(default=None, description="Provider account setting required before the flow works.")
    expires_in_secs: int


class ProviderSignInCodeRequest(UTCBaseModel):
    code: str = Field(..., min_length=1, max_length=4096, description="Code shown after signing in (paste_code flow).")


class ProviderSignInAckResponse(UTCBaseModel):
    attempt_id: str
    accepted: bool | None = None
    cancelled: bool | None = None


def _http(error: ProviderSignInError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail={"code": error.code, "message": error.message})


@router.post("/{device_id}/providers/{provider}/sign-in", response_model=ProviderSignInStartResponse)
async def start_sign_in(device_id: str, provider: str, current_user=Depends(get_current_browser_caller)):
    """Start the provider's own login on the machine and return its URL/code."""
    try:
        result = await start_provider_sign_in(owner_id=int(current_user.owner_id), device_id=device_id, provider=provider.strip().lower())
    except ProviderSignInError as error:
        raise _http(error) from error
    return ProviderSignInStartResponse(**result)


@router.post("/{device_id}/sign-in/{attempt_id}/code", response_model=ProviderSignInAckResponse)
async def submit_sign_in_code(
    device_id: str, attempt_id: str, body: ProviderSignInCodeRequest, current_user=Depends(get_current_browser_caller)
):
    """Pass the code from the provider's callback page back to the waiting CLI."""
    try:
        result = await submit_provider_sign_in_code(
            owner_id=int(current_user.owner_id), device_id=device_id, attempt_id=attempt_id, code=body.code
        )
    except ProviderSignInError as error:
        raise _http(error) from error
    return ProviderSignInAckResponse(**result)


@router.delete("/{device_id}/sign-in/{attempt_id}", response_model=ProviderSignInAckResponse)
async def cancel_sign_in(device_id: str, attempt_id: str, current_user=Depends(get_current_browser_caller)):
    try:
        result = await cancel_provider_sign_in(owner_id=int(current_user.owner_id), device_id=device_id, attempt_id=attempt_id)
    except ProviderSignInError as error:
        raise _http(error) from error
    return ProviderSignInAckResponse(**result)
