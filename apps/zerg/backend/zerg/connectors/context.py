"""Context variables for connector credential resolution.

This module provides a thread-safe way to pass the credential resolver
to connector tools during fiche execution using Python's contextvars.

The pattern mirrors the token streaming context (token_stream.py) where
the FicheRunner sets the context before invocation and tools read from it.

Usage in FicheRunner:
    from zerg.connectors.context import set_credential_resolver
    resolver = CredentialResolver(fiche_id=fiche.id, db=db)
    set_credential_resolver(resolver)
    # ... invoke fiche ...
    set_credential_resolver(None)  # cleanup

Usage in Tools:
    from zerg.connectors.context import get_credential_resolver
    resolver = get_credential_resolver()
    if resolver:
        creds = resolver.get(ConnectorType.SLACK)
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING
from typing import Optional

if TYPE_CHECKING:
    from zerg.connectors.resolver import CredentialResolver

# Context variable holding the current credential resolver
# Set by FicheRunner before invoking the fiche, read by connector tools
_credential_resolver_var: contextvars.ContextVar[Optional["CredentialResolver"]] = contextvars.ContextVar(
    "_credential_resolver_var",
    default=None,
)


def get_credential_resolver() -> Optional["CredentialResolver"]:
    """Get the current credential resolver from context.

    Returns:
        CredentialResolver if set, None otherwise.
        Tools should handle None gracefully by returning an error
        or falling back to requiring explicit credentials.
    """
    return _credential_resolver_var.get()


def set_credential_resolver(resolver: Optional["CredentialResolver"]) -> contextvars.Token:
    """Set the credential resolver for the current context.

    Should be called by FicheRunner before invoking the fiche.
    Returns a token that can be used to reset the context.

    Args:
        resolver: The CredentialResolver instance, or None to clear

    Returns:
        Token for resetting the context via reset_credential_resolver()
    """
    return _credential_resolver_var.set(resolver)


def reset_credential_resolver(token: contextvars.Token) -> None:
    """Reset the credential resolver to its previous value.

    Args:
        token: Token returned by set_credential_resolver()
    """
    _credential_resolver_var.reset(token)


__all__ = [
    "get_credential_resolver",
    "set_credential_resolver",
    "reset_credential_resolver",
]
