#!/usr/bin/env python3
"""
Test that usage_metadata works with LangChain streaming in OUR version.
This is the CORRECT approach per the investigation.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "zerg", "backend"))

from dotenv import load_dotenv
load_dotenv()

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool

@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def test_usage_metadata():
    """Test that usage_metadata contains reasoning tokens."""
    api_key = os.getenv("OPENAI_API_KEY")

    print("="*60)
    print("TEST: LangChain streaming with usage_metadata")
    print("="*60)

    llm = ChatOpenAI(
        model="gpt-5.1",
        api_key=api_key,
        streaming=True,
        reasoning_effort="high",
    )
    llm_with_tools = llm.bind_tools([add])

    print("\nInvoking with streaming=True...")
    result = llm_with_tools.invoke("What is 17 * 23? Calculate it.")

    print(f"\nResult content: {result.content[:50]}...")
    print(f"\nResponse metadata: {result.response_metadata}")

    # THE KEY: Check usage_metadata instead of response_metadata.token_usage
    usage_meta = getattr(result, "usage_metadata", None)
    print(f"\nusage_metadata: {usage_meta}")

    if usage_meta:
        reasoning = usage_meta.get("output_token_details", {}).get("reasoning", 0)
        print(f"\n✅ SUCCESS: Found {reasoning} reasoning tokens in usage_metadata")
        print(f"   input_tokens: {usage_meta.get('input_tokens')}")
        print(f"   output_tokens: {usage_meta.get('output_tokens')}")
        print(f"   total_tokens: {usage_meta.get('total_tokens')}")
        return True
    else:
        print(f"\n❌ FAIL: No usage_metadata")
        return False


if __name__ == "__main__":
    success = test_usage_metadata()

    print("\n" + "="*60)
    if success:
        print("✅ LangChain usage_metadata works - no need for raw AsyncOpenAI!")
        print("Just read from message.usage_metadata instead of llm_output")
    else:
        print("❌ usage_metadata not available in our version")
    print("="*60)
