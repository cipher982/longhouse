#!/usr/bin/env python3
"""
Generate type-safe API client from OpenAPI schema.

This prevents API endpoint mismatches by generating the client
from the single source of truth (api-schema.yml).

Note: This script now generates TypeScript clients only.
The Rust/WASM frontend has been retired.
"""

import yaml
import json
from pathlib import Path

def generate_typescript_api_client(openapi_spec):
    """Generate TypeScript API client from OpenAPI spec."""

    ts_code = """// GENERATED CODE - DO NOT EDIT
// Generated from api-schema.yml

export interface ApiClientConfig {
  baseUrl: string;
  headers?: Record<string, string>;
}

export class ApiClient {
  private baseUrl: string;
  private headers: Record<string, string>;

  constructor(config: ApiClientConfig) {
    this.baseUrl = config.baseUrl;
    this.headers = config.headers || {};
  }

  private async fetch<T>(url: string, method: string, body?: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl}${url}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...this.headers,
      },
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status} ${response.statusText}`);
    }

    return response.json();
  }

"""

    for path, methods in openapi_spec.get('paths', {}).items():
        for method, spec in methods.items():
            if method.upper() not in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH']:
                continue

            # Convert OpenAPI path to function name
            func_name = generate_function_name(path, method, spec)

            # Extract path parameters
            path_params = []
            for param in spec.get('parameters', []):
                if param.get('in') == 'path':
                    path_params.append(param['name'])

            # Generate function signature
            params = ', '.join([f'{p}: number' for p in path_params])

            # Build URL with template literals
            url_template = path
            for param in path_params:
                url_template = url_template.replace('{' + param + '}', '${' + param + '}')

            ts_code += f"""
  /** {spec.get('summary', 'API call')} */
  async {func_name}({params}): Promise<unknown> {{
    return this.fetch(`{url_template}`, '{method.upper()}');
  }}
"""

    ts_code += "}\n"
    return ts_code

def generate_function_name(path, method, spec):
    """Convert OpenAPI path to TypeScript function name."""
    # Remove /api/ prefix and convert to camelCase
    clean_path = path.replace('/api/', '').replace('/', '_').replace('{', '').replace('}', '')
    summary = spec.get('operationId', spec.get('summary', ''))

    if summary:
        # Convert to camelCase
        words = summary.lower().replace('-', '_').split('_')
        return words[0] + ''.join(word.capitalize() for word in words[1:])

    return f"{method.lower()}{clean_path.title().replace('_', '')}"

def main():
    """Generate API client from OpenAPI schema."""

    schema_path = Path(__file__).parent.parent / 'schemas' / 'api-schema.yml'
    output_path = Path(__file__).parent.parent / 'apps' / 'zerg' / 'frontend-web' / 'src' / 'generated' / 'api-client.ts'

    if not schema_path.exists():
        print(f"❌ OpenAPI schema not found: {schema_path}")
        return 1

    # Load OpenAPI schema
    with open(schema_path, 'r') as f:
        openapi_spec = yaml.safe_load(f)

    # Generate TypeScript client
    ts_code = generate_typescript_api_client(openapi_spec)

    # Write to file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(ts_code)

    print(f"✅ Generated API client: {output_path}")
    return 0

if __name__ == '__main__':
    exit(main())
