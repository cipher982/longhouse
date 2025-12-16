# Jarvis E2E Testing

## Architecture

E2E tests run in **fully containerized Docker environment** - Playwright, web app, and server all run inside Docker with no host port exposure.

```
Test Run Lifecycle:

  make test
      │
      ▼
  docker compose -f docker-compose.test.yml build && run --rm playwright
      │
      ├── Builds zerg-backend, jarvis-web, playwright images
      ├── Starts zerg-backend (waits for healthy)
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
docker compose -f docker-compose.test.yml build && \
docker compose -f docker-compose.test.yml run --rm playwright

# Run specific test file:
docker compose -f docker-compose.test.yml run --rm playwright \
  npx playwright test history-hydration

# Clean up orphaned containers/images:
make test-clean
```

## Required Environment Variables

- `OPENAI_API_KEY` - Required for zerg-backend API endpoints (must be set)

## Configuration

### Port Configuration

| Variable        | Default | Description                  |
| --------------- | ------- | ---------------------------- |
| `TEST_WEB_PORT` | 8080    | Jarvis web port (internal)   |
| `BACKEND_PORT`  | 47300   | Zerg backend port (internal) |
| `SERVER_URL`    | auto    | API server URL for tests     |

### Model Configuration

Tests use a configurable OpenAI model. By default, tests use the **mini model** for faster, cheaper runs.

| Variable                     | Default                        | Description              |
| ---------------------------- | ------------------------------ | ------------------------ |
| `JARVIS_USE_MINI_MODEL`      | `true` (tests), `false` (dev)  | Use mini model for tests |
| `JARVIS_REALTIME_MODEL`      | `gpt-4o-realtime-preview`      | Main realtime model      |
| `JARVIS_REALTIME_MODEL_MINI` | `gpt-4o-mini-realtime-preview` | Mini model for tests     |
| `JARVIS_VOICE`               | `verse`                        | Voice for realtime audio |

**Usage Examples:**

```bash
# Run tests with mini model (default)
make test

# Run tests with full model
JARVIS_USE_MINI_MODEL=false make test

# Use a specific model
JARVIS_REALTIME_MODEL=gpt-4o-realtime-preview-2024-12-17 make test
```

## Test Categories

### Tests That Run in Docker (Always)

| Test File                           | Description                          |
| ----------------------------------- | ------------------------------------ |
| `history-hydration.e2e.spec.ts`     | History persistence and UI hydration |
| `integration/api-endpoints.spec.js` | Jarvis BFF API endpoint tests (zerg) |

### Skipped Tests (Require Real OpenAI/Hardware)

These tests are skipped in Docker CI because they require real WebRTC connections or hardware:

| Test File                        | Reason                                     |
| -------------------------------- | ------------------------------------------ |
| `voice-interaction.spec.ts`      | Real OpenAI WebRTC connection              |
| `voice-modes-e2e.spec.ts`        | Voice controller initialization            |
| `voice-state-logic.spec.ts`      | Voice controller initialization            |
| `ptt-button-interaction.spec.ts` | Real OpenAI WebRTC connection              |
| `e2e/webrtc-connection.spec.js`  | WebRTC mocks don't work in headless Docker |

### Skipped Tests (Require External Services)

| Test File                          | Reason                                      |
| ---------------------------------- | ------------------------------------------- |
| `supervisor-flow.spec.ts`          | Requires Zerg backend at BACKEND_URL        |
| `conversation-persistence.spec.ts` | Needs rewrite to use new SessionManager API |

## Debugging

```bash
# Build and start services without running tests:
docker compose -f docker-compose.test.yml up -d zerg-backend jarvis-web

# Check service health:
docker compose -f docker-compose.test.yml ps

# View logs:
docker compose -f docker-compose.test.yml logs -f jarvis-web
docker compose -f docker-compose.test.yml logs -f zerg-backend

# Run tests interactively:
docker compose -f docker-compose.test.yml run --rm playwright \
  npx playwright test --debug

# Run with specific test:
docker compose -f docker-compose.test.yml run --rm playwright \
  npx playwright test history-hydration --reporter=list

# Clean up:
docker compose -f docker-compose.test.yml down -v
```

## CI/CD

Tests designed for CI with:

- Automatic container lifecycle management
- Exit code propagation
- No reliance on host state
- Test results mounted as volumes for artifact collection
- Appropriate test skipping for tests that need real hardware/services

## Expected Results

In Docker/CI:

- **8+ tests pass** (API endpoints, history hydration UI)
- **46+ tests skipped** (voice/WebRTC/external services)
- **0 tests fail**

To run the full test suite including voice tests, run locally with `make dev` and `OPENAI_API_KEY` set.
