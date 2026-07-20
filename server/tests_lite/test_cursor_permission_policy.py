from __future__ import annotations

import pytest

from zerg.services.cursor_permission_policy import normalize_cursor_permission_policy


@pytest.mark.parametrize(
    ("value", "surface", "expected"),
    [
        (None, "helm", "auto_approve"),
        (None, "console", "auto_approve"),
        ("bypass", "helm", "provider_local"),
        ("bypass", "console", "auto_approve"),
        ("remote_approve", "helm", "remote_human"),
        ("remote_approve", "console", "remote_human"),
        ("auto-approve", "helm", "auto_approve"),
    ],
)
def test_cursor_permission_policy_is_surface_aware(value, surface, expected) -> None:
    assert normalize_cursor_permission_policy(value, surface=surface) == expected


def test_cursor_console_rejects_provider_local() -> None:
    with pytest.raises(ValueError, match="no local permission UI"):
        normalize_cursor_permission_policy("provider_local", surface="console")
