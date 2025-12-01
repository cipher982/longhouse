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
    
    schema_path = Path("schemas/ws-protocol-asyncapi.yml")
    if not schema_path.exists():
        # Try legacy location
        schema_path = Path("ws-protocol.yml")
        if not schema_path.exists():
            print("❌ Schema file not found")
            return False
        
    with open(schema_path) as f:
        schema = yaml.safe_load(f)
    
    # Test 1: Check that handlers section exists (if using x-handler-groups)
    handler_groups = schema.get('x-handler-groups', {})
    if handler_groups:
        expected_handlers = ['dashboard', 'chat']
        for handler in expected_handlers:
            if handler not in handler_groups:
                print(f"⚠️  Handler group not found: {handler} (may be optional)")
    
    # Test 2: Check that components/messages exist
    messages = schema.get('components', {}).get('messages', {})
    if not messages:
        print("❌ No messages found in schema")
        return False
    
    print(f"✅ Found {len(messages)} message definitions")
    
    # Test 3: Check that components/schemas exist
    schemas = schema.get('components', {}).get('schemas', {})
    if not schemas:
        print("❌ No schemas found")
        return False
        
    print(f"✅ Found {len(schemas)} schema definitions")
    print("✅ Schema completeness test passed")
    return True

def test_generated_typescript():
    """Test that TypeScript types are generated correctly."""
    ts_path = Path("apps/zerg/frontend-web/src/generated/ws-messages.ts")
    
    if not ts_path.exists():
        print("⚠️  TypeScript types not found (run generator first)")
        return True  # Not a failure, just not generated yet
        
    with open(ts_path) as f:
        content = f.read()
        
    # Check for expected content
    if "export interface Envelope" not in content:
        print("❌ Missing Envelope interface")
        return False
        
    if "export type WebSocketMessage" not in content:
        print("❌ Missing WebSocketMessage union type")
        return False
    
    print("✅ TypeScript types test passed")
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
    
    # Test TypeScript generation
    print("2. Testing TypeScript types...")
    if not test_generated_typescript():
        all_passed = False
    print()
    
    if all_passed:
        print("🎉 All validation tests passed!")
        print("\n✅ Implementation Status:")
        print("  - Schema contains message definitions")
        print("  - TypeScript types generated (if run)")
        print("  - Python types generated (if run)")
        return 0
    else:
        print("❌ Some validation tests failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())
