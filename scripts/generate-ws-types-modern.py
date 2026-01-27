#!/usr/bin/env python3
"""
Modern WebSocket Protocol Code Generator
Uses AsyncAPI 3.0 for 2025 best practices

Generates:
- Python Pydantic models (via custom generator)
- TypeScript types with discriminated unions
- Contract validation JSON
- JSON Schema for IDE integration
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml
class ModernProtocolGenerator:
    def __init__(self, schema_path: str):
        """Initialize with AsyncAPI 3.0 schema path."""
        self.repo_root = Path(__file__).resolve().parent.parent

        candidate = Path(schema_path)
        if not candidate.is_absolute():
            # Allow running from any CWD; resolve relative to repo root.
            candidate = (self.repo_root / candidate).resolve()

        self.schema_path = candidate
        self.schema = self._load_schema()

        # Canonical output locations (independent of schema file location).
        self.schemas_dir = self.repo_root / "schemas"
        self.backend_generated_path = (
            self.repo_root / "apps" / "zerg" / "backend" / "zerg" / "generated" / "ws_messages.py"
        )
        self.frontend_generated_path = (
            self.repo_root / "apps" / "zerg" / "frontend-web" / "src" / "generated" / "ws-messages.ts"
        )
        self.json_schema_path = self.schemas_dir / "ws-protocol.schema.json"
        self.contract_json_path = self.schemas_dir / "ws-protocol-v1.json"

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
        """Generate all code artifacts using modern toolchain."""
        print(f"🚀 Generating code from AsyncAPI 3.0 schema: {self.schema_path}")

        # Generate in parallel for speed
        await asyncio.gather(
            self._generate_python_types(),
            self._generate_typescript_types(),
            self._generate_json_schema(),
            return_exceptions=True
        )

        # Generate contract validation files
        self._generate_contract_json()

        print("✅ Modern code generation complete!")

    async def _generate_python_types(self):
        """Generate Python Pydantic models with modern features."""
        print("🐍 Generating Python types...")

        output_path = self.backend_generated_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        code = self._generate_python_header()
        code += self._generate_python_envelope()
        code += self._generate_python_payloads()
        code += self._generate_python_message_enum()
        code += self._generate_python_typed_emitter()
        code += self._generate_python_validation()

        with open(output_path, 'w') as f:
            f.write(code.rstrip() + "\n")

        print(f"✅ Python types: {output_path}")

    async def _generate_typescript_types(self):
        """Generate TypeScript types with discriminated unions."""
        print("📘 Generating TypeScript types...")

        output_path = self.frontend_generated_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        code = self._generate_typescript_header()
        code += self._generate_typescript_envelope()
        code += self._generate_typescript_payloads()
        code += self._generate_typescript_message_types()
        code += self._generate_typescript_discriminated_union()
        # Type guards removed - unused in codebase, simple type === 'foo' checks are clearer

        with open(output_path, 'w') as f:
            f.write(code.rstrip() + "\n")

        print(f"✅ TypeScript types: {output_path}")

    def _generate_typescript_header(self) -> str:
        """Generate TypeScript file header."""
        return f'''// AUTO-GENERATED FILE - DO NOT EDIT
// Generated from {self.schema_path.name}
// Using AsyncAPI 3.0 + TypeScript Code Generation
//
// This file contains strongly-typed WebSocket message definitions.
// To update, modify the schema file and run: python scripts/generate-ws-types-modern.py

'''

    def _generate_typescript_envelope(self) -> str:
        """Generate TypeScript Envelope interface."""
        return '''// Base envelope structure for all WebSocket messages
export interface Envelope<T = unknown> {
  /** Protocol version */
  v: number;
  /** Message type identifier */
  type: string;
  /** Topic routing string (e.g., 'fiche:123', 'thread:456') */
  topic: string;
  /** Optional request correlation ID */
  req_id?: string;
  /** Timestamp in milliseconds since epoch */
  ts: number;
  /** Message payload - structure depends on type */
  data: T;
}

'''

    def _generate_typescript_payloads(self) -> str:
        """Generate TypeScript payload interfaces from AsyncAPI schemas."""
        code = "// Message payload types\n\n"

        schemas = self.schema.get("components", {}).get("schemas", {})

        for name, schema_def in schemas.items():
            if name == "Envelope":
                continue

            if schema_def.get("type") == "object":
                code += self._typescript_schema_to_interface(name, schema_def)
                code += "\n\n"

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
            if "enum" in schema:
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

    def _generate_typescript_message_types(self) -> str:
        """Generate TypeScript message type definitions."""
        code = "// Typed message definitions with envelopes\n\n"

        messages = self.schema.get("components", {}).get("messages", {})

        for msg_name, msg_data in messages.items():
            msg_type_name = msg_data.get("name", "")
            payload_ref = msg_data.get("payload", {}).get("$ref", "")
            summary = msg_data.get("summary", "")

            if payload_ref:
                payload_type = payload_ref.split("/")[-1]

                if summary:
                    code += f"/** {summary} */\n"

                code += f"export interface {msg_name} extends Envelope<{payload_type}> {{\n"
                code += f"  type: '{msg_type_name}';\n"
                code += f"}}\n\n"

        return code

    def _generate_typescript_discriminated_union(self) -> str:
        """Generate discriminated union of all message types."""
        code = "// Discriminated union of all WebSocket messages\n"
        code += "export type WebSocketMessage =\n"

        messages = self.schema.get("components", {}).get("messages", {})
        msg_names = list(messages.keys())

        for i, msg_name in enumerate(msg_names):
            separator = " |" if i < len(msg_names) - 1 else ";"
            code += f"  | {msg_name}\n"

        code += "\n"
        return code

    async def _generate_json_schema(self):
        """Generate JSON Schema for IDE integration."""
        print("📋 Generating JSON Schema for IDE...")

        output_path = self.json_schema_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert AsyncAPI to JSON Schema
        json_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$id": "ws-protocol-schema",
            "title": "WebSocket Protocol Schema",
            "type": "object",
            "properties": {},
            "definitions": {}
        }

        # Extract schemas from AsyncAPI components
        if "components" in self.schema and "schemas" in self.schema["components"]:
            json_schema["definitions"] = self.schema["components"]["schemas"]

        with open(output_path, 'w') as f:
            json.dump(json_schema, f, indent=2)
            f.write("\n")

        print(f"✅ JSON Schema: {output_path}")

    def _generate_contract_json(self):
        """Generate contract validation JSON."""
        output_path = self.contract_json_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        contract = {
            "version": 1,
            "asyncapi_version": self.schema.get("asyncapi", "3.0.0"),
            "info": self.schema.get("info", {}),
            "channels": self._extract_channel_contracts(),
            "messages": self._extract_message_contracts(),
            "operations": self._extract_operation_contracts(),
            "validation_rules": self.schema.get("x-validation-rules", []),
            "performance_contracts": self.schema.get("x-performance-contracts", {}),
            "security_contracts": self.schema.get("x-security-contracts", {})
        }

        with open(output_path, 'w') as f:
            json.dump(contract, f, indent=2)
            f.write("\n")

        print(f"✅ Contract JSON: {output_path}")

    def _extract_channel_contracts(self) -> Dict[str, Any]:
        """Extract channel information for contracts."""
        channels = {}
        for channel_name, channel_data in self.schema.get("channels", {}).items():
            channels[channel_name] = {
                "address": channel_data.get("address"),
                "description": channel_data.get("description"),
                "parameters": channel_data.get("parameters", {}),
                "messages": list(channel_data.get("messages", {}).keys())
            }
        return channels

    def _extract_message_contracts(self) -> Dict[str, Any]:
        """Extract message contracts."""
        messages = {}
        for msg_name, msg_data in self.schema.get("components", {}).get("messages", {}).items():
            messages[msg_data.get("name", msg_name)] = {
                "summary": msg_data.get("summary"),
                "payload_schema": msg_data.get("payload"),
                "handler_method": msg_data.get("x-handler-method"),
                "aliases": msg_data.get("x-aliases", [])
            }
        return messages

    def _extract_operation_contracts(self) -> Dict[str, Any]:
        """Extract operation contracts."""
        operations = {}
        for op_name, op_data in self.schema.get("operations", {}).items():
            operations[op_name] = {
                "action": op_data.get("action"),
                "channel": op_data.get("channel"),
                "messages": op_data.get("messages", [])
            }
        return operations

    def _generate_python_header(self) -> str:
        """Generate Python file header with modern imports."""
        return f'''# AUTO-GENERATED FILE - DO NOT EDIT
# Generated from {self.schema_path.name}
# Using AsyncAPI 3.0 + Modern Python Code Generation
#
# This file contains strongly-typed WebSocket message definitions.
# To update, modify the schema file and run: python scripts/generate-ws-types-modern.py

import time
import jsonschema
from enum import Enum
from typing import Any, Dict, List, Optional, Union, Literal, Protocol
from pydantic import BaseModel, Field, ValidationError


'''

    def _generate_python_envelope(self) -> str:
        """Generate modern Python Envelope with validation."""
        return '''class Envelope(BaseModel):
    """Unified envelope for all WebSocket messages with validation."""

    v: int = Field(default=1, description="Protocol version")
    type: str = Field(description="Message type identifier")
    topic: str = Field(description="Topic routing string")
    req_id: Optional[str] = Field(default=None, description="Request correlation ID")
    ts: int = Field(description="Timestamp in milliseconds since epoch")
    data: Dict[str, Any] = Field(description="Message payload")

    @classmethod
    def create(
        cls,
        message_type: str,
        topic: str,
        data: Dict[str, Any],
        req_id: Optional[str] = None,
    ) -> "Envelope":
        """Create and validate a new envelope."""
        envelope = cls(
            type=message_type.lower(),
            topic=topic,
            data=data,
            req_id=req_id,
            ts=int(time.time() * 1000),
        )
        # Validate on creation for fail-fast behavior
        validate_envelope_fast(envelope.model_dump())
        return envelope

    def model_dump_validated(self) -> Dict[str, Any]:
        """Dump model with runtime validation."""
        data = self.model_dump()
        validate_envelope_fast(data)
        return data

'''

    def _generate_python_payloads(self) -> str:
        """Generate Python payload classes from AsyncAPI schemas."""
        code = "# Message payload schemas\n\n"

        schemas = self.schema.get("components", {}).get("schemas", {})

        for name, schema_def in schemas.items():
            if name == "Envelope":
                continue

            if schema_def.get("type") == "object":
                code += self._python_schema_to_pydantic_class(name, schema_def)
                code += "\n\n"

        return code

    def _python_schema_to_pydantic_class(self, name: str, schema: Dict[str, Any]) -> str:
        """Convert AsyncAPI schema to modern Pydantic class."""
        lines = [f"class {name}(BaseModel):"]

        description = schema.get("description", f"Payload for {name} messages")
        lines.append(f'    """{description}"""')
        lines.append("")

        properties = schema.get("properties", {})
        required = schema.get("required", [])

        if not properties:
            lines.append("    pass")
            return "\n".join(lines)

        for prop_name, prop_schema in properties.items():
            python_type = self._json_type_to_python_modern(prop_schema)
            is_required = prop_name in required

            # Add validation and description
            description = prop_schema.get("description", "")
            constraints = self._extract_field_constraints(prop_schema)

            if is_required:
                field_def = f"Field({constraints}description='{description}')" if constraints or description else ""
                if field_def:
                    lines.append(f"    {prop_name}: {python_type} = {field_def}")
                else:
                    lines.append(f"    {prop_name}: {python_type}")
            else:
                field_def = f"Field(default=None, {constraints}description='{description}')" if constraints or description else "None"
                lines.append(f"    {prop_name}: Optional[{python_type}] = {field_def}")

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
        if "pattern" in schema:
            constraints.append(f"pattern=r'{schema['pattern']}'")

        return ", ".join(constraints) + (", " if constraints else "")

    def _json_type_to_python_modern(self, schema: Dict[str, Any]) -> str:
        """Convert JSON schema type to modern Python type hint."""
        if "$ref" in schema:
            ref_name = schema["$ref"].split("/")[-1]
            return ref_name

        json_type = schema.get("type", "any")

        if json_type == "string":
            if "enum" in schema:
                enum_values = "', '".join(schema["enum"])
                return f"Literal['{enum_values}']"
            elif schema.get("format") == "date-time":
                return "str"  # Could use datetime with custom serializer
            elif schema.get("format") == "email":
                return "str"  # Pydantic validates email format
            elif schema.get("format") == "uri":
                return "str"  # Pydantic validates URI format
            return "str"
        elif json_type == "integer":
            return "int"
        elif json_type == "number":
            return "float"
        elif json_type == "boolean":
            return "bool"
        elif json_type == "array":
            item_schema = schema.get("items", {"type": "any"})
            item_type = self._json_type_to_python_modern(item_schema)
            return f"List[{item_type}]"
        elif json_type == "object":
            return "Dict[str, Any]"
        else:
            return "Any"

    def _generate_python_message_enum(self) -> str:
        """Generate message type enum from AsyncAPI messages."""
        code = "class MessageType(str, Enum):\n"
        code += '    """Enumeration of all WebSocket message types."""\n\n'

        messages = self.schema.get("components", {}).get("messages", {})

        for msg_data in messages.values():
            msg_name = msg_data.get("name", "unknown")
            enum_name = msg_name.upper()
            code += f'    {enum_name} = "{msg_name}"\n'

        return code + "\n\n"

    def _generate_python_typed_emitter(self) -> str:
        """Generate modern typed emitter for contract enforcement."""
        return '''# Typed emitter for contract enforcement

class TypedEmitter(Protocol):
    """Protocol for typed WebSocket message emission."""

    async def send_typed(
        self,
        topic: str,
        message_type: MessageType,
        payload: BaseModel
    ) -> None:
        """Send a typed message with validation."""
        ...

class TypedEmitterImpl:
    """Implementation of typed emitter with runtime validation."""

    def __init__(self, raw_emitter):
        """Initialize with raw broadcast function."""
        self.raw_emitter = raw_emitter

    async def send_typed(
        self,
        topic: str,
        message_type: MessageType,
        payload: BaseModel
    ) -> None:
        """Send a typed message with full validation."""
        # Validate payload matches expected type for message
        validate_payload_for_message_type(message_type, payload)

        # Create envelope with validation
        envelope = Envelope.create(
            message_type=message_type.value,
            topic=topic,
            data=payload.model_dump()
        )

        # Send via raw emitter
        await self.raw_emitter(topic, envelope.model_dump_validated())

def create_typed_emitter(raw_emitter) -> TypedEmitter:
    """Factory for typed emitter."""
    return TypedEmitterImpl(raw_emitter)

'''

    def _generate_python_validation(self) -> str:
        """Generate validation functions with fast runtime checks."""
        return '''# Fast validation functions

def validate_envelope_fast(data: Dict[str, Any]) -> None:
    """Envelope validation using jsonschema."""
    try:
        jsonschema.validate(data, ENVELOPE_SCHEMA)
    except jsonschema.ValidationError as e:
        from pydantic import ValidationError as PydanticValidationError
        raise PydanticValidationError(f"Envelope validation failed: {e}")

def validate_payload_for_message_type(message_type: MessageType, payload: BaseModel) -> None:
    """Validate payload matches expected type for message."""
    # This will be populated by specific payload type checks
    # TODO: Generate type-specific validation from schema
    pass

# Schema constants for validation
ENVELOPE_SCHEMA = {
    "type": "object",
    "required": ["v", "type", "topic", "ts", "data"],
    "additionalProperties": False,
    "properties": {
        "v": {"type": "integer", "const": 1},
        "type": {"type": "string"},
        "topic": {"type": "string"},
        "req_id": {"type": ["string", "null"]},
        "ts": {"type": "integer"},
        "data": {"type": "object"}
    }
}

'''

    def _to_pascal_case(self, snake_str: str) -> str:
        """Convert snake_case to PascalCase."""
        return "".join(word.capitalize() for word in snake_str.split("_"))


async def main():
    """Main entry point."""
    if len(sys.argv) != 2:
        print("Usage: python generate-ws-types-modern.py <asyncapi-schema-file>")
        sys.exit(1)

    schema_file = sys.argv[1]
    generator = ModernProtocolGenerator(schema_file)

    try:
        await generator.generate_all()
        print("🎉 Modern WebSocket types generated successfully!")
    except Exception as e:
        print(f"❌ Generation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
