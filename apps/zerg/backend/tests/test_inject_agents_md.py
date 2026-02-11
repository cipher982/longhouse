"""Tests for AGENTS.md instruction chain injection in commis workspaces."""

from pathlib import Path

import pytest

from zerg.services.workspace_manager import inject_agents_md


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def test_inject_creates_claude_dir_and_file(workspace: Path) -> None:
    """Injection creates .claude/CLAUDE.md even with no repo AGENTS.md."""
    result = inject_agents_md(workspace)
    assert result is not None
    assert result.exists()
    assert result.parent.name == ".claude"

    content = result.read_text(encoding="utf-8")
    assert "# Longhouse Context" in content
    assert str(workspace) in content


def test_inject_includes_repo_agents_md(workspace: Path) -> None:
    """Repo-level AGENTS.md is loaded into the instruction chain."""
    agents_md = workspace / "AGENTS.md"
    agents_md.write_text("# My Project\n\nDo things the right way.\n", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "# Repository Instructions" in content
    assert "Do things the right way." in content
    assert "_Source: AGENTS.md_" in content
    assert "# Longhouse Context" in content


def test_inject_prefers_agents_md_over_claude_md(workspace: Path) -> None:
    """AGENTS.md takes priority over CLAUDE.md when both exist."""
    (workspace / "AGENTS.md").write_text("agents content", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("claude content", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "agents content" in content
    assert "claude content" not in content


def test_inject_falls_back_to_claude_md(workspace: Path) -> None:
    """Falls back to CLAUDE.md if AGENTS.md doesn't exist."""
    (workspace / "CLAUDE.md").write_text("claude content", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "claude content" in content
    assert "_Source: CLAUDE.md_" in content


def test_inject_with_project_name(workspace: Path) -> None:
    """Project name is included in Longhouse context."""
    result = inject_agents_md(workspace, project_name="my-cool-project")
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "Project: my-cool-project" in content


def test_inject_appends_to_existing_claude_md(workspace: Path) -> None:
    """Does not overwrite existing .claude/CLAUDE.md — appends Longhouse context."""
    claude_dir = workspace / ".claude"
    claude_dir.mkdir()
    existing_claude = claude_dir / "CLAUDE.md"
    existing_claude.write_text("# Existing Instructions\n\nDo not touch.\n", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "# Existing Instructions" in content
    assert "Do not touch." in content
    assert "# Longhouse Context" in content


def test_inject_does_not_duplicate_longhouse_context(workspace: Path) -> None:
    """If Longhouse context already present, skip appending."""
    claude_dir = workspace / ".claude"
    claude_dir.mkdir()
    existing_claude = claude_dir / "CLAUDE.md"
    existing_claude.write_text("# Longhouse Context\n\nAlready here.\n", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    # Should still contain the original, not duplicated
    assert content.count("# Longhouse Context") == 1


def test_inject_loads_global_instructions(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Global instructions from ~/.longhouse/agents.md are loaded."""
    fake_home = tmp_path / "fakehome"
    longhouse_dir = fake_home / ".longhouse"
    longhouse_dir.mkdir(parents=True)
    global_md = longhouse_dir / "agents.md"
    global_md.write_text("# Global\n\nAlways be kind to tests.\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "# Global Instructions" in content
    assert "Always be kind to tests." in content


def test_inject_section_ordering(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sections appear in order: Global, Repository, Longhouse Context."""
    # Set up global instructions
    fake_home = tmp_path / "fakehome"
    longhouse_dir = fake_home / ".longhouse"
    longhouse_dir.mkdir(parents=True)
    (longhouse_dir / "agents.md").write_text("global stuff", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # Set up repo instructions
    (workspace / "AGENTS.md").write_text("repo stuff", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    global_pos = content.index("# Global Instructions")
    repo_pos = content.index("# Repository Instructions")
    longhouse_pos = content.index("# Longhouse Context")

    assert global_pos < repo_pos < longhouse_pos


def test_inject_mcp_placeholder(workspace: Path) -> None:
    """Longhouse context includes MCP server placeholder note."""
    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "MCP server integration coming in 3f" in content
