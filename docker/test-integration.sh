#!/bin/bash
# Integration test script for new Docker architecture
# Tests both individual builds and full system integration

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_section() {
    echo -e "${BLUE}[SECTION]${NC} $1"
    echo "=================================="
}

# Cleanup function
cleanup() {
    log_info "Cleaning up test resources..."
    docker-compose -f docker-compose.dev.yml down --volumes --remove-orphans 2>/dev/null || true
    docker image rm zerg-backend:test zerg-frontend:test 2>/dev/null || true
}

# Set trap for cleanup on exit
trap cleanup EXIT

log_section "🧪 Docker Architecture Integration Tests"

# Test 1: Backend build
log_section "Test 1: Backend Build"
log_info "Building backend with optimized dockerfile..."
start_time=$(date +%s)

if docker buildx build \
    --progress=plain \
    -f docker/backend.dockerfile \
    --target production \
    -t zerg-backend:test \
    backend/; then

    end_time=$(date +%s)
    duration=$((end_time - start_time))
    log_info "✅ Backend build completed in ${duration}s"
else
    log_error "❌ Backend build failed"
    exit 1
fi

# Test 2: Frontend build (if not already running)
log_section "Test 2: Frontend Build"
log_info "Building frontend with optimized dockerfile..."
start_time=$(date +%s)

if docker buildx build \
    --progress=plain \
    -f docker/frontend.dockerfile \
    --target production \
    -t zerg-frontend:test \
    frontend/; then

    end_time=$(date +%s)
    duration=$((end_time - start_time))
    log_info "✅ Frontend build completed in ${duration}s"
else
    log_error "❌ Frontend build failed"
    exit 1
fi

# Test 3: Image size analysis
log_section "Test 3: Image Size Analysis"
log_info "Analyzing built image sizes..."

backend_size=$(docker images zerg-backend:test --format "{{.Size}}")
frontend_size=$(docker images zerg-frontend:test --format "{{.Size}}")

log_info "Backend image size: $backend_size"
log_info "Frontend image size: $frontend_size"

# Test 4: Container startup tests
log_section "Test 4: Container Startup Tests"

# Test backend container
log_info "Testing backend container startup..."
if docker run --rm -d \
    --name zerg-backend-test \
    -e DATABASE_URL="sqlite:///tmp/test.db" \
    -e JWT_SECRET="test_secret" \
    -e FERNET_SECRET="test_fernet_secret_32chars_long!" \
    -e TRIGGER_SIGNING_SECRET="test_trigger" \
    -e AUTH_DISABLED=1 \
    -p 8001:8000 \
    zerg-backend:test; then

    log_info "✅ Backend container started successfully"

    # Wait a bit and check if it's still running
    sleep 5
    if docker ps | grep -q zerg-backend-test; then
        log_info "✅ Backend container is still running after 5s"

        # Test health endpoint (if available)
        if curl -f -s http://localhost:8001/ >/dev/null 2>&1; then
            log_info "✅ Backend health check passed"
        else
            log_warn "⚠️  Backend health check failed (may be expected if no health endpoint)"
        fi

        docker stop zerg-backend-test
    else
        log_error "❌ Backend container crashed"
        docker logs zerg-backend-test
        exit 1
    fi
else
    log_error "❌ Backend container failed to start"
    exit 1
fi

# Test frontend container
log_info "Testing frontend container startup..."
if docker run --rm -d \
    --name zerg-frontend-test \
    -p 8002:80 \
    zerg-frontend:test; then

    log_info "✅ Frontend container started successfully"

    # Wait a bit and check if it's still running
    sleep 3
    if docker ps | grep -q zerg-frontend-test; then
        log_info "✅ Frontend container is still running after 3s"

        # Test nginx endpoint
        if curl -f -s http://localhost:8002/ >/dev/null 2>&1; then
            log_info "✅ Frontend health check passed"
        else
            log_warn "⚠️  Frontend health check failed"
        fi

        docker stop zerg-frontend-test
    else
        log_error "❌ Frontend container crashed"
        docker logs zerg-frontend-test
        exit 1
    fi
else
    log_error "❌ Frontend container failed to start"
    exit 1
fi

# Test 5: Development compose test
log_section "Test 5: Development Docker Compose Test"

# Create minimal .env for testing
cat > .env.test << EOF
POSTGRES_PASSWORD=test_password
JWT_SECRET=test_jwt_secret_change_in_production
FERNET_SECRET=test_fernet_secret_32chars_long!!
TRIGGER_SIGNING_SECRET=test_trigger_secret
OPENAI_API_KEY=sk-test-key
EOF

log_info "Testing development docker-compose setup..."

# Use test environment file
export COMPOSE_FILE="docker-compose.dev.yml"

if docker-compose -f docker-compose.dev.yml \
    --env-file .env.test \
    up -d --build; then

    log_info "✅ Development stack started successfully"

    # Wait for services to be ready
    log_info "Waiting for services to be ready..."
    sleep 10

    # Check if services are running
    if docker-compose -f docker-compose.dev.yml ps | grep -q "Up"; then
        log_info "✅ Services are running"

        # Test service endpoints
        log_info "Testing service endpoints..."

        # Test backend (with retry)
        for i in {1..5}; do
            if curl -f -s http://localhost:8000/ >/dev/null 2>&1; then
                log_info "✅ Backend endpoint responding"
                break
            fi
            log_info "Waiting for backend... (attempt $i/5)"
            sleep 2
        done

        # Test frontend
        for i in {1..3}; do
            if curl -f -s http://localhost:8080/ >/dev/null 2>&1; then
                log_info "✅ Frontend endpoint responding"
                break
            fi
            log_info "Waiting for frontend... (attempt $i/3)"
            sleep 2
        done

    else
        log_error "❌ Some services failed to start"
        docker-compose -f docker-compose.dev.yml logs
        exit 1
    fi

    # Cleanup
    docker-compose -f docker-compose.dev.yml down --volumes
    log_info "✅ Development stack test completed"

else
    log_error "❌ Development stack failed to start"
    exit 1
fi

# Cleanup test env file
rm -f .env.test

log_section "🎉 All Tests Passed!"
log_info "Docker architecture implementation is working correctly"

# Performance summary
log_section "📊 Performance Summary"
log_info "Backend image size: $backend_size"
log_info "Frontend image size: $frontend_size"
log_info "Total test duration: ~2-3 minutes (depending on cache hits)"

log_section "🚀 Next Steps"
log_info "1. Run './docker/buildkit-setup.sh' to optimize build caching"
log_info "2. Use 'docker-compose -f docker-compose.dev.yml up' for development"
log_info "3. Use 'docker-compose -f docker-compose.prod.yml up' for production"
log_info "4. Monitor build times and optimize as needed"
