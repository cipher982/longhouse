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


def _admissible_workspace_facts(
    facts: WorkspaceSessionFacts,
    *,
    device_id: str,
    since: datetime,
) -> tuple[str, datetime] | None:
    """Exclusions every workspace candidate must survive, whoever authored it.

    These are the disqualifications that hold regardless of provenance: wrong
    machine, unusable path, too old, excluded environment, system-hidden,
    sidechain, automation origin, internal canary, proof environment.
    """

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
    if _is_internal_canary(facts):
        return None
    if (
        classify_provider_proof_environment(
            cwd=path,
            machine_id=facts.device_id,
        )
        == "test"
    ):
        return None
    return path, used_at


def classify_human_workspace_candidate(
    facts: WorkspaceSessionFacts,
    *,
    device_id: str,
    since: datetime,
) -> tuple[str, datetime] | None:
    """Return path/time only when canonical facts prove a human workspace."""

    admissible = _admissible_workspace_facts(facts, device_id=device_id, since=since)
    if admissible is None:
        return None
    # Transcript roles, prompts, cwd shape, and absence of an automation marker
    # never manufacture human authorship.
    if _normalized_token(facts.launch_actor) not in HUMAN_LAUNCH_ACTORS:
        return None
    return admissible


def classify_checkout_workspace_candidate(
    facts: WorkspaceSessionFacts,
    *,
    device_id: str,
    since: datetime,
) -> tuple[str, datetime] | None:
    """A tracked checkout is a workspace even when authorship is unproven.

    This deliberately does not claim the session was human-authored, and does
    not weaken the rule above. It answers a different and cheaper question:
    is this directory a project worth offering as a place to work?

    It exists because the human stamp is produced by exactly one thing --
    Longhouse's own wrapper observing an interactive TTY. A session discovered
    and ingested from a provider's own archive can never earn it, so a
    developer who installs Longhouse on a laptop they have worked on for a year
    would otherwise be shown an empty workspace picker beside a free-text path
    box. Under the laptop-first activation path that is the common case, not an
    edge one.

    ``git_repo`` is positive evidence, not the absence of a negative: the
    session ran inside a tracked checkout. That is what separates a project
    from the scratch directories automation actually generates -- and where
    automation does run inside a real repository, the directory it names is a
    real one to suggest, so the leak is benign.
    """

    admissible = _admissible_workspace_facts(facts, device_id=device_id, since=since)
    if admissible is None:
        return None
    if not str(facts.git_repo or "").strip():
        return None
    return admissible


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
        # A proven human workspace first; failing that, a tracked checkout.
        # Both survive the same exclusions -- the second only forgoes the
        # authorship stamp that ingested sessions can never earn.
        candidate = classify_human_workspace_candidate(item, device_id=device_id, since=since) or classify_checkout_workspace_candidate(
            item, device_id=device_id, since=since
        )
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
    # Path is the last key, and the only one that cannot tie: two workspaces
    # last used in the same second score identically, and without it the order
    # came from dict insertion, i.e. from session-id ordering in the candidate
    # query. The picker then reshuffled between reads for no reason a user could
    # see. Score and recency stay descending; the tiebreak is ascending path.
    entries.sort(
        key=lambda entry: (
            -entry.score,
            -(entry.last_used_at or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            entry.path,
        )
    )
    return entries[:limit]
