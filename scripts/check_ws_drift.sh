#!/bin/bash
# Check for meaningful WebSocket contract drift (exclude timestamp-only changes)

# List of files to check (only those that exist)
FILES=""
for file in "apps/zerg/backend/zerg/generated/ws_messages.py" "apps/zerg/frontend-web/src/generated/ws_messages.ts"; do
  if git ls-files --error-unmatch "$file" >/dev/null 2>&1; then
    FILES="$FILES $file"
  fi
done

# If no files to check, exit successfully
if [ -z "$FILES" ]; then
  exit 0
fi

# Get diff output and filter out timestamp lines
DIFF_OUTPUT=$(git diff --ignore-space-at-eol $FILES)

# Check if there are any changes beyond timestamps
MEANINGFUL_LINES=$(echo "$DIFF_OUTPUT" | \
  grep -E "^[+-]" | \
  grep -v "^[+-]{3}" | \
  grep -v "Generated from ws-protocol-asyncapi.yml at" | \
  grep -v "generated_at" | \
  grep -v "^[+-] # Generated from" | \
  grep -v "^[+-]// Generated from")

if [ -n "$MEANINGFUL_LINES" ]; then
  echo ""
  echo "❌ WebSocket contract drift – commit the generated changes"
  echo "Meaningful changes detected:"
  echo "$MEANINGFUL_LINES"
  exit 1
fi
