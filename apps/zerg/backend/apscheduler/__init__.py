"""Local stub of the *apscheduler* package used only during tests.

The backend test-suite is executed from the ``backend`` directory which means
the empty *namespace package* that lived in ``backend/apscheduler`` was found
*before* the fully-featured stub we vendor at the repository root.  Python
therefore initialised a namespace package that lacked any sub-modules and the
subsequent ``import apscheduler.triggers.cron`` failed.

We solve the problem by turning the directory into a **regular** package that
directly implements the tiny subset of APScheduler required by the codebase.

Note: This file intentionally duplicates a few lines from the top-level stub
to avoid a circular import while ``apscheduler`` is still being initialised.
If you add new functionality to the main stub remember to mirror it here.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Sequence

# ---------------------------------------------------------------------------
# CronTrigger minimal implementation
# ---------------------------------------------------------------------------


class CronTrigger:  # noqa: D101 – simple stub
    def __init__(self, expr: str | None = None):
        self.expr = expr or "* * * * *"

    @classmethod
    def from_crontab(cls, expr: str | None):  # noqa: D401
        if not expr or not isinstance(expr, str):
            raise ValueError("Cron expression must be a non-empty string")
        return cls(expr)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CronTrigger {self.expr}>"


# ---------------------------------------------------------------------------
# AsyncIOScheduler tiny in-memory helper
# ---------------------------------------------------------------------------


class _Job:  # noqa: D101
    def __init__(self, func: Callable[..., Any], trigger: CronTrigger, args: Sequence[Any], job_id: str | None):
        self.func = func
        self.trigger = trigger
        self.args: tuple[Any, ...] = tuple(args)
        self.id = job_id
        self.next_run_time = None


class AsyncIOScheduler:  # noqa: D101 – minimal subset
    def __init__(self):
        self._jobs: Dict[str, _Job] = {}
        self.running: bool = False

    def start(self):  # noqa: D401
        self.running = True

    def shutdown(self):  # noqa: D401
        self.running = False
        self._jobs.clear()

    def add_job(
        self,
        func: Callable[..., Any],
        trigger: CronTrigger,
        *,
        args: Sequence[Any] | None = None,
        id: str | None = None,
        replace_existing: bool = False,
    ) -> _Job:
        if id is None:
            raise ValueError("Job id must be provided")
        if id in self._jobs and not replace_existing:
            raise ValueError(f"Job {id} already exists and replace_existing is False")
        job = _Job(func, trigger, args or (), id)
        self._jobs[id] = job
        return job

    def remove_job(self, job_id: str):
        self._jobs.pop(job_id, None)

    def get_job(self, job_id: str) -> _Job | None:  # noqa: D401
        return self._jobs.get(job_id)

    def get_jobs(self) -> List[_Job]:  # noqa: D401
        return list(self._jobs.values())


# ---------------------------------------------------------------------------
# Sub-module wiring so that ``from apscheduler.triggers.cron import CronTrigger``
# works as expected.
# ---------------------------------------------------------------------------


def _register_module(fullname: str, module: types.ModuleType) -> None:
    sys.modules.setdefault(fullname, module)


# Create parent pseudo-packages
_schedulers_pkg = types.ModuleType("apscheduler.schedulers")
_triggers_pkg = types.ModuleType("apscheduler.triggers")

# Expose AsyncIOScheduler under apscheduler.schedulers.asyncio
_asyncio_mod = types.ModuleType("apscheduler.schedulers.asyncio")
_asyncio_mod.AsyncIOScheduler = AsyncIOScheduler

# Expose CronTrigger under apscheduler.triggers.cron
_cron_mod = types.ModuleType("apscheduler.triggers.cron")
_cron_mod.CronTrigger = CronTrigger

# Attach modules so attribute access works
_schedulers_pkg.asyncio = _asyncio_mod  # type: ignore[attr-defined]
_triggers_pkg.cron = _cron_mod  # type: ignore[attr-defined]

# Register in sys.modules
_register_module("apscheduler.schedulers", _schedulers_pkg)
_register_module("apscheduler.schedulers.asyncio", _asyncio_mod)
_register_module("apscheduler.triggers", _triggers_pkg)
_register_module("apscheduler.triggers.cron", _cron_mod)

# Public re-exports
__all__ = [
    "CronTrigger",
    "AsyncIOScheduler",
]
