#!/usr/bin/env python3
"""
Complete Contract Generation from Schema-First Approach

This script generates:
1. Backend Python models
2. Validation functions
3. Mock data generators

Usage: python scripts/generate-complete-contracts.py
"""

import yaml
import json
from pathlib import Path
from typing import Dict, Any, List
from jinja2 import Template

class SchemaFirstGenerator:
    def __init__(self):
        self.openapi_schema = None
        self.asyncapi_schema = None

    def load_schemas(self):
        """Load all schema files"""
        with open('schemas/api-schema.yml') as f:
            self.openapi_schema = yaml.safe_load(f)
        with open('schemas/ws-protocol-asyncapi.yml') as f:
            self.asyncapi_schema = yaml.safe_load(f)

    def generate_backend_models(self):
        """Generate Python Pydantic models for backend"""
        schemas = self.openapi_schema['components']['schemas']

        python_code = '''
# AUTO-GENERATED - DO NOT EDIT
# Generated from api-schema.yml

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

'''

        for schema_name, schema_def in schemas.items():
            if schema_def.get('type') == 'object':
                python_code += self.generate_pydantic_model(schema_name, schema_def)

        Path('apps/zerg/backend/zerg/generated/api_models.py').write_text(python_code)
        print("✅ Generated apps/zerg/backend/zerg/generated/api_models.py")

    def generate_pydantic_model(self, name: str, schema: Dict[str, Any]) -> str:
        """Generate a Pydantic model from JSON schema"""
        required = schema.get('required', [])
        properties = schema.get('properties', {})

        model_code = f'''
class {name}(BaseModel):
'''

        for prop_name, prop_schema in properties.items():
            python_type = self.json_type_to_python(prop_schema)
            if prop_name not in required:
                python_type = f"Optional[{python_type}] = None"

            model_code += f"    {prop_name}: {python_type}\n"

        model_code += "\n"
        return model_code

    def json_type_to_python(self, schema: Dict[str, Any]) -> str:
        """Convert JSON schema type to Python type"""
        json_type = schema.get('type')
        format_type = schema.get('format')

        if '$ref' in schema:
            ref_name = schema['$ref'].split('/')[-1]
            return ref_name
        elif json_type == 'string':
            if format_type == 'date-time':
                return 'datetime'
            return 'str'
        elif json_type == 'integer':
            return 'int'
        elif json_type == 'number':
            return 'float'
        elif json_type == 'boolean':
            return 'bool'
        elif json_type == 'array':
            item_type = self.json_type_to_python(schema['items'])
            return f'List[{item_type}]'
        elif json_type == 'object':
            if schema.get('additionalProperties'):
                return 'Dict[str, Any]'
            return 'Dict[str, Any]'
        else:
            return 'Any'

    def run(self):
        """Run the complete generation process"""
        print("🚀 Starting schema-first contract generation...")

        self.load_schemas()
        print("📖 Loaded schemas")

        # Ensure output directories exist
        Path('apps/zerg/backend/zerg/generated').mkdir(exist_ok=True)

        self.generate_backend_models()

        print("✅ Schema-first generation complete!")
        print("\n📋 Next steps:")
        print("1. Update your code to import from generated contracts")
        print("2. Update backend to use generated Pydantic models")

if __name__ == "__main__":
    generator = SchemaFirstGenerator()
    generator.run()
