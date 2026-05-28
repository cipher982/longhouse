"""Shared repo-root helpers for packaged QA commands."""

from __future__ import annotations

from pathlib import Path

from zerg.services.longhouse_paths import resolve_longhouse_home


def source_checkout_root(repo_root: Path) -> bool:
    contract_path = repo_root / "server/zerg/config/managed_provider_contracts.json"
    scripts_dir = repo_root / "scripts/qa"
    return contract_path.exists() and scripts_dir.exists()


def source_repo_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if source_checkout_root(parent):
            return parent
    return None


def default_repo_root() -> Path:
    return source_repo_root() or Path.cwd()


def provider_live_evidence_base(repo_root: Path) -> Path:
    source_root = source_repo_root()
    if source_root is not None and repo_root.resolve() == source_root.resolve():
        return repo_root / ".build/canaries/provider-live"
    return resolve_longhouse_home() / "canaries/provider-live"
