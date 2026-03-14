"""Runner setup tools (Oikos-facing).

These tools are meant for the Oikos path, not for commis.
They enable a chat-first onboarding flow where Oikos can:
- list existing runners
- generate a short-lived enrollment token and show install commands

Runners are the multi-tenant-safe way to execute commands on user infrastructure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Dict
from typing import List

from zerg.connectors.context import get_credential_resolver
from zerg.crud import runner_crud
from zerg.database import db_session
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.runner_doctor import diagnose_runner
from zerg.services.runner_health import build_runner_response
from zerg.types.tools import Tool as StructuredTool


def _longhouse_api_url() -> str:
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

    owner_id = resolver.owner_id

    with db_session() as db:
        runners = runner_crud.get_runners(db=db, owner_id=owner_id, limit=200)
        connection_manager = get_runner_connection_manager()
        data = [
            build_runner_response(
                r,
                is_connected=connection_manager.is_online(r.owner_id, r.id),
            ).model_dump()
            for r in runners
        ]
        online = sum(1 for runner in data if runner["status"] == "online")
        total = len(data)
        suggested_next_step = None
        if total > 0 and online == 0:
            suggested_next_step = (
                "No runners are online. Use runner_doctor(target=...) on an offline runner or create a fresh enroll token."
            )
        return {
            "ok": True,
            "data": {
                "summary": f"{online}/{total} runners online" if total else "No runners enrolled",
                "suggested_next_step": suggested_next_step,
                "runners": data,
            },
        }


def runner_doctor(target: str) -> Dict[str, Any]:
    """Run server-side doctor diagnostics for one of the current user's runners."""
    resolver = get_credential_resolver()
    if not resolver:
        return {"ok": False, "error": {"type": "execution_error", "message": "No credential context available"}}

    owner_id = resolver.owner_id
    if not target or not str(target).strip():
        return {"ok": False, "error": {"type": "validation_error", "message": "target is required"}}

    with db_session() as db:
        target_text = str(target).strip()
        runner = None
        if target_text.startswith("runner:"):
            try:
                runner_id = int(target_text.split(":", 1)[1])
            except ValueError:
                runner_id = -1
            runner = runner_crud.get_runner(db=db, runner_id=runner_id) if runner_id > 0 else None
            if runner and runner.owner_id != owner_id:
                runner = None
        else:
            runner = runner_crud.get_runner_by_name(db=db, owner_id=owner_id, name=target_text)

        if not runner:
            return {"ok": False, "error": {"type": "validation_error", "message": f"Runner '{target_text}' not found"}}

        connection_manager = get_runner_connection_manager()
        diagnosis = diagnose_runner(
            runner,
            is_connected=connection_manager.is_online(runner.owner_id, runner.id),
        )
        return {
            "ok": True,
            "data": {
                "target": runner.name,
                "runner_id": runner.id,
                "diagnosis": diagnosis.model_dump(),
            },
        }


def runner_create_enroll_token(ttl_minutes: int = 10) -> Dict[str, Any]:
    """Create a one-time runner enrollment token and setup instructions.

    This is the chat-first equivalent of the dashboard "Add Runner" flow.
    """
    resolver = get_credential_resolver()
    if not resolver:
        return {"ok": False, "error": {"type": "execution_error", "message": "No credential context available"}}

    owner_id = resolver.owner_id

    with db_session() as db:
        token_record, plaintext_token = runner_crud.create_enroll_token(
            db=db,
            owner_id=owner_id,
            ttl_minutes=max(1, min(int(ttl_minutes), 60)),
        )

    api_url = _longhouse_api_url()
    runner_image = _runner_docker_image()
    requested_capabilities = "exec.full"

    docker_command = (
        f"# Step 1: Register runner (one-time)\n"
        f"curl -X POST {api_url}/api/runners/register \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f'  -d \'{{"enroll_token": "{plaintext_token}", "name": "my-runner", "capabilities": ["{requested_capabilities}"]}}\'\n\n'
        f"# Step 2: Save the runner_secret from the response, then run:\n"
        f"docker run -d --name longhouse-runner \\\n"
        f"  -e LONGHOUSE_URL={api_url} \\\n"
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
            "longhouse_url": api_url,
            "docker_command": docker_command,
            "one_liner_install_command": (
                f"RUNNER_REQUESTED_CAPABILITIES={requested_capabilities} "
                f"ENROLL_TOKEN={plaintext_token} bash -c 'curl -fsSL {api_url}/api/runners/install.sh | bash'"
            ),
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
            "Use this when the user needs to connect their infrastructure for fiches to run commands."
        ),
    ),
    StructuredTool.from_function(
        func=runner_doctor,
        name="runner_doctor",
        description=(
            "Run Longhouse's server-side diagnosis for one runner by name or explicit id (`runner:123`). "
            "Use this when a runner is offline, unhealthy, outdated, or needs repair guidance."
        ),
    ),
]
