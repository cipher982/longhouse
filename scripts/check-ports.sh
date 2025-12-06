#!/bin/bash
# Simple port verification script - just checks if ports are free
# DOES NOT modify .env file

set -e

ENV_FILE=".env"

# Load current .env values
if [[ ! -f "$ENV_FILE" ]]; then
    echo "❌ .env file not found. Please create one based on .env.example"
    exit 1
fi

# Load and extract current port values
source "$ENV_FILE" 2>/dev/null || true

# Get port values with fallback defaults
BACKEND_PORT=${BACKEND_PORT:-8001}
FRONTEND_PORT=${FRONTEND_PORT:-8002}

echo "🔍 Checking port availability..."
echo "  Backend:  $BACKEND_PORT"
echo "  Frontend: $FRONTEND_PORT"

# Function to check if a port is in use
check_port() {
    local port=$1
    if lsof -ti:$port >/dev/null 2>&1; then
        return 0  # Port is in use
    else
        return 1  # Port is free
    fi
}

# Check backend port
if check_port $BACKEND_PORT; then
    echo "❌ Backend port $BACKEND_PORT is already in use!"
    echo "   Please stop the process using it or update BACKEND_PORT in .env"
    exit 1
fi

# Check frontend port
if check_port $FRONTEND_PORT; then
    echo "❌ Frontend port $FRONTEND_PORT is already in use!"
    echo "   Please stop the process using it or update FRONTEND_PORT in .env"
    exit 1
fi

echo "✅ Both ports are available!"
echo "  Backend:  $BACKEND_PORT ✓"
echo "  Frontend: $FRONTEND_PORT ✓"
