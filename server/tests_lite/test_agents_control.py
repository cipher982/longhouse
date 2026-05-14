import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.routers.agents_control import CONTROL_HEARTBEAT_TIMEOUT_SECS


def test_control_heartbeat_timeout_tolerates_short_runtime_stalls():
    assert CONTROL_HEARTBEAT_TIMEOUT_SECS >= 300
