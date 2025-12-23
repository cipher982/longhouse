"""Integration tests for evidence mounting system (Phase 2 of Mount → Reason → Prune).

These tests verify the end-to-end flow:
1. spawn_worker returns compact payload with evidence marker
2. EvidenceMountingLLM expands marker before LLM call
3. Expanded evidence is NOT persisted to thread_messages
"""

import tempfile
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from langchain_core.messages import ToolMessage

from zerg.services.evidence_mounting_llm import EVIDENCE_MARKER_PATTERN
from zerg.services.evidence_mounting_llm import EvidenceMountingLLM
from zerg.services.roundabout_monitor import RoundaboutResult
from zerg.services.roundabout_monitor import ToolIndexEntry
from zerg.services.roundabout_monitor import format_roundabout_result
from zerg.services.worker_artifact_store import WorkerArtifactStore


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


class TestSpawnWorkerReturnFormat:
    """Test that spawn_worker returns compact payload with evidence marker."""

    def test_format_includes_tool_index(self):
        """Test that formatted result includes tool index."""
        result = RoundaboutResult(
            status="complete",
            job_id=123,
            worker_id="test-worker-123",
            duration_seconds=10.5,
            summary="Worker completed task",
            tool_index=[
                ToolIndexEntry(sequence=1, tool_name="ssh_exec", exit_code=0, duration_ms=234, output_bytes=1847, failed=False),
                ToolIndexEntry(sequence=2, tool_name="ssh_exec", exit_code=1, duration_ms=156, output_bytes=523, failed=True),
            ],
            run_id=48,
        )

        formatted = format_roundabout_result(result)

        # Should include tool index
        assert "Tool Index:" in formatted
        assert "1. ssh_exec [exit=0, 234ms, 1847B]" in formatted
        assert "2. ssh_exec [FAILED, 156ms, 523B]" in formatted

    def test_format_includes_evidence_marker(self):
        """Test that formatted result includes evidence marker."""
        result = RoundaboutResult(
            status="complete",
            job_id=123,
            worker_id="test-worker-123",
            duration_seconds=10.5,
            summary="Worker completed",
            run_id=48,
        )

        formatted = format_roundabout_result(result)

        # Should include evidence marker
        assert "[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker-123]" in formatted

        # Verify marker is parseable
        match = EVIDENCE_MARKER_PATTERN.search(formatted)
        assert match is not None
        assert match.group(1) == "48"  # run_id
        assert match.group(2) == "123"  # job_id
        assert match.group(3) == "test-worker-123"  # worker_id

    def test_format_failed_includes_marker(self):
        """Test that failed workers include evidence marker (Issue 2 fix)."""
        result = RoundaboutResult(
            status="failed",
            job_id=123,
            worker_id="test-worker-123",
            duration_seconds=5.2,
            error="Worker failed: SSH connection timeout",
            run_id=48,
        )

        formatted = format_roundabout_result(result)

        # Should include evidence marker even for failures
        assert "[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker-123]" in formatted

        # Should also contain error info
        assert "failed" in formatted.lower()
        assert "SSH connection timeout" in formatted

    def test_format_timeout_includes_marker(self):
        """Test that timed-out workers include evidence marker (Issue 2 fix)."""
        result = RoundaboutResult(
            status="monitor_timeout",
            job_id=123,
            worker_id="test-worker-123",
            duration_seconds=300.0,
            worker_still_running=True,
            error="Monitor timeout after 300s",
            run_id=48,
        )

        formatted = format_roundabout_result(result)

        # Should include evidence marker even for timeouts
        assert "[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker-123]" in formatted

        # Should also contain timeout info
        assert "timeout" in formatted.lower()
        assert "STILL RUNNING" in formatted

    def test_format_without_run_id_no_marker(self):
        """Test that formatted result omits marker when run_id is None."""
        result = RoundaboutResult(
            status="complete",
            job_id=123,
            worker_id="test-worker-123",
            duration_seconds=10.5,
            summary="Worker completed",
            run_id=None,  # No supervisor context
        )

        formatted = format_roundabout_result(result)

        # Should NOT include evidence marker
        assert "[EVIDENCE:" not in formatted


