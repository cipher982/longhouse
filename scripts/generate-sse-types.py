#!/usr/bin/env python3
"""
SSE Event Protocol Code Generator
Generates Python and TypeScript types from AsyncAPI 3.0 schema

Generates:
- Python Pydantic models for payloads
- Python SSEEventType enum
- Python emit_sse_event() typed emitter
- TypeScript interfaces for payloads
- TypeScript SSEEventType union
- TypeScript SSEEventMap discriminated union
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml


class SSETypeGenerator:
    def __init__(self, schema_path: str):
        """Initialize with AsyncAPI 3.0 schema path."""
        self.repo_root = Path(__file__).resolve().parent.parent
        self.schema_path = self.repo_root / schema_path
        self.schema = self._load_schema()

        # Canonical output locations
        self.backend_generated_path = (
            self.repo_root / "apps" / "zerg" / "backend" / "zerg" / "generated" / "sse_events.py"
        )
        self.frontend_generated_path = (
            self.repo_root / "apps" / "zerg" / "frontend-web" / "src" / "generated" / "sse-events.ts"
        )

    def _load_schema(self) -> Dict[str, Any]:
        """Load and validate AsyncAPI schema."""
        if not self.schema_path.exists():
            print(f"❌ Schema file not found: {self.schema_path}")
            sys.exit(1)

        try:
            with open(self.schema_path, 'r') as f:
                schema = yaml.safe_load(f)

            # Validate it's AsyncAPI 3.0
            if schema.get('asyncapi') != '3.0.0':
                print(f"⚠️  Schema is not AsyncAPI 3.0 (found: {schema.get('asyncapi')})")

            return schema
        except Exception as e:
            print(f"❌ Error loading schema: {e}")
            sys.exit(1)

    async def generate_all(self):
        """Generate all code artifacts."""
        print(f"🚀 Generating SSE types from AsyncAPI 3.0 schema: {self.schema_path}")

        # Generate in parallel for speed
        await asyncio.gather(
            self._generate_python_types(),
            self._generate_typescript_types(),
            return_exceptions=True
        )

        print("✅ SSE type generation complete!")

    async def _generate_python_types(self):
        """Generate Python Pydantic models and typed emitter."""
        print("🐍 Generating Python types...")

        output_path = self.backend_generated_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        code = self._generate_python_header()
        code += self._generate_python_payloads()
        code += self._generate_python_event_enum()
        code += self._generate_python_emitter()

        with open(output_path, 'w') as f:
            f.write(code.rstrip() + "\n")

        print(f"✅ Python types: {output_path}")

    async def _generate_typescript_types(self):
        """Generate TypeScript types with discriminated unions."""
        print("📘 Generating TypeScript types...")

        output_path = self.frontend_generated_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        code = self._generate_typescript_header()
        code += self._generate_typescript_payloads()
        code += self._generate_typescript_event_type()
        code += self._generate_typescript_event_map()

        with open(output_path, 'w') as f:
            f.write(code.rstrip() + "\n")

        print(f"✅ TypeScript types: {output_path}")

    # ========== Python Generation ==========

    def _generate_python_header(self) -> str:
        """Generate Python file header with imports."""
        return f'''# AUTO-GENERATED FILE - DO NOT EDIT
# Generated from {self.schema_path.name}
# Using AsyncAPI 3.0 + SSE Protocol Code Generation
#
# This file contains strongly-typed SSE event definitions.
# To update, modify the schema file and run: python scripts/generate-sse-types.py schemas/sse-events.asyncapi.yml

import json
from enum import Enum
from typing import Any, Dict, Optional, Literal
from pydantic import BaseModel, Field


'''

    def _generate_python_payloads(self) -> str:
        """Generate Python payload classes from AsyncAPI schemas."""
        code = "# Event payload schemas\n\n"

        schemas = self.schema.get("components", {}).get("schemas", {})

        # Skip envelope and common types that are referenced
        skip_schemas = {"SSEEnvelope"}

        for name, schema_def in schemas.items():
            if name in skip_schemas:
                continue

            if schema_def.get("type") == "object":
                code += self._python_schema_to_pydantic_class(name, schema_def)
                code += "\n\n"
            elif schema_def.get("type") == "string" and "enum" in schema_def:
                # Generate enum for status fields
                code += self._python_enum_to_class(name, schema_def)
                code += "\n\n"

        return code

    def _python_schema_to_pydantic_class(self, name: str, schema: Dict[str, Any]) -> str:
        """Convert AsyncAPI schema to Pydantic class."""
        lines = [f"class {name}(BaseModel):"]

        description = schema.get("description", f"Payload for {name}")
        lines.append(f'    """{description}"""')
        lines.append("")

        properties = schema.get("properties", {})
        required = schema.get("required", [])

        if not properties:
            lines.append("    pass")
            return "\n".join(lines)

        for prop_name, prop_schema in properties.items():
            python_type = self._json_type_to_python(prop_schema)
            is_required = prop_name in required

            # Add field metadata
            description = prop_schema.get("description", "").replace("'", "\\'")
            constraints = self._extract_field_constraints(prop_schema)

            if is_required:
                field_def = f"Field({constraints}description='{description}')" if constraints or description else ""
                if field_def:
                    lines.append(f"    {prop_name}: {python_type} = {field_def}")
                else:
                    lines.append(f"    {prop_name}: {python_type}")
            else:
                field_def = f"Field(default=None, {constraints}description='{description}')" if constraints or description else "Field(default=None)"
                lines.append(f"    {prop_name}: Optional[{python_type}] = {field_def}")

        return "\n".join(lines)

    def _python_enum_to_class(self, name: str, schema: Dict[str, Any]) -> str:
        """Convert string enum schema to Python Enum."""
        lines = [f"class {name}(str, Enum):"]
        description = schema.get("description", "")
        lines.append(f'    """{description}"""')
        lines.append("")

        for value in schema.get("enum", []):
            enum_name = value.upper()
            lines.append(f'    {enum_name} = "{value}"')

        return "\n".join(lines)

    def _extract_field_constraints(self, schema: Dict[str, Any]) -> str:
        """Extract Pydantic field constraints from JSON schema."""
        constraints = []

        if "minimum" in schema:
            constraints.append(f"ge={schema['minimum']}")
        if "maximum" in schema:
            constraints.append(f"le={schema['maximum']}")
        if "minLength" in schema:
            constraints.append(f"min_length={schema['minLength']}")
        if "maxLength" in schema:
            constraints.append(f"max_length={schema['maxLength']}")
        # Skip const - Pydantic doesn't have const= validator, use Literal type instead

        return ", ".join(constraints) + (", " if constraints else "")

    def _json_type_to_python(self, schema: Dict[str, Any]) -> str:
        """Convert JSON schema type to Python type hint."""
        if "$ref" in schema:
            ref_name = schema["$ref"].split("/")[-1]
            return ref_name

        json_type = schema.get("type", "any")

        if json_type == "string":
            if "const" in schema:
                const_value = schema["const"]
                return f"Literal['{const_value}']"
            elif "enum" in schema:
                enum_values = "', '".join(schema["enum"])
                return f"Literal['{enum_values}']"
            elif schema.get("format") == "date-time":
                return "str"
            return "str"
        elif json_type == "integer":
            return "int"
        elif json_type == "number":
            return "float"
        elif json_type == "boolean":
            return "bool"
        elif json_type == "array":
            item_schema = schema.get("items", {"type": "any"})
            item_type = self._json_type_to_python(item_schema)
            return f"list[{item_type}]"
        elif json_type == "object":
            return "Dict[str, Any]"
        else:
            return "Any"

    def _generate_python_event_enum(self) -> str:
        """Generate SSEEventType enum from AsyncAPI messages."""
        code = "class SSEEventType(str, Enum):\n"
        code += '    """Enumeration of all SSE event types."""\n\n'

        messages = self.schema.get("components", {}).get("messages", {})

        for msg_data in messages.values():
            event_name = msg_data.get("x-sse-event-type") or msg_data.get("name", "unknown")
            enum_name = event_name.upper()
            code += f'    {enum_name} = "{event_name}"\n'

        return code + "\n\n"

    def _generate_python_emitter(self) -> str:
        """Generate emit_sse_event() typed helper."""
        return '''# Typed emitter for SSE events

def emit_sse_event(
    event_type: SSEEventType,
    payload: BaseModel,
    event_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create a typed SSE event dict ready for yield.

    Returns dict with 'event', 'data', and optionally 'id' keys.
    Use like: yield emit_sse_event(SSEEventType.SUPERVISOR_STARTED, SupervisorStartedPayload(...))

    Args:
        event_type: SSE event type enum value
        payload: Pydantic model instance for the event payload
        event_id: Optional event ID for resumable streams

    Returns:
        Dict ready for SSE yield (with 'event', 'data', 'id' keys)
    """
    result = {
        "event": event_type.value,
        "data": json.dumps(payload.model_dump()),
    }

    if event_id is not None:
        result["id"] = str(event_id)

    return result
