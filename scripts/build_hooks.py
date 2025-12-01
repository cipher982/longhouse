"""
Build hooks for automatic WebSocket type generation.
Integrated with Hatch build system for contract-first development.
"""

import subprocess
import os
from pathlib import Path
from hatchling.plugin import hookimpl


class WebSocketTypeGeneratorHook:
    """Build hook that regenerates WebSocket types when schema changes."""
    
    PLUGIN_NAME = "websocket-type-generator"
    
    def __init__(self, root: str, config: dict):
        self.root = Path(root)
        self.config = config
        
    def initialize(self, version: str, build_data: dict) -> None:
        """Initialize and run WebSocket type generation."""
        print("🔄 Running WebSocket type generation...")
        
        schema_path = self.root / "ws-protocol-asyncapi.yml"
        generator_path = self.root / "scripts" / "generate-ws-types-modern.py"
        
        if not schema_path.exists():
            print(f"⚠️  Schema file not found: {schema_path}, skipping generation")
            return
            
        if not generator_path.exists():
            print(f"⚠️  Generator script not found: {generator_path}, skipping generation")
            return
            
        try:
            # Run the modern generator
            result = subprocess.run(
                ["python3", str(generator_path), str(schema_path)],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode != 0:
                print(f"❌ WebSocket type generation failed:")
                print(result.stderr)
            else:
                print("✅ WebSocket types generated successfully")
                
        except subprocess.TimeoutExpired:
            print("⚠️  WebSocket type generation timed out")
        except Exception as e:
            print(f"⚠️  WebSocket type generation error: {e}")


@hookimpl
def hatch_register_build_hook():
    """Register the WebSocket type generator hook."""
    return WebSocketTypeGeneratorHook