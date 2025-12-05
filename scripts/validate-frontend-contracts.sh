#!/bin/bash
set -e

# Frontend Contract Validation Script
# This script should be run before any commit that touches frontend code
# It ensures API contracts are valid and prevents runtime failures

echo "🔍 Frontend Contract Validation Starting..."
echo "═══════════════════════════════════════════════════════════════"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

FRONTEND_DIR="frontend-web"
SCHEMA_FILE="openapi.json"
VALIDATION_FAILED=0

cd "$(dirname "$0")/.."

# Step 1: Check if OpenAPI schema exists and is recent
echo -e "${BLUE}📋 Step 1: OpenAPI Schema Freshness${NC}"
if [ ! -f "$SCHEMA_FILE" ]; then
    echo -e "${RED}❌ OpenAPI schema not found: $SCHEMA_FILE${NC}"
    exit 1
fi

# Check if schema is older than backend code
SCHEMA_AGE=$(find "$SCHEMA_FILE" -mtime +1 2>/dev/null | wc -l || echo 0)
if [ "$SCHEMA_AGE" -gt 0 ]; then
    echo -e "${YELLOW}⚠️  OpenAPI schema is more than 1 day old${NC}"
    echo "   Consider regenerating with: cd backend && uv run python -c \"from zerg.main import app; ...\" "
fi

# Step 2: Validate schema completeness
echo -e "${BLUE}📋 Step 2: Schema Completeness Check${NC}"

# Count endpoints with empty schemas
EMPTY_SCHEMAS=$(grep -c '"schema": {}' "$SCHEMA_FILE" 2>/dev/null || echo 0)
if [ "$EMPTY_SCHEMAS" -gt 0 ]; then
    echo -e "${RED}❌ Found $EMPTY_SCHEMAS endpoints with empty response schemas${NC}"
    echo "   This prevents TypeScript type generation and contract validation"

    # Show which endpoints have empty schemas
    echo "   Endpoints with empty schemas:"
    grep -B 10 '"schema": {}' "$SCHEMA_FILE" | grep '"/' | tail -5 | sed 's/^/     /'

    VALIDATION_FAILED=1
fi

# Step 3: Check TypeScript type generation
echo -e "${BLUE}📋 Step 3: TypeScript Type Generation${NC}"
cd "$FRONTEND_DIR"

# Regenerate types from current schema
echo "   Regenerating types from OpenAPI schema..."
bun run generate:api > /dev/null 2>&1

# Check if types file was created successfully
if [ ! -f "src/generated/openapi-types.ts" ]; then
    echo -e "${RED}❌ Failed to generate TypeScript types${NC}"
    VALIDATION_FAILED=1
else
    echo -e "${GREEN}✅ TypeScript types generated successfully${NC}"

    # Check for 'unknown' types which indicate schema gaps
    UNKNOWN_TYPES=$(grep -c ": unknown" src/generated/openapi-types.ts 2>/dev/null || echo 0)
    if [ "$UNKNOWN_TYPES" -gt 10 ]; then
        echo -e "${YELLOW}⚠️  High number of 'unknown' types ($UNKNOWN_TYPES) - schema may be incomplete${NC}"
    fi
fi

# Step 4: TypeScript compilation check
echo -e "${BLUE}📋 Step 4: TypeScript Compilation${NC}"
if bun run validate:types > /dev/null 2>&1; then
    echo -e "${GREEN}✅ TypeScript compilation passed${NC}"
else
    echo -e "${RED}❌ TypeScript compilation failed${NC}"
    echo "   Run 'bun run validate:types' to see detailed errors"
    VALIDATION_FAILED=1
fi

# Step 5: Test API usage in components
echo -e "${BLUE}📋 Step 5: Component API Usage Validation${NC}"

# Look for potential contract violations in component code
HARDCODED_TYPES=$(grep -r "interface.*Summary\|interface.*Agent\|: {" src/pages/ | grep -v ".test." | wc -l)
if [ "$HARDCODED_TYPES" -gt 5 ]; then
    echo -e "${YELLOW}⚠️  Found $HARDCODED_TYPES local type definitions - consider using generated types${NC}"
fi

# Check for API calls without proper error handling
UNHANDLED_APIS=$(grep -r "fetch(" src/ | grep -v "catch\|onError" | wc -l)
if [ "$UNHANDLED_APIS" -gt 0 ]; then
    echo -e "${YELLOW}⚠️  Found $UNHANDLED_APIS API calls without error handling${NC}"
fi

# Step 6: Run unit tests
echo -e "${BLUE}📋 Step 6: Unit Test Validation${NC}"
if bun run test -- --run > /dev/null 2>&1; then
    echo -e "${GREEN}✅ All unit tests passed${NC}"
else
    echo -e "${RED}❌ Unit tests failed${NC}"
    echo "   Run 'bun run test' to see detailed errors"
    VALIDATION_FAILED=1
fi

cd ..

# Final report
echo ""
echo "═══════════════════════════════════════════════════════════════"
if [ "$VALIDATION_FAILED" -eq 1 ]; then
    echo -e "${RED}❌ Frontend contract validation FAILED${NC}"
    echo "   🚫 Commit blocked - fix issues above before proceeding"
    exit 1
else
    echo -e "${GREEN}✅ Frontend contract validation PASSED${NC}"
    echo "   🚀 Safe to commit and deploy"
fi
echo "═══════════════════════════════════════════════════════════════"