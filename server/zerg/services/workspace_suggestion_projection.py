"""Catalog-safe workspace candidate classification and ranking.

Persistence adapters provide bounded pages of canonical session facts.  This
module owns the one admission rule and frecency projection shared by catalogd
and the compatibility archive database; it deliberately imports neither
store's models nor runtime database wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.services.internal_sessions import INTERNAL_CANARY_LABEL_PREFIXES
from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.internal_sessions import is_internal_canary_provider_filter
from zerg.services.session_launch_provenance import HIDDEN_FROM_DEFAULT_ORIGIN_KINDS
from zerg.services.session_launch_provenance import HUMAN_LAUNCH_ACTORS

# One request may cross several noisy automation pages, but remains explicitly
# bounded even if a broken producer floods the recency window.
WORKSPACE_CANDIDATE_PAGE_SIZE = 5_000
WORKSPACE_CANDIDATE_MAX_PAGES = 20

_RECENCY_BUCKETS: tuple[tuple[float, int], ...] = (
    (1.0, 100),
    (4.0, 70),
    (14.0, 50),
    (31.0, 30),
)
_RECENCY_TAIL_WEIGHT = 10
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


@dataclass(frozen=True)
class WorkspaceSessionFacts:
    """Minimum canonical facts needed to prove a human workspace."""

    device_id: str | None
    provider: str | None
    environment: str | None
    project: str | None
    cwd: str | None
    git_repo: str | None
    git_branch: str | None
    last_activity_at: datetime | None
    started_at: datetime | None
    first_user_message_preview: str | None
    origin_kind: str | None
    hidden_from_default_timeline: bool
    user_hidden_from_timeline: bool
    launch_actor: str | None
    is_sidechain: bool


def _normalized_token(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _is_internal_canary(facts: WorkspaceSessionFacts) -> bool:
    if is_internal_canary_provider_filter(facts.provider):
        return True
    project = str(facts.project or "").strip().lower()
    device_id = str(facts.device_id or "").strip().lower()
    return any(
        project == prefix or project.startswith(f"{prefix}-") or device_id == prefix or device_id.endswith(f"-{prefix}")
        for prefix in INTERNAL_CANARY_LABEL_PREFIXES
    )


def classify_human_workspace_candidate(
    facts: WorkspaceSessionFacts,
    *,
    device_id: str,
    since: datetime,
) -> tuple[str, datetime] | None:
    """Return path/time only when canonical facts prove a human workspace."""

    if facts.device_id != device_id:
        return None
    path = str(facts.cwd or "")
    if not path.startswith("/"):
        return None
    used_at = facts.last_activity_at or facts.started_at
    if used_at is None:
        return None
    if used_at.tzinfo is None:
        used_at = used_at.replace(tzinfo=timezone.utc)
    if used_at < since:
        return None
    if _normalized_token(facts.environment) in _EXCLUDED_ENVIRONMENTS:
        return None
    # User curation hides a timeline card, not the fact that a human chose a
    # directory. Only system provenance and sidechain policy disqualify it.
    if facts.hidden_from_default_timeline or facts.is_sidechain:
        return None
    if _normalized_token(facts.origin_kind) in HIDDEN_FROM_DEFAULT_ORIGIN_KINDS:
        return None
    # Transcript roles, prompts, cwd shape, and absence of an automation marker
    # never manufacture human authorship.
    if _normalized_token(facts.launch_actor) not in HUMAN_LAUNCH_ACTORS:
        return None
    if _is_internal_canary(facts):
        return None
    if (
        classify_provider_proof_environment(
            cwd=path,
            machine_id=facts.device_id,
            first_user_text=facts.first_user_message_preview,
        )
        == "test"
    ):
        return None
    return path, used_at


def _recency_weight(age_days: float) -> int:
    for threshold, weight in _RECENCY_BUCKETS:
        if age_days <= threshold:
            return weight
    return _RECENCY_TAIL_WEIGHT


def _compact_path(path: str) -> str:
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


def rank_human_workspace_candidates(
    facts: list[WorkspaceSessionFacts],
    *,
    device_id: str,
    now: datetime,
    days_back: int,
    limit: int,
) -> list[WorkspaceSuggestionEntry]:
    """Classify, deduplicate, label, and rank human workspaces."""

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since = now - timedelta(days=days_back)

    @dataclass
    class _Group:
        count: int = 0
        score: float = 0.0
        last_used_at: datetime | None = None
        git_repo: str | None = None
        git_branch: str | None = None

    groups: dict[str, _Group] = {}
    for item in facts:
        candidate = classify_human_workspace_candidate(item, device_id=device_id, since=since)
        if candidate is None:
            continue
        path, used_at = candidate
        age_days = max(0.0, (now - used_at).total_seconds() / 86400.0)
        group = groups.setdefault(path, _Group())
        group.count += 1
        group.score += _recency_weight(age_days)
        if group.last_used_at is None or used_at > group.last_used_at:
            group.last_used_at = used_at
            group.git_repo = item.git_repo
            group.git_branch = item.git_branch

    entries = [
        WorkspaceSuggestionEntry(
            path=path,
            label=_label(path, group.git_repo, group.git_branch),
            git_repo=group.git_repo,
            git_branch=group.git_branch,
            score=group.score,
            last_used_at=group.last_used_at,
            session_count=group.count,
        )
        for path, group in groups.items()
    ]
    entries.sort(
        key=lambda entry: (entry.score, entry.last_used_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return entries[:limit]
