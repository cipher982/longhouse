#!/bin/bash
# BuildKit optimization setup script
# Sets up Docker BuildKit with advanced caching and multi-platform support

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

# Check if Docker is running
if ! docker info >/dev/null 2>&1; then
    log_error "Docker is not running. Please start Docker and try again."
    exit 1
fi

log_info "Setting up Docker BuildKit with optimizations..."

# Enable BuildKit (Docker 20.10+)
export DOCKER_BUILDKIT=1
export BUILDKIT_PROGRESS=plain

log_info "Checking Docker version..."
DOCKER_VERSION=$(docker version --format '{{.Server.Version}}')
log_info "Docker version: $DOCKER_VERSION"

# Create buildx builder with advanced features if it doesn't exist
BUILDER_NAME="zerg-builder"

if ! docker buildx ls | grep -q "$BUILDER_NAME"; then
    log_info "Creating new buildx builder: $BUILDER_NAME"
    docker buildx create \
        --name "$BUILDER_NAME" \
        --driver docker-container \
        --driver-opt network=host \
        --use
else
    log_info "Using existing buildx builder: $BUILDER_NAME"
    docker buildx use "$BUILDER_NAME"
fi

# Bootstrap the builder
log_info "Bootstrapping buildx builder..."
docker buildx inspect --bootstrap

# Create cache directories
CACHE_DIR="/tmp/docker-cache"
mkdir -p "$CACHE_DIR"/{backend,frontend}
log_info "Created cache directories at $CACHE_DIR"

# Set up build cache configuration
cat > docker-compose.cache.yml << EOF
# Cache configuration for docker-compose
# Use with: docker-compose -f docker-compose.yml -f docker-compose.cache.yml build

version: '3.8'

services:
  backend:
    build:
      cache_from:
        - type=local,src=$CACHE_DIR/backend
        - type=registry,ref=\${REGISTRY:-ghcr.io/your-org/zerg}/backend:buildcache
      cache_to:
        - type=local,dest=$CACHE_DIR/backend,mode=max
        - type=registry,ref=\${REGISTRY:-ghcr.io/your-org/zerg}/backend:buildcache,mode=max

  frontend:
    build:
      cache_from:
        - type=local,src=$CACHE_DIR/frontend
        - type=registry,ref=\${REGISTRY:-ghcr.io/your-org/zerg}/frontend:buildcache
      cache_to:
        - type=local,dest=$CACHE_DIR/frontend,mode=max
        - type=registry,ref=\${REGISTRY:-ghcr.io/your-org/zerg}/frontend:buildcache,mode=max
EOF

log_info "Created docker-compose.cache.yml for advanced caching"

# Create build script with metrics
cat > build-with-metrics.sh << 'EOF'
#!/bin/bash
# Build script with performance metrics

set -euo pipefail

SERVICE=${1:-all}
TARGET=${2:-production}

log_info() {
    echo -e "\033[0;32m[INFO]\033[0m $1"
}

start_time=$(date +%s)

case "$SERVICE" in
    "backend"|"all")
        log_info "Building backend (target: $TARGET)..."
        backend_start=$(date +%s)

        docker buildx build \
            --target "$TARGET" \
            --cache-from type=local,src=/tmp/docker-cache/backend \
            --cache-to type=local,dest=/tmp/docker-cache/backend,mode=max \
            --progress=plain \
            -f docker/backend.dockerfile \
            -t zerg-backend:latest \
            backend/

        backend_end=$(date +%s)
        backend_duration=$((backend_end - backend_start))
        log_info "Backend build completed in ${backend_duration}s"
        ;;
esac

case "$SERVICE" in
    "frontend"|"all")
        log_info "Building frontend (target: $TARGET)..."
        frontend_start=$(date +%s)

        docker buildx build \
            --target "$TARGET" \
            --cache-from type=local,src=/tmp/docker-cache/frontend \
            --cache-to type=local,dest=/tmp/docker-cache/frontend,mode=max \
            --progress=plain \
            -f docker/frontend.dockerfile \
            -t zerg-frontend:latest \
            frontend/

        frontend_end=$(date +%s)
        frontend_duration=$((frontend_end - frontend_start))
        log_info "Frontend build completed in ${frontend_duration}s"
        ;;
esac

end_time=$(date +%s)
total_duration=$((end_time - start_time))
log_info "Total build time: ${total_duration}s"
EOF

chmod +x build-with-metrics.sh

log_info "Created build-with-metrics.sh script"

# Create cache cleanup script
cat > cleanup-cache.sh << 'EOF'
#!/bin/bash
# Docker cache cleanup script

set -euo pipefail

log_info() {
    echo -e "\033[0;32m[INFO]\033[0m $1"
}

log_info "Cleaning up Docker build cache..."

# Clean BuildKit cache
docker buildx prune -f

# Clean local cache directories
rm -rf /tmp/docker-cache/*

# Clean unused Docker images
docker image prune -f

# Clean unused volumes
docker volume prune -f

log_info "Cache cleanup completed"
EOF

chmod +x cleanup-cache.sh

log_info "Created cleanup-cache.sh script"

# Test BuildKit functionality
log_info "Testing BuildKit functionality..."
docker buildx ls

log_info "BuildKit setup completed successfully!"
log_info "Available commands:"
log_info "  - ./build-with-metrics.sh [backend|frontend|all] [development|production]"
log_info "  - ./cleanup-cache.sh"
log_info "  - docker-compose -f docker-compose.dev.yml -f docker-compose.cache.yml up"

# Show cache usage
if [ -d "$CACHE_DIR" ]; then
    CACHE_SIZE=$(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1 || echo "0B")
    log_info "Current cache size: $CACHE_SIZE"
fi
