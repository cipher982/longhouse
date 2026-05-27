#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = REPO / "openapi.json"
DEFAULT_OUTPUT = REPO / "ios" / "Sources" / "Shared" / "Generated" / "SessionAPI.generated.swift"

ROOT_SCHEMAS = [
    "TimelineSessionsListResponse",
    "TimelineSessionCardResponse",
    "SessionResponse",
    "SessionThreadResponse",
    "SessionWorkspaceResponse",
    "SessionProjectionResponse",
    "SessionProjectionItemResponse",
    "EventResponse",
    "SessionInputResponse",
    "QueuedInputSummary",
    "SessionDraftReplyResponse",
    "SessionLoopModeResponse",
    "SessionTurnsListResponse",
    "SessionTurnResponse",
    "SessionTurnTimingResponse",
]

SWIFT_RESERVED = {
    "associatedtype",
    "class",
    "deinit",
    "enum",
    "extension",
    "fileprivate",
    "func",
    "import",
    "init",
    "inout",
    "internal",
    "let",
    "open",
    "operator",
    "private",
    "protocol",
    "public",
    "static",
    "struct",
    "subscript",
    "typealias",
    "var",
    "break",
    "case",
    "continue",
    "default",
    "defer",
    "do",
    "else",
    "fallthrough",
    "for",
    "guard",
    "if",
    "in",
    "repeat",
    "return",
    "switch",
    "where",
    "while",
    "as",
    "Any",
    "catch",
    "false",
    "is",
    "nil",
    "rethrows",
    "super",
    "self",
    "Self",
    "throw",
    "throws",
    "true",
    "try",
}


def swift_type_name(schema_name: str) -> str:
    return f"API{schema_name}"


def camel_case(name: str) -> str:
    parts = [part for part in name.split("_") if part]
    if not parts:
        return name
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def escape_identifier(name: str) -> str:
    if name in SWIFT_RESERVED:
        return f"`{name}`"
    return name


def ref_name(ref: str) -> str:
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix):
        raise ValueError(f"unsupported ref: {ref}")
    return ref[len(prefix) :]


def nullable_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    variants = schema.get("anyOf")
    if not isinstance(variants, list):
        return schema, False
    non_null = [item for item in variants if item.get("type") != "null"]
    if len(non_null) == 1 and len(non_null) != len(variants):
        return non_null[0], True
    return schema, False


def referenced_schema_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        if "$ref" in schema:
            names.add(ref_name(schema["$ref"]))
        for value in schema.values():
            names.update(referenced_schema_names(value))
    elif isinstance(schema, list):
        for item in schema:
            names.update(referenced_schema_names(item))
    return names


def dependency_closure(components: dict[str, Any], roots: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def visit(name: str) -> None:
        if name in seen:
            return
        if name not in components:
            raise KeyError(f"OpenAPI schema {name!r} not found")
        seen.add(name)
        for dep in sorted(referenced_schema_names(components[name])):
            visit(dep)
        ordered.append(name)

    for root in roots:
        visit(root)
    return ordered


def swift_type(schema: dict[str, Any]) -> str:
    schema, nullable = nullable_schema(schema)
    if "$ref" in schema:
        value = swift_type_name(ref_name(schema["$ref"]))
    elif schema.get("type") == "array":
        value = f"[{swift_type(schema.get('items', {'type': 'object'}))}]"
    elif schema.get("type") == "object" and "additionalProperties" in schema:
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            value = f"[String: {swift_type(additional)}]"
        else:
            value = "[String: JSONValue]"
    elif schema.get("type") == "object" and not schema.get("properties"):
        value = "[String: JSONValue]"
    elif schema.get("type") == "integer":
        value = "Int"
    elif schema.get("type") == "number":
        value = "Double"
    elif schema.get("type") == "boolean":
        value = "Bool"
    elif schema.get("type") == "string":
        value = "String"
    else:
        value = "JSONValue"
    return f"{value}?" if nullable else value


def _swift_enum_case(value: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", value)
    parts = [part for part in parts if part]
    if not parts:
        return "_unknown"
    head = parts[0].lower()
    tail = "".join(part[:1].upper() + part[1:].lower() for part in parts[1:])
    case = head + tail
    if case[:1].isdigit():
        case = "_" + case
    return escape_identifier(case)


def render_struct(name: str, schema: dict[str, Any]) -> str:
    enum_values = schema.get("enum") if schema.get("type") == "string" else None
    if isinstance(enum_values, list) and enum_values:
        type_name = swift_type_name(name)
        lines = [f"enum {type_name}: String, Codable, Hashable, Sendable, CaseIterable {{"]
        for value in enum_values:
            text = str(value)
            lines.append(f"    case {_swift_enum_case(text)} = \"{text}\"")
        lines.append("}")
        return "\n".join(lines)

    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    lines = [f"struct {swift_type_name(name)}: Codable, Hashable, Sendable {{"]
    if not properties:
        lines.append("    let value: [String: JSONValue]?")
    for json_name, prop_schema in properties.items():
        prop_type = swift_type(prop_schema)
        if json_name not in required and not prop_type.endswith("?"):
            prop_type += "?"
        prop_name = escape_identifier(camel_case(json_name))
        lines.append(f"    let {prop_name}: {prop_type}")
    lines.append("}")
    return "\n".join(lines)


def render_file(schema: dict[str, Any], roots: list[str]) -> str:
    components = schema["components"]["schemas"]
    names = dependency_closure(components, roots)
    body = "\n\n".join(render_struct(name, components[name]) for name in names)
    return f"""// @generated from openapi.json by scripts/generate/ios_api_models.py
// Do not edit by hand.
//
// The generated event DTOs intentionally reuse the hand-written JSONValue type.
// Decode with JSONDecoder.snakeCase so tool_input_json payload keys are preserved.

import Foundation

{body}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Swift DTOs for the iOS session API from OpenAPI.")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Fail if the generated file is stale.")
    args = parser.parse_args()

    schema = json.loads(args.schema.read_text())
    rendered = render_file(schema, ROOT_SCHEMAS)

    if args.check:
        current = args.output.read_text() if args.output.exists() else ""
        if current != rendered:
            print(f"{args.output} is stale; run scripts/generate/ios_api_models.py", file=sys.stderr)
            return 1
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
