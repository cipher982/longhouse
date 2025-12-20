/**
 * SupervisorChatController - Manages text chat with Supervisor via Zerg backend
 *
 * Responsibilities:
 * - Send messages to POST /api/jarvis/chat (SSE streaming)
 * - Handle SSE stream for streaming responses
 * - Load conversation history from GET /api/jarvis/history
 * - Emit events for UI updates via stateManager
 *
 * Usage:
 *   const controller = new SupervisorChatController();
 *   await controller.initialize();
 *   await controller.sendMessage("Hello, assistant!");
 */

import { logger } from '@jarvis/core';
import { stateManager } from './state-manager';
import { conversationController } from './conversation-controller';
import { CONFIG, toAbsoluteUrl } from './config';
import { eventBus } from './event-bus';

export interface SupervisorChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
}

export interface SupervisorChatConfig {
  maxRetries?: number;
  retryDelay?: number;
}

/**
 * SSE event types from /api/jarvis/chat
 */
interface SSEConnectedEvent {
  message: string;
  run_id: number;
  client_correlation_id?: string;
}

interface SSESupervisorEvent {
  type: string; // All event types including worker_* events
  payload: {
    // Core fields
    run_id?: number;
    message?: string;
    result?: string;
    error?: string;
    status?: string;
    // Worker lifecycle fields
    job_id?: number;
    task?: string;
    worker_id?: string;
    summary?: string;
    duration_ms?: number;
    // Worker tool fields
    tool_name?: string;
    tool_call_id?: string;
    tool_args_preview?: string;
    result_preview?: string;
  };
  client_correlation_id?: string;
}

export class SupervisorChatController {
  private config: SupervisorChatConfig;
  private currentAbortController: AbortController | null = null;
  private currentRunId: number | null = null;
  private lastCorrelationId: string | null = null;
  private watchdogTimer: number | null = null;
  private readonly WATCHDOG_TIMEOUT_MS = 60000;

  constructor(config: SupervisorChatConfig = {}) {
    this.config = {
      maxRetries: config.maxRetries || 3,
      retryDelay: config.retryDelay || 1000,
    };
  }

  /**
   * Initialize the controller
   */
  async initialize(): Promise<void> {
    logger.info('[SupervisorChat] Initialized');
  }

