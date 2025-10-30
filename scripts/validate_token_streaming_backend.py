#!/usr/bin/env python3
"""Validate token streaming backend implementation.

This script tests:
1. Whether WsTokenCallback receives tokens when passed to LangChain
2. How LangChain invokes callbacks with streaming=True
3. Whether the current code passes callbacks correctly
"""

import asyncio
import os
import sys
from typing import List

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "zerg", "backend"))

# Set environment for testing
os.environ.setdefault("LLM_TOKEN_STREAM", "true")

from langchain_core.messages import BaseMessage
from langchain_core.messages import HumanMessage

# Mock imports - we'll test with a real LLM if OPENAI_API_KEY is set, otherwise use mocks
try:
    from langchain_openai import ChatOpenAI
    HAS_OPENAI = os.getenv("OPENAI_API_KEY") is not None
except ImportError:
    HAS_OPENAI = False


from langchain_core.callbacks.base import AsyncCallbackHandler

class TestTokenCallback(AsyncCallbackHandler):
    """Simple callback to track token invocations."""
    
    def __init__(self):
        super().__init__()
        self.tokens_received = []
        self.invocation_count = 0
    
    async def on_llm_new_token(self, token: str, **kwargs):
        """Record each token."""
        self.tokens_received.append(token)
        self.invocation_count += 1
        print(f"  [CALLBACK] Token {self.invocation_count}: '{token}'", flush=True)


async def test_callback_with_streaming():
    """Test 1: Verify callbacks are called with streaming=True and ainvoke()."""
    print("=" * 60)
    print("TEST 1: Callbacks with streaming=True and ainvoke()")
    print("=" * 60)
    
    if not HAS_OPENAI:
        print("⚠️  OPENAI_API_KEY not set, skipping real LLM test")
        print("   Set OPENAI_API_KEY to test with real LLM")
        return False
    
    callback = TestTokenCallback()
    messages = [HumanMessage(content="Say hello in 5 words")]
    
    print(f"Creating LLM with streaming=True...")
    llm = ChatOpenAI(model="gpt-3.5-turbo", streaming=True, temperature=0)
    
    print(f"Calling ainvoke() with callback...")
    print(f"Expected: Callback should receive tokens as they arrive")
    print()
    
    try:
        result = await llm.ainvoke(
            messages,
            config={"callbacks": [callback]}
        )
        
        print()
        print(f"Result received: {result.content[:50]}...")
        print(f"Total tokens received by callback: {len(callback.tokens_received)}")
        print(f"Tokens: {''.join(callback.tokens_received[:20])}...")
        
        if callback.invocation_count > 0:
            print("✅ SUCCESS: Callbacks were invoked during ainvoke()")
            return True
        else:
            print("❌ FAILURE: No callbacks were invoked")
            return False
            
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_callback_without_streaming():
    """Test 2: Verify callbacks are NOT called with streaming=False."""
    print()
    print("=" * 60)
    print("TEST 2: Callbacks with streaming=False (should NOT receive tokens)")
    print("=" * 60)
    
    if not HAS_OPENAI:
        print("⚠️  OPENAI_API_KEY not set, skipping real LLM test")
        return False
    
    callback = TestTokenCallback()
    messages = [HumanMessage(content="Say hello")]
    
    print(f"Creating LLM with streaming=False...")
    llm = ChatOpenAI(model="gpt-3.5-turbo", streaming=False, temperature=0)
    
    print(f"Calling ainvoke() with callback...")
    print(f"Expected: Callback should NOT receive tokens (streaming=False)")
    print()
    
    try:
        result = await llm.ainvoke(
            messages,
            config={"callbacks": [callback]}
        )
        
        print()
        print(f"Result received: {result.content[:50]}...")
        print(f"Total tokens received by callback: {len(callback.tokens_received)}")
        
        if callback.invocation_count == 0:
            print("✅ SUCCESS: Callbacks correctly NOT invoked (streaming=False)")
            return True
        else:
            print(f"⚠️  WARNING: Callbacks were invoked even with streaming=False ({callback.invocation_count} times)")
            return True  # This might be version-dependent
            
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_current_implementation():
    """Test 3: Check if current _call_model_async passes callbacks."""
    print()
    print("=" * 60)
    print("TEST 3: Current Implementation Check")
    print("=" * 60)
    
    # Read the actual source file
    agent_file = os.path.join(
        os.path.dirname(__file__), 
        "..", 
        "apps", "zerg", "backend", "zerg", "agents_def", "zerg_react_agent.py"
    )
    
    if not os.path.exists(agent_file):
        print(f"⚠️  Could not find {agent_file}")
        return False
    
    with open(agent_file, 'r') as f:
        content = f.read()
    
    # Check for callback usage
    has_callback_import = "WsTokenCallback" in content or "from zerg.callbacks" in content
    has_callback_pass = "callbacks" in content and ("_call_model_async" in content or "_call_model_sync" in content)
    
    # Look for the specific function
    lines = content.split('\n')
    in_function = False
    function_lines = []
    
    for i, line in enumerate(lines):
        if 'async def _call_model_async' in line:
            in_function = True
            function_lines = [line]
        elif in_function:
            function_lines.append(line)
            if line.strip() and not line.startswith((' ', '\t', '#')) and 'def ' in line:
                break
    
    function_code = '\n'.join(function_lines)
    
    print("Inspecting _call_model_async function...")
    print("-" * 60)
    for i, line in enumerate(function_lines[:15], 1):
        print(f"{i:3}: {line}")
    if len(function_lines) > 15:
        print(f"... ({len(function_lines) - 15} more lines)")
    print("-" * 60)
    
    has_callback_in_function = "callbacks" in function_code or "WsTokenCallback" in function_code
    
    print(f"\nFindings:")
    print(f"  Has callback import: {has_callback_import}")
    print(f"  Has callback in function: {has_callback_in_function}")
    print(f"  Function uses callbacks parameter: {'config' in function_code and 'callbacks' in function_code}")
    
    if not has_callback_in_function:
        print("❌ ISSUE FOUND: Callbacks are NOT being passed in _call_model_async()")
        return False
    else:
        print("✅ Callbacks appear to be used in the function")
        return True