'''

    # ========== TypeScript Generation ==========

    def _generate_typescript_header(self) -> str:
        """Generate TypeScript file header."""
        return f'''// AUTO-GENERATED FILE - DO NOT EDIT
// Generated from {self.schema_path.name}
// Using AsyncAPI 3.0 + SSE Protocol Code Generation
//
// This file contains strongly-typed SSE event definitions.
// To update, modify the schema file and run: python scripts/generate-sse-types.py schemas/sse-events.asyncapi.yml

'''

    def _generate_typescript_payloads(self) -> str:
        """Generate TypeScript payload interfaces from AsyncAPI schemas."""
        code = "// Event payload types\n\n"

        schemas = self.schema.get("components", {}).get("schemas", {})

        # Skip envelope
        skip_schemas = {"SSEEnvelope"}

        for name, schema_def in schemas.items():
            if name in skip_schemas:
                continue

            if schema_def.get("type") == "object":
                code += self._typescript_schema_to_interface(name, schema_def)
                code += "\n\n"
            elif schema_def.get("type") == "string" and "enum" in schema_def:
                # Generate type alias for enum
                enum_values = " | ".join(f'"{v}"' for v in schema_def["enum"])
                description = schema_def.get("description", "")
                if description:
                    code += f"/** {description} */\n"
                code += f"export type {name} = {enum_values};\n\n"

        return code

    def _typescript_schema_to_interface(self, name: str, schema: Dict[str, Any]) -> str:
        """Convert AsyncAPI schema to TypeScript interface."""
        lines = []

        description = schema.get("description", "")
        if description:
            lines.append(f"/** {description} */")

        lines.append(f"export interface {name} {{")

        properties = schema.get("properties", {})
        required = schema.get("required", [])

        if not properties:
            lines.append("  // No properties")
        else:
            for prop_name, prop_schema in properties.items():
                ts_type = self._json_type_to_typescript(prop_schema)
                is_required = prop_name in required
                optional_marker = "" if is_required else "?"

                prop_desc = prop_schema.get("description", "")
                if prop_desc:
                    lines.append(f"  /** {prop_desc} */")

                lines.append(f"  {prop_name}{optional_marker}: {ts_type};")

        lines.append("}")

        return "\n".join(lines)

    def _json_type_to_typescript(self, schema: Dict[str, Any]) -> str:
        """Convert JSON schema type to TypeScript type."""
        if "$ref" in schema:
            ref_name = schema["$ref"].split("/")[-1]
            return ref_name

        json_type = schema.get("type", "unknown")

        if json_type == "string":
            if "const" in schema:
                const_value = schema["const"]
                return f'"{const_value}"'
            elif "enum" in schema:
                enum_values = " | ".join(f'"{v}"' for v in schema["enum"])
                return enum_values
            return "string"
        elif json_type == "integer" or json_type == "number":
            return "number"
        elif json_type == "boolean":
            return "boolean"
        elif json_type == "array":
            items = schema.get("items", {})
            item_type = self._json_type_to_typescript(items)
            return f"{item_type}[]"
        elif json_type == "object":
            # For generic objects, use Record or any
            if "additionalProperties" in schema:
                return "Record<string, any>"
            return "Record<string, any>"
        else:
            return "unknown"

    def _generate_typescript_event_type(self) -> str:
        """Generate SSEEventType union type."""
        code = "// SSE event type union\n"

        messages = self.schema.get("components", {}).get("messages", {})
        event_types = []

        for msg_data in messages.values():
            event_name = msg_data.get("x-sse-event-type") or msg_data.get("name", "unknown")
            event_types.append(f'"{event_name}"')

        code += "export type SSEEventType =\n"
        for i, event_type in enumerate(event_types):
            is_last = i == len(event_types) - 1
            if i == 0:
                code += f"  {event_type}\n"
            elif is_last:
                code += f"  | {event_type};\n"
            else:
                code += f"  | {event_type}\n"

        code += "\n"
        return code

    def _generate_typescript_event_map(self) -> str:
        """Generate SSEEventMap discriminated union."""
        code = "// SSE event discriminated union for type-safe event handling\n"
        code += "export type SSEEventMap =\n"

        messages = self.schema.get("components", {}).get("messages", {})
        event_entries = []

        for msg_name, msg_data in messages.items():
            event_type = msg_data.get("x-sse-event-type") or msg_data.get("name", "unknown")
            payload_ref = msg_data.get("payload", {}).get("$ref", "")

            if payload_ref:
                payload_type = payload_ref.split("/")[-1]
                event_entries.append((event_type, payload_type))

        for i, (event_type, payload_type) in enumerate(event_entries):
            is_last = i == len(event_entries) - 1
            if i == 0:
                code += f'  {{ event: "{event_type}"; data: {payload_type}; id?: number }}\n'
            elif is_last:
                code += f'  | {{ event: "{event_type}"; data: {payload_type}; id?: number }};\n'
            else:
                code += f'  | {{ event: "{event_type}"; data: {payload_type}; id?: number }}\n'

        code += "\n"
        return code


async def main():
    """Main entry point."""
    if len(sys.argv) != 2:
        print("Usage: python scripts/generate-sse-types.py <asyncapi-schema-file>")
        sys.exit(1)

    schema_file = sys.argv[1]
    generator = SSETypeGenerator(schema_file)

    try:
        await generator.generate_all()
        print("🎉 SSE types generated successfully!")
    except Exception as e:
        print(f"❌ Generation failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
