"""Test application entry point.

This module provides the entry point for the test version of the application,
used by E2E tests with isolated commis databases.
"""

import os

# Must set environment before any zerg imports
# E2E tests should set ENVIRONMENT=test:e2e
if "ENVIRONMENT" not in os.environ:
    os.environ["ENVIRONMENT"] = "test"

# Clear any cached session factories before creating app
from zerg.database import clear_commis_caches  # noqa: E402

clear_commis_caches()

from zerg.core.config import load_config  # noqa: E402
from zerg.core.factory import create_app  # noqa: E402

# Create app with proper test configuration based on environment
config = load_config()
app = create_app(config)

if __name__ == "__main__":
    import sys

    import uvicorn

    # Check if port is specified via command line (--port=X)
    port = None
    for arg in sys.argv:
        if arg.startswith("--port="):
            port = int(arg.split("=")[1])
            break

    # Fallback to commis-based port calculation
    if port is None:
        commis_id = os.getenv("TEST_COMMIS_ID", "0")
        port = 8000 + int(commis_id)

    uvicorn.run(app, host="0.0.0.0", port=port)
