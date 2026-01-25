"""Tests for skills API endpoints."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zerg.main import app


@pytest.fixture
def client() -> TestClient:
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def skill_workspace(tmp_path: Path) -> Path:
    """Create a workspace with test skills."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    github_dir = skills_dir / "github"
    github_dir.mkdir()
    (github_dir / "SKILL.md").write_text(
        """---
name: github
description: GitHub integration
emoji: "\U0001f419"
---

# GitHub Skill
Use github tools.
"""
    )

    slack_dir = skills_dir / "slack"
    slack_dir.mkdir()
    (slack_dir / "SKILL.md").write_text(
        """---
name: slack
description: Slack messaging
---

# Slack Skill
"""
    )

    return tmp_path


class TestPathValidation:
    """Tests for workspace path validation."""

    def test_path_traversal_blocked(self, client: TestClient) -> None:
        """Path traversal attempts are blocked."""
        response = client.get("/api/skills", params={"workspace_path": "/etc"})
        assert response.status_code == 400
        assert "must be within" in response.json()["detail"]

    def test_path_traversal_with_dotdot(self, client: TestClient) -> None:
        """Path with .. is blocked."""
        response = client.get(
            "/api/skills",
            params={"workspace_path": "/var/jarvis/workspaces/../../../etc"},
        )
        assert response.status_code == 400

    def test_valid_workspace_path(self, client: TestClient) -> None:
        """Valid workspace path is allowed."""
        # This may not exist, but path validation should pass
        response = client.get(
            "/api/skills",
            params={"workspace_path": "/var/jarvis/workspaces/test"},
        )
        # Should not get 400 for path validation
        # May get 200 with empty list if directory doesn't exist
        assert response.status_code == 200


class TestSkillsAPI:
    """Tests for skills API endpoints."""

    def test_list_skills(self, client: TestClient) -> None:
        """List all skills."""
        response = client.get("/api/skills")
        assert response.status_code == 200

        data = response.json()
        assert "skills" in data
        assert "total" in data
        assert "eligible_count" in data
        # Should have bundled skills at minimum
        assert data["total"] >= 3

    def test_list_skills_with_workspace(self, client: TestClient, skill_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """List skills from workspace."""
        # Set JARVIS_WORKSPACE_PATH to parent of test workspace to allow access
        monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(skill_workspace.parent))

        response = client.get("/api/skills", params={"workspace_path": str(skill_workspace)})
        assert response.status_code == 200

        data = response.json()
        names = [s["name"] for s in data["skills"]]
        # Should include workspace skills
        assert "github" in names

    def test_list_skills_filter_source(self, client: TestClient) -> None:
        """Filter skills by source."""
        response = client.get("/api/skills", params={"source": "bundled"})
        assert response.status_code == 200

        data = response.json()
        for skill in data["skills"]:
            assert skill["source"] == "bundled"

    def test_list_skills_invalid_source(self, client: TestClient) -> None:
        """Invalid source returns 400."""
        response = client.get("/api/skills", params={"source": "invalid"})
        assert response.status_code == 400

    def test_list_skills_eligible_only(self, client: TestClient) -> None:
        """Filter to eligible skills only."""
        response = client.get("/api/skills", params={"eligible_only": True})
        assert response.status_code == 200

        data = response.json()
        for skill in data["skills"]:
            assert skill["eligible"] is True

    def test_get_skill_commands(self, client: TestClient) -> None:
        """Get user-invocable commands."""
        response = client.get("/api/skills/commands")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
        if len(data) > 0:
            assert "name" in data[0]
            assert "description" in data[0]

    def test_get_skills_prompt(self, client: TestClient) -> None:
        """Get skills prompt for system prompt."""
        response = client.get("/api/skills/prompt")
        assert response.status_code == 200

        data = response.json()
        assert "prompt" in data
        assert "skill_count" in data
        assert "version" in data

    def test_get_skill_detail(self, client: TestClient) -> None:
        """Get specific skill details."""
        # First list skills to find one
        list_response = client.get("/api/skills")
        skills = list_response.json()["skills"]
        if not skills:
            pytest.skip("No skills available")

        skill_name = skills[0]["name"]
        response = client.get(f"/api/skills/{skill_name}")
        assert response.status_code == 200

        data = response.json()
        assert data["name"] == skill_name
        assert "content" in data
        assert "requirements" in data

    def test_get_skill_not_found(self, client: TestClient) -> None:
        """Get nonexistent skill returns 404."""
        response = client.get("/api/skills/nonexistent-skill-12345")
        assert response.status_code == 404

    def test_reload_skills(self, client: TestClient) -> None:
        """Reload skills from filesystem."""
        response = client.post("/api/skills/reload")
        assert response.status_code == 200

        data = response.json()
        assert "message" in data
        assert "total" in data
        assert data["message"] == "Skills reloaded"
