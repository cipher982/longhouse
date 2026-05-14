import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.routers.agents_control import CONTROL_HEARTBEAT_TIMEOUT_SECS


def test_control_heartbeat_timeout_is_a_watchdog_not_a_stale_socket_lease():
    assert 30 <= CONTROL_HEARTBEAT_TIMEOUT_SECS <= 120
