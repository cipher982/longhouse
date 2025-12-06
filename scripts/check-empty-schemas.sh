#!/bin/bash
set -e

# Check for empty OpenAPI response schemas that break type generation

EMPTY_COUNT=$(grep -c '"schema": {}' openapi.json || echo 0)

echo "🔍 Checking OpenAPI schema completeness..."
echo "📊 Found $EMPTY_COUNT endpoints with empty response schemas"

if [ "$EMPTY_COUNT" -gt 5 ]; then
    echo "❌ Too many empty response schemas ($EMPTY_COUNT) - type safety compromised"
    echo ""
    echo "🔍 Endpoints with empty schemas:"
    python3 -c "
import json
schema = json.load(open('openapi.json'))
count = 0
for path, methods in schema['paths'].items():
    for method, details in methods.items():
        if isinstance(details, dict) and 'responses' in details:
            resp_200 = details['responses'].get('200', {})
            json_content = resp_200.get('content', {}).get('application/json', {})
            if json_content.get('schema') == {}:
                print(f'   {method.upper()} {path}')
                count += 1
                if count >= 10:  # Limit output
                    print('   ... and more')
                    break
"
    echo ""
    echo "💡 Add response_model=SomeModel to these endpoints in backend/zerg/routers/"
    exit 1
fi

echo "✅ Schema completeness check passed ($EMPTY_COUNT empty schemas within tolerance)"
