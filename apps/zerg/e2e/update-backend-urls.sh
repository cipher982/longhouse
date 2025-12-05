#!/bin/bash
# Update all test files to use dynamic backend URLs

set -e

echo "Updating test files to use dynamic backend URLs..."

# Find all test files with hardcoded backend URLs
for file in tests/*.spec.ts tests/helpers/*.ts; do
  if [ -f "$file" ]; then
    # Check if file contains hardcoded URLs
    if grep -q "http://localhost:8001" "$file"; then
      echo "Updating $file..."
      
      # Replace direct API calls to use the backendUrl fixture
      sed -i.bak "s|await page\.request\.get('http://localhost:8001|await page.request.get(backendUrl + '|g" "$file"
      sed -i.bak "s|await page\.request\.post('http://localhost:8001|await page.request.post(backendUrl + '|g" "$file"
      sed -i.bak "s|await page\.request\.patch('http://localhost:8001|await page.request.patch(backendUrl + '|g" "$file"
      sed -i.bak "s|await page\.request\.put('http://localhost:8001|await page.request.put(backendUrl + '|g" "$file"
      sed -i.bak "s|await page\.request\.delete('http://localhost:8001|await page.request.delete(backendUrl + '|g" "$file"
      
      # Replace fetch calls
      sed -i.bak "s|fetch('http://localhost:8001|fetch(backendUrl + '|g" "$file"
      
      # Replace WebSocket URLs
      sed -i.bak "s|'ws://localhost:8001|backendUrl.replace('http', 'ws') + '|g" "$file"
      sed -i.bak "s|\"ws://localhost:8001|backendUrl.replace('http', 'ws') + '|g" "$file"
      
      # Replace string concatenations
      sed -i.bak "s|http://localhost:8001/|\" + backendUrl + \"/|g" "$file"
      
      # Clean up backup files
      rm -f "${file}.bak"
    fi
  fi
done

echo "✅ Updated all test files to use dynamic backend URLs"
echo ""
echo "Next steps:"
echo "1. Update test function signatures to include backendUrl parameter"
echo "2. Test the changes with: bun run test"