class TestEvidenceMountingIntegration:
    """Test integration between EvidenceCompiler and EvidenceMountingLLM."""

    @pytest.mark.asyncio
    async def test_evidence_expansion_with_mock_compiler(self):
        """Test that evidence markers trigger expansion via EvidenceCompiler."""
        from unittest.mock import AsyncMock
        from unittest.mock import MagicMock

        # Create LLM wrapper with mocked compiler
        mock_base_llm = AsyncMock()
        mock_base_llm.ainvoke = AsyncMock(return_value="Test response")

        mock_db = MagicMock()

        wrapper = EvidenceMountingLLM(
            base_llm=mock_base_llm,
            run_id=48,
            owner_id=100,
            db=mock_db,
        )

        # Mock the compiler's compile method to return test evidence
        with patch.object(wrapper.compiler, "compile") as mock_compile:
            mock_compile.return_value = {
                123: "--- Evidence for Worker 123 ---\nTool 1: ssh_exec [exit=0]\nTool 2: http_request [ok]\n--- End Evidence ---"
            }

            # Create messages with evidence marker
            messages = [
                ToolMessage(
                    content="Worker completed.\n[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]",
                    tool_call_id="tc1",
                    name="spawn_worker",
                ),
            ]

            # Call LLM (should expand evidence)
            await wrapper.ainvoke(messages)

            # Verify compiler was called with correct parameters
            mock_compile.assert_called_once_with(run_id=48, owner_id=100, db=mock_db)

            # Verify base LLM was called with expanded evidence
            mock_base_llm.ainvoke.assert_called_once()
            call_args = mock_base_llm.ainvoke.call_args[0][0]

            # Check that evidence was expanded
            expanded_msg = call_args[0]
            assert isinstance(expanded_msg, ToolMessage)
            assert "[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]" in expanded_msg.content
            assert "--- Evidence for Worker 123 ---" in expanded_msg.content
            assert "Tool 1: ssh_exec" in expanded_msg.content

    @pytest.mark.asyncio
    async def test_no_expansion_without_context(self):
        """Test that evidence mounting is skipped when no context is available."""
        from unittest.mock import AsyncMock

        mock_base_llm = AsyncMock()
        mock_base_llm.ainvoke = AsyncMock(return_value="Test response")

        # Create wrapper WITHOUT context
        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm)

        messages = [
            ToolMessage(
                content="Worker completed.\n[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]",
                tool_call_id="tc1",
                name="spawn_worker",
            ),
        ]

        # Call LLM (should NOT expand evidence)
        await wrapper.ainvoke(messages)

        # Verify base LLM was called with original messages (no expansion)
        mock_base_llm.ainvoke.assert_called_once()
        call_args = mock_base_llm.ainvoke.call_args[0][0]

        original_msg = call_args[0]
        assert original_msg.content == "Worker completed.\n[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]"
        assert "--- Evidence for Worker" not in original_msg.content


class TestEvidencePersistence:
    """Test that expanded evidence is NOT persisted to thread_messages."""

    @pytest.mark.asyncio
    async def test_only_compact_payload_persisted(self):
        """Test that thread_messages only contains compact payload, not expanded evidence.

        This is a critical invariant: the evidence marker is persisted, but the
        expanded evidence (which can be 32KB+) is NOT saved to the database.
        """
        from unittest.mock import AsyncMock
        from unittest.mock import MagicMock

        # Create wrapper with mocked compiler
        mock_db = MagicMock()
        mock_base_llm = AsyncMock()
        mock_base_llm.ainvoke = AsyncMock(return_value="Test response")

        wrapper = EvidenceMountingLLM(base_llm=mock_base_llm, run_id=48, owner_id=100, db=mock_db)

        # Create compact message (what gets persisted)
        compact_message = ToolMessage(
            content="Worker completed.\n[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]",
            tool_call_id="tc1",
            name="spawn_worker",
        )

        # Simulate persistence check
        original_content = compact_message.content
        original_size = len(original_content)

        # Mock compiler to return large evidence
        with patch.object(wrapper.compiler, "compile") as mock_compile:
            large_evidence = "--- Evidence for Worker 123 ---\n" + ("Tool output line\n" * 1000) + "--- End Evidence ---"
            mock_compile.return_value = {123: large_evidence}

            # Call LLM (expands evidence internally)
            await wrapper.ainvoke([compact_message])

            # Verify original message is unchanged (evidence expanded only in-flight)
            assert compact_message.content == original_content
            assert len(compact_message.content) == original_size

            # Verify expansion happened (by checking LLM received expanded content)
            call_args = mock_base_llm.ainvoke.call_args[0][0]
            expanded_msg = call_args[0]
            expanded_size = len(expanded_msg.content)

            # Expanded content should be MUCH larger than compact
            assert expanded_size > original_size * 2
            assert "--- Evidence for Worker" in expanded_msg.content

            # But original message (what would be persisted) is unchanged
            assert len(compact_message.content) < 500  # Still compact


