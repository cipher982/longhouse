"""Tests for EvidenceCompiler - deterministic evidence assembly within budgets."""

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from zerg.crud.crud import create_user
from zerg.models.models import Course
from zerg.models.models import User
from zerg.models.models import CommisJob
from zerg.services.evidence_compiler import EvidenceCompiler
from zerg.services.evidence_compiler import ToolArtifact
from zerg.services.commis_artifact_store import CommisArtifactStore


@pytest.fixture
def temp_artifact_store(tmp_path: Path) -> CommisArtifactStore:
    """Create a temporary artifact store for testing."""
    return CommisArtifactStore(base_path=str(tmp_path / "commis"))


@pytest.fixture
def test_user(db_session: Session) -> User:
    """Create a test user."""
    return create_user(db_session, email="test@example.com")


@pytest.fixture
def concierge_run(db_session: Session, sample_fiche) -> Course:
    """Create a concierge run for testing."""
    from zerg.crud import create_thread
    from zerg.models.enums import CourseStatus
    from zerg.models.enums import CourseTrigger

    # Create a thread for the fiche
    thread = create_thread(db_session, fiche_id=sample_fiche.id, title="Test Run")

    run = Course(
        fiche_id=sample_fiche.id,
        thread_id=thread.id,
        status=CourseStatus.RUNNING,
        trigger=CourseTrigger.MANUAL,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


class TestEvidenceCompiler:
    """Test suite for EvidenceCompiler."""

    def test_compile_no_commis(self, db_session: Session, sample_fiche, concierge_run: Course):
        """Test compilation with no commis jobs."""
        compiler = EvidenceCompiler(db=db_session)
        evidence = compiler.compile(course_id=concierge_run.id, owner_id=sample_fiche.owner_id)

        assert evidence == {}

    def test_compile_commis_not_started(
        self, db_session: Session, sample_fiche, concierge_run: Course, temp_artifact_store: CommisArtifactStore
    ):
        """Test compilation when commis job exists but hasn't started (no commis_id)."""
        # Create commis job without commis_id (not started)
        job = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Test task",
            status="queued",
            commis_id=None,  # Not started
        )
        db_session.add(job)
        db_session.commit()

        compiler = EvidenceCompiler(artifact_store=temp_artifact_store, db=db_session)
        evidence = compiler.compile(course_id=concierge_run.id, owner_id=sample_fiche.owner_id)

        assert evidence == {}

    def test_compile_single_commis_with_tool_outputs(
        self, db_session: Session, sample_fiche, concierge_run: Course, temp_artifact_store: CommisArtifactStore
    ):
        """Test compilation with a single commis that has tool outputs."""
        # Create commis with artifacts
        commis_id = temp_artifact_store.create_commis(
            task="Check disk space",
            config={"model": "gpt-4"},
            owner_id=sample_fiche.owner_id,
        )

        # Add tool outputs
        ssh_output = json.dumps(
            {
                "ok": True,
                "data": {
                    "host": "server1",
                    "command": "df -h",
                    "exit_code": 0,
                    "stdout": "Filesystem      Size  Used Avail Use%\n/dev/sda1       100G   45G   55G  45%",
                    "stderr": "",
                    "duration_ms": 234,
                },
            }
        )
        temp_artifact_store.save_tool_output(commis_id, "ssh_exec", ssh_output, sequence=1)

        # Create commis job
        job = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Check disk space",
            status="success",
            commis_id=commis_id,
        )
        db_session.add(job)
        db_session.commit()

        compiler = EvidenceCompiler(artifact_store=temp_artifact_store, db=db_session)
        evidence = compiler.compile(course_id=concierge_run.id, owner_id=sample_fiche.owner_id, budget_bytes=10000)

        assert job.id in evidence
        assert "001_ssh_exec.txt" in evidence[job.id]
        assert "exit=0" in evidence[job.id]
        assert "df -h" in evidence[job.id]
        assert "--- Evidence for Commis" in evidence[job.id]
        assert "--- End Evidence ---" in evidence[job.id]

    def test_compile_for_job_single_commis(
        self, db_session: Session, sample_fiche, concierge_run: Course, temp_artifact_store: CommisArtifactStore
    ):
        """Test compile_for_job returns evidence for a single commis."""
        commis_id = temp_artifact_store.create_commis(
            task="Check uptime",
            config={"model": "gpt-4"},
            owner_id=sample_fiche.owner_id,
        )

        uptime_output = json.dumps(
            {
                "ok": True,
                "data": {
                    "host": "server1",
                    "command": "uptime",
                    "exit_code": 0,
                    "stdout": "up 10 days,  2:34, 3 users",
                    "stderr": "",
                    "duration_ms": 50,
                },
            }
        )
        temp_artifact_store.save_tool_output(commis_id, "ssh_exec", uptime_output, sequence=1)

        job = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Check uptime",
            status="success",
            commis_id=commis_id,
        )
        db_session.add(job)
        db_session.commit()

        compiler = EvidenceCompiler(artifact_store=temp_artifact_store, db=db_session)
        evidence = compiler.compile_for_job(
            job_id=job.id,
            commis_id=commis_id,
            owner_id=sample_fiche.owner_id,
            budget_bytes=5000,
        )

        assert "tool_calls/001_ssh_exec.txt" in evidence
        assert "uptime" in evidence
        assert "--- Evidence for Commis" in evidence
        assert "--- End Evidence ---" in evidence

    def test_prioritization_failures_first(
        self, db_session: Session, sample_fiche, concierge_run: Course, temp_artifact_store: CommisArtifactStore
    ):
        """Test that failed tool outputs are prioritized first."""
        commis_id = temp_artifact_store.create_commis(
            task="Test failures",
            config={"model": "gpt-4"},
            owner_id=sample_fiche.owner_id,
        )

        # Add successful tool output
        success_output = json.dumps(
            {
                "ok": True,
                "data": {
                    "host": "server1",
                    "command": "ls",
                    "exit_code": 0,
                    "stdout": "file1.txt\nfile2.txt",
                    "stderr": "",
                    "duration_ms": 100,
                },
            }
        )
        temp_artifact_store.save_tool_output(commis_id, "ssh_exec", success_output, sequence=1)

        # Add failed tool output (should be prioritized)
        failed_output = json.dumps(
            {
                "ok": True,
                "data": {
                    "host": "server1",
                    "command": "bad-command",
                    "exit_code": 127,
                    "stdout": "",
                    "stderr": "bash: bad-command: command not found",
                    "duration_ms": 50,
                },
            }
        )
        temp_artifact_store.save_tool_output(commis_id, "ssh_exec", failed_output, sequence=2)

        # Create commis job
        job = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Test failures",
            status="success",
            commis_id=commis_id,
        )
        db_session.add(job)
        db_session.commit()

        compiler = EvidenceCompiler(artifact_store=temp_artifact_store, db=db_session)
        evidence = compiler.compile(course_id=concierge_run.id, owner_id=sample_fiche.owner_id, budget_bytes=10000)

        # Failed output should appear first (with [FAILED] tag)
        evidence_str = evidence[job.id]
        failed_pos = evidence_str.find("[FAILED]")
        success_file_pos = evidence_str.find("001_ssh_exec.txt")

        assert failed_pos != -1, "Failed output should be present"
        assert success_file_pos != -1, "Success output should be present"
        assert failed_pos < success_file_pos, "Failed output should appear before success output"

    def test_truncation_with_head_tail(
        self, db_session: Session, sample_fiche, concierge_run: Course, temp_artifact_store: CommisArtifactStore
    ):
        """Test that large outputs are truncated with head+tail strategy."""
        commis_id = temp_artifact_store.create_commis(
            task="Test truncation",
            config={"model": "gpt-4"},
            owner_id=sample_fiche.owner_id,
        )

        # Create large output (50KB) to ensure truncation
        large_stdout = "LINE\n" * 10000  # ~50KB of content
        large_output = json.dumps(
            {
                "ok": True,
                "data": {
                    "host": "server1",
                    "command": "cat large_file.txt",
                    "exit_code": 0,
                    "stdout": large_stdout,
                    "stderr": "",
                    "duration_ms": 500,
                },
            }
        )
        temp_artifact_store.save_tool_output(commis_id, "ssh_exec", large_output, sequence=1)

        # Create commis job
        job = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Test truncation",
            status="success",
            commis_id=commis_id,
        )
        db_session.add(job)
        db_session.commit()

        # Use small budget to force truncation
        compiler = EvidenceCompiler(artifact_store=temp_artifact_store, db=db_session)
        evidence = compiler.compile(course_id=concierge_run.id, owner_id=sample_fiche.owner_id, budget_bytes=5000)

        evidence_str = evidence[job.id]

        # Should contain truncation marker
        assert "truncated" in evidence_str.lower(), "Should contain truncation marker"

        # Should be within budget (with reasonable margin)
        evidence_bytes = len(evidence_str.encode("utf-8"))
        assert evidence_bytes <= 6000, f"Evidence exceeds budget: {evidence_bytes} bytes"  # Allow margin

    def test_budget_enforcement_multiple_commis(
        self, db_session: Session, sample_fiche, concierge_run: Course, temp_artifact_store: CommisArtifactStore
    ):
        """Test that budget is divided among multiple commis."""
        # Create two commis
        commis1_id = temp_artifact_store.create_commis(task="Commis 1", config={}, owner_id=sample_fiche.owner_id)
        commis2_id = temp_artifact_store.create_commis(task="Commis 2", config={}, owner_id=sample_fiche.owner_id)

        # Add outputs to both
        for commis_id in [commis1_id, commis2_id]:
            output = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "host": "server1",
                        "command": "echo test",
                        "exit_code": 0,
                        "stdout": "test output",
                        "stderr": "",
                        "duration_ms": 100,
                    },
                }
            )
            temp_artifact_store.save_tool_output(commis_id, "ssh_exec", output, sequence=1)

        # Create commis jobs
        job1 = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Commis 1",
            status="success",
            commis_id=commis1_id,
        )
        job2 = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Commis 2",
            status="success",
            commis_id=commis2_id,
        )
        db_session.add_all([job1, job2])
        db_session.commit()

        compiler = EvidenceCompiler(artifact_store=temp_artifact_store, db=db_session)
        evidence = compiler.compile(course_id=concierge_run.id, owner_id=sample_fiche.owner_id, budget_bytes=4000)

        # Both commis should have evidence
        assert len(evidence) == 2
        assert job1.id in evidence
        assert job2.id in evidence

        # Total size should be within budget
        total_bytes = sum(len(e.encode("utf-8")) for e in evidence.values())
        assert total_bytes <= 5000, f"Total evidence exceeds budget: {total_bytes} bytes"  # Allow margin

    def test_owner_id_security_scoping(
        self, db_session: Session, sample_fiche, concierge_run: Course, temp_artifact_store: CommisArtifactStore
    ):
        """Test that owner_id prevents cross-user evidence leakage."""
        # Create another user
        other_user = create_user(db_session, email="other@example.com")

        # Create commis for test_user
        commis_id = temp_artifact_store.create_commis(
            task="Private task",
            config={},
            owner_id=sample_fiche.owner_id,
        )
        output = json.dumps({"ok": True, "data": {"result": "sensitive data"}})
        temp_artifact_store.save_tool_output(commis_id, "ssh_exec", output, sequence=1)

        # Create commis job
        job = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Private task",
            status="success",
            commis_id=commis_id,
        )
        db_session.add(job)
        db_session.commit()

        # Try to compile with other_user's owner_id
        compiler = EvidenceCompiler(artifact_store=temp_artifact_store, db=db_session)
        evidence = compiler.compile(course_id=concierge_run.id, owner_id=other_user.id)

        # Should get no evidence (different owner)
        assert evidence == {}

    def test_missing_artifacts_graceful_degradation(
        self, db_session: Session, sample_fiche, concierge_run: Course, temp_artifact_store: CommisArtifactStore
    ):
        """Test that missing commis artifacts are handled gracefully."""
        # Create commis job with non-existent commis_id
        job = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Test task",
            status="success",
            commis_id="non-existent-commis",
        )
        db_session.add(job)
        db_session.commit()

        compiler = EvidenceCompiler(artifact_store=temp_artifact_store, db=db_session)
        evidence = compiler.compile(course_id=concierge_run.id, owner_id=sample_fiche.owner_id)

        # Should have entry with error message
        assert job.id in evidence
        assert "unavailable" in evidence[job.id].lower()

    def test_error_envelope_marked_as_failed(
        self, db_session: Session, sample_fiche, concierge_run: Course, temp_artifact_store: CommisArtifactStore
    ):
        """Test that error envelopes (ok=False) are marked as failed."""
        commis_id = temp_artifact_store.create_commis(task="Test errors", config={}, owner_id=sample_fiche.owner_id)

        # Add error envelope
        error_output = json.dumps({"ok": False, "error": {"type": "EXECUTION_ERROR", "message": "Connection timeout"}})
        temp_artifact_store.save_tool_output(commis_id, "ssh_exec", error_output, sequence=1)

        # Create commis job
        job = CommisJob(
            owner_id=sample_fiche.owner_id,
            concierge_course_id=concierge_run.id,
            task="Test errors",
            status="failed",
            commis_id=commis_id,
        )
        db_session.add(job)
        db_session.commit()

        compiler = EvidenceCompiler(artifact_store=temp_artifact_store, db=db_session)
        evidence = compiler.compile(course_id=concierge_run.id, owner_id=sample_fiche.owner_id, budget_bytes=10000)

        # Should be marked as failed
        assert "[FAILED]" in evidence[job.id]
        assert "Connection timeout" in evidence[job.id]

    def test_discover_tool_artifacts(self, temp_artifact_store: CommisArtifactStore):
        """Test artifact discovery and metadata extraction."""
        commis_id = temp_artifact_store.create_commis(task="Test", config={}, owner_id=1)

        # Add multiple tool outputs
        outputs = [
            (1, "ssh_exec", json.dumps({"ok": True, "data": {"exit_code": 0}})),
            (2, "ssh_exec", json.dumps({"ok": True, "data": {"exit_code": 127}})),
            (3, "http_request", json.dumps({"ok": True, "data": {}})),
        ]

        for seq, tool_name, output in outputs:
            temp_artifact_store.save_tool_output(commis_id, tool_name, output, sequence=seq)

        compiler = EvidenceCompiler(artifact_store=temp_artifact_store)
        artifacts = compiler._discover_tool_artifacts(commis_id)

        assert len(artifacts) == 3

        # Check sequence numbers
        sequences = [a.sequence for a in artifacts]
        assert sequences == [1, 2, 3]

        # Check tool names
        assert artifacts[0].tool_name == "ssh_exec"
        assert artifacts[1].tool_name == "ssh_exec"
        assert artifacts[2].tool_name == "http_request"

        # Check failure detection
        assert not artifacts[0].failed  # exit_code=0
        assert artifacts[1].failed  # exit_code=127
        assert not artifacts[2].failed  # no exit_code

    def test_prioritize_artifacts(self, temp_artifact_store: CommisArtifactStore):
        """Test artifact prioritization logic."""
        # Create artifacts with different properties
        artifacts = [
            ToolArtifact(sequence=1, filename="001_ssh_exec.txt", tool_name="ssh_exec", size_bytes=100, exit_code=0, failed=False),
            ToolArtifact(sequence=2, filename="002_ssh_exec.txt", tool_name="ssh_exec", size_bytes=200, exit_code=127, failed=True),
            ToolArtifact(sequence=3, filename="003_ssh_exec.txt", tool_name="ssh_exec", size_bytes=150, exit_code=0, failed=False),
        ]

        compiler = EvidenceCompiler(artifact_store=temp_artifact_store)
        prioritized = compiler._prioritize_artifacts(artifacts)

        # Failed should be first
        assert prioritized[0].failed
        assert prioritized[0].sequence == 2

        # Then most recent (sequence 3)
        assert not prioritized[1].failed
        assert prioritized[1].sequence == 3

        # Then oldest (sequence 1)
        assert not prioritized[2].failed
        assert prioritized[2].sequence == 1

    def test_truncate_with_head_tail(self, temp_artifact_store: CommisArtifactStore):
        """Test head+tail truncation strategy."""
        compiler = EvidenceCompiler(artifact_store=temp_artifact_store)

        # Test no truncation needed
        short_content = "Hello world"
        result = compiler._truncate_with_head_tail(short_content, budget=1000)
        assert result == short_content

        # Test truncation with smaller budget to force truncation
        long_content = "A" * 10000
        result = compiler._truncate_with_head_tail(long_content, budget=3000)

        # Should contain truncation marker (case-insensitive check)
        result_lower = result.lower()
        assert "truncated" in result_lower, f"Expected truncation marker in result (len={len(result)})"

        # Should be within budget (approximately)
        assert len(result.encode("utf-8")) <= 3100  # Small margin for marker

        # Should start with 'A's (head)
        assert result.startswith("AAA")

        # Should end with 'A's (tail)
        assert result.rstrip().endswith("AAA")
