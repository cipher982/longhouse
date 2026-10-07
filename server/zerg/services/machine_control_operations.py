"""Machine Agent control operation status vocabulary.

The operation lifecycle itself is implemented inline by catalogd; this module
owns the shared status sets and the lease grace.
"""

from __future__ import annotations

MACHINE_OPERATION_TIMEOUT_GRACE_SECS = 30
NONTERMINAL_OPERATION_STATUSES = {"queued", "running"}
TERMINAL_OPERATION_STATUSES = {"succeeded", "failed", "timed_out"}
