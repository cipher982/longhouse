"""Application logging configuration.

Structured formatter, noise suppression, and root logger setup.
Extracted from main.py to keep the app factory focused.
"""

import logging


class StructuredFormatter(logging.Formatter):
    """Formatter that renders structured fields for grep-able telemetry logs.

    For logs with 'extra' dict, formats as:
        2025-12-15 03:19:33 INFO [LONGHOUSE] Starting session session_id=abc123
    """

    BUILTIN_ATTRS = {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "thread",
        "threadName",
        "exc_info",
        "exc_text",
        "stack_info",
        "event",
        "tag",
    }

    def format(self, record):
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        level = f"{record.levelname:7}"

        tag = getattr(record, "tag", None)
        if tag:
            prefix = f"{level} [{tag:7}]"
        else:
            prefix = f"{level}          "

        parts = [timestamp, prefix, record.getMessage()]

        extra_fields = []
        for key, value in record.__dict__.items():
            if key not in self.BUILTIN_ATTRS and not key.startswith("_"):
                if isinstance(value, str) and len(value) > 50:
                    value_str = value[:47] + "..."
                else:
                    value_str = str(value)
                extra_fields.append(f"{key}={value_str}")

        if extra_fields:
            parts.append(" ".join(extra_fields))

        output = " ".join(parts)

        if record.exc_info and record.exc_info[1] is not None:
            output += "\n" + self.formatException(record.exc_info)

        return output


NOISY_MODULES = (
    # Internal chatty modules
    "zerg.routers.websocket",
    "zerg.websocket.manager",
    "zerg.events.event_bus",
    "zerg.services.ops_events",
    "zerg.services.auto_seed",
    # Third-party libraries
    "openai",
    "openai._base_client",
    "openai._utils",
    "stainless",
    "stainless._base_client",
    "httpx",
    "httpcore",
)


def configure_logging(log_level_name: str) -> None:
    """Set up structured logging with noise suppression."""
    try:
        log_level = getattr(logging, log_level_name.upper())
    except AttributeError:
        log_level = logging.INFO

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    root_logger.addHandler(handler)

    for noisy_mod in NOISY_MODULES:
        logging.getLogger(noisy_mod).setLevel(logging.WARNING)

    # SSE and uvicorn noise
    logging.getLogger("sse_starlette").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