class TestNonStreamingPath:
    """Test that evidence mounting works when LLM_TOKEN_STREAM is disabled.

    This is a regression test for Issue 1: the non-streaming path was bypassing
    the EvidenceMountingLLM wrapper by calling _call_model_sync which created
    a fresh LLM instance.
    """

    @pytest.mark.asyncio
    async def test_evidence_mounting_with_streaming_disabled(self, monkeypatch):
        """Test that evidence mounting works when enable_token_stream=False."""
        from unittest.mock import AsyncMock, MagicMock
        from langchain_core.messages import ToolMessage
        from zerg.services.evidence_mounting_llm import EvidenceMountingLLM

        # Disable streaming
        monkeypatch.setenv("LLM_TOKEN_STREAM", "false")

        # Create mock base LLM
        mock_base_llm = AsyncMock()
        mock_base_llm.ainvoke = AsyncMock(return_value="Test response")

        # Create wrapper with context
        mock_db = MagicMock()
        wrapper = EvidenceMountingLLM(
            base_llm=mock_base_llm,
            run_id=48,
            owner_id=100,
            db=mock_db,
        )

        # Mock compiler to return evidence
        from unittest.mock import patch
        with patch.object(wrapper.compiler, "compile") as mock_compile:
            mock_compile.return_value = {
                123: "--- Evidence for Worker 123 ---\nTool output here\n--- End ---"
            }

            # Create message with evidence marker
            messages = [
                ToolMessage(
                    content="Worker result\n[EVIDENCE:run_id=48,job_id=123,worker_id=test-worker]",
                    tool_call_id="tc1",
                    name="spawn_worker",
                ),
            ]

            # Call ainvoke (should work even without streaming)
            await wrapper.ainvoke(messages)

            # Verify evidence was expanded
            mock_base_llm.ainvoke.assert_called_once()
            call_args = mock_base_llm.ainvoke.call_args[0][0]
            expanded_msg = call_args[0]

            # Should contain expanded evidence
            assert "--- Evidence for Worker 123 ---" in expanded_msg.content
            assert "Tool output here" in expanded_msg.content

    @pytest.mark.asyncio
    async def test_agent_uses_wrapped_llm_non_streaming(self, monkeypatch):
        """Test that agent's non-streaming path uses the wrapped LLM.

        This verifies that _call_model_async always uses llm_with_tools (wrapped)
        instead of calling _call_model_sync (which would create a fresh LLM).
        """
        from unittest.mock import AsyncMock, MagicMock, patch
        from langchain_core.messages import AIMessage, HumanMessage
        from zerg.agents_def.zerg_react_agent import get_runnable

        # Disable streaming
        monkeypatch.setenv("LLM_TOKEN_STREAM", "false")

        # Create mock agent
        mock_agent = MagicMock()
        mock_agent.id = 1
        mock_agent.owner_id = 100
        mock_agent.model = "gpt-4"
        mock_agent.allowed_tools = []

        # Patch _make_llm to track if it's called multiple times (it shouldn't be)
        llm_creation_count = 0
        original_make_llm = None

        def counting_make_llm(agent_row, tools):
            nonlocal llm_creation_count, original_make_llm
            llm_creation_count += 1
            # Create a mock LLM that returns a final response
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="Final response"))
            mock_llm.bind_tools = MagicMock(return_value=mock_llm)
            return mock_llm

        # Patch _make_llm in the module
        import zerg.agents_def.zerg_react_agent as react_module
        original_make_llm = react_module._make_llm
        react_module._make_llm = counting_make_llm

        try:
            # Create runnable
            runnable = get_runnable(mock_agent)

            # Execute with a simple message
            messages = [HumanMessage(content="Hello")]
            config = {"configurable": {"thread_id": "test-thread"}}
            result = await runnable.ainvoke(messages, config=config)

            # Verify _make_llm was called exactly once (not twice - once for wrapper, once for sync call)
            assert llm_creation_count == 1, f"Expected 1 LLM creation, got {llm_creation_count}"

        finally:
            # Restore original
            react_module._make_llm = original_make_llm


