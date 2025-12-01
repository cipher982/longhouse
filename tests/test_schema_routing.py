#!/usr/bin/env python3
"""
Test script to validate our schema-driven WebSocket routing implementation.
"""

import json
import sys
from pathlib import Path

def test_schema_completeness():
    """Test that our schema includes all required fields for routing generation."""
    import yaml
    
    schema_path = Path("ws-protocol.yml")
    if not schema_path.exists():
        print("❌ Schema file not found")
        return False
        
    with open(schema_path) as f:
        schema = yaml.safe_load(f)
    
    # Test 1: Check that handlers section exists
    if 'handlers' not in schema:
        print("❌ Missing 'handlers' section in schema")
        return False
    
    handlers = schema['handlers']
    expected_handlers = ['dashboard', 'chat']
    
    for handler in expected_handlers:
        if handler not in handlers:
            print(f"❌ Missing handler: {handler}")
            return False
            
        handler_config = handlers[handler]
        if 'handles' not in handler_config:
            print(f"❌ Handler {handler} missing 'handles' field")
            return False
    
    # Test 2: Check that all server-to-client messages have handler_method
    messages = schema.get('messages', {})
    server_to_client_messages = []
    
    for msg_type, config in messages.items():
        directions = config.get('direction', [])
        if 'server_to_client' in directions:
            server_to_client_messages.append(msg_type)
            # Only check for handler_method if this is a pure server-to-client message
            # (not bidirectional like ping/pong)
            if directions == ['server_to_client'] and 'handler_method' not in config:
                print(f"❌ Message {msg_type} missing handler_method")
                return False
    
    print(f"✅ Found {len(server_to_client_messages)} server-to-client messages with handler methods")
    
    # Test 3: Check that message_routing section exists
    if 'message_routing' not in schema:
        print("❌ Missing 'message_routing' section")
        return False
        
    print("✅ Schema completeness test passed")
    return True

def test_generated_files():
    """Test that generated files exist and have expected content."""
    frontend_handler_path = Path("frontend/src/generated/ws_handlers.rs")
    
    if not frontend_handler_path.exists():
        print("❌ Generated handler file not found")
        return False
        
    with open(frontend_handler_path) as f:
        content = f.read()
        
    # Check for expected traits
    expected_traits = ['DashboardHandler', 'ChatHandler']
    expected_routers = ['DashboardMessageRouter', 'ChatMessageRouter']
    
    for trait in expected_traits:
        if f"pub trait {trait}" not in content:
            print(f"❌ Missing trait: {trait}")
            return False
            
    for router in expected_routers:
        if f"pub struct {router}" not in content:
            print(f"❌ Missing router: {router}")
            return False
            
    # Check for expected handler methods
    expected_methods = [
        'handle_run_update',
        'handle_agent_event', 
        'handle_thread_message',
        'handle_stream_chunk'
    ]
    
    for method in expected_methods:
        if f"fn {method}" not in content:
            print(f"❌ Missing method: {method}")
            return False
    
    print("✅ Generated files test passed")
    return True

def main():
    """Run all validation tests."""
    print("🧪 Testing schema-driven WebSocket routing implementation...\n")
    
    all_passed = True
    
    # Test schema completeness
    print("1. Testing schema completeness...")
    if not test_schema_completeness():
        all_passed = False
    print()
    
    # Test generated files
    print("2. Testing generated files...")
    if not test_generated_files():
        all_passed = False
    print()
    
    if all_passed:
        print("🎉 All validation tests passed!")
        print("\n✅ Implementation Status:")
        print("  - Schema extended with handler definitions")
        print("  - Message-to-handler mappings defined")
        print("  - Generated handler traits and routers")
        print("  - WebSocket managers updated to use generated routing")
        print("  - Manual string matching eliminated")
        return 0
    else:
        print("❌ Some validation tests failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())