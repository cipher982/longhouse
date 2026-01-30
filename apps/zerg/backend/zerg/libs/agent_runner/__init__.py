"""DEPRECATED: Use 'from hatch import run, Backend, AgentResult' instead.

This module is a thin wrapper around the hatch package for backwards compatibility.
It will be removed in a future version.

Usage (preferred):
    from hatch import run, Backend, AgentResult

Usage (deprecated, still works):
    from zerg.libs.agent_runner import run, Backend, AgentResult
"""

import warnings

warnings.warn(
    "zerg.libs.agent_runner is deprecated. Use 'from hatch import run, Backend, AgentResult' instead.",
    DeprecationWarning,
    stacklevel=2,
)

from hatch import AgentResult  # noqa: E402
from hatch import Backend  # noqa: E402
from hatch import run  # noqa: E402

__all__ = ["run", "Backend", "AgentResult"]
