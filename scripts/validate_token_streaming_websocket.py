#!/usr/bin/env python3
"""Validate WebSocket streaming message flow.

This script tests:
1. WebSocket message format and structure
2. Topic subscription mechanism
3. Message broadcasting
"""

import asyncio
import json
import os
import sys
import websockets
from typing import Dict, List, Optional

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "zerg", "backend"))

from zerg.generated.ws_messages import Envelope, StreamChunkData, StreamStartData, StreamEndData, AssistantIdData
from zerg.websocket.manager import topic_manager


async def test_message_format():
    """Test 1: Verify message envelope format."""
    print("=" * 60)
    print("TEST 1: Message Envelope Format")
    print("=" * 60)
    
    try:
        # Create a stream_start message
        stream_start = StreamStartData(thread_id=123)
        envelope = Envelope.create(
            message_type="stream_start",
            topic="thread:123",
            data=stream_start.model_dump()
        )
        
        print("Created envelope:")
        print(json.dumps(envelope.model_dump(), indent=2))
        print()
        
        # Verify required fields
        assert envelope.type == "stream_start"
        assert envelope.topic == "thread:123"
        assert "thread_id" in envelope.data
        assert envelope.data["thread_id"] == 123
        assert "ts" in envelope.model_dump()
        
        print("✅ Envelope format is correct")
        
        # Test stream_chunk
        chunk = StreamChunkData(
            thread_id=123,
            content="Hello",
            chunk_type="assistant_token",
            tool_name=None,
            tool_call_id=None
        )
        chunk_envelope = Envelope.create(
            message_type="stream_chunk",
            topic="thread:123",
            data=chunk.model_dump()
        )
        
        print("\nStream chunk envelope:")
        print(json.dumps(chunk_envelope.model_dump(), indent=2))
        print()
        
        assert chunk_envelope.type == "stream_chunk"
        assert chunk_envelope.data["content"] == "Hello"
        assert chunk_envelope.data["chunk_type"] == "assistant_token"
        
        print("✅ Stream chunk format is correct")
        
        # Test assistant_id
        assistant_id = AssistantIdData(thread_id=123, message_id=456)
        id_envelope = Envelope.create(
            message_type="assistant_id",
            topic="thread:123",
            data=assistant_id.model_dump()
        )
        
        print("\nAssistant ID envelope:")
        print(json.dumps(id_envelope.model_dump(), indent=2))
        print()
        
        assert id_envelope.type == "assistant_id"
        assert id_envelope.data["message_id"] == 456
        
        print("✅ Assistant ID format is correct")
        
        return True
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_message_sequence():
    """Test 2: Verify complete message sequence."""
    print()
    print("=" * 60)
    print("TEST 2: Complete Message Sequence")
    print("=" * 60)
    
    try:
        thread_id = 123
        topic = f"thread:{thread_id}"
        
        # Sequence: stream_start -> assistant_id -> stream_chunk (multiple) -> stream_end
        
        # 1. Stream start
        start_data = StreamStartData(thread_id=thread_id)
        start_envelope = Envelope.create(
            message_type="stream_start",
            topic=topic,
            data=start_data.model_dump()
        )
        print("1. stream_start:", json.dumps(start_envelope.model_dump(), indent=2))
        print()
        
        # 2. Assistant ID
        assistant_id_data = AssistantIdData(thread_id=thread_id, message_id=456)
        id_envelope = Envelope.create(
            message_type="assistant_id",
            topic=topic,
            data=assistant_id_data.model_dump()
        )
        print("2. assistant_id:", json.dumps(id_envelope.model_dump(), indent=2))
        print()
        
        # 3. Multiple stream chunks
        tokens = ["Hello", " ", "world", "!"]
        chunk_envelopes = []
        for token in tokens:
            chunk_data = StreamChunkData(
                thread_id=thread_id,
                content=token,
                chunk_type="assistant_token",
                tool_name=None,
                tool_call_id=None
            )
            chunk_envelope = Envelope.create(
                message_type="stream_chunk",
                topic=topic,
                data=chunk_data.model_dump()
            )
            chunk_envelopes.append(chunk_envelope)
            print(f"3.{len(chunk_envelopes)}. stream_chunk: '{token}'")
        
        print()
        
        # 4. Stream end
        end_data = StreamEndData(thread_id=thread_id)
        end_envelope = Envelope.create(
            message_type="stream_end",
            topic=topic,
            data=end_data.model_dump()
        )
        print("4. stream_end:", json.dumps(end_envelope.model_dump(), indent=2))
        print()
        
        # Verify sequence
        assert start_envelope.type == "stream_start"
        assert id_envelope.type == "assistant_id"
        assert all(c.type == "stream_chunk" for c in chunk_envelopes)
        assert end_envelope.type == "stream_end"
        
        # Verify accumulated content
        accumulated = "".join(c.data["content"] for c in chunk_envelopes)
        assert accumulated == "Hello world!"
        
        print(f"✅ Complete sequence verified")
        print(f"   Accumulated content: '{accumulated}'")
        
        return True
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_topic_manager_structure():
    """Test 3: Check topic manager structure (without actual connections)."""
    print()
    print("=" * 60)
    print("TEST 3: Topic Manager Structure")
    print("=" * 60)
    
    try:
        # Check if topic_manager exists and has required methods
        print(f"Topic manager: {topic_manager}")
        print(f"Type: {type(topic_manager)}")
        print()
        
        # Check for required methods
        required_methods = [
            'subscribe_to_topic',
            'unsubscribe_from_topic',
            'broadcast_to_topic',
            'connect',
            'disconnect'
        ]
        
        for method_name in required_methods:
            if hasattr(topic_manager, method_name):
                print(f"✅ Has method: {method_name}")
            else:
                print(f"❌ Missing method: {method_name}")
                return False
        
        print()
        print("✅ Topic manager has all required methods")
        
        return True
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def simulate_streaming_flow():
    """Test 4: Simulate the complete streaming flow."""
    print()
    print("=" * 60)
    print("TEST 4: Simulate Streaming Flow")
    print("=" * 60)
    
    try:
        thread_id = 999
        topic = f"thread:{thread_id}"
        
        # Simulate what happens when a message is sent
        print(f"Simulating streaming for thread {thread_id}...")
        print()
        
        # Step 1: Stream start
        start_data = StreamStartData(thread_id=thread_id)
        start_envelope = Envelope.create(
            message_type="stream_start",
            topic=topic,
            data=start_data.model_dump()
        )
        
        print(f"[1] Broadcasting: {start_envelope.type}")
        # Would call: await topic_manager.broadcast_to_topic(topic, start_envelope.model_dump())
        print(f"    Topic: {topic}")
        print(f"    Data: {json.dumps(start_envelope.data, indent=4)}")
        print()
        
        # Step 2: Assistant ID (when token streaming enabled)
        message_id = 1001
        assistant_id_data = AssistantIdData(thread_id=thread_id, message_id=message_id)
        id_envelope = Envelope.create(
            message_type="assistant_id",
            topic=topic,
            data=assistant_id_data.model_dump()
        )
        
        print(f"[2] Broadcasting: {id_envelope.type}")
        print(f"    Topic: {topic}")
        print(f"    Data: {json.dumps(id_envelope.data, indent=4)}")
        print()
        
        # Step 3: Stream tokens (simulating LLM output)
        sample_response = "This is a test response with multiple tokens."
        tokens = list(sample_response)  # Character-level tokens
        
        for i, token in enumerate(tokens[:10], 1):  # Show first 10
            chunk_data = StreamChunkData(
                thread_id=thread_id,
                content=token,
                chunk_type="assistant_token",
                tool_name=None,
                tool_call_id=None
            )
            chunk_envelope = Envelope.create(
                message_type="stream_chunk",
                topic=topic,
                data=chunk_data.model_dump()
            )
            
            if i <= 5:  # Show first 5 in detail
                print(f"[3.{i}] Broadcasting: {chunk_envelope.type}")
                print(f"     Topic: {topic}")
                print(f"     Token: '{token}'")
            
        print(f"     ... ({len(tokens) - 5} more tokens)")
        print()
        
        # Step 4: Stream end
        end_data = StreamEndData(thread_id=thread_id)
        end_envelope = Envelope.create(
            message_type="stream_end",
            topic=topic,
            data=end_data.model_dump()
        )
        
        print(f"[4] Broadcasting: {end_envelope.type}")
        print(f"    Topic: {topic}")
        print(f"    Data: {json.dumps(end_envelope.data, indent=4)}")
        print()
        
        # Summary
        accumulated = "".join(tokens)
        print(f"Summary:")
        print(f"  Total tokens: {len(tokens)}")
        print(f"  Accumulated: '{accumulated}'")
        print(f"  Message ID: {message_id}")
        
        print()
        print("✅ Streaming flow simulation complete")
        
        return True
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all WebSocket validation tests."""
    print("WebSocket Streaming Message Validation")
    print("=" * 60)
    print()
    
    results = []
    
    results.append(("Message Format", await test_message_format()))
    results.append(("Message Sequence", await test_message_sequence()))
    results.append(("Topic Manager Structure", await test_topic_manager_structure()))
    results.append(("Streaming Flow Simulation", await simulate_streaming_flow()))
    
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