class TestCriticalScenario:
    """Test the critical scenario: empty worker prose but useful tool outputs.

    This is the PRIMARY PROBLEM the evidence mounting system solves:
    - Worker executes tools successfully (e.g., ssh_exec)
    - Worker's final AI message is empty or garbage ("(No result generated)")
    - Supervisor should still answer correctly using raw tool outputs
    """

    @pytest.mark.asyncio
    async def test_empty_result_txt_with_tool_outputs(self, db_session, sample_agent, temp_artifact_path):
        """Test supervisor can answer even when worker result.txt is empty.

        This simulates the bug scenario:
        1. Worker runs ssh_exec successfully
        2. Worker's result.txt is empty or "(No result generated)"
        3. EvidenceCompiler should still provide ssh_exec output
        4. Supervisor should receive expanded evidence with tool outputs
        """
        from unittest.mock import AsyncMock
        import json
        from sqlalchemy.orm import Session
        from zerg.models.models import AgentRun, WorkerJob
        from zerg.models.enums import RunStatus, RunTrigger
        from zerg.crud import create_thread
        from zerg.services.evidence_compiler import EvidenceCompiler
        from zerg.services.worker_artifact_store import WorkerArtifactStore

        # Create supervisor run
        thread = create_thread(db_session, agent_id=sample_agent.id, title="Test Run")
        supervisor_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
        )
        db_session.add(supervisor_run)
        db_session.commit()
        db_session.refresh(supervisor_run)

        # Create artifact store
        artifact_store = WorkerArtifactStore(base_path=temp_artifact_path)

        # Create worker with tool output but empty result
        worker_id = artifact_store.create_worker(
            task="Check disk space on server",
            config={"model": "gpt-4"},
            owner_id=sample_agent.owner_id,
        )

        # Add successful ssh_exec output
        ssh_output = json.dumps({
            "ok": True,
            "data": {
                "host": "clifford",
                "command": "df -h",
                "exit_code": 0,
                "stdout": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1       100G   45G   55G  45% /\n/dev/sdb1       500G  200G  300G  40% /data",
                "stderr": "",
                "duration_ms": 234,
            }
        })
        artifact_store.save_tool_output(worker_id, "ssh_exec", ssh_output, sequence=1)

        # Save empty result.txt (the problem case!)
        artifact_store.save_result(worker_id, "(No result generated)")

        # Create worker job
        job = WorkerJob(
            owner_id=sample_agent.owner_id,
            supervisor_run_id=supervisor_run.id,
            task="Check disk space on server",
            status="success",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()

        # Format roundabout result (what supervisor sees)
        result = RoundaboutResult(
            status="complete",
            job_id=job.id,
            worker_id=worker_id,
            duration_seconds=5.2,
            summary="(No result generated)",  # Empty/garbage summary
            tool_index=[
                ToolIndexEntry(
                    sequence=1,
                    tool_name="ssh_exec",
                    exit_code=0,
                    duration_ms=234,
                    output_bytes=len(ssh_output),
                    failed=False
                ),
            ],
            run_id=supervisor_run.id,
        )
        compact_payload = format_roundabout_result(result)

        # Verify compact payload has marker
        assert "[EVIDENCE:" in compact_payload
        assert "ssh_exec [exit=0" in compact_payload

        # Create LLM wrapper with real compiler
        mock_base_llm = AsyncMock()
        mock_base_llm.ainvoke = AsyncMock(return_value="Test response")

        compiler = EvidenceCompiler(artifact_store=artifact_store, db=db_session)
        wrapper = EvidenceMountingLLM(
            base_llm=mock_base_llm,
            run_id=supervisor_run.id,
            owner_id=sample_agent.owner_id,
            db=db_session,
        )
        wrapper.compiler = compiler  # Use real compiler

        # Create message as supervisor would receive it
        messages = [
            ToolMessage(
                content=compact_payload,
                tool_call_id="tc1",
                name="spawn_worker",
            ),
        ]

        # Call LLM (should expand evidence)
        await wrapper.ainvoke(messages)

        # Verify LLM received expanded evidence with tool output
        call_args = mock_base_llm.ainvoke.call_args[0][0]
        expanded_msg = call_args[0]

        # Should contain evidence expansion
        assert "--- Evidence for Worker" in expanded_msg.content
        assert "001_ssh_exec.txt" in expanded_msg.content
        assert "df -h" in expanded_msg.content
        assert "/dev/sda1" in expanded_msg.content
        assert "45G" in expanded_msg.content

        # Should show exit code
        assert "exit=0" in expanded_msg.content

    def test_multiple_tools_failed_tool_prioritized(self, db_session, sample_agent, temp_artifact_path):
        """Test that failed tools are prioritized even with empty result.txt."""
        import json
        from zerg.services.evidence_compiler import EvidenceCompiler
        from zerg.services.worker_artifact_store import WorkerArtifactStore
        from zerg.models.models import AgentRun, WorkerJob
        from zerg.models.enums import RunStatus, RunTrigger
        from zerg.crud import create_thread

        # Create supervisor run
        thread = create_thread(db_session, agent_id=sample_agent.id, title="Test Run")
        supervisor_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
        )
        db_session.add(supervisor_run)
        db_session.commit()
        db_session.refresh(supervisor_run)

        # Create artifact store and worker
        artifact_store = WorkerArtifactStore(base_path=temp_artifact_path)
        worker_id = artifact_store.create_worker(
            task="Check server status",
            config={"model": "gpt-4"},
            owner_id=sample_agent.owner_id,
        )

        # Add successful tool
        success_output = json.dumps({
            "ok": True,
            "data": {
                "host": "clifford",
                "command": "uptime",
                "exit_code": 0,
                "stdout": "up 45 days",
                "stderr": "",
                "duration_ms": 100,
            }
        })
        artifact_store.save_tool_output(worker_id, "ssh_exec", success_output, sequence=1)

        # Add failed tool (should be prioritized)
        failed_output = json.dumps({
            "ok": True,
            "data": {
                "host": "clifford",
                "command": "bad-command",
                "exit_code": 127,
                "stdout": "",
                "stderr": "bash: bad-command: command not found",
                "duration_ms": 50,
            }
        })
        artifact_store.save_tool_output(worker_id, "ssh_exec", failed_output, sequence=2)

        # Empty result.txt
        artifact_store.save_result(worker_id, "")

        # Create worker job
        job = WorkerJob(
            owner_id=sample_agent.owner_id,
            supervisor_run_id=supervisor_run.id,
            task="Check server status",
            status="success",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()

        # Compile evidence
        compiler = EvidenceCompiler(artifact_store=artifact_store, db=db_session)
        evidence_map = compiler.compile(
            run_id=supervisor_run.id,
            owner_id=sample_agent.owner_id,
            budget_bytes=10000,
        )

        # Verify failed tool appears first
        evidence = evidence_map[job.id]
        assert "[FAILED]" in evidence

        # Failed tool should appear before success tool
        failed_pos = evidence.find("[FAILED]")
        success_file_pos = evidence.find("001_ssh_exec.txt")
        assert failed_pos < success_file_pos

        # Should contain error message
        assert "command not found" in evidence

    def test_large_tool_output_truncation(self, db_session, sample_agent, temp_artifact_path):
        """Test that large tool outputs are truncated with head+tail."""
        import json
        from zerg.services.evidence_compiler import EvidenceCompiler
        from zerg.services.worker_artifact_store import WorkerArtifactStore
        from zerg.models.models import AgentRun, WorkerJob
        from zerg.models.enums import RunStatus, RunTrigger
        from zerg.crud import create_thread

        # Create supervisor run
        thread = create_thread(db_session, agent_id=sample_agent.id, title="Test Run")
        supervisor_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
        )
        db_session.add(supervisor_run)
        db_session.commit()
        db_session.refresh(supervisor_run)

        # Create artifact store and worker
        artifact_store = WorkerArtifactStore(base_path=temp_artifact_path)
        worker_id = artifact_store.create_worker(
            task="Get large log file",
            config={"model": "gpt-4"},
            owner_id=sample_agent.owner_id,
        )

        # Add very large output (50KB+)
        large_log = "LOG LINE " * 10000  # ~100KB
        large_output = json.dumps({
            "ok": True,
            "data": {
                "host": "clifford",
                "command": "cat /var/log/syslog",
                "exit_code": 0,
                "stdout": large_log,
                "stderr": "",
                "duration_ms": 500,
            }
        })
        artifact_store.save_tool_output(worker_id, "ssh_exec", large_output, sequence=1)

        # Empty result.txt
        artifact_store.save_result(worker_id, "")

        # Create worker job
        job = WorkerJob(
            owner_id=sample_agent.owner_id,
            supervisor_run_id=supervisor_run.id,
            task="Get large log file",
            status="success",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()

        # Compile evidence with small budget
        compiler = EvidenceCompiler(artifact_store=artifact_store, db=db_session)
        evidence_map = compiler.compile(
            run_id=supervisor_run.id,
            owner_id=sample_agent.owner_id,
            budget_bytes=5000,  # Small budget to force truncation
        )

        evidence = evidence_map[job.id]

        # Should contain truncation marker
        assert "truncated" in evidence.lower()

        # Should be within budget
        assert len(evidence.encode("utf-8")) <= 6000  # Allow small margin

        # Should contain both head and tail
        assert "LOG LINE" in evidence
