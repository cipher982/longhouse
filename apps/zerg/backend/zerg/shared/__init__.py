"""Shared utilities for Zerg jobs.

These utilities are portable versions of Sauron's shared modules,
used by scheduled jobs migrated from Sauron.
"""

from .email import send_alert_email
from .email import send_digest_email
from .email import send_email
from .ssh import SSHResult
from .ssh import run_ssh_command
from .ssh import test_ssh_connection

__all__ = [
    "SSHResult",
    "run_ssh_command",
    "test_ssh_connection",
    "send_email",
    "send_alert_email",
    "send_digest_email",
]