  /**
   * Load conversation history from server
   * Returns messages in the format expected by the UI
   */
  async loadHistory(limit: number = 50): Promise<SupervisorChatMessage[]> {
    try {
      logger.info('[SupervisorChat] Loading history from server...');

      const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/history?limit=${limit}`);
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'include', // Cookie auth
      });

      if (!response.ok) {
        throw new Error(`Failed to load history: ${response.status} ${response.statusText}`);
      }

      const data = await response.json();

      // Transform server format to UI format
      // Server returns: { messages: Array<{ role, content, timestamp }>, total }
      const messages: SupervisorChatMessage[] = (data.messages || []).map((msg: any) => ({
        role: msg.role,
        content: msg.content,
        timestamp: new Date(msg.timestamp),
      }));

      logger.info(`[SupervisorChat] Loaded ${messages.length} messages from history`);
      return messages;
    } catch (error) {
      logger.error('[SupervisorChat] Failed to load history:', error);
      throw error;
    }
  }

  /**
   * Send a text message to the Supervisor and handle SSE response stream
   */
  async sendMessage(text: string, clientCorrelationId?: string, options?: { model?: string; reasoning_effort?: string }): Promise<void> {
    if (!text || text.trim().length === 0) {
      throw new Error('Cannot send empty message');
    }

    const trimmedText = text.trim();
    logger.info(`[SupervisorChat] Sending message (clientCorrelationId=${clientCorrelationId}, model=${options?.model}, reasoning=${options?.reasoning_effort}): ${trimmedText}`);

    // Cancel any previous stream
    if (this.currentAbortController) {
      if (this.lastCorrelationId) {
        stateManager.updateAssistantStatus(this.lastCorrelationId, 'canceled');
      }
      this.currentAbortController.abort();
    }

    this.lastCorrelationId = clientCorrelationId || null;

    // Create new abort controller for this request
    this.currentAbortController = new AbortController();

    // Start watchdog timer if we have a correlation ID
    if (clientCorrelationId) {
      this.startWatchdog(clientCorrelationId);
    }

    try {
      // Start SSE stream
      await this.streamChatResponse(trimmedText, this.currentAbortController.signal, clientCorrelationId, options);

      logger.info('[SupervisorChat] Message sent and stream completed');
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        logger.info('[SupervisorChat] Message stream aborted');
        return;
      }

      logger.error('[SupervisorChat] Failed to send message:', error);
      throw error;
    } finally {
      this.clearWatchdog();
      this.currentAbortController = null;
      this.currentRunId = null;
      // Only clear if this was the correlation ID we were tracking for this call
      if (this.lastCorrelationId === clientCorrelationId) {
        this.lastCorrelationId = null;
      }
    }
  }

  /**
   * Start the 60s watchdog timer
   */
  private startWatchdog(correlationId: string): void {
    this.clearWatchdog();
    this.watchdogTimer = window.setTimeout(() => {
      logger.warn(`[SupervisorChat] Watchdog timeout for ${correlationId}`);
      stateManager.updateAssistantStatus(correlationId, 'error');
      stateManager.showToast('Timed out waiting for response', 'error');
      this.cancel();
    }, this.WATCHDOG_TIMEOUT_MS);
  }

  /**
   * Reset ("pet") the watchdog timer to keep the request alive
   */
  private petWatchdog(correlationId?: string): void {
    const id = correlationId ?? this.lastCorrelationId;
    if (this.watchdogTimer && id) {
      this.startWatchdog(id);
    }
  }

  /**
   * Clear any active watchdog timer
   */
  private clearWatchdog(): void {
    if (this.watchdogTimer !== null) {
      window.clearTimeout(this.watchdogTimer);
      this.watchdogTimer = null;
    }
  }

  /**
   * Stream chat response via SSE
   */
  private async streamChatResponse(
    message: string,
    signal: AbortSignal,
    client_correlation_id?: string,
    options?: { model?: string; reasoning_effort?: string }
  ): Promise<void> {
    const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/chat`);

    logger.info('[SupervisorChat] Initiating SSE stream to:', url);

    const requestBody: Record<string, unknown> = { message, client_correlation_id };
    if (options?.model) {
      requestBody.model = options.model;
    }
    if (options?.reasoning_effort) {
      requestBody.reasoning_effort = options.reasoning_effort;
    }

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
      },
      credentials: 'include', // Cookie auth
      body: JSON.stringify(requestBody),
      signal,
    });

    logger.info(`[SupervisorChat] Response status: ${response.status} ${response.statusText}`);
    logger.info('[SupervisorChat] Response headers:', {
      contentType: response.headers?.get?.('content-type') ?? null,
      transferEncoding: response.headers?.get?.('transfer-encoding') ?? null,
      connection: response.headers?.get?.('connection') ?? null,
    });

    if (!response.ok) {
      throw new Error(`Chat request failed: ${response.status} ${response.statusText}`);
    }

    const body = response.body;
    if (!body) {
      throw new Error('No response body for SSE stream');
    }

    // Process SSE stream
    await this.processSSEStream(body, signal);
  }

  /**
   * Process Server-Sent Events stream from ReadableStream
   */
  private async processSSEStream(body: ReadableStream<Uint8Array>, signal: AbortSignal): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let chunkCount = 0;

    logger.info('[SupervisorChat] Starting to read SSE stream...');

    const abortDone = (): Promise<{ done: true; value: undefined }> =>
      new Promise((resolve) => {
        if (signal.aborted) {
          resolve({ done: true, value: undefined });
          return;
        }
        signal.addEventListener('abort', () => resolve({ done: true, value: undefined }), { once: true });
      });

    // Helper to process SSE messages from buffer
    const processBuffer = async (): Promise<string> => {
      // Normalize line endings and split on double newlines (SSE message boundary)
      const normalizedBuffer = buffer.replace(/\r\n/g, '\n');
      const parts = normalizedBuffer.split('\n\n');

      // Keep the last part as remaining buffer (might be incomplete)
      const remaining = parts.pop() || '';

      logger.debug(`[SupervisorChat] Processing ${parts.length} complete messages, remaining: ${remaining.length} chars`);

      for (const message of parts) {
        if (message.trim() === '') continue;

        const lines = message.split('\n');
        let eventType = '';
        let data = '';

        // Parse SSE message format
        for (const line of lines) {
          if (line.startsWith('event:')) {
            eventType = line.substring(6).trim();
          } else if (line.startsWith('data:')) {
            data = line.substring(5).trim();
          }
        }

        logger.info(`[SupervisorChat] Processing SSE event: ${eventType}`);

        // Handle the event
        if (data) {
          try {
            const parsedData = JSON.parse(data);
            await this.handleSSEEvent(eventType || 'message', parsedData);
          } catch (error) {
            logger.warn('[SupervisorChat] Failed to parse SSE data:', { data, error });
          }
        }
      }

      return remaining;
    };

    try {
      while (true) {
        const { done, value } = await Promise.race([reader.read(), abortDone()]);
        chunkCount++;

        if (done) {
          logger.info(`[SupervisorChat] Stream ended after ${chunkCount} reads`);
          // Process any remaining complete messages in buffer
          if (buffer.trim()) {
            // Add a final \n\n to ensure the last message is processed
            buffer += '\n\n';
            await processBuffer();
          }
          break;
        }

        // Decode chunk and add to buffer
        const chunk = decoder.decode(value, { stream: true });
        buffer += chunk;
        logger.debug(`[SupervisorChat] Received chunk #${chunkCount} (${chunk.length} chars)`);

        // Process complete SSE messages
        buffer = await processBuffer();
      }
    } finally {
      reader.releaseLock();
    }
  }

  /**
   * Handle individual SSE events
   */
  private async handleSSEEvent(eventType: string, data: any): Promise<void> {
    logger.debug('[SupervisorChat] SSE event:', { eventType, data });

    const correlationId = data.client_correlation_id;

    // Handle connected event separately
    if (eventType === 'connected') {
      this.petWatchdog(correlationId);
      this.currentRunId = data.run_id;
      logger.info(`[SupervisorChat] Connected to run ${data.run_id}, correlationId: ${correlationId}`);
      if (correlationId) {
        stateManager.updateAssistantStatus(correlationId, 'typing');
      }
      return;
    }

    // Handle heartbeat
    if (eventType === 'heartbeat') {
      this.petWatchdog();
      logger.debug('[SupervisorChat] Heartbeat');
      return;
    }

    // All other events have format: { type: "...", payload: {...}, timestamp: "..." }
    const event = data as SSESupervisorEvent;
    const payload = event.payload || {};

    switch (eventType) {
      case 'supervisor_started':
        this.petWatchdog(correlationId);
        logger.info('[SupervisorChat] Supervisor started');
        if (payload.run_id) {
          this.currentRunId = payload.run_id;
        }
        if (correlationId) {
          stateManager.updateAssistantStatus(correlationId, 'typing');
        }
        // Emit supervisor started event for progress UI
        if (this.currentRunId) {
          eventBus.emit('supervisor:started', {
            runId: this.currentRunId,
            task: 'Processing message...',
            timestamp: Date.now(),
          });
        }
        break;

      case 'supervisor_thinking':
        this.petWatchdog(correlationId);
        logger.info('[SupervisorChat] Supervisor thinking:', payload.message);
        if (correlationId) {
          stateManager.updateAssistantStatus(correlationId, 'typing');
        }
        if (payload.message && this.currentRunId) {
          // Emit thinking event for progress UI
          eventBus.emit('supervisor:thinking', {
            message: payload.message,
            timestamp: Date.now(),
          });
        }
        break;

      case 'supervisor_complete':
        this.petWatchdog(correlationId);
        logger.info('[SupervisorChat] Supervisor complete');
        const result = payload.result;

        if (payload.status === 'cancelled') {
          if (correlationId) {
            stateManager.updateAssistantStatus(correlationId, 'canceled');
          }
        } else if (result && typeof result === 'string') {
          // Set streaming text incrementally (simulate streaming for smooth UX)
          conversationController.startStreaming(correlationId);

          // Stream the result in chunks
          const chunkSize = 10; // characters per chunk
          for (let i = 0; i < result.length; i += chunkSize) {
            const chunk = result.substring(i, i + chunkSize);
            conversationController.appendStreaming(chunk, correlationId);
            // Small delay for visual effect
            await new Promise(resolve => setTimeout(resolve, 10));
          }

          // Finalize the message
          await conversationController.finalizeStreaming();

          if (correlationId) {
            stateManager.updateAssistantStatus(correlationId, 'final', result);
          }
        }

        // Clear supervisor progress UI
        if (this.currentRunId) {
          eventBus.emit('supervisor:complete', {
            runId: this.currentRunId,
            result: result || 'Task completed',
            status: 'success',
            timestamp: Date.now(),
          });
        }
        break;

      case 'error':
        logger.error('[SupervisorChat] Supervisor error:', payload.error || payload.message);
        const errorMsg = payload.error || payload.message || 'Unknown error';
        stateManager.showToast(`Error: ${errorMsg}`, 'error');

        if (correlationId) {
          stateManager.updateAssistantStatus(correlationId, 'error');
        }

        if (this.currentRunId) {
          eventBus.emit('supervisor:error', {
            message: errorMsg,
            timestamp: Date.now(),
          });
        }
        break;

      // ===== Worker lifecycle events (v2.1) =====
      case 'worker_spawned':
        this.petWatchdog(correlationId);
        logger.info('[SupervisorChat] Worker spawned:', payload.job_id);
        eventBus.emit('supervisor:worker_spawned', {
          jobId: payload.job_id || 0,
          task: payload.task || 'Worker task',
          timestamp: Date.now(),
        });
        break;

      case 'worker_started':
        this.petWatchdog(correlationId);
        logger.info('[SupervisorChat] Worker started:', payload.job_id);
        eventBus.emit('supervisor:worker_started', {
          jobId: payload.job_id || 0,
          workerId: payload.worker_id,
          timestamp: Date.now(),
        });
        break;

      case 'worker_complete':
        this.petWatchdog(correlationId);
        logger.info(`[SupervisorChat] Worker complete: job=${payload.job_id} status=${payload.status}`);
        eventBus.emit('supervisor:worker_complete', {
          jobId: payload.job_id || 0,
          workerId: payload.worker_id,
          status: payload.status || 'unknown',
          durationMs: payload.duration_ms,
          timestamp: Date.now(),
        });
        break;

      case 'worker_summary_ready':
        this.petWatchdog(correlationId);
        logger.info('[SupervisorChat] Worker summary ready:', payload.job_id);
        eventBus.emit('supervisor:worker_summary', {
          jobId: payload.job_id || 0,
          workerId: payload.worker_id,
          summary: payload.summary || '',
          timestamp: Date.now(),
        });
        break;

      // ===== Worker tool events (v2.1 Activity Ticker) =====
      case 'worker_tool_started':
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Worker tool started:', payload.tool_name);
        eventBus.emit('worker:tool_started', {
          workerId: payload.worker_id || '',
          toolName: payload.tool_name || '',
          toolCallId: payload.tool_call_id || '',
          argsPreview: payload.tool_args_preview,
          timestamp: Date.now(),
        });
        break;

      case 'worker_tool_completed':
        this.petWatchdog(correlationId);
        logger.debug('[SupervisorChat] Worker tool completed:', payload.tool_name);
        eventBus.emit('worker:tool_completed', {
          workerId: payload.worker_id || '',
          toolName: payload.tool_name || '',
          toolCallId: payload.tool_call_id || '',
          durationMs: payload.duration_ms || 0,
          resultPreview: payload.result_preview,
          timestamp: Date.now(),
        });
        break;

      case 'worker_tool_failed':
        this.petWatchdog(correlationId);
        logger.warn(`[SupervisorChat] Worker tool failed: ${payload.tool_name} - ${payload.error}`);
        eventBus.emit('worker:tool_failed', {
          workerId: payload.worker_id || '',
          toolName: payload.tool_name || '',
          toolCallId: payload.tool_call_id || '',
          durationMs: payload.duration_ms || 0,
          error: payload.error || 'Unknown error',
          timestamp: Date.now(),
        });
        break;

      default:
        logger.debug('[SupervisorChat] Unknown SSE event type:', { eventType, data });
    }
  }

  /**
   * Cancel current message stream
   */
  cancel(): void {
    if (this.currentAbortController) {
      logger.info('[SupervisorChat] Cancelling current stream');
      this.currentAbortController.abort();
      this.currentAbortController = null;
    }

    if (this.currentRunId) {
      eventBus.emit('supervisor:cleared', { timestamp: Date.now() });
      this.currentRunId = null;
    }
  }

  /**
   * Clear server-side conversation history
   * Creates a new Supervisor thread, effectively clearing all history
   */
  async clearHistory(): Promise<void> {
    try {
      logger.info('[SupervisorChat] Clearing server-side history...');

      const url = toAbsoluteUrl(`${CONFIG.JARVIS_API_BASE}/history`);
      const response = await fetch(url, {
        method: 'DELETE',
        credentials: 'include', // Cookie auth
      });

      if (!response.ok) {
        throw new Error(`Failed to clear history: ${response.status} ${response.statusText}`);
      }

      logger.info('[SupervisorChat] Server-side history cleared');
    } catch (error) {
      logger.error('[SupervisorChat] Failed to clear history:', error);
      throw error;
    }
  }

  /**
   * Clean up resources
   */
  dispose(): void {
    this.cancel();
    logger.info('[SupervisorChat] Disposed');
  }
}
