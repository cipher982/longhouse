from __future__ import annotations

from ._shared import _with_action


def _add_archive_backlog_reason(
    reasons: list[str],
    actions: list[str],
    *,
    archive_state: str,
    archive_mode: str,
    archive_pending_ranges: int,
    archive_pending_bytes: int,
    archive_dead_ranges: int,
    archive_dead_bytes: int,
) -> None:
    if archive_dead_ranges > 0 or archive_dead_bytes > 0:
        reasons.append("archive_dead_lettered")
        _with_action(actions, "Inspect archive dead letters: longhouse archive status")
        _with_action(actions, "Retry recoverable archive dead letters: longhouse archive retry-dead --path <path>")
    if archive_pending_ranges <= 0 and archive_pending_bytes <= 0:
        return
    if archive_mode == "paused" or archive_state == "paused":
        reasons.append("archive_repair_paused")
    elif archive_state in {"draining", "scanning", "uploading"}:
        reasons.append("archive_repair_draining")
    else:
        reasons.append("archive_backlog_pending")
    _with_action(actions, "Inspect archive backlog: longhouse archive status")


__all__ = ["_add_archive_backlog_reason"]
