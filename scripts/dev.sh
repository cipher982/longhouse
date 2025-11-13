#!/bin/bash
# Unified development script for Zerg + Jarvis
# Handles proper startup and shutdown

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Track what we started
STARTED_ZERG=false
STARTED_JARVIS=false

cleanup() {
    echo ""
    echo -e "${YELLOW}🛑 Shutting down everything...${NC}"

    # Stop Jarvis first
    if [ "$STARTED_JARVIS" = true ]; then
        echo -e "${BLUE}  Stopping Jarvis...${NC}"
        cd apps/jarvis && make stop 2>/dev/null || true
        cd ../..
    fi

    # Stop Zerg Docker containers
    if [ "$STARTED_ZERG" = true ]; then
        echo -e "${BLUE}  Stopping Zerg containers...${NC}"
        make zerg-down 2>/dev/null || true
    fi

    echo -e "${GREEN}✅ All services stopped${NC}"
    exit 0
}

# Trap Ctrl+C and call cleanup
trap cleanup INT TERM

echo -e "${BLUE}🚀 Starting full development environment...${NC}"

# Start Zerg backend
echo -e "${BLUE}📦 Starting Zerg (Docker containers)...${NC}"
make zerg-up
STARTED_ZERG=true
echo -e "${GREEN}✅ Zerg running on port 47300${NC}"

# Start Jarvis
echo -e "${BLUE}🤖 Starting Jarvis (Voice Agent)...${NC}"
cd apps/jarvis
make start &
JARVIS_PID=$!
STARTED_JARVIS=true
cd ../..

# Wait for services to be ready
echo -e "${YELLOW}⏳ Waiting for services to be ready...${NC}"
sleep 3

# Check if everything is running
echo -e "${BLUE}📊 Service Status:${NC}"
cd apps/jarvis && make status
cd ../..

echo ""
echo -e "${GREEN}✅ Development environment ready!${NC}"
echo -e "${BLUE}   Zerg Backend:  http://localhost:47300${NC}"
echo -e "${BLUE}   Zerg Frontend: http://localhost:47200${NC}"
echo -e "${BLUE}   Jarvis UI:     http://localhost:8080${NC}"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop everything${NC}"
echo ""

# Keep script running and wait for Jarvis process
wait $JARVIS_PID
