"""Shared runtime interruption exception."""

from __future__ import annotations


class RunnerInterrupted(Exception):
    """Raised when runtime execution pauses for external input."""

    def __init__(self, interrupt_value: dict):
        self.interrupt_value = interrupt_value
        super().__init__(f"Runner interrupted: {interrupt_value}")
