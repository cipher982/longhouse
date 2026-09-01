"""Validation shared by the canonical catalog-backed session readers."""

from zerg.auth.caller import caller_principal
from zerg.auth.managed_session_tokens import ManagedSessionToken
from zerg.services.session_listing_types import SessionListingError
from zerg.services.session_listing_types import SessionListParams
from zerg.services.session_listing_types import SessionListResult

__all__ = ["SessionListingError", "SessionListParams", "SessionListResult", "validate_managed_hook_scope"]


def validate_managed_hook_scope(auth: object, params: SessionListParams) -> None:
    """Restrict managed-session hook tokens to their bounded project lookup."""
    auth = caller_principal(auth)
    if not isinstance(auth, ManagedSessionToken):
        return

    token_project = str(auth.project or "").strip()
    requested_project = str(params.project or "").strip()
    if not token_project or token_project != requested_project:
        raise SessionListingError(403, "Managed-session hook scope requires a matching project filter")
    if (
        params.provider is not None
        or params.environment is not None
        or params.include_test
        or params.include_automation
        or params.device_id is not None
        or params.query is not None
        or params.offset != 0
        or params.limit > 5
        or params.days_back > 7
        or params.sort not in {None, "recency"}
        or params.mode != "lexical"
        or params.context_mode != "forensic"
        or not params.hide_autonomous
    ):
        raise SessionListingError(403, "Managed-session hook scope only supports bounded recent project lookup")
