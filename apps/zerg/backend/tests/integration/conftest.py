"""Configuration for integration tests.

Integration tests make REAL API calls and are skipped by default.
Run them explicitly with: make test-integration
"""

import pytest


def pytest_configure(config):
    """Register the integration marker."""
    config.addinivalue_line("markers", "integration: marks tests as integration tests (real API calls)")


@pytest.fixture(autouse=True)
def integration_test_timeout(request):
    """Set longer timeout for integration tests (60s instead of default 10s)."""
    # Only apply to integration tests
    if "integration" in request.keywords:
        # Set timeout marker if not already set
        if not any(marker.name == "timeout" for marker in request.node.iter_markers()):
            request.node.add_marker(pytest.mark.timeout(60))


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless -m integration is specified."""
    # Check if integration marker is in the marker expression
    marker_expr = config.getoption("-m", default="")

    if "integration" not in marker_expr:
        skip_integration = pytest.mark.skip(reason="Integration tests skipped (run with: make test-integration)")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)
