"""Unit tests for machine name sanitization.

Covers the cases Codex identified:
- Spaces → hyphens (systemd ExecStart safety)
- XML special chars stripped (plist safety)
- Edge cases: empty, all-special, very long
"""

import pytest

from zerg.services.shipper.token import sanitize_machine_name


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Basic hostname (unchanged)
        ("work-macbook", "work-macbook"),
        # Spaces become hyphens
        ("work laptop", "work-laptop"),
        ("my  work  laptop", "my-work-laptop"),
        # Leading/trailing whitespace stripped
        ("  macbook  ", "macbook"),
        # XML chars stripped — these would break launchd plist
        ("mac&book", "macbook"),
        ("mac<book>", "macbook"),
        ('mac"book', "macbook"),
        ("mac'book", "macbook"),
        # Combination: spaces + XML chars
        ("Dave's Work Laptop", "Daves-Work-Laptop"),
        # Multiple hyphens collapsed
        ("work--laptop", "work-laptop"),
        ("work - laptop", "work-laptop"),
        # Truncated at 64 chars
        ("a" * 80, "a" * 64),
        # Empty string or all-stripped → "unknown"
        ("", "unknown"),
        ("&<>\"'", "unknown"),
        ("   ", "unknown"),
    ],
)
def test_sanitize_machine_name(raw, expected):
    assert sanitize_machine_name(raw) == expected


def test_sanitize_preserves_dots_and_underscores():
    """Dots and underscores are valid hostname chars and should pass through."""
    assert sanitize_machine_name("my_host.local") == "my_host.local"


def test_sanitize_numbers_ok():
    assert sanitize_machine_name("server01") == "server01"


def test_sanitize_already_clean_hostname():
    """Real-world hostname like 'davidrose-macbook-pro' is unchanged."""
    assert sanitize_machine_name("davidrose-macbook-pro") == "davidrose-macbook-pro"
