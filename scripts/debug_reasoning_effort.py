#!/usr/bin/env python3
"""Quick debug script to verify reasoning_effort parameter works."""

import os
import sys

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "zerg", "backend"))

from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv()

def test_reasoning_effort():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set")
        return

    # Test with the model from config
    model = "gpt-5.1"  # What's in config/models.json

    print(f"Testing model: {model}")
    print("-" * 50)

    for effort in ["none", "low", "medium", "high"]:
        print(f"\n=== Testing reasoning_effort={effort} ===")

        kwargs = {
            "model": model,
            "api_key": api_key,
        }

        if effort != "none":
            kwargs["reasoning_effort"] = effort

        try:
            llm = ChatOpenAI(**kwargs)
            result = llm.invoke("What is 2+2? Answer in one word.")

            # Check response metadata for token usage
            meta = getattr(result, "response_metadata", {})
            usage = meta.get("token_usage") or meta.get("usage") or {}

            print(f"Response: {result.content}")
            print(f"Model used: {meta.get('model_name', 'unknown')}")
            print(f"Token usage: {usage}")

            # Check for reasoning tokens
            details = usage.get("completion_tokens_details", {})
            reasoning_tokens = details.get("reasoning_tokens", 0)
            print(f"Reasoning tokens: {reasoning_tokens}")

        except Exception as e:
            print(f"ERROR: {e}")

if __name__ == "__main__":
    test_reasoning_effort()
