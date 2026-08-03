"""One identity shared by in-process Runtime Host background workers."""

from __future__ import annotations

from uuid import uuid4

RUNTIME_BOOT_ID = str(uuid4())
