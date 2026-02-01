# Longhouse Runner

Lightweight daemon that connects to the Longhouse platform and executes commands on user infrastructure.

> **Note:** Environment variables still use `SWARMLET_` prefix during transition. See `LONGHOUSE_URL` alias below.

## Quick Start

### Using Bun (Development)

```bash
# Install dependencies (from repo root - using workspace)
cd /path/to/repo/root
bun install

# Set environment variables
export LONGHOUSE_URL=http://localhost:30080  # or SWARMLET_URL (legacy)
export RUNNER_NAME=my-runner
export RUNNER_SECRET=your_secret_here

# Run the daemon
bun run --filter @longhouse/runner start

# Or use watch mode
bun run --filter @longhouse/runner dev
```

### Using Docker

```bash
# Build locally (from repo root)
cd /path/to/repo/root
docker build -t longhouse/runner:latest apps/runner

docker run -d --name longhouse-runner \
  -e LONGHOUSE_URL=http://localhost:30080 \
  -e RUNNER_NAME=my-runner \
  -e RUNNER_SECRET=your_secret_here \
  longhouse/runner:latest
```

## Configuration

All configuration is via environment variables:

| Variable                 | Required | Default                | Description                                   |
| ------------------------ | -------- | ---------------------- | --------------------------------------------- |
| `LONGHOUSE_URL`          | No       | `ws://localhost:47300` | Longhouse API URL (ws:// or wss://)           |
| `SWARMLET_URL`           | No       | -                      | Legacy alias for LONGHOUSE_URL                |
| `RUNNER_NAME`            | *        | -                      | Runner name (alternative to RUNNER_ID)        |
| `RUNNER_ID`              | *        | -                      | Runner ID from registration                   |
| `RUNNER_SECRET`          | Yes      | -                      | Runner secret from registration               |
| `RUNNER_CAPABILITIES`    | No       | `exec.readonly`        | Comma-separated capabilities (e.g., exec.full)|
| `HEARTBEAT_INTERVAL_MS`  | No       | `30000`                | Heartbeat interval in milliseconds            |
| `RECONNECT_DELAY_MS`     | No       | `5000`                 | Initial reconnect delay in milliseconds       |
| `MAX_RECONNECT_DELAY_MS` | No       | `60000`                | Maximum reconnect delay (exponential backoff) |

\* Either `RUNNER_NAME` or `RUNNER_ID` is required. Name-based auth is simpler for dev.

## Registration

Before running the daemon, you need to register it with the Longhouse platform:

1. Create an enrollment token via the Swarmlet API or UI
2. Use the token to register the runner and get your `RUNNER_ID` and `RUNNER_SECRET`
3. Configure the daemon with these credentials

See the main Longhouse documentation for detailed registration instructions.

## Features

- **Auto-reconnect**: Exponential backoff reconnection on disconnect
- **Heartbeat**: Keeps connection alive and updates last-seen timestamp
- **Command execution**: Runs shell commands with real-time stdout/stderr streaming
- **Output capping**: Limits output to 50KB per job with truncation
- **Timeout support**: Enforces command timeouts with graceful and forced termination
- **Graceful shutdown**: Handles SIGINT/SIGTERM for clean shutdown

## Protocol

The runner communicates with the Longhouse platform via WebSocket:

### Runner → Server

- `hello`: Initial authentication with runner_id and secret
- `heartbeat`: Periodic keep-alive
- `exec_chunk`: Streaming command output (stdout/stderr)
- `exec_done`: Command completion with exit code
- `exec_error`: Command execution error

### Server → Runner

- `exec_request`: Execute a command with timeout
- `exec_cancel`: Cancel a running command (optional v1)

## Security

- Outbound-only connection (works behind NAT)
- Secret-based authentication (hashed on server)
- No persistent storage of credentials
- Output size limits to prevent memory exhaustion

## Development

```bash
# Format code
bun x prettier --write src/

# Type check
bun x tsc --noEmit
```

## License

MIT
