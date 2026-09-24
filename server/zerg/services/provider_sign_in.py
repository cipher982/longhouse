"""Relay a provider's own login CLI between a user's phone and their machine.

The Machine Agent runs the manifest-declared sign-in argv (``sign_in`` in
``schemas/managed_providers.yml``) and returns the verification URL and device
code the CLI prints; for paste-back flows the user's code is written to the
waiting CLI. The credential never passes through Longhouse: the provider CLI
stores it on that machine, and the engine's next readiness update reports the
provider ready.
"""

from __future__ import annotations

from typing import Any

from zerg.services.machine_control_channel import get_machine_control_channel_registry

COMMAND_START = "provider.sign_in.start"
COMMAND_CODE = "provider.sign_in.code"
COMMAND_CANCEL = "provider.sign_in.cancel"
# The engine waits up to 20 s for the CLI to print its URL/code.
START_TIMEOUT_SECS = 30


class ProviderSignInError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


async def _command(*, owner_id: int, device_id: str, command_type: str, payload: dict[str, Any], timeout_secs: int = 15) -> dict[str, Any]:
    registry = get_machine_control_channel_registry()
    if not registry.is_online(owner_id=owner_id, device_id=device_id):
        raise ProviderSignInError(409, "machine_offline", f"{device_id} is offline")
    response = await registry.send_command(
        owner_id=owner_id,
        device_id=device_id,
        session_id=None,
        command_type=command_type,
        payload=payload,
        timeout_secs=timeout_secs,
    )
    if not response.transport_ok:
        raise ProviderSignInError(504, "machine_unreachable", response.error or f"{device_id} did not answer")
    message = response.message or {}
    if not message.get("ok"):
        error = message.get("error") or {}
        raise ProviderSignInError(409, str(error.get("code") or "sign_in_failed"), str(error.get("message") or "Sign-in failed"))
    result = message.get("result")
    return result if isinstance(result, dict) else {}


async def start_provider_sign_in(*, owner_id: int, device_id: str, provider: str) -> dict[str, Any]:
    registry = get_machine_control_channel_registry()
    if not registry.supports(owner_id=owner_id, device_id=device_id, capability=f"{provider}.sign_in"):
        raise ProviderSignInError(409, "sign_in_unsupported", f"{device_id} cannot relay a {provider} sign-in")
    return await _command(
        owner_id=owner_id,
        device_id=device_id,
        command_type=COMMAND_START,
        payload={"provider": provider},
        timeout_secs=START_TIMEOUT_SECS,
    )


async def submit_provider_sign_in_code(*, owner_id: int, device_id: str, attempt_id: str, code: str) -> dict[str, Any]:
    return await _command(
        owner_id=owner_id,
        device_id=device_id,
        command_type=COMMAND_CODE,
        payload={"attempt_id": attempt_id, "code": code},
    )


async def cancel_provider_sign_in(*, owner_id: int, device_id: str, attempt_id: str) -> dict[str, Any]:
    return await _command(
        owner_id=owner_id,
        device_id=device_id,
        command_type=COMMAND_CANCEL,
        payload={"attempt_id": attempt_id},
    )
