"""Workspace suggestions for the session-launch picker.

Server-owned, frecency-ranked, git-labeled list of recent working directories
for one machine. Both the iOS launch sheet and the web launch modal consume
this instead of re-deriving suggestions client-side from the timeline.

Scoped strictly by ``device_id`` (no ``environment`` fallback): the picker
lists ``device_id`` values, so suggestions must match the same axis or a
renamed machine's ghost history leaks in. See
``services.machines_directory`` for the device list this pairs with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.machines_directory import build_machines_directory

# Frecency: sum a recency weight per session in a cwd group. Frequent AND
# recent directories dominate; a single stale session barely registers.
_RECENCY_BUCKETS: tuple[tuple[float, int], ...] = (
    (1.0, 100),
    (4.0, 70),
    (14.0, 50),
    (31.0, 30),
)
_RECENCY_TAIL_WEIGHT = 10

# Machine-label environments that are never real workspaces to surface.
_EXCLUDED_ENVIRONMENTS = frozenset({"test", "e2e"})


@dataclass(frozen=True)
class WorkspaceSuggestionEntry:
    path: str
    label: str
    git_repo: str | None
    git_branch: str | None
    score: float
    last_used_at: datetime | None
    session_count: int

    def to_response(self) -> dict[str, object]:
        return {
            "path": self.path,
            "label": self.label,
            "git_repo": self.git_repo,
            "git_branch": self.git_branch,
            "score": self.score,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "session_count": self.session_count,
        }


def _recency_weight(age_days: float) -> int:
    for threshold, weight in _RECENCY_BUCKETS:
        if age_days <= threshold:
            return weight
    return _RECENCY_TAIL_WEIGHT


def _compact_path(path: str) -> str:
    """``/Users/x/git/zerg`` → ``~/git/zerg``."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "Users":
        return "~/" + "/".join(parts[3:]) if len(parts) > 3 else "~"
    return path


def _repo_name(git_repo: str | None) -> str | None:
    if not git_repo:
        return None
    name = git_repo.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or None


def _label(path: str, git_repo: str | None, git_branch: str | None) -> str:
    repo = _repo_name(git_repo)
    if repo:
        return f"{repo} ({git_branch})" if git_branch else repo
    return _compact_path(path)


def build_workspace_suggestions(
    db: Session,
    *,
    owner_id: int,
    device_id: str,
    limit: int = 12,
    days_back: int = 45,
) -> list[WorkspaceSuggestionEntry]:
    """Ranked recent workspaces for ``device_id`` owned by ``owner_id``.

    Returns ``[]`` for an unknown/unenrolled device so the picker degrades to
    manual path entry instead of erroring.
    """
    enrolled = {entry.device_id for entry in build_machines_directory(db, owner_id=owner_id)}
    if device_id not in enrolled:
        return []

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days_back)

    stmt = (
        select(
            AgentSession.cwd,
            AgentSession.git_repo,
            AgentSession.git_branch,
            AgentSession.last_activity_at,
            AgentSession.started_at,
        )
        .where(AgentSession.device_id == device_id)
        .where(AgentSession.cwd.is_not(None))
        .where(AgentSession.cwd.like("/%"))
        .where(AgentSession.environment.notin_(_EXCLUDED_ENVIRONMENTS))
    )

    @dataclass
    class _Group:
        count: int = 0
        score: float = 0.0
        last_used_at: datetime | None = None
        git_repo: str | None = None
        git_branch: str | None = None

    groups: dict[str, _Group] = {}
    for cwd, git_repo, git_branch, last_activity_at, started_at in db.execute(stmt).all():
        used_at = last_activity_at or started_at
        if used_at is None:
            continue
        if used_at.tzinfo is None:
            used_at = used_at.replace(tzinfo=timezone.utc)
        if used_at < since:
            continue

        age_days = max(0.0, (now - used_at).total_seconds() / 86400.0)
        group = groups.get(cwd)
        if group is None:
            group = _Group()
            groups[cwd] = group
        group.count += 1
        group.score += _recency_weight(age_days)
        # git metadata + last_used come from the most-recent session in the group.
        if group.last_used_at is None or used_at > group.last_used_at:
            group.last_used_at = used_at
            group.git_repo = git_repo
            group.git_branch = git_branch

    entries = [
        WorkspaceSuggestionEntry(
            path=cwd,
            label=_label(cwd, group.git_repo, group.git_branch),
            git_repo=group.git_repo,
            git_branch=group.git_branch,
            score=group.score,
            last_used_at=group.last_used_at,
            session_count=group.count,
        )
        for cwd, group in groups.items()
    ]
    entries.sort(
        key=lambda e: (e.score, e.last_used_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return entries[:limit]
