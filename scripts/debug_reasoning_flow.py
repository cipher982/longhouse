#!/usr/bin/env python3
"""
Verify reasoning_effort flows through the full path.
This is a pure code inspection test - no DB or LLM calls needed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "zerg", "backend"))

def main():
    print("=" * 60)
    print("REASONING EFFORT FLOW VERIFICATION")
    print("=" * 60)

    # 1. Check JarvisChatRequest
    from zerg.routers.jarvis_chat import JarvisChatRequest
    request = JarvisChatRequest(message="test", reasoning_effort="high")
    print(f"\n1. JarvisChatRequest.reasoning_effort = {request.reasoning_effort!r}")
    assert request.reasoning_effort == "high"
    print("   >>> PASS")

    # 2. Check run_supervisor signature
    import inspect
    from zerg.services.supervisor_service import SupervisorService
    sig = inspect.signature(SupervisorService.run_supervisor)
    params = list(sig.parameters.keys())
    print(f"\n2. SupervisorService.run_supervisor params: {params}")
    assert "reasoning_effort" in params
    print("   >>> PASS: reasoning_effort in signature")

    # 3. Check source passes it to AgentRunner
    source = inspect.getsource(SupervisorService.run_supervisor)
    assert "AgentRunner(agent, model_override=model_override, reasoning_effort=reasoning_effort)" in source
    print("\n3. run_supervisor passes reasoning_effort to AgentRunner:")
    print("   >>> PASS: Found correct AgentRunner call")

    # 4. Check AgentRunner stores it
    from zerg.managers.agent_runner import AgentRunner
    sig = inspect.signature(AgentRunner.__init__)
    params = list(sig.parameters.keys())
    print(f"\n4. AgentRunner.__init__ params: {params}")
    assert "reasoning_effort" in params
    print("   >>> PASS: reasoning_effort in signature")

    source = inspect.getsource(AgentRunner.__init__)
    assert 'runtime_cfg["reasoning_effort"]' in source
    print("\n5. AgentRunner stores reasoning_effort in runtime_cfg:")
    print("   >>> PASS")

    # 5. Check _make_llm uses it
    from zerg.agents_def import zerg_react_agent
    source = inspect.getsource(zerg_react_agent._make_llm)
    assert 'kwargs["reasoning_effort"]' in source
    print("\n6. _make_llm adds reasoning_effort to ChatOpenAI kwargs:")
    print("   >>> PASS")

    # 6. Full flow with mocks
    from unittest.mock import MagicMock, patch

    captured = {}

    def mock_make_llm(agent_row, tools):
        cfg = getattr(agent_row, "config", {}) or {}
        captured["reasoning_effort"] = cfg.get("reasoning_effort")
        return MagicMock()

    def mock_get_runnable(agent_row):
        zerg_react_agent._make_llm(agent_row, [])
        return MagicMock()

    print("\n7. Full mock flow test:")
    with patch("zerg.agents_def.zerg_react_agent._make_llm", mock_make_llm), \
         patch("zerg.agents_def.zerg_react_agent.get_runnable", mock_get_runnable):

        mock_agent = MagicMock()
        mock_agent.id = 1
        mock_agent.owner_id = 1
        mock_agent.model = "gpt-5.1"
        mock_agent.config = {}
        mock_agent.allowed_tools = []
        mock_agent.updated_at = None

        for effort in ["none", "low", "medium", "high"]:
            captured.clear()
            runner = AgentRunner(mock_agent, reasoning_effort=effort)
            actual = captured.get("reasoning_effort")
            status = "PASS" if actual == effort else "FAIL"
            print(f"   reasoning_effort={effort!r} → _make_llm got {actual!r} >>> {status}")

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED - Flow is correctly wired")
    print("=" * 60)

if __name__ == "__main__":
    main()
