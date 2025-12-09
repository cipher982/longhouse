# E2E Testing Guide

## Bridge Mode E2E Tests

The `bridge-mode.e2e.test.tsx` file contains end-to-end tests for the full Jarvis integration with bridge mode enabled.

### What These Tests Cover

1. **Auto-connect on mount** - Verifies realtime session connects automatically
2. **Text message round-trip** - Sends message through appController to backend
3. **PTT interaction** - Tests push-to-talk with connection guards
4. **Reconnect after failure** - Tests error recovery and retry
5. **Mode toggle state** - Verifies UI disables controls when disconnected

### Prerequisites

**Required services:**

- `jarvis-server` (Express bridge server)
- `zerg-backend` (optional, for full supervisor features)
- `postgres` (if using persistent storage)

**Environment variables:**

```bash
VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true  # Enable bridge mode
VITE_JARVIS_DEVICE_SECRET=your-secret    # Auth for supervisor features (optional)
```

**OpenAI Configuration:**
One of:

- Real OpenAI API key in jarvis-server config
- Mock responses configured in jarvis-server
- Accept connection failures (tests will gracefully handle and report)

### Running the Tests

#### Full Stack (Recommended)

```bash
# Terminal 1: Start all services
cd /Users/davidrose/git/zerg
make dev

# Terminal 2: Run e2e tests
cd apps/jarvis/apps/web
export VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true
bun test bridge-mode.e2e
```

#### Individual Service Testing

```bash
# Start only jarvis-server
make jarvis

# Run tests
cd apps/jarvis/apps/web
export VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true
bun test bridge-mode.e2e
```

#### CI/CD

These tests automatically skip when `VITE_JARVIS_ENABLE_REALTIME_BRIDGE` is not set to `'true'`.

```bash
# Regular test run (skips e2e)
bun test

# Run with e2e (requires services)
VITE_JARVIS_ENABLE_REALTIME_BRIDGE=true bun test
```

### Test Behavior

**When services are available:**

- Tests verify full connection flow
- Validates real backend integration
- Tests actual voice/audio setup

**When services are NOT available:**

- Tests verify connection _attempt_ was made
- Validates error handling and retry logic
- Checks UI guards work correctly
- Logs informational messages (not failures)

This allows tests to be valuable in both integration and unit test contexts.

### Debugging Failures

```bash
# Check services are running
docker ps
# Should see: jarvis-server, postgres, zerg-backend

# Check logs
docker logs docker-jarvis-server-1 -f

# Verify bridge mode is enabled
echo $VITE_JARVIS_ENABLE_REALTIME_BRIDGE
# Should print: true

# Run tests with verbose output
bun test bridge-mode.e2e --reporter=verbose
```

### Adding New E2E Tests

When adding tests:

1. **Use `describeIf`** - Automatically skips if bridge mode disabled
2. **Handle both success and failure** - Services may not be available
3. **Add timeouts** - Connection/network operations need time
4. **Log helpful messages** - Distinguish expected vs unexpected failures
5. **Mock media devices** - Use the beforeEach pattern for getUserMedia

Example:

```typescript
it('should test new feature', async () => {
  render(<AppProvider><App /></AppProvider>)

  // Wait for services
  await waitFor(() => {
    expect(someCondition).toBe(true)
  }, { timeout: 5000 })

  // Test with graceful handling
  if (serviceAvailable) {
    // Test full integration
  } else {
    console.log('[E2E] Service unavailable - testing fallback')
    // Test error handling
  }
}, 10000) // Allow time for network
```

### Related Documentation

- [MIGRATION.md](../MIGRATION.md) - Bridge mode vs standalone mode
- [AGENTS.md](../../../../AGENTS.md) - Overall platform architecture
- [docker-compose.yml](../../../../../docker/docker-compose.yml) - Service definitions
