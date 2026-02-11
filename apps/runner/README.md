# Longhouse Runner

Daemon that executes commands for a Longhouse instance over WebSocket.

## Dev run

```bash
bun install
export LONGHOUSE_URL=http://localhost:8080
export RUNNER_NAME=my-runner
export RUNNER_SECRET=your_secret_here
bun run --filter @longhouse/runner start
```

## Docker

```bash
docker build -t longhouse/runner:latest apps/runner

docker run -d --name longhouse-runner \
  -e LONGHOUSE_URL=http://localhost:8080 \
  -e RUNNER_NAME=my-runner \
  -e RUNNER_SECRET=your_secret_here \
  longhouse/runner:latest
```

## Required env

- `LONGHOUSE_URL` (ws/wss or http/https)
- `RUNNER_NAME` or `RUNNER_ID`
- `RUNNER_SECRET`

Optional: `RUNNER_CAPABILITIES`, `HEARTBEAT_INTERVAL_MS`.
