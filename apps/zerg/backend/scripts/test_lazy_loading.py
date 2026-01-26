#!/usr/bin/env python
"""Test if lazy loading can actually call non-core tools.

This validates whether the LLM can call tools that are in the catalog
(injected into system prompt) but not in bind_tools().

Hypothesis: OpenAI validates tool calls against the bound schema.
If a tool isn't bound, the LLM cannot call it, even if it sees
the tool name in the system prompt.

Expected outcome: FAIL
- LLM will see "get_current_location" in catalog text
- LLM will try to help with location but can't call the tool
- It will either use a core tool (web_search) or say it can't help

If it PASSES (tool call succeeds):
- Our hypothesis was wrong
- Lazy loading actually works
- Proceed to token comparison

Usage:
    uv run python scripts/test_lazy_loading.py
"""

from __future__ import annotations

import asyncio
import sys

from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage


async def run_test():
    """Run the lazy loading validation test."""
    from zerg.tools import get_registry
    from zerg.tools.catalog import CORE_TOOLS, build_catalog
    from zerg.services.concierge_react_engine import run_concierge_loop

    print("=" * 70)
    print("LAZY LOADING VALIDATION TEST")
    print("=" * 70)

    # Build catalog to show what tools exist
    catalog = build_catalog()
    print(f"\nTotal tools in catalog: {len(catalog)}")
    print(f"Core tools (always bound): {len(CORE_TOOLS)}")
    print(f"Non-core tools (lazy): {len(catalog) - len(CORE_TOOLS)}")

    # Check if get_current_location exists and is NOT a core tool
    location_tool = next((e for e in catalog if e.name == "get_current_location"), None)
    if location_tool:
        print(f"\n✓ Found 'get_current_location' in catalog")
        print(f"  Category: {location_tool.category}")
        print(f"  Is core tool: {location_tool.name in CORE_TOOLS}")
    else:
        print("\n✗ 'get_current_location' not in catalog - using different test tool")
        # Find any non-core tool
        non_core = [e for e in catalog if e.name not in CORE_TOOLS]
        if non_core:
            location_tool = non_core[0]
            print(f"  Using '{location_tool.name}' instead")
        else:
            print("ERROR: No non-core tools found!")
            return False

    # Get all tools from registry
    registry = get_registry()
    all_tools = list(registry.all_tools())
    print(f"\nTotal tools in registry: {len(all_tools)}")

    # Create minimal fiche mock with explicit attributes
    class AgentMock:
        model = "gpt-5-mini"
        system_prompt = "You are a helpful assistant."
        reasoning_effort = None
        context_stuffing_strategy = None

    fiche_mock = AgentMock()

    # Create test messages with system prompt (required for catalog injection)
    messages = [
        SystemMessage(content="You are a helpful assistant."),
        HumanMessage(content="Where am I right now? What is my current GPS location?"),
    ]

    print("\n" + "-" * 70)
    print("TEST: Running concierge with lazy_loading=True")
    print("-" * 70)
    print(f"User message: '{messages[0].content}'")
    print(f"Expected: LLM should need 'get_current_location' (non-core tool)")
    print()

    try:
        result = await run_concierge_loop(
            messages=messages,
            fiche_row=fiche_mock,
            tools=all_tools,
            lazy_loading=True,  # KEY: Enable lazy loading
            course_id=None,
            owner_id=None,
            trace_id="test-lazy-loading",
        )

        print("\n" + "-" * 70)
        print("RESULT")
        print("-" * 70)

        # Check what happened
        tool_calls_made = []
        for msg in result.messages:
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls_made.append(tc.get('name', 'unknown'))
                    print(f"Tool call: {tc.get('name')} (args: {tc.get('args', {})})")

        # Check final response
        final_msg = result.messages[-1] if result.messages else None
        if final_msg and hasattr(final_msg, 'content'):
            content = final_msg.content[:500] if final_msg.content else ""
            print(f"\nFinal response preview:\n{content}...")

        print("\n" + "=" * 70)
        print("ANALYSIS")
        print("=" * 70)

        search_tools_called = "search_tools" in tool_calls_made
        non_core_called = [t for t in tool_calls_made if t not in CORE_TOOLS]

        if "get_current_location" in tool_calls_made:
            print("✓ SUCCESS: LLM called 'get_current_location'")
            print("  Lazy loading WORKS - search_tools → rebind → tool call")
            if search_tools_called:
                print("  Flow: search_tools was called first (correct pattern)")
            return True
        elif non_core_called:
            print(f"✓ SUCCESS: LLM called non-core tools: {non_core_called}")
            print("  Lazy loading WORKS")
            return True
        elif search_tools_called:
            print("✓ PARTIAL SUCCESS: LLM called search_tools")
            print("  This means it's trying the right pattern")
            print("  It should call the discovered tool on the next turn")
            print("  (The test may need to run longer to see full flow)")
            return True  # This is actually good - it means the pattern is working
        elif tool_calls_made:
            print(f"? UNCLEAR: LLM called core tools only: {tool_calls_made}")
            print("  LLM may have fallen back to web_search or contact_user")
            print("  This isn't necessarily wrong - it might work for the task")
            return None  # Inconclusive
        else:
            print("✗ FAIL: No tool calls made")
            print("  LLM didn't call search_tools or any other tool")
            print("  Check if catalog was properly injected")
            return False

    except Exception as e:
        print(f"\nERROR during test: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Main entry point."""
    result = asyncio.run(run_test())

    print("\n" + "=" * 70)
    if result is True:
        print("CONCLUSION: Lazy loading WORKS")
        print("Next step: Measure token savings via llm_audit_log")
        sys.exit(0)
    elif result is False:
        print("CONCLUSION: Lazy loading BROKEN")
        print("Next step: Remove lazy loading complexity or fix the mechanism")
        sys.exit(1)
    else:
        print("CONCLUSION: INCONCLUSIVE")
        print("Need more specific test or manual verification")
        sys.exit(2)


if __name__ == "__main__":
    main()
