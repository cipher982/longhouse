"""Runner setup tools (Jarvis/Supervisor-facing).

These tools are meant for the Supervisor/Jarvis path, not for workers.
They enable a chat-first onboarding flow where Jarvis can:
- list existing runners
- generate a short-lived enrollment token and show install commands

Runners are the multi-tenant-safe replacement for ssh_exec key-mounting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Dict
from typing import List

from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.crud import runner_crud
from zerg.schemas.runner_schemas import RunnerResponse


def _swarmlet_api_url() -> str:
    """Get the public API URL for runner setup commands.

    Requires APP_PUBLIC_URL to be set in all environments.
    """
    from zerg.config import get_settings

    settings = get_settings()
    if not settings.app_public_url:
        raise RuntimeError("APP_PUBLIC_URL not configured. Set this in your environment.")
    return settings.app_public_url


def _runner_docker_image() -> str:
    """Get the runner docker image to use in setup commands."""
    from zerg.config import get_settings

    return get_settings().runner_docker_image


def runner_list() -> Dict[str, Any]:
    """List the current user's runners (names, ids, status, last seen).

    Returns a success envelope with a compact list suitable for chat display.
    """
    resolver = get_credential_resolver()
    if not resolver:
        return {"ok": False, "error": {"type": "execution_error", "message": "No credential context available"}}

    db = resolver.db
    owner_id = resolver.owner_id

    runners = runner_crud.get_runners(db=db, owner_id=owner_id, limit=200)
    data = [RunnerResponse.model_validate(r).model_dump() for r in runners]
    return {"ok": True, "data": {"runners": data}}


def runner_create_enroll_token(ttl_minutes: int = 10) -> Dict[str, Any]:
    """Create a one-time runner enrollment token and setup instructions.

    This is the chat-first equivalent of the dashboard "Add Runner" flow.
    """
    resolver = get_credential_resolver()
    if not resolver:
        return {"ok": False, "error": {"type": "execution_error", "message": "No credential context available"}}

    db = resolver.db
    owner_id = resolver.owner_id

    token_record, plaintext_token = runner_crud.create_enroll_token(
        db=db,
        owner_id=owner_id,
        ttl_minutes=max(1, min(int(ttl_minutes), 60)),
    )

    swarmlet_url = _swarmlet_api_url()
    runner_image = _runner_docker_image()

    docker_command = (
        f"# Step 1: Register runner (one-time)\n"
        f"curl -X POST {swarmlet_url}/api/runners/register \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f'  -d \'{{"enroll_token": "{plaintext_token}", "name": "my-runner"}}\'\n\n'
        f"# Step 2: Save the runner_secret from the response, then run:\n"
        f"docker run -d --name swarmlet-runner \\\n"
        f"  -e SWARMLET_URL={swarmlet_url} \\\n"
        f"  -e RUNNER_NAME=my-runner \\\n"
        f"  -e RUNNER_SECRET=<secret_from_step_1> \\\n"
        f"  {runner_image}"
    )

    expires_at: datetime = token_record.expires_at

    return {
        "ok": True,
        "data": {
            "enroll_token": plaintext_token,
            "expires_at": expires_at.isoformat(),
            "swarmlet_url": swarmlet_url,
            "docker_command": docker_command,
        },
    }


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=runner_list,
        name="runner_list",
        description="List your connected runners (id, name, status, last_seen, metadata).",
    ),
    StructuredTool.from_function(
        func=runner_create_enroll_token,
        name="runner_create_enroll_token",
        description=(
            "Create a one-time enrollment token and show setup commands to install a Runner. "
            "Use this when the user needs to connect their infrastructure for agents to run commands."
        ),
    ),
]
