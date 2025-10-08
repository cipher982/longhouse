"""Typed Pydantic model for the ``Trigger.config`` JSON blob.

The backend currently stores the *config* column of the ``triggers`` table as
un-structured JSON so we can extend it freely when new trigger providers or
features land.  While this gives great flexibility it also shifts a fair
amount of parsing & validation boilerplate to *every* call-site which needs a
value from the blob – callers end up writing repetitive and brittle

    ``(trigger.config or {}).get("history_id")``

expressions.

Introducing a **thin** Pydantic model provides two advantages while remaining
100 % backwards-compatible with the existing database rows and test-suite:

1.  *Type safety & IDE completion* – the most common fields become first class
    attributes so the codebase can simply access ``trigger.config_obj.history_id``.
2.  *Gradual adoption* – the legacy dictionary remains available (``trigger.config``),
    therefore no production behaviour changes until individual modules opt-in
    to the new accessor.

Only the fields that are currently used by the backend are declared.  The
model is configured with ``extra="allow"`` so any future / unknown keys are
preserved when round-tripping through the helper property.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import Literal
from typing import Optional

from pydantic import BaseModel
from pydantic import Field

ProviderLiteral = Literal["gmail", "outlook"]  # extend when additional providers land


class TriggerConfig(BaseModel):  # noqa: D101 – obvious from context
    provider: ProviderLiteral = Field("gmail", description="E-mail provider this trigger listens to")

    # Most recently processed Gmail *historyId* so we can request incremental
    # updates.  Only present for Gmail triggers.
    history_id: Optional[int] = Field(None, description="Last processed Gmail history id")

    # Epoch-ms when the Gmail watch expires.  Tracked so the polling loop can
    # renew ~24 h before expiry.
    watch_expiry: Optional[int] = Field(None, description="Gmail watch expiry (epoch-ms)")

    # Optional filter definition used by *email_filtering.matches*.
    filters: Optional[Dict[str, Any]] = Field(None, description="User supplied filter rules")

    model_config = {
        "extra": "allow",  # keep forward compatibility – unknown keys are retained
        "frozen": True,  # hashable so it can be cached if needed
    }

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def dict_for_storage(self) -> Dict[str, Any]:  # noqa: D401 – tiny helper
        """Return a *dict* representation that can be assigned to the ORM field.

        Using a helper instead of :py:meth:`pydantic.BaseModel.model_dump` keeps
        the call-sites short and avoids importing *pydantic* everywhere.
        """

        # ``model_dump`` would be the canonical method but Pydantic v2 only
        # exists in optional dev dependencies.  To stay compatible with
        # production where *pydantic* v1 might still be present we use ``dict``.
        return dict(self)  # type: ignore[arg-type]
