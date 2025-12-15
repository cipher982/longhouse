# Swarmlet Runner

Lightweight daemon that connects to the Swarmlet platform and executes commands on user infrastructure.

## Quick Start

### Using Bun (Development)

```bash
# Install dependencies (from repo root - using workspace)
cd /path/to/repo/root
bun install

# Set environment variables
export SWARMLET_URL=ws://localhost:47300
export RUNNER_ID=123
export RUNNER_SECRET=your_secret_here

# Run the daemon
bun run src/index.ts

# Or use watch mode
bun run dev
```

### Using Docker

```bash
docker build -t swarmlet-runner .

docker run -d --name swarmlet-runner \
  -e SWARMLET_URL=ws://localhost:47300 \
  -e RUNNER_ID=123 \
  -e RUNNER_SECRET=your_secret_here \
  swarmlet-runner
```

## Configuration

All configuration is via environment variables:

| Variable                 | Required | Default                | Description                                   |
| ------------------------ | -------- | ---------------------- | --------------------------------------------- |
| `SWARMLET_URL`           | No       | `ws://localhost:47300` | Swarmlet API URL (ws:// or wss://)            |
| `RUNNER_ID`              | Yes      | -                      | Runner ID from registration                   |
| `RUNNER_SECRET`          | Yes      | -                      | Runner secret from registration               |
| `HEARTBEAT_INTERVAL_MS`  | No       | `30000`                | Heartbeat interval in milliseconds            |
| `RECONNECT_DELAY_MS`     | No       | `5000`                 | Initial reconnect delay in milliseconds       |
| `MAX_RECONNECT_DELAY_MS` | No       | `60000`                | Maximum reconnect delay (exponential backoff) |

## Registration

Before running the daemon, you need to register it with the Swarmlet platform:

1. Create an enrollment token via the Swarmlet API or UI
2. Use the token to register the runner and get your `RUNNER_ID` and `RUNNER_SECRET`
3. Configure the daemon with these credentials

See the main Swarmlet documentation for detailed registration instructions.

## Features

- **Auto-reconnect**: Exponential backoff reconnection on disconnect
- **Heartbeat**: Keeps connection alive and updates last-seen timestamp
- **Command execution**: Runs shell commands with real-time stdout/stderr streaming
- **Output capping**: Limits output to 50KB per job with truncation
- **Timeout support**: Enforces command timeouts with graceful and forced termination
- **Graceful shutdown**: Handles SIGINT/SIGTERM for clean shutdown

## Protocol

The runner communicates with the Swarmlet platform via WebSocket:

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
