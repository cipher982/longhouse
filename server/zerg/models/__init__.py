"""Database models with lazy compatibility re-exports.

Importing a narrow model module (notably ``models.live_store`` from catalogd)
must not initialize the Runtime Host's archive database module.  Keep the
historical ``from zerg.models import User`` API, but resolve those names only
when callers actually request them.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "GUID": ("zerg.models.types", "GUID"),
    "AgentSession": ("zerg.models.agents", "AgentSession"),
    "AgentEvent": ("zerg.models.agents", "AgentEvent"),
    "AgentsBase": ("zerg.models.agents", "AgentsBase"),
    "MachineControlOperation": ("zerg.models.agents", "MachineControlOperation"),
    "APNSDeviceRegistration": ("zerg.models.apns_device_registration", "APNSDeviceRegistration"),
    "APNSLiveActivityRegistration": ("zerg.models.apns_live_activity_registration", "APNSLiveActivityRegistration"),
    "APNSWidgetPushState": ("zerg.models.apns_widget_push_state", "APNSWidgetPushState"),
    "DeviceToken": ("zerg.models.device_token", "DeviceToken"),
    "User": ("zerg.models.user", "User"),
    "RefreshSession": ("zerg.models.refresh_session", "RefreshSession"),
    "SessionShare": ("zerg.models.session_share", "SessionShare"),
    "SessionShareEvent": ("zerg.models.session_share", "SessionShareEvent"),
    "AccountConnectorCredential": ("zerg.models.models", "AccountConnectorCredential"),
    "KnowledgeDocument": ("zerg.models.models", "KnowledgeDocument"),
    "KnowledgeSource": ("zerg.models.models", "KnowledgeSource"),
    "MemoryEmbedding": ("zerg.models.models", "MemoryEmbedding"),
    "MemoryFile": ("zerg.models.models", "MemoryFile"),
    "Runner": ("zerg.models.models", "Runner"),
    "RunnerEnrollToken": ("zerg.models.models", "RunnerEnrollToken"),
    "RunnerJob": ("zerg.models.models", "RunnerJob"),
    "UserTask": ("zerg.models.models", "UserTask"),
    "UserSkill": ("zerg.models.models", "UserSkill"),
    "MachinePresence": ("zerg.models.machine_presence", "MachinePresence"),
    "NotificationClientPresence": (
        "zerg.models.notification_client_presence",
        "NotificationClientPresence",
    ),
    "NotificationEvent": ("zerg.models.notification_event", "NotificationEvent"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})
