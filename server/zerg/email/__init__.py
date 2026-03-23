"""Email provider abstraction layer.

This namespace introduces a *very thin* interface that unifies provider-
specific logic (Gmail today, Outlook/IMAP tomorrow) so core services can
delegate provider-specific operations without conditional branches.
"""

from __future__ import annotations

# Re-export helpers so call-sites can simply ``from zerg.email import providers``
from . import providers  # noqa: F401  (re-export)
