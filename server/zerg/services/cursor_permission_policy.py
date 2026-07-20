"""Surface-aware Cursor permission policy normalization."""

from __future__ import annotations

from typing import Literal
from typing import cast

AUTO_APPROVE = "auto_approve"
PROVIDER_LOCAL = "provider_local"
REMOTE_HUMAN = "remote_human"

CursorPermissionPolicy = Literal["auto_approve", "provider_local", "remote_human"]
CursorPermissionSurface = Literal["helm", "console"]


def normalize_cursor_permission_policy(
    value: str | None,
    *,
    surface: CursorPermissionSurface,
) -> CursorPermissionPolicy:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if not normalized:
        return AUTO_APPROVE
    if normalized in {AUTO_APPROVE, PROVIDER_LOCAL, REMOTE_HUMAN}:
        policy = normalized
    elif normalized == "remote_approve":
        policy = REMOTE_HUMAN
    elif normalized == "bypass":
        policy = PROVIDER_LOCAL if surface == "helm" else AUTO_APPROVE
    else:
        raise ValueError(f"unsupported Cursor {surface} permission policy: {value}")
    if surface == "console" and policy == PROVIDER_LOCAL:
        raise ValueError("Cursor Console cannot use provider_local because it has no local permission UI")
    return cast(CursorPermissionPolicy, policy)


def cursor_permission_wire_mode(policy: CursorPermissionPolicy) -> str:
    """Translate canonical policy to the existing managed-launch wire contract."""

    return "remote_approve" if policy == REMOTE_HUMAN else "bypass"
