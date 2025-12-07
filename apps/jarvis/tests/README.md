# Jarvis E2E Testing

## Architecture

E2E tests run in **fully containerized Docker environment** - Playwright, web app, and server all run inside Docker with no host port exposure.

```
Test Run Lifecycle:

  make test
      │
      ▼
  docker compose -f docker-compose.test.yml run --rm playwright
      │
      ├── Builds jarvis-server, jarvis-web, playwright images
      ├── Starts jarvis-server (waits for healthy)
      ├── Starts jarvis-web (waits for healthy)
      ├── Runs playwright container against http://jarvis-web:8080 (internal network)
      │
      ▼
  Tests complete, exit code captured
      │
      ▼
  docker-compose down -v (cleanup)
```

## Key Principles

- **Zero host port exposure** - All services on internal Docker network
- **No hardcoded ports** - Ports configurable via environment variables
- **Complete isolation** - Tests don't depend on `make dev` or any host services
- **Docker-managed lifecycle** - `depends_on: condition: service_healthy` handles orchestration
- **Clean teardown** - Containers and volumes removed after each run

## Running Tests

```bash
# From apps/jarvis directory:
make test

# Or run directly:
docker compose -f docker-compose.test.yml run --rm playwright

# Clean up orphaned containers/images:
make test-clean
```

## Required Environment Variables

- `OPENAI_API_KEY` - Required for real API tests

## Configuration

All port configuration uses defaults with environment variable overrides:

| Variable           | Default | Description                   |
| ------------------ | ------- | ----------------------------- |
| `TEST_SERVER_PORT` | 8787    | Jarvis server port (internal) |
| `TEST_WEB_PORT`    | 8080    | Jarvis web port (internal)    |

## Test Files

- `history-hydration.e2e.spec.ts` - Tests conversation history persistence with real OpenAI API

## Debugging

```bash
# Build and start services without running tests:
docker compose -f docker-compose.test.yml up -d jarvis-server jarvis-web

# Check service health:
docker compose -f docker-compose.test.yml ps

# View logs:
docker compose -f docker-compose.test.yml logs -f jarvis-web
docker compose -f docker-compose.test.yml logs -f jarvis-server

# Run tests interactively:
docker compose -f docker-compose.test.yml run --rm playwright npx playwright test --debug

# Clean up:
docker compose -f docker-compose.test.yml down -v
```

## CI/CD

Tests designed for CI with:

- Automatic container lifecycle management
- Exit code propagation
- No reliance on host state
- Test results mounted as volumes for artifact collection
