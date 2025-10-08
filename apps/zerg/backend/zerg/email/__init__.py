"""Email provider abstraction layer.

This namespace introduces a *very thin* interface that unifies provider-
specific logic (Gmail today, Outlook/IMAP tomorrow) so that the
``EmailTriggerService`` can delegate provider-specific operations instead of
embedding conditional branches.

Only *skeletons* are added for now – functionality is delegated to the
existing Gmail helpers so behaviour remains **unchanged** while we migrate in
small, reviewable steps.
"""

from __future__ import annotations

# Re-export helpers so call-sites can simply ``from zerg.email import providers``
from . import providers  # noqa: F401  (re-export)
