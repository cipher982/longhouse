#!/usr/bin/env python3
"""Validate tool registry matches schema contracts."""

import sys
import yaml
from pathlib import Path

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "zerg" / "backend"))

def validate_tool_registry():
    """Validate backend tool registry against schema."""

    # Load schema
    schema_file = Path(__file__).parent.parent / "asyncapi" / "tools.yml"
    with open(schema_file) as f:
        schema = yaml.safe_load(f)

    expected_tools = set(schema['components']['schemas']['ToolName']['enum'])
    expected_mappings = schema['components']['schemas']['ToolRegistry']['properties']['tool_mappings']['properties']

    # Import and get actual tools
    from zerg.tools.builtin import BUILTIN_TOOLS

    actual_tools = set(tool.name for tool in BUILTIN_TOOLS)

    # Check all expected tools exist
    missing_tools = expected_tools - actual_tools
    if missing_tools:
        print(f"❌ Missing tools in registry: {missing_tools}")
        return False

    # Check no extra tools (would require schema update)
    extra_tools = actual_tools - expected_tools
    if extra_tools:
        print(f"⚠️  Extra tools in registry (update schema): {extra_tools}")
        # Note: Don't fail for extra tools, just warn

    # Validate tool-server mappings by checking tool origins
    print("🔍 Validating tool-server mappings...")

    # Group tools by their module origin to verify server mapping
    from zerg.tools.builtin.http_tools import TOOLS as HTTP_TOOLS
    from zerg.tools.builtin.math_tools import TOOLS as MATH_TOOLS
    from zerg.tools.builtin.datetime_tools import TOOLS as DATETIME_TOOLS
    from zerg.tools.builtin.uuid_tools import TOOLS as UUID_TOOLS

    tool_to_module = {}
    for tool in HTTP_TOOLS:
        tool_to_module[tool.name] = "http"
    for tool in MATH_TOOLS:
        tool_to_module[tool.name] = "math"
    for tool in DATETIME_TOOLS:
        tool_to_module[tool.name] = "datetime"
    for tool in UUID_TOOLS:
        tool_to_module[tool.name] = "uuid"

    # Validate mappings
    mapping_errors = []
    for tool_name, server_info in expected_mappings.items():
        expected_server = server_info['const']

        if tool_name not in tool_to_module:
            mapping_errors.append(f"Tool {tool_name} not found in registry")
            continue

        actual_server = tool_to_module[tool_name]
        if actual_server != expected_server:
            mapping_errors.append(f"Tool {tool_name}: expected server '{expected_server}', got '{actual_server}'")

    if mapping_errors:
        print("❌ Tool-server mapping errors:")
        for error in mapping_errors:
            print(f"  - {error}")
        return False

    print(f"✅ Tool registry validation passed")
    print(f"  - {len(expected_tools)} tools validated")
    print(f"  - All tool-server mappings correct")

    return True

def main():
    """Main entry point."""
    try:
        if validate_tool_registry():
            sys.exit(0)
        else:
            sys.exit(1)
    except Exception as e:
        print(f"❌ Validation failed with error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
