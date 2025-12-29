"""Prompt composer - builds final prompts from templates + user context.

This module takes base templates (generic agent behavior) and injects user-specific
context (servers, integrations, preferences) to create complete system prompts.
"""

from zerg.crud import runner_crud
from zerg.database import get_db
from zerg.prompts.templates import BASE_JARVIS_PROMPT
from zerg.prompts.templates import BASE_SUPERVISOR_PROMPT
from zerg.prompts.templates import BASE_WORKER_PROMPT


def format_user_context(ctx: dict) -> str:
    """Format user context section for prompt injection.

    Args:
        ctx: User context dictionary from the database

    Returns:
        Formatted string describing the user
    """
    parts = []

    if name := ctx.get("display_name"):
        role = ctx.get("role", "user")
        location = ctx.get("location")
        loc_str = f" based in {location}" if location else ""
        parts.append(f"{name} - {role}{loc_str}")

    if desc := ctx.get("description"):
        parts.append(desc)

    if instructions := ctx.get("custom_instructions"):
        parts.append(f"\nUser preferences: {instructions}")

    return "\n".join(parts) if parts else "(No user context configured)"


def format_servers(servers: list[dict]) -> str:
    """Format server list for prompt injection.

    Args:
        servers: List of server dictionaries with name, ip, purpose, etc.

    Returns:
        Formatted string describing available servers
    """
    if not servers:
        return "(No servers configured)"

    lines = []
    for s in servers:
        name = s.get("name", "unknown")
        ip = s.get("ip", "")
        purpose = s.get("purpose", "")
        platform = s.get("platform", "")
        notes = s.get("notes", "")
        ssh_alias = s.get("ssh_alias", "")
        ssh_user = s.get("ssh_user", "")
        ssh_port = s.get("ssh_port", "")
        ssh_host = s.get("ssh_host", "") or ip

        line = f"**{name}**"
        if ip:
            line += f" ({ip})"
        if purpose:
            line += f" - {purpose}"
        if platform:
            line += f" [{platform}]"
        if ssh_alias:
            line += f"\n  SSH alias: {ssh_alias}"
        if ssh_user and ssh_host:
            port_suffix = f":{ssh_port}" if ssh_port else ""
            line += f"\n  SSH: {ssh_user}@{ssh_host}{port_suffix}"
        if notes:
            line += f"\n  Notes: {notes}"

        lines.append(line)

    return "\n".join(lines)


def format_server_names(servers: list[dict]) -> str:
    """Format just server names for inline reference.

    Args:
        servers: List of server dictionaries

    Returns:
        Comma-separated list of server names
    """
    if not servers:
        return "no servers configured"
    return ", ".join(s.get("name", "unknown") for s in servers)


def format_integrations(integrations: dict) -> str:
    """Format integrations section for prompt injection.

    Args:
        integrations: Dictionary of integration name -> description

    Returns:
        Formatted string describing user's integrations
    """
    if not integrations:
        return "(No integrations configured)"

    lines = []
    for key, value in integrations.items():
        lines.append(f"- **{key}**: {value}")

    return "\n".join(lines)


def format_online_runners(owner_id: int) -> str:
    """Format available runners for worker prompt.

    Queries the database for online runners and formats them for the worker.

    Args:
        owner_id: User ID to get runners for

    Returns:
        Formatted instruction string for runner_exec vs ssh_exec
    """
    db = next(get_db())
    try:
        runners = runner_crud.get_runners(db, owner_id=owner_id)
        runner_names = [r.name for r in runners if r.status == "online"]
    finally:
        db.close()

    if runner_names:
        names_str = ", ".join(f'"{n}"' for n in runner_names)
        return f"""**Use runner_exec** for these targets (faster, more secure):
- Online runners: {names_str}
- Example: `runner_exec(target="{runner_names[0]}", command="df -h")`

**Only use ssh_exec as fallback** if runner_exec fails or target has no runner."""
    else:
        return """**No runners currently online.** Use ssh_exec for command execution:
- `ssh_exec(host="<server>", command="...")`

Note: Runner daemons provide faster, more secure execution when available."""


def build_supervisor_prompt(user) -> str:
    """Build complete supervisor prompt with user context.

    Args:
        user: SQLAlchemy User model instance

    Returns:
        Complete system prompt for supervisor agents
    """
    ctx = user.context or {}

    return BASE_SUPERVISOR_PROMPT.format(
        user_context=format_user_context(ctx),
        servers=format_servers(ctx.get("servers", [])),
        integrations=format_integrations(ctx.get("integrations", {})),
    )


def build_worker_prompt(user) -> str:
    """Build complete worker prompt with user context.

    Args:
        user: SQLAlchemy User model instance

    Returns:
        Complete system prompt for worker agents
    """
    ctx = user.context or {}

    return BASE_WORKER_PROMPT.format(
        servers=format_servers(ctx.get("servers", [])),
        user_context=format_user_context(ctx),
        online_runners=format_online_runners(user.id),
    )


def build_jarvis_prompt(user, enabled_tools: list[dict]) -> str:
    """Build complete Jarvis prompt with user context and tools.

    Args:
        user: SQLAlchemy User model instance
        enabled_tools: List of tool dicts with 'name' and 'description' keys

    Returns:
        Complete system prompt for Jarvis
    """
    ctx = user.context or {}

    # Format direct tools
    if enabled_tools:
        tool_lines = [f"- **{t['name']}** - {t.get('description', '')}" for t in enabled_tools]
        direct_tools = "\n".join(tool_lines)
    else:
        direct_tools = "(No direct tools currently enabled)"

    # Format limitations based on what's NOT available
    limitations = []
    tool_names = [t["name"] for t in enabled_tools]
    if "calendar" not in tool_names:
        limitations.append("- Calendar/reminders (no tool configured)")
    if "smart_home" not in tool_names:
        limitations.append("- Smart home control (no tool configured)")
    limitations_str = "\n".join(limitations) if limitations else "None currently"

    return BASE_JARVIS_PROMPT.format(
        user_context=format_user_context(ctx),
        direct_tools=direct_tools,
        server_names=format_server_names(ctx.get("servers", [])),
        limitations=limitations_str,
    )
