#!/bin/bash
# Validate Swarm Platform setup
# Checks all dependencies, configuration, and file structure

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo ""
echo "🔍 Swarm Platform Setup Validation"
echo "===================================="
echo ""

ERRORS=0
WARNINGS=0

# Helper functions
check_pass() {
    echo -e "  ${GREEN}✓${NC} $1"
}

check_warn() {
    echo -e "  ${YELLOW}⚠${NC} $1"
    ((WARNINGS++))
}

check_fail() {
    echo -e "  ${RED}✗${NC} $1"
    ((ERRORS++))
}

check_info() {
    echo -e "  ${BLUE}ℹ${NC} $1"
}

# 1. Check directory structure
echo "1. Directory Structure"
echo "----------------------"

if [ -d "apps/jarvis" ]; then
    check_pass "apps/jarvis/ exists"
else
    check_fail "apps/jarvis/ missing"
fi

if [ -d "apps/zerg" ]; then
    check_pass "apps/zerg/ exists"
else
    check_fail "apps/zerg/ missing"
fi

if [ -d "packages/config" ]; then
    check_pass "packages/config/ exists"
else
    check_fail "packages/config/ missing"
fi

# 2. Check dependencies
echo ""
echo "2. Dependencies"
echo "---------------"

if command -v node &> /dev/null; then
    NODE_VERSION=$(node --version)
    check_pass "Node.js installed ($NODE_VERSION)"
else
    check_fail "Node.js not found - install Node.js 18+"
fi

if command -v bun &> /dev/null; then
    BUN_VERSION=$(bun --version)
    check_pass "Bun installed ($BUN_VERSION)"
else
    check_fail "Bun not found - install with: curl -fsSL https://bun.sh/install | bash"
fi

if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version)
    check_pass "Python installed ($PYTHON_VERSION)"
else
    check_fail "Python 3 not found - install Python 3.11+"
fi

if command -v uv &> /dev/null; then
    UV_VERSION=$(uv --version)
    check_pass "uv installed ($UV_VERSION)"
else
    check_warn "uv not found - install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

if [ -f "bun.lock" ] && [ -d "node_modules" ]; then
    check_pass "Bun dependencies installed"
else
    check_warn "Dependencies not installed - run: bun install"
fi

if [ -d "apps/zerg/frontend-web/src/jarvis" ]; then
    check_pass "Jarvis UI present (unified SPA)"
else
    check_fail "Jarvis UI missing (expected apps/zerg/frontend-web/src/jarvis)"
fi

if [ -f "apps/zerg/backend/.venv/bin/python" ] || [ -f "apps/zerg/backend/uv.lock" ]; then
    check_pass "Zerg backend dependencies configured"
else
    check_warn "Zerg dependencies not installed - run: cd apps/zerg/backend && uv sync"
fi

# 3. Check configuration
echo ""
echo "3. Configuration"
echo "----------------"

if [ -f ".env" ]; then
    check_pass ".env file exists"

    # Check critical variables
    if grep -q "JARVIS_DEVICE_SECRET" .env && ! grep -q "JARVIS_DEVICE_SECRET=\"\"" .env; then
        check_pass "JARVIS_DEVICE_SECRET configured"
    else
        check_fail "JARVIS_DEVICE_SECRET not set in .env"
    fi

    if grep -q "OPENAI_API_KEY.*sk-" .env; then
        check_pass "OPENAI_API_KEY configured"
    else
        check_warn "OPENAI_API_KEY not set or invalid in .env"
    fi

    if grep -q "JWT_SECRET" .env && ! grep -q "JWT_SECRET=\"dev-secret\"" .env; then
        check_pass "JWT_SECRET configured"
    else
        check_warn "JWT_SECRET is default value - change for production"
    fi

    if grep -q "DATABASE_URL" .env; then
        check_pass "DATABASE_URL configured"
    else
        check_fail "DATABASE_URL not set in .env"
    fi

else
    check_fail ".env file missing - copy from .env.example.swarm"
fi

# 4. Check key files
echo ""
echo "4. Key Files"
echo "------------"

KEY_FILES=(
    "apps/zerg/backend/zerg/routers/jarvis.py"
    "apps/jarvis/packages/core/src/jarvis-api-client.ts"
    "apps/zerg/frontend-web/src/pages/JarvisChatPage.tsx"
    "apps/zerg/frontend-web/src/jarvis/lib/state-manager.ts"
    "apps/zerg/backend/scripts/seed_jarvis_agents.py"
    "scripts/test-jarvis-integration.sh"
    "docs/completed/jarvis_integration.md"
    "docs/DEPLOYMENT.md"
)

for file in "${KEY_FILES[@]}"; do
    if [ -f "$file" ]; then
        check_pass "$(basename $file)"
    else
        check_fail "$file missing"
    fi
done

# 5. Check database
echo ""
echo "5. Database"
echo "-----------"

if [ -f "apps/zerg/backend/app.db" ]; then
    check_pass "SQLite database exists"

    # Check for tables
    TABLES=$(sqlite3 apps/zerg/backend/app.db ".tables" 2>/dev/null || echo "")
    if echo "$TABLES" | grep -q "agent_runs"; then
        check_pass "agent_runs table exists"
    else
        check_warn "agent_runs table missing - run: cd apps/zerg/backend && uv run alembic upgrade head"
    fi

    if echo "$TABLES" | grep -q "agents"; then
        check_pass "agents table exists"
    else
        check_warn "agents table missing - run migrations"
    fi
else
    check_info "Database not initialized - will be created on first run"
fi

# 6. Check migrations
echo ""
echo "6. Migrations"
echo "-------------"

if [ -f "apps/zerg/backend/alembic/versions/a1b2c3d4e5f6_add_summary_to_agent_run.py" ]; then
    check_pass "AgentRun.summary migration exists"
else
    check_fail "Summary column migration missing"
fi

MIGRATION_COUNT=$(ls apps/zerg/backend/alembic/versions/*.py 2>/dev/null | wc -l | tr -d ' ')
check_info "Found $MIGRATION_COUNT migration files"

# Summary
echo ""
echo "===================================="
echo "Summary"
echo "===================================="

if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}✅ All checks passed!${NC}"
    echo ""
    echo "Your Swarm Platform is properly configured."
    echo ""
    echo "Next steps:"
    echo "  1. Run migrations: cd apps/zerg/backend && uv run alembic upgrade head"
    echo "  2. Seed agents: make seed-jarvis-agents"
    echo "  3. Start platform: make swarm-dev"
    echo "  4. Test integration: ./scripts/test-jarvis-integration.sh"
elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}⚠ Setup has $WARNINGS warning(s)${NC}"
    echo ""
    echo "Platform will work but some features may not be optimal."
    echo "Review warnings above and fix before production deployment."
else
    echo -e "${RED}✗ Setup has $ERRORS error(s) and $WARNINGS warning(s)${NC}"
    echo ""
    echo "Fix errors above before starting the platform."
    echo ""
    echo "Quick fixes:"
    echo "  - Missing .env: cp .env.example.swarm .env && nano .env"
    echo "  - Missing deps: bun install"
    echo "  - Python deps: cd apps/zerg/backend && uv sync"
    exit 1
fi

echo ""
