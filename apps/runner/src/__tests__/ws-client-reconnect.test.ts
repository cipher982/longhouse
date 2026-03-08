import { afterEach, describe, expect, it } from 'bun:test';
import net from 'node:net';
import { setTimeout as sleep } from 'node:timers/promises';

import { RunnerWebSocketClient } from '../ws-client';

const metadata = () => ({
  platform: 'linux',
  arch: 'x64',
  runner_version: '0.1.3',
  docker_available: false,
});

describe('RunnerWebSocketClient reconnect behavior', () => {
  let server: net.Server | null = null;
  const sockets = new Set<net.Socket>();

  afterEach(async () => {
    for (const socket of sockets) {
      socket.destroy();
    }
    sockets.clear();

    if (server) {
      await new Promise<void>((resolve) => server!.close(() => resolve()));
      server = null;
    }
  });

  it('retries when the websocket opening handshake stalls', async () => {
    let connections = 0;

    server = net.createServer((socket) => {
      connections += 1;
      sockets.add(socket);
      socket.on('close', () => sockets.delete(socket));
      socket.on('data', () => {
        // Intentionally swallow the HTTP upgrade request so the ws client
        // must rely on handshakeTimeout instead of hanging forever.
      });
    });

    await new Promise<void>((resolve) => server!.listen(0, '127.0.0.1', () => resolve()));
    const address = server.address();
    if (!address || typeof address === 'string') {
      throw new Error('Expected TCP server address');
    }

    const client = new RunnerWebSocketClient(
      {
        longhouseUrl: `http://127.0.0.1:${address.port}`,
        longhouseUrls: [`http://127.0.0.1:${address.port}`],
        runnerId: null,
        runnerName: 'lh-test-runner',
        runnerSecret: 'secret_123',
        heartbeatIntervalMs: 30000,
        reconnectDelayMs: 25,
        maxReconnectDelayMs: 50,
        connectTimeoutMs: 50,
        capabilities: ['exec.full'],
      },
      metadata,
    );

    await client.start();
    await sleep(220);
    client.stop();

    expect(connections).toBeGreaterThanOrEqual(2);
  });
});
