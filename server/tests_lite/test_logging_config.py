from __future__ import annotations

import logging

from zerg.logging_config import StructuredFormatter


def _record(*, name: str = "zerg.services.searchd", level: int = logging.INFO, message: str = "ready"):
    record = logging.LogRecord(name, level, __file__, 1, message, (), None)
    record.created = 0
    record.msecs = 0
    return record


def test_structured_formatter_uses_utc_and_logger_component():
    output = StructuredFormatter().format(_record())

    assert output == "1970-01-01T00:00:00.000Z INFO    [ZERG.SERVICES.SEARCHD] ready"


def test_structured_formatter_renders_tag_event_and_extra_fields():
    record = _record(name="zerg.services.catalogd", level=logging.WARNING, message="state changed")
    record.tag = "CATALOGD"
    record.event = "supervisor_state_changed"
    record.status = "degraded"
    record.restart_count = 2

    output = StructuredFormatter().format(record)

    assert "WARNING [CATALOGD] state changed" in output
    assert "event=supervisor_state_changed" in output
    assert "status=degraded" in output
    assert "restart_count=2" in output


def test_structured_formatter_keeps_traceback():
    try:
        raise RuntimeError("synthetic failure")
    except RuntimeError:
        record = _record(level=logging.ERROR, message="operation failed")
        record.exc_info = __import__("sys").exc_info()

    output = StructuredFormatter().format(record)

    assert "ERROR   [ZERG.SERVICES.SEARCHD] operation failed" in output
    assert "RuntimeError: synthetic failure" in output
