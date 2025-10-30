#!/usr/bin/env python3
"""Inspect current implementation to verify findings.

This script directly inspects the code to verify:
1. Whether callbacks are passed to LLM
2. How _call_model_async is implemented
3. Whether enable_token_stream is used
"""

import os
import sys
import re


def inspect_agent_file():
    """Inspect zerg_react_agent.py for callback usage."""
    print("=" * 60)
    print("Inspecting: apps/zerg/backend/zerg/agents_def/zerg_react_agent.py")
    print("=" * 60)
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    agent_file = os.path.join(
        project_root,
        "apps", "zerg", "backend", "zerg", "agents_def", "zerg_react_agent.py"
    )
    
    if not os.path.exists(agent_file):
        print(f"❌ File not found: {agent_file}")
        return False
    
    with open(agent_file, 'r') as f:
        content = f.read()
    
    lines = content.split('\n')
    
    # Find _call_model_async
    print("\n1. Finding _call_model_async function:")
    print("-" * 60)
    
    in_function = False
    function_start = None
    function_lines = []
    
    for i, line in enumerate(lines, 1):
        if 'async def _call_model_async' in line:
            function_start = i
            in_function = True
            function_lines = [line]
        elif in_function:
            if line.strip() and not line.startswith((' ', '\t', '#', '"""', "'''")) and 'def ' in line:
                break
            function_lines.append(line)
    
    if function_start:
        print(f"   Found at line {function_start}")
        print(f"   Total lines: {len(function_lines)}")
        print()
        print("   Function body:")
        for i, line in enumerate(function_lines[:15], function_start):
            marker = ">>> " if "callbacks" in line or "WsTokenCallback" in line else "    "
            print(f"{marker}{i:4}: {line}")
        if len(function_lines) > 15:
            print(f"    ... ({len(function_lines) - 15} more lines)")
    else:
        print("   ❌ Function not found!")
        return False
    
    # Check for callback usage
    print("\n2. Checking for callback usage:")
    print("-" * 60)
    
    has_import = "from zerg.callbacks" in content or "import.*WsTokenCallback" in content
    has_callback_in_function = "WsTokenCallback" in '\n'.join(function_lines)
    has_config_callbacks = "config" in '\n'.join(function_lines) and "callbacks" in '\n'.join(function_lines)
    
    print(f"   Has callback import: {has_import}")
    print(f"   Uses WsTokenCallback in function: {has_callback_in_function}")
    print(f"   Passes callbacks via config: {has_config_callbacks}")
    
    if not has_callback_in_function:
        print("\n   ❌ ISSUE: Callbacks are NOT passed to LLM!")
        print("   Expected: WsTokenCallback() instance passed via config")
        return False
    
    # Check _call_model_sync
    print("\n3. Finding _call_model_sync function:")
    print("-" * 60)
    
    sync_function_lines = []
    for i, line in enumerate(lines, 1):
        if 'def _call_model_sync' in line:
            in_sync = True
            sync_function_lines = [line]
        elif in_sync:
            if line.strip() and not line.startswith((' ', '\t', '#', '"""', "'''")) and 'def ' in line:
                break
            sync_function_lines.append(line)
    
    if sync_function_lines:
        print(f"   Found _call_model_sync ({len(sync_function_lines)} lines)")
        has_sync_callbacks = "callbacks" in '\n'.join(sync_function_lines) or "WsTokenCallback" in '\n'.join(sync_function_lines)
        print(f"   Uses callbacks: {has_sync_callbacks}")
        if has_sync_callbacks:
            for line in sync_function_lines:
                if "callbacks" in line or "WsTokenCallback" in line:
                    print(f"   >>> {line}")
    else:
        print("   (Function not found or empty)")
    
    # Check enable_token_stream usage
    print("\n4. Checking enable_token_stream usage:")
    print("-" * 60)
    
    enable_token_checks = [
        ("_make_llm function", "_make_llm" in content and "enable_token_stream" in content),
        ("_call_model_async", "enable_token_stream" in '\n'.join(function_lines)),
        ("Agent executor", "enable_token_stream" in content),
    ]
    
    for check_name, found in enable_token_checks:
        status = "✅" if found else "❌"
        print(f"   {status} {check_name}: {found}")
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    all_ok = has_callback_in_function and has_config_callbacks
    
    if all_ok:
        print("✅ Callbacks are properly implemented")
    else:
        print("❌ Callbacks are NOT being passed to LLM")
        print("\nRequired changes:")
        print("  1. Import WsTokenCallback in _call_model_async")
        print("  2. Create callback instance when enable_token_stream is True")
        print("  3. Pass via config={'callbacks': [callback]} to ainvoke()")
    
    return all_ok


def inspect_agent_runner():
    """Inspect AgentRunner for context management."""
    print("\n" + "=" * 60)
    print("Inspecting: apps/zerg/backend/zerg/managers/agent_runner.py")
    print("=" * 60)
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    runner_file = os.path.join(
        project_root,
        "apps", "zerg", "backend", "zerg", "managers", "agent_runner.py"
    )
    
    if not os.path.exists(runner_file):
        print(f"❌ File not found: {runner_file}")
        return False
    
    with open(runner_file, 'r') as f:
        content = f.read()
    
    # Check for context management
    has_set_context = "set_current_thread_id" in content
    has_context_reset = "set_current_thread_id(None)" in content or "set_current_thread_id(None)" in content
    
    print("\n1. Context variable management:")
    print("-" * 60)
    print(f"   Sets thread context: {has_set_context}")
    print(f"   Resets context: {has_context_reset}")
    
    # Find the usage
    lines = content.split('\n')
    context_lines = []
    for i, line in enumerate(lines, 1):
        if "set_current_thread_id" in line:
            context_lines.append((i, line.strip()))
    
    if context_lines:
        print(f"\n   Found {len(context_lines)} usage(s):")
        for line_num, line in context_lines:
            print(f"      {line_num:4}: {line}")
    
    print("\n" + "=" * 60)
    if has_set_context and has_context_reset:
        print("✅ Context management looks correct")
        return True
    else:
        print("⚠️  Context management may have issues")
        return False


def main():
    """Run inspection."""
    print("Token Streaming Implementation Inspection")
    print("=" * 60)
    print()
    
    agent_ok = inspect_agent_file()
    runner_ok = inspect_agent_runner()
    
    print("\n" + "=" * 60)
    print("FINAL RESULT")
    print("=" * 60)
    
    if agent_ok and runner_ok:
        print("✅ Implementation is correct")
        return 0
    elif not agent_ok:
        print("❌ CRITICAL: Callbacks not being passed to LLM")
        print("   This is the main blocker for token streaming")
        return 1
    else:
        print("⚠️  Some issues found")
        return 1


if __name__ == "__main__":
    sys.exit(main())

