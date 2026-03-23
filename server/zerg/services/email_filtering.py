"""Simple matching helpers for trigger-side e-mail filters.

The goal is **deterministic unit-testable** matching – we do *not* attempt to
mirror Gmail search syntax 1-to-1.  The current rule-set is intentionally
minimal and can be expanded later without breaking backwards compatibility.

Supported filter keys (all optional):

• ``query``              – naive *substring* match (case-insensitive) on
                           *Subject* **or** *From* headers.
• ``from_contains``      – List[str]; any substring match on *From* header.
• ``subject_contains``   – List[str]; same for *Subject*.
• ``label_include``      – List[str]; message must contain *all* labels.
• ``label_exclude``      – List[str]; message must **not** contain *any*.

If a key is provided but the relevant header or attribute is *missing* we
consider it **non-matching** – this makes the matcher fail-safe.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Dict
from typing import List

logger = logging.getLogger(__name__)


def _contains_any(haystack: str | None, needles: List[str]) -> bool:  # noqa: D401 – helper
    if haystack is None:
        return False
    lower = haystack.lower()
    return any(n.lower() in lower for n in needles)


def matches(msg_meta: Dict[str, Any], filters: Dict[str, Any] | None) -> bool:  # noqa: D401 – main helper
    """Return ``True`` if *msg_meta* satisfies *filters*.

    ``msg_meta`` structure (as returned by ``gmail_api.get_message_metadata``):
        {
            "id": "abc",
            "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
            "headers": {"From": "foo@bar.com", "Subject": "Hello"},
        }
    """

    if not filters:  # fast-path – no filters configured
        return True

    label_ids = set((msg_meta.get("labelIds") or []))
    headers = msg_meta.get("headers", {})

    # ------------------------------------------------------------------
    # Label include / exclude
    # ------------------------------------------------------------------
    include = filters.get("label_include") or []
    if include and not set(include).issubset(label_ids):
        return False

    exclude = filters.get("label_exclude") or []
    if exclude and set(exclude).intersection(label_ids):
        return False

    # ------------------------------------------------------------------
    # From / Subject contains
    # ------------------------------------------------------------------
    if filters.get("from_contains"):
        if not _contains_any(headers.get("From"), filters["from_contains"]):
            return False

    if filters.get("subject_contains"):
        if not _contains_any(headers.get("Subject"), filters["subject_contains"]):
            return False

    # ------------------------------------------------------------------
    # Simple text query – treat space-separated terms as AND set
    # ------------------------------------------------------------------
    query = filters.get("query")
    if query:
        tokens = [t.strip() for t in query.split() if t.strip()]
        qhay = (headers.get("From", "") + " " + headers.get("Subject", "")).lower()
        if not all(tok.lower() in qhay for tok in tokens):
            return False

    return True
