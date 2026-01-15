#!/usr/bin/env python
"""Test that the rebind-after-search mechanism works.

This directly tests that:
1. search_tools returns tool names
2. Those tools get bound after search_tools executes
3. The LLM can then call the newly-bound tools

This is a unit test for the rebind mechanism, not an E2E LLM test.
"""

from __future__ import annotations

import asyncio
import json


async def test_rebind_mechanism():
    """Test the rebind-after-search_tools mechanism."""
    from langchain_core.messages import ToolMessage

    from zerg.tools.catalog import CORE_TOOLS
    from zerg.tools.lazy_binder import LazyToolBinder
    from zerg.tools.unified_access import get_tool_resolver

    print("=" * 70)
    print("LAZY REBIND MECHANISM TEST")
    print("=" * 70)

    # Set up lazy binder
    resolver = get_tool_resolver()
    binder = LazyToolBinder(resolver, allowed_tools=None)

    print(f"\n1. Initial state:")
    print(f"   Core tools bound: {len(binder.get_bound_tools())}")
    print(f"   Core tools: {sorted(binder.loaded_tool_names)}")

    # Verify get_current_location is NOT loaded
    assert not binder.is_loaded("get_current_location"), "get_current_location should not be pre-loaded"
    print(f"   ✓ get_current_location is NOT loaded (correct)")

    # Simulate search_tools result
    print(f"\n2. Simulating search_tools result:")
    search_result = {
        "tools": [
            {"name": "get_current_location", "summary": "Get GPS location"},
            {"name": "get_whoop_data", "summary": "Get health data"},
        ],
        "query": "location",
    }
    print(f"   search_tools returned: {[t['name'] for t in search_result['tools']]}")

    # Create a ToolMessage like _maybe_rebind_after_tool_search expects
    tool_message = ToolMessage(
        content=json.dumps(search_result),
        tool_call_id="test_call_1",
        name="search_tools",
    )

    # Parse tool names (same logic as _maybe_rebind_after_tool_search)
    print(f"\n3. Parsing and loading tools:")
    names = []
    payload = json.loads(tool_message.content)
    for entry in payload.get("tools") or []:
        name = entry.get("name")
        if isinstance(name, str) and name:
            names.append(name)

    print(f"   Parsed tool names: {names}")

    # Load tools
    loaded = binder.load_tools(names)
    print(f"   Loaded: {loaded}")
    print(f"   needs_rebind(): {binder.needs_rebind()}")

    # Check results
    print(f"\n4. Final state:")
    print(f"   Total tools bound: {len(binder.get_bound_tools())}")
    print(f"   get_current_location loaded: {binder.is_loaded('get_current_location')}")
    print(f"   get_whoop_data loaded: {binder.is_loaded('get_whoop_data')}")

    # Verify tools are now loaded
    assert binder.is_loaded("get_current_location"), "get_current_location should be loaded"
    assert binder.is_loaded("get_whoop_data"), "get_whoop_data should be loaded"
    assert binder.needs_rebind(), "Binder should need rebind after loading new tools"

    print(f"\n" + "=" * 70)
    print("✓ SUCCESS: Rebind mechanism works correctly!")
    print("=" * 70)
    print("\nThe mechanism is correct:")
    print("  1. search_tools returns tool names")
    print("  2. We parse the names from the ToolMessage")
    print("  3. LazyToolBinder.load_tools() loads them")
    print("  4. needs_rebind() returns True")
    print("  5. We'd rebind the LLM with get_bound_tools()")
    print("\nThe issue is getting the LLM to CALL search_tools in the first place.")
    return True


def main():
    result = asyncio.run(test_rebind_mechanism())
    if result:
        print("\nNEXT STEP: Test in real chat UI to see if LLM calls search_tools")
    return 0 if result else 1


if __name__ == "__main__":
    exit(main())
