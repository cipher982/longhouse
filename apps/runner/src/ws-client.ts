/**
 * WebSocket client with auto-reconnect logic.
 *
 * Manages connection lifecycle, message routing, and exponential backoff reconnection.
 */

import WebSocket from 'ws';
import type { RunnerConfig } from './config';
import type {
  HelloMessage,
  HeartbeatMessage,
  ExecChunkMessage,
  ExecDoneMessage,
  ExecErrorMessage,
  ServerToRunnerMessage,
  getRunnerMetadata,
} from './protocol';
import { CommandExecutor } from './executor';

export class RunnerWebSocketClient {
  private ws: WebSocket | null = null;
  private config: RunnerConfig;
  private executor: CommandExecutor;
  private heartbeatInterval: Timer | null = null;
  private reconnectTimeout: Timer | null = null;
  private currentReconnectDelay: number;
  private shouldReconnect: boolean = true;
  private isConnecting: boolean = false;

  constructor(config: RunnerConfig, getMetadata: typeof getRunnerMetadata) {
    this.config = config;
    this.executor = new CommandExecutor();
    this.currentReconnectDelay = config.reconnectDelayMs;
    this.getMetadata = getMetadata;
  }

  private getMetadata: typeof getRunnerMetadata;

  /**
   * Start the WebSocket connection.
   */
  async start(): Promise<void> {
    console.log('[ws-client] Starting runner...');
    await this.connect();
  }

  /**
   * Stop the WebSocket connection and prevent reconnection.
   */
  stop(): void {
    console.log('[ws-client] Stopping runner...');
    this.shouldReconnect = false;

    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }

    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = null;
    }

    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  /**
   * Connect to the server.
   */
  private async connect(): Promise<void> {
    if (this.isConnecting || (this.ws && this.ws.readyState === WebSocket.OPEN)) {
      return;
    }

    this.isConnecting = true;

    // Convert HTTP(S) URL to WS(S) URL
    let wsUrl = this.config.swarmletUrl;
    if (wsUrl.startsWith('http://')) {
      wsUrl = wsUrl.replace('http://', 'ws://');
    } else if (wsUrl.startsWith('https://')) {
      wsUrl = wsUrl.replace('https://', 'wss://');
    }

    // Append WebSocket path
    const fullUrl = `${wsUrl}/api/runners/ws`;

    console.log(`[ws-client] Connecting to ${fullUrl}...`);

    try {
      this.ws = new WebSocket(fullUrl);

      this.ws.on('open', () => this.onOpen());
      this.ws.on('message', (data) => this.onMessage(data));
      this.ws.on('error', (error) => this.onError(error));
      this.ws.on('close', () => this.onClose());
    } catch (error) {
      console.error('[ws-client] Connection error:', error);
      this.isConnecting = false;
      this.scheduleReconnect();
    }
  }

  /**
   * Handle WebSocket open event.
   */
  private onOpen(): void {
    console.log('[ws-client] Connected to server');
    this.isConnecting = false;
    this.currentReconnectDelay = this.config.reconnectDelayMs; // Reset backoff

    // Send hello message
    const helloMsg: HelloMessage = {
      type: 'hello',
      runner_id: this.config.runnerId,
      secret: this.config.runnerSecret,
      metadata: this.getMetadata(),
    };

    this.send(helloMsg);

    // Start heartbeat
    this.startHeartbeat();
  }

  /**
   * Handle incoming messages from server.
   */
  private onMessage(data: WebSocket.RawData): void {
    try {
      const message: ServerToRunnerMessage = JSON.parse(data.toString());

      console.log(`[ws-client] Received message: ${message.type}`);

      switch (message.type) {
        case 'exec_request':
          this.handleExecRequest(message);
          break;
        case 'exec_cancel':
          this.handleExecCancel(message);
          break;
        default:
          console.warn(`[ws-client] Unknown message type:`, message);
      }
    } catch (error) {
      console.error('[ws-client] Failed to parse message:', error);
    }
  }

  /**
   * Handle execution request from server.
   */
  private handleExecRequest(message: ServerToRunnerMessage): void {
    if (message.type !== 'exec_request') return;

    const { job_id, command, timeout_secs } = message;

    console.log(`[ws-client] Executing job ${job_id}: ${command}`);

    this.executor.execute(job_id, command, timeout_secs, {
      onStdout: (chunk) => {
        const msg: ExecChunkMessage = {
          type: 'exec_chunk',
          job_id,
          stream: 'stdout',
          data: chunk,
        };
        this.send(msg);
      },
      onStderr: (chunk) => {
        const msg: ExecChunkMessage = {
          type: 'exec_chunk',
          job_id,
          stream: 'stderr',
          data: chunk,
        };
        this.send(msg);
      },
      onComplete: (exitCode, durationMs) => {
        const msg: ExecDoneMessage = {
          type: 'exec_done',
          job_id,
          exit_code: exitCode,
          duration_ms: durationMs,
        };
        this.send(msg);
      },
      onError: (error) => {
        const msg: ExecErrorMessage = {
          type: 'exec_error',
          job_id,
          error,
        };
        this.send(msg);
      },
    });
  }

  /**
   * Handle execution cancellation from server.
   */
  private handleExecCancel(message: ServerToRunnerMessage): void {
    if (message.type !== 'exec_cancel') return;

    const { job_id } = message;
    console.log(`[ws-client] Canceling job ${job_id}`);

    const canceled = this.executor.cancel(job_id);
    if (!canceled) {
      console.warn(`[ws-client] Job ${job_id} not found for cancellation`);
    }
  }

  /**
   * Handle WebSocket error event.
   */
  private onError(error: Error): void {
    console.error('[ws-client] WebSocket error:', error);
  }

  /**
   * Handle WebSocket close event.
   */
  private onClose(): void {
    console.log('[ws-client] Connection closed');
    this.isConnecting = false;

    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }

    this.ws = null;

    if (this.shouldReconnect) {
      this.scheduleReconnect();
    }
  }

  /**
   * Schedule reconnection with exponential backoff.
   */
  private scheduleReconnect(): void {
    if (this.reconnectTimeout) {
      return; // Already scheduled
    }

    console.log(`[ws-client] Reconnecting in ${this.currentReconnectDelay}ms...`);

    this.reconnectTimeout = setTimeout(() => {
      this.reconnectTimeout = null;
      this.connect();

      // Exponential backoff
      this.currentReconnectDelay = Math.min(
        this.currentReconnectDelay * 2,
        this.config.maxReconnectDelayMs
      );
    }, this.currentReconnectDelay);
  }

  /**
   * Send a message to the server.
   */
  private send(message: any): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('[ws-client] Cannot send message, not connected');
      return;
    }

    try {
      this.ws.send(JSON.stringify(message));
    } catch (error) {
      console.error('[ws-client] Failed to send message:', error);
    }
  }

  /**
   * Start sending periodic heartbeats.
   */
  private startHeartbeat(): void {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
    }

    this.heartbeatInterval = setInterval(() => {
      const msg: HeartbeatMessage = { type: 'heartbeat' };
      this.send(msg);
      console.log('[ws-client] Sent heartbeat');
    }, this.config.heartbeatIntervalMs);
  }
}