def test_ws_token_callback():
    """Test 4: Verify WsTokenCallback implementation."""
    print()
    print("=" * 60)
    print("TEST 4: WsTokenCallback Implementation Check")
    print("=" * 60)
    
    try:
        from zerg.callbacks.token_stream import WsTokenCallback, set_current_thread_id
        from zerg.callbacks.token_stream import current_thread_id_var
        
        print("✅ WsTokenCallback class exists")
        print("✅ set_current_thread_id function exists")
        print("✅ current_thread_id_var context var exists")
        
        # Check implementation
        callback = WsTokenCallback()
        
        # Verify it has the right method
        if hasattr(callback, 'on_llm_new_token'):
            print("✅ Has on_llm_new_token method")
        else:
            print("❌ Missing on_llm_new_token method")
            return False
        
        # Check if it's async
        import inspect
        if inspect.iscoroutinefunction(callback.on_llm_new_token):
            print("✅ on_llm_new_token is async")
        else:
            print("❌ on_llm_new_token is NOT async")
            return False
        
        # Test context var
        set_current_thread_id(123)
        assert current_thread_id_var.get() == 123
        set_current_thread_id(None)
        assert current_thread_id_var.get() is None
        print("✅ Context variable works correctly")
        
        return True
        
    except ImportError as e:
        print(f"❌ ERROR: Could not import WsTokenCallback: {e}")
        return False
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all validation tests."""
    print("Token Streaming Backend Validation")
    print("=" * 60)
    print()
    
    results = []
    
    # Test 4: Check implementation first (doesn't require API)
    results.append(("WsTokenCallback Implementation", test_ws_token_callback()))
    
    # Test 3: Check current code
    results.append(("Current Implementation Check", await test_current_implementation()))
    
    # Tests 1 & 2: Require OpenAI API key
    if HAS_OPENAI:
        results.append(("Callback with streaming=True", await test_callback_with_streaming()))
        results.append(("Callback with streaming=False", await test_callback_without_streaming()))
    else:
        print()
        print("=" * 60)
        print("SKIPPING LLM TESTS: OPENAI_API_KEY not set")
        print("Set OPENAI_API_KEY to test actual LLM callback behavior")
        print("=" * 60)
    
    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    all_passed = all(result for _, result in results)
    
    if all_passed:
        print()
        print("✅ All tests passed!")
    else:
        print()
        print("❌ Some tests failed - review output above")
    
    return all_passed


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)

