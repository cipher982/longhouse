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

    # 1. Check OikosChatRequest
    from zerg.routers.oikos_chat import OikosChatRequest
    request = OikosChatRequest(message="test", reasoning_effort="high")
    print(f"\n1. OikosChatRequest.reasoning_effort = {request.reasoning_effort!r}")
    assert request.reasoning_effort == "high"
    print("   >>> PASS")

    # 2. Check run_oikos signature
    import inspect
    from zerg.services.oikos_service import OikosService
    sig = inspect.signature(OikosService.run_oikos)
    params = list(sig.parameters.keys())
    print(f"\n2. OikosService.run_oikos params: {params}")
    assert "reasoning_effort" in params
    print("   >>> PASS: reasoning_effort in signature")

    # 3. Check source passes it to Runner
    source = inspect.getsource(OikosService.run_oikos)
    assert "Runner(" in source and "reasoning_effort=reasoning_effort" in source
    print("\n3. run_oikos passes reasoning_effort to Runner:")
    print("   >>> PASS: Found Runner call with reasoning_effort")

    # 4. Check Runner stores it
    from zerg.managers.fiche_runner import Runner
    sig = inspect.signature(Runner.__init__)
    params = list(sig.parameters.keys())
    print(f"\n4. Runner.__init__ params: {params}")
    assert "reasoning_effort" in params
    print("   >>> PASS: reasoning_effort in signature")

    source = inspect.getsource(Runner.__init__)
    assert 'runtime_cfg["reasoning_effort"]' in source
    print("\n5. Runner stores reasoning_effort in runtime_cfg:")
    print("   >>> PASS")

    # 5. Check _make_llm uses it
    from zerg.services import oikos_react_engine
    source = inspect.getsource(oikos_react_engine._make_llm)
    assert 'kwargs["reasoning_effort"]' in source
    print("\n6. _make_llm adds reasoning_effort to chat kwargs:")
    print("   >>> PASS")

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED - Flow is correctly wired")
    print("=" * 60)

if __name__ == "__main__":
    main()
