"""Regression tests for .env.example contents."""

from __future__ import annotations

from pathlib import Path


def test_env_example_includes_pubsub_sa_email():
    """Pub/Sub OIDC service account email should be documented."""

    repo_root = Path(__file__).resolve().parents[4]
    env_example = repo_root / ".env.example"
    content = env_example.read_text()
    assert "PUBSUB_SA_EMAIL" in content
